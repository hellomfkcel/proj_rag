"""P-TASK：Celery 应用实例。

三类队列：
- ingestion_queue   摄入 Pipeline（长耗时，可长退避重试）
- retrieval_queue   查询 Pipeline（用户在线等待，不做长退避重试）
- stamping_queue    盖戳 Pipeline（低优先级、可分批、可断点续跑）

Celery Beat 定时任务：
- reconcile_mirror   每小时 — 结构镜像对账（§13.7b）
- reconcile_stamps   每 15 分钟 — 戳记对账（§14.5c）

OTel Trace Context 传播（跨 API → Worker 进程边界）：
- before_task_publish: 任意 .delay()/.apply_async() 调用时，从当前 OTel span
  提取 trace_id/span_id 注入 Celery 任务 headers
- task_prerun: Worker 收到任务后，从 headers 还原 trace context，
  创建 CONSUMER span 作为 Pipeline 执行树的根节点
- 覆盖全部 7 处任务调度点（检索/摄入/盖戳/对账/事件），无需每个改
"""

import os
from celery import Celery
from celery.schedules import crontab
from celery.signals import worker_process_init, before_task_publish, task_prerun
from kombu import Queue

from src.config import Settings


# ══════════════════════════════════════════════════════════════════
# OTel Tracing 初始化（每个 worker 子进程启动时自动调用）
# ══════════════════════════════════════════════════════════════════

@worker_process_init.connect(weak=False)
def _init_otel_on_worker_startup(**kwargs):
    """Celery worker 子进程启动时初始化 OTel SDK。

    设置全局 TracerProvider → Haystack Pipeline Component 自动产生 span。
    服务名从 OTEL_SERVICE_NAME 环境变量读取。
    """
    service_name = os.getenv("OTEL_SERVICE_NAME", "celery-worker")
    try:
        from src.platform.obs.tracing import init_tracing
        init_tracing(service_name)
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════
# Trace Context 注入（PRODUCER 侧 — API/outbox_relay/events 进程）
# ══════════════════════════════════════════════════════════════════

@before_task_publish.connect(weak=False)
def _inject_traceparent_on_publish(headers=None, **kwargs):
    """在 Celery 任务发布前，将当前 OTel span context 注入任务 headers。

    所有 .delay() / .apply_async() 调用点都会经过这里，无需逐个修改。
    """
    try:
        from opentelemetry import trace
        span = trace.get_current_span()
        ctx = span.get_span_context()
        if not ctx.is_valid:
            return
        if headers is None:
            return
        # W3C Trace Context 标准格式: {version}-{trace_id}-{span_id}-{flags}
        # flags: 0x01 = sampled
        headers["traceparent"] = (
            f"00-{format(ctx.trace_id, '032x')}"
            f"-{format(ctx.span_id, '016x')}"
            f"-{ctx.trace_flags:02x}"
        )
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════
# Trace Context 提取（CONSUMER 侧 — worker 子进程）
# ══════════════════════════════════════════════════════════════════

@task_prerun.connect(weak=False)
def _extract_traceparent_on_prerun(task=None, **kwargs):
    """Worker 执行任务前，从 headers 还原 trace context。

    用 NonRecordingSpan(parent_ctx) 设置 parent，创建 CONSUMER span。
    后续 Haystack Pipeline Component 的 span 自动挂在它下面。
    """
    try:
        from opentelemetry import trace, context
        from opentelemetry.trace import SpanContext, TraceFlags, NonRecordingSpan

        tp = task.request.get("headers", {}).get("traceparent") if task else None
        if not tp:
            return

        parts = tp.split("-")
        if len(parts) != 4:
            return

        parent_sc = SpanContext(
            trace_id=int(parts[1], 16),
            span_id=int(parts[2], 16),
            is_remote=True,
            trace_flags=TraceFlags(int(parts[3], 16)),
        )

        tracer = trace.get_tracer("rag-v14")
        task_name = getattr(task, "name", "celery-task") if task else "celery-task"

        # 用 NonRecordingSpan 包裹 parent SpanContext，这是 OTel 设置 parent 的标准方式
        parent_ctx = trace.set_span_in_context(NonRecordingSpan(parent_sc))
        span = tracer.start_span(
            task_name,
            context=parent_ctx,
            kind=trace.SpanKind.CONSUMER,
        )
        span.set_attribute("messaging.system", "celery")

        token = context.attach(trace.set_span_in_context(span))
        if task:
            task._otel_span = span
            task._otel_token = token

    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════
# Span 清理（CONSUMER 侧 — 任务完成后）
# ══════════════════════════════════════════════════════════════════

from celery.signals import task_postrun

@task_postrun.connect(weak=False)
def _cleanup_span_on_postrun(task=None, **kwargs):
    """任务执行完毕后关闭 CONSUMER span 并清理上下文。"""
    try:
        span = getattr(task, "_otel_span", None)
        token = getattr(task, "_otel_token", None)
        if token is not None:
            from opentelemetry import context
            context.detach(token)
        if span is not None:
            span.end()
    except Exception:
        pass

settings = Settings()

celery_app = Celery(
    "rag_v14",
    broker=settings.redis_url,
    backend=settings.redis_url,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Asia/Shanghai",
    enable_utc=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_soft_time_limit=600,
    task_default_queue="ingestion_queue",
    task_routes={
        "src.ingest.service.ingest_document_task": {"queue": "ingestion_queue"},
        "src.chat.service.retrieve_and_generate_task": {"queue": "retrieval_queue"},
        "src.ingest.service.stamp_channel_task": {"queue": "stamping_queue"},
        "src.platform.task.reconciliation.reconcile_mirror_beat": {"queue": "ingestion_queue"},
        "src.platform.task.reconciliation.reconcile_stamps_beat": {"queue": "stamping_queue"},
    },
    # Celery Beat 定时调度
    beat_schedule={
        "reconcile-mirror-every-hour": {
            "task": "src.platform.task.reconciliation.reconcile_mirror_beat",
            "schedule": crontab(minute=0),  # 每小时整点
        },
        "reconcile-stamps-every-15min": {
            "task": "src.platform.task.reconciliation.reconcile_stamps_beat",
            "schedule": crontab(minute="*/15"),  # 每 15 分钟
        },
    },
)

# 显式声明 worker 可消费的队列（使用 kombu.Queue 对象，兼容 Celery 5.4+）
celery_app.conf.task_queues = [
    Queue("ingestion_queue", exchange="ingestion_queue", routing_key="ingestion_queue"),
    Queue("retrieval_queue", exchange="retrieval_queue", routing_key="retrieval_queue"),
    Queue("stamping_queue", exchange="stamping_queue", routing_key="stamping_queue"),
]

# ── 任务自动发现：Celery worker 需要 import 所有带 @celery_app.task 装饰器的模块 ──
# 这必须在 celery_app 实例化后执行，否则 worker 报 NotRegistered
import src.ingest.service     # noqa: F401 — ingest_document_task, stamp_channel_task
import src.chat.service       # noqa: F401 — retrieve_and_generate_task
import src.platform.task.reconciliation  # noqa: F401 — reconcile_mirror_beat, reconcile_stamps_beat


# ══════════════════════════════════════════════════════════════════
# Celery Trace Context 传播（模块级——确保 API 和 Worker 双侧生效）
# ══════════════════════════════════════════════════════════════════
# CeleryInstrumentor 注入 traceparent 到任务消息头，使 API→Worker→
# Pipeline 整条链路共享同一个 trace_id，在 Grafana Tempo 中形成一条完整 Trace。
# instrument() 幂等，API 进程和 Worker 进程各自调用一次即可。

def _instrument_celery_tracing() -> None:
    try:
        from opentelemetry.instrumentation.celery import CeleryInstrumentor
        CeleryInstrumentor().instrument()
    except Exception:
        pass

_instrument_celery_tracing()

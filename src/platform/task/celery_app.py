"""P-TASK：Celery 应用实例。

三类队列：
- ingestion_queue   摄入 Pipeline（长耗时，可长退避重试）
- retrieval_queue   查询 Pipeline（用户在线等待，不做长退避重试）
- stamping_queue    盖戳 Pipeline（低优先级、可分批、可断点续跑）

Celery Beat 定时任务：
- reconcile_mirror   每小时 — 结构镜像对账（§13.7b）
- reconcile_stamps   每 15 分钟 — 戳记对账（§14.5c）
"""

from celery import Celery
from celery.schedules import crontab
from kombu import Queue

from src.config import Settings

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

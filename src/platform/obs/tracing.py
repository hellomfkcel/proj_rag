"""P-OBS：OpenTelemetry Tracing 初始化。

OTel Collector 单一出口（HTTP 4318 / gRPC 4317）→ Tempo → Grafana/Tempo 查询。
Haystack Pipeline 节点自动产生 span，Span 名 {pipeline_name}.{component_name}。
权限相关 span（authz.check / authz.filter / authz.prefilter）由 P-AUTHC 手动创建。
"""

import os
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource, SERVICE_NAME
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

_tracing_initialized = False
_log_export_initialized = False


def init_tracing(service_name: str = "rag-v14"):
    """初始化 OpenTelemetry Tracing + 日志导出。

    在应用启动时调用一次（API/worker/embedding-service 统一入口）。
    将 span 导出到 OTel Collector（HTTP 4318），并启用 OTLP 日志导出（→Loki，
    §8.2 应用→Collector→Tempo/Loki/Prometheus 单一出口）。
    ★ 同时启用 Haystack Pipeline 的 OpenTelemetry tracing。
    """
    global _tracing_initialized
    if _tracing_initialized:
        return

    # P-OBS 拥有结构化日志（§8.0）：opt out Haystack 的 structlog 接管，
    # 须在 import haystack 之前设置，否则 Haystack 会覆盖 structlog 配置。
    os.environ["HAYSTACK_LOGGING_IGNORE_STRUCTLOG"] = "true"
    # 统一各进程（API/worker/embedding）的 structlog JSON + trace_id 配置
    from src.platform.obs.logger import setup_logging
    setup_logging()

    # Pre-import Haystack to resolve circular import with OTel
    try:
        import haystack  # noqa: F401
        import haystack.tracing  # noqa: F401
    except ImportError:
        pass

    from src.config import Settings
    otel_endpoint = Settings().otel_endpoint

    resource = Resource.create({SERVICE_NAME: service_name})
    provider = TracerProvider(resource=resource)

    otlp_exporter = OTLPSpanExporter(endpoint=f"{otel_endpoint}/v1/traces")
    # 控制批次大小和发送频率，避免单批 span 过多导致 gRPC 消息体超限（默认 4 MiB → 已调至 32 MiB）
    # max_export_batch_size: 单批最多 256 个 span
    # schedule_delay_millis: 每 2 秒发送一次（更频繁 = 更小批次）
    # max_queue_size: 内存中最多缓冲 2048 个 span
    provider.add_span_processor(BatchSpanProcessor(
        otlp_exporter,
        max_export_batch_size=256,
        schedule_delay_millis=2000,
        max_queue_size=2048,
    ))

    trace.set_tracer_provider(provider)

    # ★ 启用 Haystack Pipeline 的 OTel tracing（Haystack 2.31+ 需要独立包）
    try:
        from opentelemetry import trace as _otel_trace
        from haystack.tracing import enable_tracing
        from haystack_integrations.tracing.opentelemetry import OpenTelemetryTracer as _HsOtelTracer
        _hs_tracer = _HsOtelTracer(_otel_trace.get_tracer("haystack"))
        enable_tracing(_hs_tracer)
    except ImportError as e:
        import logging
        logging.getLogger(__name__).warning(
            "Haystack OpenTelemetry tracing integration not available, "
            "Haystack Pipeline components will not produce OTel spans. "
            "Install haystack-integrations-tracing-opentelemetry to enable. "
            "Import error: %s", e
        )
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            "Failed to initialize Haystack OpenTelemetry tracing: %s. "
            "Haystack Pipeline components will not produce OTel spans.",
            e, exc_info=True
        )

    # 注册进程退出时的优雅关闭（确保 BatchSpanProcessor 缓冲区中的 span 被 flush）
    _register_shutdown_hook()

    # OTLP 日志导出（structlog JSON → Collector → Loki），fail-open
    init_log_export(service_name)

    _tracing_initialized = True


def init_log_export(service_name: str = "rag-v14") -> None:
    """P-OBS：OTLP 日志导出（§8.2 应用→Collector→Loki）。

    structlog 渲染后的 JSON（含 32hex trace_id）经 stdlib LoggingHandler
    作为 OTLP LogRecord 导出；trace_id/span_id 由 OTel 上下文自动附加到
    OTLP 记录结构。Loki 日志行内带 32hex trace_id，供 Grafana derivedField
    提取并跳转 Tempo（§4 四方互跳）。

    fail-open（§8.5 可观测 fail-open）：Collector 不可达不阻塞业务，
    由 BatchLogRecordProcessor 缓冲/丢弃。
    """
    global _log_export_initialized
    if _log_export_initialized:
        return
    _log_export_initialized = True

    try:
        import logging as _logging
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        from opentelemetry._logs import set_logger_provider

        from src.config import Settings
        otel_endpoint = Settings().otel_endpoint

        resource = Resource.create({SERVICE_NAME: service_name})
        provider = LoggerProvider(resource=resource)
        provider.add_log_record_processor(BatchLogRecordProcessor(
            OTLPLogExporter(endpoint=f"{otel_endpoint}/v1/logs")))
        set_logger_provider(provider)

        # 防止 OTel SDK 自身日志进入导出环（export 失败告警不再回流到该 handler）
        _logging.getLogger("opentelemetry").propagate = False
        # 所有 stdlib 日志（含 structlog 渲染后的 JSON）导出到 Collector
        _logging.getLogger().addHandler(LoggingHandler(level=_logging.NOTSET))

        _register_log_shutdown()
    except Exception:
        pass  # fail-open：日志导出失败不影响业务


def _register_log_shutdown():
    """注册 atexit 处理器，确保进程退出时日志缓冲区被 flush。"""
    import atexit

    def _shutdown_logs():
        try:
            from opentelemetry._logs import get_logger_provider
            provider = get_logger_provider()
            if hasattr(provider, "force_flush"):
                provider.force_flush(timeout_millis=5000)
            if hasattr(provider, "shutdown"):
                provider.shutdown()
        except Exception:
            pass

    atexit.register(_shutdown_logs)


def _register_shutdown_hook():
    """注册 atexit 处理器，确保进程正常退出时 OTel span 被 flush 并关闭。

    注意：不覆盖 SIGTERM handler——uvicorn reload 等机制依赖默认 SIGTERM 行为。
    atexit 在进程正常退出时触发，覆盖了容器/K8s SIGTERM 导致的正常退出场景。
    BatchSpanProcessor 自身也有定时 flush（schedule_delay_millis），
    非正常退出（SIGKILL/crash）丢失少量 span 是可接受的。
    """
    import atexit

    def _shutdown():
        provider = trace.get_tracer_provider()
        if hasattr(provider, "force_flush"):
            try:
                provider.force_flush(timeout_millis=5000)
            except Exception:
                pass
        if hasattr(provider, "shutdown"):
            try:
                provider.shutdown()
            except Exception:
                pass

    atexit.register(_shutdown)


def get_tracer(name: str = "rag-v14"):
    """获取 OpenTelemetry Tracer 实例。"""
    return trace.get_tracer(name)


def get_current_trace_id() -> str:
    """读取当前 OTel span 的 trace_id，格式化为 32 位十六进制字符串。

    P-OBS 统一提供，供日志 processor / P-AUTHC request_id / B-CHAT 持久化使用。
    当前无活跃 span 或 OTel 未初始化时返回空字符串（fail-open，不抛异常）。
    """
    try:
        span_context = trace.get_current_span().get_span_context()
        if span_context.is_valid:
            return format(span_context.trace_id, "032x")
    except Exception:
        pass
    return ""


def get_current_span_id() -> str:
    """读取当前 OTel span 的 span_id，格式化为 16 位十六进制字符串。"""
    try:
        span_context = trace.get_current_span().get_span_context()
        if span_context.is_valid:
            return format(span_context.span_id, "016x")
    except Exception:
        pass
    return ""


def instrument_fastapi(app):
    """对 FastAPI 应用进行自动埋点。

    自动为每个 HTTP 请求创建 span，记录 method/path/status_code。
    """
    FastAPIInstrumentor.instrument_app(app)

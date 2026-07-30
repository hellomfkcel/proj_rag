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


def init_tracing(service_name: str = "rag-v14"):
    """初始化 OpenTelemetry Tracing。

    在应用启动时调用一次。将全部 span 导出到 OTel Collector（HTTP 4318）。
    先导入 Haystack 以避免循环引用，再初始化 OTel SDK。
    """
    global _tracing_initialized
    if _tracing_initialized:
        return

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
    provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
    provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))

    trace.set_tracer_provider(provider)
    _tracing_initialized = True


def get_tracer(name: str = "rag-v14"):
    """获取 OpenTelemetry Tracer 实例。"""
    return trace.get_tracer(name)


def instrument_fastapi(app):
    """对 FastAPI 应用进行自动埋点。

    自动为每个 HTTP 请求创建 span，记录 method/path/status_code。
    """
    FastAPIInstrumentor.instrument_app(app)

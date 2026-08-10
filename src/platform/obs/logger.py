"""P-OBS：结构化日志配置。

基于 structlog，输出 JSON 格式到 stdout，并注入 OTel trace_id/span_id。
日志经 OTLP 导出（init_tracing → init_log_export）送达 Loki（§8.2 单一出口）。

P-OBS 拥有结构化日志（§8.0）：通过 HAYSTACK_LOGGING_IGNORE_STRUCTLOG opt out
Haystack 对 structlog 的接管，使本配置（JSON + trace_id 32hex）保持权威，
从而保证 Loki 日志行内包含与 Tempo 一致的 32 位十六进制 trace_id（§4 四方互跳）。
"""

import logging
import os
import structlog
import sys


def _add_trace_context(logger, method_name, event_dict):
    """为每条日志注入 OTel trace_id/span_id（32hex / 16hex）。

    使日志行内直接携带与 Tempo 一致的 32 位十六进制 trace_id，
    供 Grafana Loki derivedField 正则提取并跳转 Tempo（§4 四方日志可互跳）。
    fail-open：无活跃 span / OTel 未初始化时不加字段、不抛异常。
    仅注入 ID，绝不携带 credential 等敏感上下文（§1.4/§27.1）。
    """
    try:
        from opentelemetry import trace as _otel_trace
        span_context = _otel_trace.get_current_span().get_span_context()
        if span_context.is_valid:
            event_dict["trace_id"] = format(span_context.trace_id, "032x")
            event_dict["span_id"] = format(span_context.span_id, "016x")
    except Exception:
        pass
    return event_dict


def setup_logging(log_level: str = "INFO") -> None:
    """配置 structlog：JSON 输出到 stdout，日志行内注入 trace_id/span_id。

    调用一次，在应用启动时执行（API/worker/embedding-service 经 init_tracing 统一触发）。
    """
    # P-OBS 拥有结构化日志：opt out Haystack 的 structlog 接管（须在 import haystack 之前生效）。
    os.environ.setdefault("HAYSTACK_LOGGING_IGNORE_STRUCTLOG", "true")

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            _add_trace_context,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            # compact separators：日志行 `"trace_id":"<hex>"` 无空格，便于 Grafana derivedField 正则匹配
            structlog.processors.JSONRenderer(separators=(',', ':')),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # 设置标准库 logging 的级别以匹配
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, log_level.upper(), logging.INFO),
    )


def get_logger(name: str = __name__) -> structlog.stdlib.BoundLogger:
    """获取 structlog logger 实例。"""
    return structlog.get_logger(name)

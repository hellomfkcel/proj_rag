"""P-OBS：结构化日志配置。

基于 structlog，输出 JSON 格式到 stdout。
OTel tracing 由 Haystack 自动提供，无需额外配置。
"""

import logging
import structlog
import sys


def setup_logging(log_level: str = "INFO") -> None:
    """配置 structlog：JSON 格式输出到 stdout。

    调用一次，在应用启动时执行。

    注：实际运行中 init_tracing()（引入 Haystack）会以 Haystack 的
    configure_logging() 覆盖此配置，其内置 correlate_logs_with_traces
    已在每条日志上注入 trace_id/span_id（int 形式），故此处无需显式注入。
    """
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.processors.JSONRenderer(),
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

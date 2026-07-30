"""P-OBS：可观测模块。

提供：
- structlog 结构化日志
- OTel tracing（SDK 初始化 + FastAPI 自动埋点 + Haystack Pipeline span）
- Metric 门面（阶段三接入）
"""

from .logger import get_logger
from .tracing import init_tracing, get_tracer, instrument_fastapi
from .metrics import (
    record_authz_decision,
    record_authz_call_failed,
    set_mirror_gap,
    set_stamp_drift,
    set_orphan_stamp,
    record_filtered_rate,
)

__all__ = [
    "get_logger",
    "init_tracing",
    "get_tracer",
    "instrument_fastapi",
    "record_authz_decision",
    "record_authz_call_failed",
    "set_mirror_gap",
    "set_stamp_drift",
    "set_orphan_stamp",
    "record_filtered_rate",
]

"""P-OBS：Metric 门面 — OTLP 导出 + 内存 fallback。

提供 6 个关键指标（文档 v14.md §8.3）：
- authz_decision_total{endpoint, decision}   Counter（allow/deny/indeterminate 三态）
- authz_call_failed_total{endpoint, kind}    Counter（timeout/connection/http_error）
- mirror_gap / stamp_drift / orphan_stamp     Gauge
- filtered_rate{layer}                        Histogram（layer1/layer3）

生产环境：通过 OTel OTLP Metrics 导出到 Collector → Prometheus → Grafana。
本地开发：OTLP 不可达时自动降级到 structlog + 内存累计（fail-open）。

初始化：在应用启动时调用 init_metrics()。
"""

import threading
from typing import Dict, Optional

from src.config import Settings
from src.platform.obs.logger import get_logger

log = get_logger(__name__)

# ── OTel Metrics（生产路径）──────────────────────────────────────
_meter = None
_otel_counters: Dict[str, object] = {}
_otel_gauges: Dict[str, float] = {}  # OTel Gauge 当前值缓存

# ── 内存 fallback（开发期/OTLP 不可达时）─────────────────────────
_lock = threading.Lock()
_counters: Dict[str, int] = {}
_gauges: Dict[str, float] = {}


def _inc(key: str, delta: int = 1):
    with _lock:
        _counters[key] = _counters.get(key, 0) + delta


def _set_gauge(key: str, value: float):
    with _lock:
        _gauges[key] = value


def _snapshot() -> Dict[str, object]:
    """返回当前内存 metric 快照（供 /metrics 调试端点使用）。"""
    with _lock:
        return {"counters": dict(_counters), "gauges": dict(_gauges)}


# ── 初始化 ────────────────────────────────────────────────────────

_metrics_initialized = False


def init_metrics() -> None:
    """初始化 OTLP Metrics 导出。

    fail-open：OTLP 不可达时降级到内存累计 + structlog 输出。
    幂等：多次调用安全。
    """
    global _meter, _metrics_initialized
    if _metrics_initialized:
        return

    s = Settings()
    otlp_endpoint = s.otel_endpoint  # Settings 的统一 OTLP 端点属性

    try:
        from opentelemetry import metrics as otel_metrics
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import (
            PeriodicExportingMetricReader,
        )
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
            OTLPMetricExporter,
        )

        if otlp_endpoint:
            exporter = OTLPMetricExporter(
                endpoint=f"{otlp_endpoint.rstrip('/')}/v1/metrics",
            )
        else:
            # 无 OTLP endpoint → 跳过 OTel 初始化，仅用内存 fallback
            log.info("metrics_otlp_skipped", reason="no OTLP endpoint configured")
            _metrics_initialized = True
            return

        reader = PeriodicExportingMetricReader(exporter, export_interval_millis=15000)
        provider = MeterProvider(metric_readers=[reader])
        otel_metrics.set_meter_provider(provider)

        _meter = provider.get_meter("rag-v14", "1.0.0")
        log.info("metrics_otlp_initialized", endpoint=otlp_endpoint)
    except ImportError:
        log.info("metrics_otlp_skipped", reason="opentelemetry SDK not installed")
    except Exception as exc:
        log.warning("metrics_otlp_init_failed", error=str(exc),
                    msg="falling back to in-memory metrics")

    _metrics_initialized = True


def _get_or_create_counter(name: str, description: str = "", unit: str = "1"):
    """获取或创建 OTel Counter。OTel 不可用时返回 None。"""
    if _meter is None:
        return None
    key = f"counter:{name}"
    if key not in _otel_counters:
        try:
            from opentelemetry.metrics import Counter
            _otel_counters[key] = _meter.create_counter(
                name=name, description=description, unit=unit,
            )
        except Exception:
            return None
    return _otel_counters[key]


# ── Counter 指标 ──────────────────────────────────────────────────

def record_authz_decision(endpoint: str, decision: str):
    """记录一次权限判定结果。decision ∈ {allow, deny, indeterminate}。"""
    _inc(f"authz_decision_total/{endpoint}/{decision}")
    log.debug("metric_authz_decision", endpoint=endpoint, decision=decision)

    counter = _get_or_create_counter(
        "authz_decision_total",
        description="Permission service decision count",
        unit="1",
    )
    if counter is not None:
        try:
            counter.add(1, {"endpoint": endpoint, "decision": decision})
        except Exception:
            pass


def record_authz_call_failed(endpoint: str, kind: str):
    """记录一次权限调用失败。kind ∈ {timeout, connection, http_error}。"""
    _inc(f"authz_call_failed_total/{endpoint}/{kind}")
    log.warning("metric_authz_call_failed", endpoint=endpoint, kind=kind)

    counter = _get_or_create_counter(
        "authz_call_failed_total",
        description="Permission service call failure count",
        unit="1",
    )
    if counter is not None:
        try:
            counter.add(1, {"endpoint": endpoint, "kind": kind})
        except Exception:
            pass


# ── Gauge 指标 ─────────────────────────────────────────────────────

def set_mirror_gap(value: int):
    """结构镜像对账缺口数。"""
    _set_gauge("mirror_gap", float(value))
    if value > 0:
        log.warning("metric_mirror_gap", value=value)


def set_stamp_drift(value: int):
    """戳记漂移数。"""
    _set_gauge("stamp_drift", float(value))
    if value > 0:
        log.warning("metric_stamp_drift", value=value)


def set_orphan_stamp(value: int):
    """缺 vis_version 的绕路写入 chunk 数。"""
    _set_gauge("orphan_stamp", float(value))
    if value > 0:
        log.warning("metric_orphan_stamp", value=value)


def record_filtered_rate(layer: str, total: int, filtered: int):
    """记录检索层过滤率。layer ∈ {layer1, layer3}。"""
    rate = filtered / max(total, 1)
    _set_gauge(f"filtered_rate/{layer}", rate)
    log.debug("metric_filtered_rate", layer=layer, total=total, filtered=filtered, rate=rate)

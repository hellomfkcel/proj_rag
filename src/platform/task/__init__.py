"""P-TASK：任务基础设施模块。

提供：
- Celery 应用实例（四类队列：ingestion / retrieval / stamping / outbox-relay）
- run_pipeline_sync           Haystack Pipeline 同步执行（worker 内）
- run_pipeline_async          Haystack Pipeline → Celery 任务包装（异步提交）
- outbox_relay                Outbox 轮询搬运（独立进程）
"""

from .celery_app import celery_app
from .pipeline_runner import run_pipeline_sync, run_pipeline_async

__all__ = [
    "celery_app",
    "run_pipeline_sync",
    "run_pipeline_async",
]

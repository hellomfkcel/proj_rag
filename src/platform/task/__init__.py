"""P-TASK：任务基础设施模块。

提供：
- Celery 应用实例（四类队列：ingestion / retrieval / stamping / outbox-relay）
- outbox_relay                Outbox 轮询搬运（独立进程）
- pipeline_runner             执行 Pipeline 的包装（仅在 worker 内按需导入）

注意：此处**不**再 re-export pipeline_runner，避免任意 src.platform.task.* 的
包导入连带拉入 haystack（pipeline_runner 顶层 `from haystack import Pipeline`）。
api/stamping/outbox/visibility 等不执行 Pipeline 的进程因此无需安装 haystack/torch。
需要 run_pipeline_sync/run_pipeline_async 时直接 import 子模块：
    from src.platform.task.pipeline_runner import run_pipeline_sync
"""

from .celery_app import celery_app

__all__ = [
    "celery_app",
]

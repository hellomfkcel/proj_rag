"""B-INGEST：摄入管线模块。

提供：
- ingest_document_task       Haystack 摄入 Pipeline 任务
- stamp_channel_task         盖戳管道（六条纪律）
- should_abort               阶段三 execution_epoch 栅栏检查
- cleanup_mount_chunks       卸载时清理 Milvus chunk
- has_execution / get_parse_status

独占数据：ingest_execution / chunks
"""

from .service import (
    ingest_document_task,
    stamp_channel_task,
    should_abort,
    cleanup_mount_chunks,
    has_execution,
    get_parse_status,
)

__all__ = [
    "ingest_document_task",
    "stamp_channel_task",
    "should_abort",
    "cleanup_mount_chunks",
    "has_execution",
    "get_parse_status",
]

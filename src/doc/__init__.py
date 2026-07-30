"""B-DOC：文档与目录管理模块。

提供：
- submit_ingest_task         文档登记 + register + link + 同步触发解析
- trigger_parse              触发解析（发 DocumentMounted 事件）
- delete_document_from_kb    从 KB 移除文档（purge=false）

独占数据：document / document_kb_mount / directory / document_directory_entry / outbox
"""

from .service import (
    submit_ingest_task,
    trigger_parse,
    delete_document_from_kb,
)

__all__ = [
    "submit_ingest_task",
    "trigger_parse",
    "delete_document_from_kb",
]

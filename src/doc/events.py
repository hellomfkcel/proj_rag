"""B-DOC：领域事件定义。

B-DOC 发布的事件：
- DocumentMounted     文档挂载完成（触发摄入）
- DocumentUnmounted   文档从 KB 移除（触发清理）
- MountEnabledChanged 启用/停用状态变更
"""

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4


def _event_id() -> str:
    return str(uuid4())


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class DomainEvent:
    """领域事件信封（v14.md §3.1）。"""
    event_type: str
    event_id: str = field(default_factory=_event_id)
    occurred_at: str = field(default_factory=_now_iso)
    trace_id: str = ""
    tenant_id: str = ""
    payload_schema_version: str = "v1"
    payload: dict = field(default_factory=dict)

    def to_outbox_dict(self) -> dict:
        return {
            "event_type": self.event_type,
            "payload": json.dumps(self.payload, default=str),
            "tenant_id": self.tenant_id,
            "trace_id": self.trace_id,
            "status": "pending",
        }


# ── 具体事件 ─────────────────────────────────────────────────


def document_mounted_event(
    document_id: str,
    mount_id: str,
    kb_id: str,
    tenant_id: str,
    chunking_config_version: str = "v1",
    trace_id: str = "",
) -> DomainEvent:
    return DomainEvent(
        event_type="DocumentMounted",
        tenant_id=tenant_id,
        trace_id=trace_id,
        payload={
            "document_id": document_id,
            "mount_id": mount_id,
            "kb_id": kb_id,
            "chunking_config_version": chunking_config_version,
        },
    )


def document_unmounted_event(
    mount_id: str,
    kb_id: str,
    tenant_id: str,
    trace_id: str = "",
) -> DomainEvent:
    return DomainEvent(
        event_type="DocumentUnmounted",
        tenant_id=tenant_id,
        trace_id=trace_id,
        payload={
            "mount_id": mount_id,
            "kb_id": kb_id,
        },
    )


def mount_enabled_changed_event(
    mount_id: str,
    is_enabled: bool,
    tenant_id: str,
    trace_id: str = "",
) -> DomainEvent:
    return DomainEvent(
        event_type="MountEnabledChanged",
        tenant_id=tenant_id,
        trace_id=trace_id,
        payload={
            "mount_id": mount_id,
            "is_enabled": is_enabled,
        },
    )

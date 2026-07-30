"""P-AUDIT：审计模块。

提供：
- emit_audit_event          审计事件写入（阶段一：structlog 日志）
- emit_audit_event_txn      高风险同步写入（阶段一：同 emit_audit_event）

阶段二升级为落 audit_logs 表，阶段三 emit_audit_event_txn 升级为同事务。
"""

from .service import emit_audit_event, emit_audit_event_txn

__all__ = ["emit_audit_event", "emit_audit_event_txn"]

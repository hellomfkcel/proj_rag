"""P-AUDIT：审计模块。

提供：
- emit_audit_event          审计事件写入 audit_logs 表
- emit_audit_event_txn      高风险同步写入（支持同事务）
"""

from .service import emit_audit_event, emit_audit_event_txn

__all__ = ["emit_audit_event", "emit_audit_event_txn"]

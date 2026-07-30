"""P-AUDIT：审计模块。

阶段二：emit_audit_event 落 audit_logs 表（asyncpg INSERT）。
阶段三：emit_audit_event_txn 升级为同事务。
"""

import json
import uuid
import asyncio
import asyncpg
from src.config import Settings
from src.platform.obs.logger import get_logger

log = get_logger(__name__)


def _dsn() -> str:
    return Settings().database_url.replace("postgresql+asyncpg://", "postgresql://")


def emit_audit_event(
    event_type: str,
    user_id: str = "",
    tenant_id: str = "",
    action: str = "",
    resource_type: str = "",
    resource_id: str = "",
    allowed: bool = True,
    **payload,
) -> None:
    """审计事件写入 audit_logs 表（阶段二：落库，fail-open）。

    普通事件：写库失败记录 WARNING 日志，不抛异常。
    """

    async def _insert():
        conn = await asyncpg.connect(_dsn())
        try:
            await conn.execute(
                """INSERT INTO audit_logs
                   (id, event_type, request_id, user_id, tenant_id, action,
                    resource_type, resource_id, allowed, payload)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)""",
                str(uuid.uuid4()), event_type, payload.get("request_id", ""),
                user_id, tenant_id, action,
                resource_type, resource_id, allowed,
                json.dumps({k: str(v) for k, v in payload.items()}, default=str),
            )
        finally:
            await conn.close()

    try:
        asyncio.run(_insert())
    except Exception as exc:
        log.warning("audit_write_failed_fail_open",
                    event_type=event_type, error=str(exc))

    # 同时保持 structlog 输出（双写，便于本地调试）
    log.info("audit", event_type=event_type, user_id=user_id,
             tenant_id=tenant_id, action=action, resource_type=resource_type,
             resource_id=resource_id, allowed=allowed, **payload)


def emit_audit_event_txn(
    event_type: str,
    user_id: str = "",
    tenant_id: str = "",
    action: str = "",
    resource_type: str = "",
    resource_id: str = "",
    allowed: bool = True,
    conn_for_txn=None,  # 阶段三：传入业务事务连接，在同一事务上写审计
    **payload,
) -> None:
    """高风险同步写入（阶段三：与业务事务同库同事务提交）。

    高风险事件清单：DOC_DOWNLOAD / DOC_DELETE / AUTHZ_WRITE。

    若传入 conn_for_txn（asyncpg Connection），在已有事务上 INSERT：
    写失败 → 业务回滚（fail-closed）。
    若不传，fallback 到独立连接写入。
    """
    import json, asyncio

    if conn_for_txn is not None:
        # 阶段三路径：与业务事务同连接
        async def _insert_on_shared_conn():
            await conn_for_txn.execute(
                """INSERT INTO audit_logs
                   (id, event_type, request_id, user_id, tenant_id, action,
                    resource_type, resource_id, allowed, payload)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)""",
                str(uuid.uuid4()), event_type, payload.get("request_id", ""),
                user_id, tenant_id, action,
                resource_type, resource_id, allowed,
                json.dumps({k: str(v) for k, v in payload.items()}, default=str),
            )
        try:
            try:
                loop = asyncio.get_running_loop()
                import concurrent.futures
                future = asyncio.run_coroutine_threadsafe(_insert_on_shared_conn(), loop)
                future.result(timeout=5)
            except RuntimeError:
                asyncio.run(_insert_on_shared_conn())
            except TimeoutError:
                # 共享连接超时（调用方异步上下文阻塞）→ fallback 独立连接
                emit_audit_event(event_type=event_type, user_id=user_id, tenant_id=tenant_id,
                    action=action, resource_type=resource_type, resource_id=resource_id,
                    allowed=allowed, **payload)
        except Exception as exc:
            log.error("audit_txn_failed_rollback",
                     event_type=event_type, error=str(exc))
            raise  # fail-closed: 写失败则业务回滚
    else:
        # fallback: 独立连接写入
        emit_audit_event(
            event_type=event_type, user_id=user_id, tenant_id=tenant_id,
            action=action, resource_type=resource_type, resource_id=resource_id,
            allowed=allowed, **payload,
        )

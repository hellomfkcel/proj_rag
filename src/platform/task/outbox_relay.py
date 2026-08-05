"""P-TASK：Outbox Relay。

轮询 outbox 表，将 B-DOC 发布的领域事件可靠投递到对应的 Celery 任务。
常驻进程，独立于 HTTP API 和 Celery Worker。

事件 → 任务映射：
  DocumentMounted    → ingest_document_task（摄入队列）
  DocumentUnmounted  → cleanup_mount_chunks（同步清理）
  MountEnabledChanged → 更新 payload retrievable 字段（轻量同步操作）
"""

import asyncio
import json
import time

import asyncpg

from src.config import Settings
from src.platform.obs.logger import get_logger

log = get_logger(__name__)

# ── 事件 → 任务 dispatch 表 ────────────────────────────────────────

async def _dispatch_event(event_type: str, payload: dict, envelope_tenant_id: str = ""):
    """根据事件类型分发到对应的 Celery 任务。

    tenant_id 按设计文档 §3.1 定义在事件信封层（outbox.tenant_id 列），
    不在 payload 字典中。此处显式传入，避免 handler 从 payload 中误读。
    """
    if event_type == "DocumentMounted":
        await _handle_document_mounted(payload, envelope_tenant_id)
    elif event_type == "DocumentUnmounted":
        await _handle_document_unmounted(payload)
    elif event_type == "MountEnabledChanged":
        _handle_mount_enabled_changed(payload)
    else:
        log.debug("outbox_event_ignored", event_type=event_type)


async def _handle_document_mounted(payload: dict, envelope_tenant_id: str = ""):
    """DocumentMounted → 提交 ingest_document_task 到 ingestion_queue。

    这是从注册→解析的唯一触发路径（v14.md §13.3.2）：
    trigger_parse 只写 outbox，不直接调 Celery。
    outbox_relay 是 DocumentMounted → B-INGEST 的唯一桥接。
    """
    from src.ingest.service import ingest_document_task

    document_id = payload.get("document_id", "")
    mount_id = payload.get("mount_id", "")
    kb_id = payload.get("kb_id", "")
    # tenant_id 来自事件信封（outbox.tenant_id 列），非 payload 字段（§3.1）
    tenant_id = envelope_tenant_id or payload.get("tenant_id", "")
    chunking_config_version = payload.get("chunking_config_version", "v1")

    if not mount_id or not document_id:
        log.warning("document_mounted_invalid_payload", payload_keys=list(payload.keys()))
        return

    # 查询当前 execution_epoch（trigger_parse 已写入 ingest_execution）
    import asyncpg as _apg
    try:
        s = Settings()
        dsn = s.database_url.replace("postgresql+asyncpg://", "postgresql://")
        conn = await _apg.connect(dsn)
        try:
            epoch = await conn.fetchval(
                "SELECT execution_epoch FROM ingest_executions WHERE mount_id=$1", mount_id)
            epoch = epoch or 1
        finally:
            await conn.close()
    except Exception:
        epoch = 1

    ingest_document_task.delay(
        mount_id=mount_id,
        document_id=document_id,
        kb_id=kb_id,
        tenant_id=tenant_id,
        chunking_config_version=chunking_config_version,
        pipeline_yaml_version="v1",
        execution_epoch=epoch,
    )
    log.info("outbox_dispatched_ingest", mount_id=mount_id, document_id=document_id)


async def _handle_document_unmounted(payload: dict):
    """DocumentUnmounted → 触发 chunk 清理。"""
    from src.ingest.service import cleanup_mount_chunks

    mount_id = payload.get("mount_id", "")
    kb_id = payload.get("kb_id", "")

    if mount_id:
        try:
            import asyncpg as _apg
            s = Settings()
            dsn = s.database_url.replace("postgresql+asyncpg://", "postgresql://")
            conn = await _apg.connect(dsn)
            try:
                doc_id = await conn.fetchval(
                    "SELECT document_id FROM document_kb_mounts WHERE id=$1", mount_id)
            finally:
                await conn.close()

            if doc_id:
                cleanup_mount_chunks(mount_id, str(doc_id), kb_id)
        except Exception as exc:
            log.warning("unmounted_cleanup_failed", mount_id=mount_id, error=str(exc))


def _handle_mount_enabled_changed(payload: dict):
    """MountEnabledChanged → 更新 Milvus payload retrievable 字段。"""
    mount_id = payload.get("mount_id", "")
    is_enabled = payload.get("is_enabled", True)

    if mount_id:
        try:
            # 直接更新 Milvus（轻量操作，无需走完整摄入 Pipeline）
            from pymilvus import Collection, connections
            s = Settings()
            connections.connect("default", host=s.milvus_host, port=str(s.milvus_port))
            try:
                col = Collection("rag_documents")
                # 查询该 mount 的 chunks 并更新 retrievable
                results = col.query(
                    expr=f'mount_id == "{mount_id}"',
                    output_fields=["id"],
                    limit=5000,
                )
                for batch_start in range(0, len(results), 500):
                    batch = results[batch_start:batch_start + 500]
                    for row in batch:
                        col.upsert([{"id": row["id"], "retrievable": is_enabled}])
                    col.flush()
            finally:
                connections.disconnect("default")
            log.info("mount_enabled_updated", mount_id=mount_id, is_enabled=is_enabled)
        except Exception as exc:
            log.warning("mount_enabled_update_failed", mount_id=mount_id, error=str(exc))


# ── 主轮询循环 ──────────────────────────────────────────────────────

async def _poll_and_dispatch():
    """轮询 outbox 表并将领域事件分发到对应的 Celery 任务。"""
    settings = Settings()
    dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(dsn)

    log.info("outbox_relay_started")

    try:
        while True:
            rows = await conn.fetch(
                "SELECT id, event_type, payload, tenant_id FROM outbox "
                "WHERE status = 'pending' ORDER BY created_at LIMIT 50"
            )
            for row in rows:
                event_type = row["event_type"]
                payload = row["payload"]
                envelope_tenant_id = row["tenant_id"] or ""
                if isinstance(payload, str):
                    payload = json.loads(payload)

                try:
                    await _dispatch_event(event_type, payload, envelope_tenant_id)
                    await conn.execute(
                        "UPDATE outbox SET status = 'sent' WHERE id = $1", row["id"])
                except Exception as exc:
                    log.error("outbox_dispatch_failed",
                              event_type=event_type, error=str(exc))
                    # Mark as failed so it doesn't block the queue forever
                    await conn.execute(
                        "UPDATE outbox SET status = 'failed' WHERE id = $1", row["id"])

            await asyncio.sleep(1)
    finally:
        await conn.close()


def main():
    """Outbox relay 入口（常驻进程）。"""
    asyncio.run(_poll_and_dispatch())


if __name__ == "__main__":
    main()

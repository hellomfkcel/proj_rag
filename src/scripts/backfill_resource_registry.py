"""一次性回填：将 PostgreSQL 中已存在但未在权限服务注册的文档和挂载关系进行补注册。

运行方式:
  conda activate rag_dev_v14
  cd /home/mfkcel/proj_rag_dev
  python -m src.scripts.backfill_resource_registry

背景:
  权限系统采用 write-through 注册模式——文档上传时通过 submit_ingest_task
  同步调用 register_resource + link_resource 在权限服务登记资源。
  但以下场景会导致资源未注册:
  - v14 早期版本的文档上传路径尚未包含 register_resource 调用
  - 权限服务数据被重建/重置后，RAG 侧的文档记录成为孤儿
  - reset_rag_data.py 仅重新播种 KB 注册，不处理文档注册

  未注册文档的影响:
  - 无法删除（doc:unmount 判定时，Cerbos 因缺少 retired 属性而 deny）
  - 无法检索（vis_version=0，六条件过滤器排除）
  - doc:view/doc:download 同样 deny

原理:
  - 从 documents 表扫描所有文档，逐个尝试注册（幂等，已注册的跳过）
  - 从 document_kb_mounts 表扫描所有挂载，逐个尝试 link（幂等，已链接的跳过）
  - 使用 service_api_key 进行服务间认证（无需用户 JWT）
  - 构建系统主体 ctx（is_service_account=true），操作记录可审计
"""

import sys
sys.path = [p for p in sys.path if 'ros' not in p]

import asyncio
import asyncpg
import uuid
from datetime import datetime, timezone

from src.config import Settings
from src.permission.authz import register_resource, link_resource
from src.permission.context import RequestContext
from src.platform.obs.logger import get_logger

log = get_logger(__name__)

BATCH_SIZE = 50  # 每批处理数量，控制权限服务压力


def _dsn():
    return Settings().database_url.replace("postgresql+asyncpg://", "postgresql://")


def _build_system_ctx(tenant_id: str) -> RequestContext:
    """构建系统主体 ctx——用于服务间调用，is_service_account=true。"""
    return RequestContext(
        request_id=str(uuid.uuid4()).replace("-", "")[:32],
        user_id="system:backfill",
        tenant_id=tenant_id,
        credential="",  # 生命周期端点使用 service_api_key 认证，不需要 JWT
        roles=["system"],
        groups=[],
        principals=["user:system:backfill", "role:system"],
        is_service_account=True,
    )


async def _backfill_documents():
    """注册未在权限服务登记的文档。"""
    conn = await asyncpg.connect(_dsn())
    try:
        total = await conn.fetchval("SELECT count(*) FROM documents")
        log.info("backfill_documents_start", total=total)

        registered = 0
        skipped = 0
        failed = 0

        offset = 0
        while True:
            rows = await conn.fetch(
                "SELECT id, tenant_id, filename, uploaded_by "
                "FROM documents ORDER BY created_at DESC "
                "LIMIT $1 OFFSET $2",
                BATCH_SIZE, offset,
            )
            if not rows:
                break

            for row in rows:
                doc_id = str(row["id"])
                tenant_id = row["tenant_id"]
                filename = row["filename"]
                uploaded_by = row["uploaded_by"] or "unknown"
                owner = f"user:{uploaded_by}"

                try:
                    ctx = _build_system_ctx(tenant_id)
                    register_resource(ctx, "document", doc_id, owner, name=filename)
                    registered += 1
                except RuntimeError as exc:
                    msg = str(exc).lower()
                    if "already exists" in msg or "conflict" in msg or "409" in str(exc):
                        skipped += 1
                    else:
                        log.warning("backfill_document_failed",
                                    doc_id=doc_id, tenant_id=tenant_id,
                                    error=str(exc)[:200])
                        failed += 1
                except Exception as exc:
                    log.warning("backfill_document_failed",
                                doc_id=doc_id, tenant_id=tenant_id,
                                error=str(exc)[:200])
                    failed += 1

            offset += BATCH_SIZE
            log.info("backfill_documents_progress",
                     processed=min(offset, total), total=total,
                     registered=registered, skipped=skipped, failed=failed)

        log.info("backfill_documents_complete",
                 total=total, registered=registered,
                 skipped=skipped, failed=failed)
        return {"registered": registered, "skipped": skipped, "failed": failed}
    finally:
        await conn.close()


async def _backfill_mounts():
    """补链接未在权限服务登记的挂载关系。"""
    conn = await asyncpg.connect(_dsn())
    try:
        total = await conn.fetchval("SELECT count(*) FROM document_kb_mounts")
        log.info("backfill_mounts_start", total=total)

        linked = 0
        skipped = 0
        failed = 0

        offset = 0
        while True:
            rows = await conn.fetch(
                "SELECT m.document_id, m.kb_id, d.tenant_id "
                "FROM document_kb_mounts m "
                "JOIN documents d ON m.document_id = d.id "
                "ORDER BY m.mounted_at DESC "
                "LIMIT $1 OFFSET $2",
                BATCH_SIZE, offset,
            )
            if not rows:
                break

            for row in rows:
                doc_id = str(row["document_id"])
                kb_id = str(row["kb_id"])
                tenant_id = row["tenant_id"]

                try:
                    ctx = _build_system_ctx(tenant_id)
                    link_resource(ctx, doc_id, kb_id)
                    linked += 1
                except RuntimeError as exc:
                    msg = str(exc).lower()
                    if "already exists" in msg or "conflict" in msg or "409" in str(exc):
                        skipped += 1
                    else:
                        log.warning("backfill_link_failed",
                                    doc_id=doc_id, kb_id=kb_id,
                                    error=str(exc)[:200])
                        failed += 1
                except Exception as exc:
                    log.warning("backfill_link_failed",
                                doc_id=doc_id, kb_id=kb_id,
                                error=str(exc)[:200])
                    failed += 1

            offset += BATCH_SIZE
            log.info("backfill_mounts_progress",
                     processed=min(offset, total), total=total,
                     linked=linked, skipped=skipped, failed=failed)

        log.info("backfill_mounts_complete",
                 total=total, linked=linked, skipped=skipped, failed=failed)
        return {"linked": linked, "skipped": skipped, "failed": failed}
    finally:
        await conn.close()


async def main():
    """主入口：先回填文档注册，再回填挂载链接。"""
    log.info("backfill_resource_registry_start")

    doc_result = await _backfill_documents()
    mount_result = await _backfill_mounts()

    log.info("backfill_resource_registry_complete",
             documents=doc_result, mounts=mount_result)

    print(f"\n回填完成:")
    print(f"  文档: 注册 {doc_result['registered']}, "
          f"跳过(已存在) {doc_result['skipped']}, 失败 {doc_result['failed']}")
    print(f"  挂载: 链接 {mount_result['linked']}, "
          f"跳过(已存在) {mount_result['skipped']}, 失败 {mount_result['failed']}")


if __name__ == "__main__":
    asyncio.run(main())

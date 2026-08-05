"""B-DOC：文档与目录管理。

提供：
- submit_ingest_task     文档登记 + register + link + 同步触发解析
- trigger_parse          触发解析（发 DocumentMounted 事件）
- delete_document_from_kb 从 KB 移除（purge=false）

职责：文档物理登记与去重、挂载关系、触发解析、在写路径同步调权限服务生命周期端口。
不做：不执行解析、不写执行状态、不碰 chunk、不碰任何权益数据、不提供授权界面。
"""

import asyncio
import asyncpg
import hashlib
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

from src.config import Settings
from src.doc.models import (
    Document, KnowledgeBase, DocumentKBMount,
)
from src.doc.events import document_mounted_event
from src.platform.store.backend import StorageBackend
from src.permission.context import RequestContext
from src.permission.authz import register_resource, link_resource, unlink_resource, retire_resource


def _build_ctx(user_id: str, tenant_id: str, request_id: str = "", credential: str = "") -> RequestContext:
    """构造最小 RequestContext，供 doc/service.py 调 P-AUTHC 生命周期门面使用。

    P-AUTHC 门面只消费 ctx 的 request_id 和 credential 字段（用于构造 Envelope），
    以及 resource_type/resource_id/owner 参数。其余字段对生命周期调用不影响。
    """
    return RequestContext(
        request_id=request_id or str(uuid.uuid4()).replace("-", "")[:32],
        user_id=user_id,
        tenant_id=tenant_id,
        credential=credential or "",
        roles=[],
        groups=[],
        principals=[f"user:{user_id}"],
    )


def _get_store() -> StorageBackend:
    s = Settings()
    return StorageBackend(
        endpoint_url=s.s3_endpoint_url,
        access_key=s.s3_access_key,
        secret_key=s.s3_secret_key,
        bucket=s.s3_bucket,
    )


def _compute_fingerprint(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def submit_ingest_task(
    user_id: str,
    tenant_id: str,
    kb_id: str,
    filename: str,
    file_content: bytes,
    auto_parse: bool = True,
    request_id: str = "",
    credential: str = "",
) -> dict:
    """文档上传入口：只登记不解析。

    行为契约（§13.3.1 + §13.7 先调权限服务后提交本地）：
    1. 计算指纹，按 (tenant_id, fingerprint) 查 document → 存在则复用
    2. 不存在：生成 doc_id → 写 SeaweedFS → ★先调 register_resource
       → 成功后 INSERT INTO documents
       → register 失败即中止，不写 documents 表
    3. 查/建挂载关系 → ★先调 link_resource → 成功后 INSERT INTO document_kb_mounts
       → link 失败即中止，不写挂载表
    4. 如果 auto_parse=true：登记后立即内部调 trigger_parse

    异常安全：唯一可能的不一致是"权限服务有、本地无"（孤儿镜像），
    安全且可被 §13.7b 结构镜像对账回收。绝不会出现"本地有、权限服务无"。
    """
    store = _get_store()
    fingerprint = _compute_fingerprint(file_content)

    # 使用原始 asyncpg 连接（阶段一简单走同步 psycopg2 或直接 dict 操作）
    # 开发期：直接在 PostgreSQL 上用简单的方式操作
    import asyncpg
    import asyncio

    async def _do():
        s = Settings()
        conn = await asyncpg.connect(
            s.database_url.replace("postgresql+asyncpg://", "postgresql://")
        )

        try:
            # 1. 物理去重
            doc_row = await conn.fetchrow(
                "SELECT id, storage_path FROM documents WHERE tenant_id=$1 AND content_fingerprint=$2",
                tenant_id, fingerprint,
            )
            doc_id = str(doc_row["id"]) if doc_row else None
            is_duplicate = doc_id is not None

            if not doc_id:
                # 生成 doc_id（在写 S3 和 register 之前）
                doc_id = str(uuid.uuid4())

                # 写入 SeaweedFS（物理存储先于 DB——S3 不可回滚，
                # 若后续 register 失败则留下孤儿 S3 对象，可被定期清理脚本回收）
                s3_key = f"docs/{tenant_id}/{doc_id}/{filename}"
                storage_path = store.put(s3_key, file_content, "application/octet-stream")

                # ★ 先调权限服务 register_resource（§13.7）
                # 失败即中止：不写 documents 表，S3 孤儿可接受
                ctx = _build_ctx(user_id, tenant_id, request_id, credential)
                register_resource(ctx, "document", doc_id, f"user:{user_id}", name=filename)

                # register 成功后写 documents 表
                # 若此步失败：resource_registry 中有记录而 documents 表无记录
                # → 孤儿镜像，安全方向，可被 §13.7b 对账回收
                await conn.execute(
                    """INSERT INTO documents (id, tenant_id, filename, content_fingerprint,
                       storage_path, file_size, mime_type, uploaded_by)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8)""",
                    doc_id, tenant_id, filename, fingerprint,
                    storage_path, len(file_content), "", user_id,
                )

            # 2. 挂载关系
            mount_row = await conn.fetchrow(
                "SELECT id FROM document_kb_mounts WHERE document_id=$1 AND kb_id=$2",
                doc_id, kb_id,
            )
            mount_id = str(mount_row["id"]) if mount_row else None

            if not mount_id:
                mount_id = str(uuid.uuid4())

                # ★ 先调权限服务 link_resource（§13.7）
                # 失败即中止：不写 document_kb_mounts 表
                ctx = _build_ctx(user_id, tenant_id, request_id, credential)
                link_resource(ctx, doc_id, kb_id)

                # link 成功后写 document_kb_mounts 表
                # 若此步失败：mount_registry 中有记录而 document_kb_mounts 表无记录
                # → 孤儿镜像，安全方向
                await conn.execute(
                    """INSERT INTO document_kb_mounts (id, document_id, kb_id, mounted_by)
                       VALUES ($1,$2,$3,$4)""",
                    mount_id, doc_id, kb_id, user_id,
                )

            # 3. 如果 auto_parse，触发解析
            parse_status = "not_parsed"
            if auto_parse:
                await _trigger_parse_internal(conn, mount_id, kb_id, tenant_id)
                parse_status = "queued"

            return {
                "document_id": doc_id,
                "mount_id": mount_id,
                "parse_status": parse_status,
                "duplicate": is_duplicate,
            }
        finally:
            await conn.close()

    return asyncio.run(_do())


def trigger_parse(mount_id: str, kb_id: str, tenant_id: str) -> dict:
    """触发解析：发 DocumentMounted 事件。

    行为：
    1. 解析去重（经 B-INGEST get_parse_status）
    2. 经 P-CONFIG 取当前切分配置版本并锚定
    3. 同事务写 outbox 发布 DocumentMounted
    """
    import asyncpg
    import asyncio

    async def _do():
        s = Settings()
        conn = await asyncpg.connect(
            s.database_url.replace("postgresql+asyncpg://", "postgresql://")
        )

        try:
            return await _trigger_parse_internal(conn, mount_id, kb_id, tenant_id)
        finally:
            await conn.close()

    return asyncio.run(_do())


async def _trigger_parse_internal(conn, mount_id: str, kb_id: str, tenant_id: str) -> dict:
    """内部：在已有事务连接中触发解析。"""
    from src.platform.config.service import resolve_chunking_config

    cc = resolve_chunking_config(kb_id)

    # 从 mount 反查 document_id（submit_ingest_task 在同一事务中已创建 mount 记录）
    doc_row = await conn.fetchrow(
        "SELECT document_id FROM document_kb_mounts WHERE id=$1", mount_id)
    document_id = str(doc_row["document_id"]) if doc_row else ""

    # 写 outbox 发布 DocumentMounted
    event = document_mounted_event(
        document_id=document_id,
        mount_id=mount_id,
        kb_id=kb_id,
        tenant_id=tenant_id,
        chunking_config_version=cc.version,
    )
    import json
    payload = event.to_outbox_dict()
    await conn.execute(
        """INSERT INTO outbox (id, event_type, payload, tenant_id, trace_id, status, created_at)
           VALUES ($1,$2,$3,$4,$5,$6,$7)""",
        str(uuid.uuid4()), payload["event_type"],
        json.dumps(event.payload, default=str),
        payload["tenant_id"], payload["trace_id"],
        payload["status"], datetime.now(timezone.utc),
    )

    return {"mount_id": mount_id, "parse_status": "queued"}


def delete_document_from_kb(
    user_id: str,
    doc_id: str,
    kb_id: str,
    tenant_id: str,
    purge: bool = False,
    request_id: str = "",
    credential: str = "",
) -> dict:
    """从 KB 移除文档。

    purge=false（阶段一）：
    - 权限 doc:unmount（带 channel.kb）
    - 同步调 unlink_resource → 删挂载 → 发 DocumentUnmounted
    purge=true（阶段一返回 501）：
    - 阶段三实现
    """
    if purge:
        return _purge_document(user_id, doc_id, tenant_id, request_id, credential)

    import asyncpg
    import asyncio

    async def _do():
        s = Settings()
        conn = await asyncpg.connect(
            s.database_url.replace("postgresql+asyncpg://", "postgresql://")
        )

        try:
            # 同步调权限服务 unlink_resource（经 P-AUTHC 门面，单一出口）
            ctx = _build_ctx(user_id, tenant_id, request_id, credential)
            unlink_resource(ctx, doc_id, kb_id)

            # 查询 mount_id 用于级联删除
            mount_row = await conn.fetchrow(
                "SELECT id FROM document_kb_mounts WHERE document_id=$1 AND kb_id=$2",
                doc_id, kb_id,
            )
            mount_id = str(mount_row["id"]) if mount_row else None

            # 按外键依赖顺序：先删子表，后删主表
            if mount_id:
                await conn.execute(
                    "DELETE FROM ingest_executions WHERE mount_id=$1", mount_id,
                )
            await conn.execute(
                "DELETE FROM document_kb_mounts WHERE document_id=$1 AND kb_id=$2",
                doc_id, kb_id,
            )

            # 检查是否还有剩余挂载——无剩余则自动退役文档
            remaining = await conn.fetchval(
                "SELECT count(*) FROM document_kb_mounts WHERE document_id=$1", doc_id,
            )
            if remaining == 0:
                retire_resource(ctx, "document", doc_id)
                await conn.execute("DELETE FROM documents WHERE id=$1", doc_id)

            # 发 DocumentUnmounted 事件
            from src.doc.events import document_unmounted_event
            event = document_unmounted_event(
                mount_id=f"{doc_id}-{kb_id}",
                kb_id=kb_id,
                tenant_id=tenant_id,
            )
            import json
            payload = event.to_outbox_dict()
            await conn.execute(
                """INSERT INTO outbox (id, event_type, payload, tenant_id, trace_id, status, created_at)
                   VALUES ($1,$2,$3,$4,$5,$6,$7)""",
                str(uuid.uuid4()), payload["event_type"],
                json.dumps(event.payload, default=str),
                payload["tenant_id"], payload["trace_id"],
                payload["status"], datetime.now(timezone.utc),
            )

            return {"status": "deleted", "doc_id": doc_id, "kb_id": kb_id}
        finally:
            await conn.close()

    return asyncio.run(_do())


# ══════════════════════════════════════════════════════════════════
# _purge_document（阶段三：purge=true 四合一 retire）
# ══════════════════════════════════════════════════════════════════

def _purge_document(user_id: str, doc_id: str, tenant_id: str,
                    request_id: str = "", credential: str = "") -> dict:
    """彻底删除文档（阶段三 v14.md §13.4.3 + §13.7 先调权限服务后提交本地）。

    操作顺序（不可颠倒）：
    1. 快照——取当前全部挂载
    2. ★ 先调权限服务 retire_resource（四合一原子操作）
       → 失败即中止，不删任何本地数据
    3. retire 成功后：逐个删除挂载 → 发事件 → 清理 Milvus chunk
    4. 删除 S3 文件
    5. 审计 + 删除 document 记录
    """

    async def _do():
        s = Settings()
        conn = await asyncpg.connect(
            s.database_url.replace("postgresql+asyncpg://", "postgresql://")
        )
        store = _get_store()

        try:
            # 1. 快照：取当前全部挂载（在 retire 之前）
            mounts = await conn.fetch(
                "SELECT id, kb_id FROM document_kb_mounts WHERE document_id = $1", doc_id
            )
            from src.platform.obs.logger import get_logger
            mount_count = len(mounts)
            log = get_logger(__name__)

            if mount_count == 0:
                return {"status": "not_found", "reason": "no mounts for document", "doc_id": doc_id}

            # 2. ★ 先调权限服务 retire_resource（§13.7 + §13.4.3）
            #    retire 在权限服务侧原子完成四件事：
            #    a. 回收全部 acl  b. 回收全部 restriction
            #    c. 解除全部挂载镜像（unlinked=true） d. 置 retired=true
            #    失败即中止——不删任何本地数据
            ctx = _build_ctx(user_id, tenant_id, request_id, credential)
            retire_resource(ctx, "document", doc_id)

            # 3. retire 成功后：清理本地挂载 + Milvus chunk
            for row in mounts:
                mount_id = str(row["id"]); kb_id = str(row["kb_id"])
                await conn.execute("DELETE FROM document_kb_mounts WHERE id = $1", mount_id)

                from src.doc.events import document_unmounted_event
                event = document_unmounted_event(mount_id=mount_id, kb_id=kb_id, tenant_id=tenant_id)
                import json
                payload = event.to_outbox_dict()
                await conn.execute(
                    """INSERT INTO outbox (id, event_type, payload, tenant_id, trace_id, status, created_at)
                       VALUES ($1,$2,$3,$4,$5,$6,$7)""",
                    str(uuid.uuid4()), payload["event_type"],
                    json.dumps(event.payload, default=str),
                    payload["tenant_id"], payload["trace_id"],
                    payload["status"], datetime.now(timezone.utc),
                )
                from src.ingest.service import cleanup_mount_chunks
                cleanup_mount_chunks(mount_id, doc_id, kb_id)

            # 4. 删除 S3 文件
            storage_path = await conn.fetchval(
                "SELECT storage_path FROM documents WHERE id = $1", doc_id)
            if storage_path:
                try: store.delete(storage_path.replace("s3://rag-files/", ""))
                except Exception: log.warning("s3_delete_failed", key=storage_path)

            # 5. 审计 + 删除 document 记录
            from src.platform.audit.service import emit_audit_event_txn
            emit_audit_event_txn(event_type="DOC_DELETE", user_id=user_id, tenant_id=tenant_id,
                                 action="doc:purge", resource_type="document", resource_id=doc_id,
                                 allowed=True, conn_for_txn=conn)
            emit_audit_event_txn(event_type="AUTHZ_WRITE", user_id=user_id, tenant_id=tenant_id,
                                 action="retire", resource_type="document", resource_id=doc_id,
                                 allowed=True, conn_for_txn=conn)

            await conn.execute("DELETE FROM documents WHERE id = $1", doc_id)
            log.info("document_purged", doc_id=doc_id, mount_count=mount_count)
            return {"status": "purged", "doc_id": doc_id, "mounts_cleaned": mount_count}
        finally:
            await conn.close()

    return asyncio.run(_do())

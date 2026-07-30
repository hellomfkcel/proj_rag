"""Phase 2 KB + 文档管理 REST 端点。"""

import uuid
from datetime import datetime, timezone
from typing import Optional, List
import concurrent.futures as _cf

import asyncpg
from fastapi import APIRouter, HTTPException, Depends, Query, BackgroundTasks
from pydantic import BaseModel

from src.config import Settings
from src.api.deps import get_request_context
from src.permission.context import RequestContext

router = APIRouter(prefix="/api/v1", tags=["kb"])


def _dsn():
    return Settings().database_url.replace("postgresql+asyncpg://", "postgresql://")


# ── Models ──

class KBCreateRequest(BaseModel):
    name: str
    description: str = ""
    chunking_strategy: str = "sentence"  # sentence / word / passage

class KBUpdateRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None

class ChunkingConfigResponse(BaseModel):
    kb_id: str
    version: str
    haystack_strategy: str
    split_length: int | None = None
    split_overlap: int | None = None
    advanced_params: dict | None = None  # P1-7: 策略专属高级参数

class ChunkingConfigPatch(BaseModel):
    haystack_strategy: Optional[str] = None
    split_length: Optional[int] = None
    split_overlap: Optional[int] = None
    advanced_params: Optional[dict] = None  # P1-7: e.g. {breakpoint_threshold_percentile:50, buffer_size:1}

class KBResponse(BaseModel):
    id: str
    tenant_id: str
    name: str
    description: str
    owner_id: str
    status: str
    created_at: str

class DocResponse(BaseModel):
    document_id: str
    mount_id: str
    filename: str
    file_size: int
    mime_type: str
    parse_status: str
    is_enabled: bool
    uploaded_by: str
    mounted_at: str
    created_at: str

class DocUpdateRequest(BaseModel):
    is_enabled: Optional[bool] = None

class TriggerParseResponse(BaseModel):
    mount_id: str
    parse_status: str


# ── KB CRUD ──

@router.get("/knowledge-bases", response_model=List[KBResponse])
async def list_kbs(ctx: RequestContext = Depends(get_request_context)):
    conn = await asyncpg.connect(_dsn())
    try:
        rows = await conn.fetch(
            "SELECT id, tenant_id, name, description, owner_id, status, created_at "
            "FROM knowledge_bases WHERE tenant_id=$1 ORDER BY created_at DESC",
            ctx.tenant_id,
        )
        return [_kb_to_dict(r) for r in rows]
    finally:
        await conn.close()


@router.post("/knowledge-bases", response_model=KBResponse, status_code=201)
async def create_kb(body: KBCreateRequest, ctx: RequestContext = Depends(get_request_context)):
    kb_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    # ★ 先调权限服务 register_resource（§13.7）
    # 失败即中止：不写 knowledge_bases 表
    from src.permission.authz import register_resource
    try:
        register_resource(ctx, "kb", kb_id, f"user:{ctx.user_id}")
    except RuntimeError:
        raise HTTPException(502, "doc:authz_write_failed — failed to register KB in permission service")

    conn = await asyncpg.connect(_dsn())
    try:
        await conn.execute(
            "INSERT INTO knowledge_bases (id, tenant_id, name, description, owner_id, status, created_at) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7)",
            kb_id, ctx.tenant_id, body.name, body.description, ctx.user_id, "active", now,
        )
        # Validate chunking strategy
        valid_strategies = {"sentence", "word", "passage", "semantic", "hierarchical"}
        strategy = body.chunking_strategy if body.chunking_strategy in valid_strategies else "sentence"

        # 种子配置
        await conn.execute(
            "INSERT INTO chunking_configs (kb_id, version, haystack_strategy) VALUES ($1,'v1',$2) "
            "ON CONFLICT DO NOTHING", kb_id, strategy)
        await conn.execute(
            "INSERT INTO retrieval_configs (scope_type, scope_id, top_k) VALUES ('kb',$1,10) "
            "ON CONFLICT DO NOTHING", kb_id)
        # 创建 kb_bound 目录
        await conn.execute(
            "INSERT INTO directories (id, tenant_id, name, directory_type, bound_kb_id, created_by) "
            "VALUES ($1,$2,$3,'kb_bound',$4,$5)",
            str(uuid.uuid4()), ctx.tenant_id, body.name, kb_id, ctx.user_id,
        )
    finally:
        await conn.close()

    return KBResponse(
        id=kb_id, tenant_id=ctx.tenant_id, name=body.name,
        description=body.description, owner_id=ctx.user_id,
        status="active", created_at=now.isoformat(),
    )


@router.patch("/knowledge-bases/{kb_id}", response_model=KBResponse)
async def update_kb(kb_id: str, body: KBUpdateRequest, ctx: RequestContext = Depends(get_request_context)):
    conn = await asyncpg.connect(_dsn())
    try:
        row = await conn.fetchrow("SELECT * FROM knowledge_bases WHERE id=$1 AND tenant_id=$2", kb_id, ctx.tenant_id)
        if not row:
            raise HTTPException(404, "doc:not_found")

        new_name = body.name or row["name"]
        new_desc = body.description or row["description"]

        await conn.execute(
            "UPDATE knowledge_bases SET name=$1, description=$2 WHERE id=$3",
            new_name, new_desc, kb_id,
        )
        return KBResponse(
            id=kb_id, tenant_id=row["tenant_id"], name=new_name,
            description=new_desc, owner_id=row["owner_id"],
            status=row["status"], created_at=row["created_at"].isoformat(),
        )
    finally:
        await conn.close()


@router.delete("/knowledge-bases/{kb_id}")
def delete_kb(kb_id: str, ctx: RequestContext = Depends(get_request_context)):
    import asyncio as _a
    async def _do():
        conn = await asyncpg.connect(_dsn())
        try:
            row = await conn.fetchrow("SELECT * FROM knowledge_bases WHERE id=$1 AND tenant_id=$2", kb_id, ctx.tenant_id)
            if not row: raise HTTPException(404, "doc:not_found")
            mounts = await conn.fetchval("SELECT count(*) FROM document_kb_mounts WHERE kb_id=$1", kb_id)
            if mounts > 0: raise HTTPException(409, f"KB has {mounts} active document mounts — remove them first")

            # ★ 先调权限服务 retire_resource（§13.7）
            # 失败即中止：不删本地数据
            from src.permission.authz import retire_resource
            try:
                retire_resource(ctx, "kb", kb_id)
            except RuntimeError:
                raise HTTPException(502, "doc:authz_write_failed — failed to retire KB in permission service")

            # retire 成功后清理本地数据
            await conn.execute("DELETE FROM knowledge_bases WHERE id=$1", kb_id)
            await conn.execute("DELETE FROM directories WHERE bound_kb_id=$1", kb_id)
            await conn.execute("DELETE FROM chunking_configs WHERE kb_id=$1", kb_id)
            await conn.execute("DELETE FROM retrieval_configs WHERE scope_type='kb' AND scope_id=$1", kb_id)
            return {"status": "deleted", "kb_id": kb_id}
        finally:
            await conn.close()
    return _a.run(_do())


# ── Chunking Config ──

@router.get("/knowledge-bases/{kb_id}/chunking-config", response_model=ChunkingConfigResponse)
async def get_chunking_config(kb_id: str, ctx: RequestContext = Depends(get_request_context)):
    """读取 KB 的当前切分配置。"""
    conn = await asyncpg.connect(_dsn())
    try:
        row = await conn.fetchrow(
            "SELECT kb_id, version, haystack_strategy, split_length, split_overlap, advanced_params "
            "FROM chunking_configs WHERE kb_id=$1 ORDER BY version DESC LIMIT 1",
            kb_id,
        )
        if not row:
            raise HTTPException(404, "kb:not_found")
        import json as _json
        adv = row["advanced_params"] or {}
        if isinstance(adv, str):
            adv = _json.loads(adv)
        return ChunkingConfigResponse(
            kb_id=str(row["kb_id"]),
            version=row["version"],
            haystack_strategy=row["haystack_strategy"] or "sentence",
            split_length=row["split_length"],
            split_overlap=row["split_overlap"],
            advanced_params=adv if isinstance(adv, dict) else None,
        )
    finally:
        await conn.close()


@router.patch("/knowledge-bases/{kb_id}/chunking-config", response_model=ChunkingConfigResponse)
async def update_chunking_config(kb_id: str, body: ChunkingConfigPatch, ctx: RequestContext = Depends(get_request_context)):
    """更新 KB 切分配置 — 更新已有版本或插入新版本。"""
    valid_strategies = {"sentence", "word", "passage", "semantic", "hierarchical"}
    conn = await asyncpg.connect(_dsn())
    try:
        # Check KB exists
        kb_row = await conn.fetchrow("SELECT id FROM knowledge_bases WHERE id=$1 AND tenant_id=$2", kb_id, ctx.tenant_id)
        if not kb_row:
            raise HTTPException(404, "kb:not_found")

        current = await conn.fetchrow(
            "SELECT version, haystack_strategy, split_length, split_overlap, advanced_params "
            "FROM chunking_configs WHERE kb_id=$1 ORDER BY version DESC LIMIT 1",
            kb_id,
        )

        new_strategy = body.haystack_strategy or (current["haystack_strategy"] if current else "sentence")
        if new_strategy not in valid_strategies:
            new_strategy = "sentence"

        new_length = body.split_length if body.split_length is not None else (current["split_length"] if current else 256)
        new_overlap = body.split_overlap if body.split_overlap is not None else (current["split_overlap"] if current else 32)
        # P1-7: Merge advanced_params — body overrides current values
        import json as _json
        curr_adv = current["advanced_params"] if current else {}
        if isinstance(curr_adv, str):
            curr_adv = _json.loads(curr_adv) if curr_adv else {}
        if not isinstance(curr_adv, dict):
            curr_adv = {}
        new_adv = dict(curr_adv)
        if body.advanced_params is not None:
            new_adv.update(body.advanced_params)
        new_adv_json = _json.dumps(new_adv)

        if current:
            # Update existing row in-place
            await conn.execute(
                "UPDATE chunking_configs SET haystack_strategy=$1, split_length=$2, split_overlap=$3, "
                "advanced_params=$4::jsonb WHERE kb_id=$5 AND version=$6",
                new_strategy, new_length, new_overlap, new_adv_json, kb_id, current["version"],
            )
            ver = current["version"]
            # Also sync to the associated directory name if KB-bound dir exists
            await conn.execute(
                "UPDATE directories SET name=(SELECT name FROM knowledge_bases WHERE id=$1) WHERE bound_kb_id=$1",
                kb_id,
            )
        else:
            # Insert first config
            await conn.execute(
                "INSERT INTO chunking_configs (kb_id, version, haystack_strategy, split_length, split_overlap, advanced_params) "
                "VALUES ($1,'v1',$2,$3,$4,$5::jsonb)",
                kb_id, new_strategy, new_length, new_overlap, new_adv_json,
            )
            ver = "v1"

        return ChunkingConfigResponse(
            kb_id=kb_id, version=ver,
            haystack_strategy=new_strategy,
            split_length=new_length,
            split_overlap=new_overlap,
            advanced_params=new_adv,
        )
    finally:
        await conn.close()


# ── Document List ──

@router.get("/knowledge-bases/{kb_id}/documents", response_model=List[DocResponse])
async def list_documents(
    kb_id: str,
    ctx: RequestContext = Depends(get_request_context),
    search: str = Query(default=""),
    status: str = Query(default=""),
    sort_by: str = Query(default="created_at"),
    order: str = Query(default="desc"),
):
    conn = await asyncpg.connect(_dsn())
    try:
        where = "m.kb_id = $1"
        args = [kb_id]
        idx = 2

        if search:
            where += f" AND d.filename ILIKE ${idx}"
            args.append(f"%{search}%")
            idx += 1
        if status:
            where += f" AND ie.parse_status = ${idx}"
            args.append(status)
            idx += 1

        safe_sort = "created_at" if sort_by not in ("filename", "file_size", "created_at") else sort_by
        safe_order = "DESC" if order.upper() != "ASC" else "ASC"

        query = f"""
            SELECT d.id as doc_id, m.id as mount_id, d.filename, d.file_size, d.mime_type,
                   COALESCE(ie.parse_status, 'not_parsed') as parse_status,
                   m.is_enabled, d.uploaded_by, m.mounted_at, d.created_at
            FROM document_kb_mounts m
            JOIN documents d ON m.document_id = d.id
            LEFT JOIN ingest_executions ie ON ie.mount_id = m.id
            WHERE {where}
            ORDER BY d.{safe_sort} {safe_order}
            LIMIT 200
        """
        rows = await conn.fetch(query, *args)
        return [_doc_to_dict(r) for r in rows]
    finally:
        await conn.close()


# ── Document Detail ──

@router.get("/documents/{doc_id}")
async def get_document(doc_id: str, ctx: RequestContext = Depends(get_request_context)):
    conn = await asyncpg.connect(_dsn())
    try:
        row = await conn.fetchrow("SELECT * FROM documents WHERE id=$1", doc_id)
        if not row:
            raise HTTPException(404, "doc:not_found")
        return {
            "id": str(row["id"]), "tenant_id": row["tenant_id"],
            "filename": row["filename"], "file_size": row["file_size"],
            "mime_type": row["mime_type"], "uploaded_by": row["uploaded_by"],
            "created_at": row["created_at"].isoformat(),
        }
    finally:
        await conn.close()


# ── Document Chunks ──

@router.get("/documents/{doc_id}/chunks")
async def get_document_chunks(doc_id: str, ctx: RequestContext = Depends(get_request_context)):
    """从 Milvus 查询文档的所有 chunk（限制 100 条）。"""
    from pymilvus import connections as _mc, Collection

    try:
        s = Settings()
        _mc.connect("default", host=s.milvus_host, port=str(s.milvus_port))
        col = Collection("rag_documents")
        col.load()

        results = col.query(
            expr=f'document_id == "{doc_id}"',
            output_fields=["content"],
            limit=100,
        )
        _mc.disconnect("default")

        return [{"chunk_id": str(r.get("id", f"c{i}")), "content": str(r.get("content", ""))[:500]}
                for i, r in enumerate(results)]
    except Exception as e:
        # Milvus unavailable or collection not found — return empty
        return []


# ── Document Content ──

@router.get("/documents/{doc_id}/content")
async def get_document_content(doc_id: str, ctx: RequestContext = Depends(get_request_context)):
    """从 P-STORE 读取文档原文。"""
    conn = await asyncpg.connect(_dsn())
    try:
        row = await conn.fetchrow(
            "SELECT filename, storage_path FROM documents WHERE id=$1", doc_id)
        if not row:
            raise HTTPException(404, "doc:not_found")

        storage_path = row["storage_path"]
        # Dev mode: try local filesystem if DEV_DOCS_DIR is configured
        import os as _os
        s = Settings()
        dev_dir = s.dev_docs_dir
        if dev_dir:
            test_path = _os.path.join(dev_dir, row["filename"])
            if _os.path.exists(test_path):
                with open(test_path, encoding="utf-8") as f:
                    return {"content": f.read()[:50000]}

        # Production: read from SeaweedFS via P-STORE
        from src.platform.store.service import read_file
        content = read_file(storage_path or f"documents/{doc_id}")
        return {"content": content[:50000] if content else ""}
    finally:
        await conn.close()


# ── Document Download ──

@router.get("/documents/{doc_id}/download")
async def download_document(doc_id: str, ctx: RequestContext = Depends(get_request_context)):
    """生成签名 URL 并 302 重定向。"""
    from fastapi.responses import RedirectResponse

    conn = await asyncpg.connect(_dsn())
    try:
        row = await conn.fetchrow(
            "SELECT filename, storage_path FROM documents WHERE id=$1", doc_id)
        if not row:
            raise HTTPException(404, "doc:not_found")

        # Dev mode: try local file if DEV_DOCS_DIR is configured
        import os as _os
        s = Settings()
        dev_dir = s.dev_docs_dir
        if dev_dir:
            test_path = _os.path.join(dev_dir, row["filename"])
            if _os.path.exists(test_path):
                from fastapi.responses import FileResponse
                return FileResponse(test_path, filename=row["filename"])

        # Production: generate presigned URL from SeaweedFS S3
        from src.config import Settings as _S
        import boto3
        s = _S()
        client = boto3.client("s3",
            endpoint_url=s.s3_endpoint_url,
            aws_access_key_id=s.s3_access_key,
            aws_secret_access_key=s.s3_secret_key,
        )
        url = client.generate_presigned_url(
            "get_object",
            Params={"Bucket": s.s3_bucket, "Key": row["storage_path"] or f"documents/{doc_id}"},
            ExpiresIn=3600,
        )
        return RedirectResponse(url=url)
    finally:
        await conn.close()


# ── Document Update (is_enabled) ──

@router.patch("/documents/{doc_id}/kb/{kb_id}")
async def update_document_mount(
    doc_id: str, kb_id: str,
    body: DocUpdateRequest,
    ctx: RequestContext = Depends(get_request_context),
):
    conn = await asyncpg.connect(_dsn())
    try:
        if body.is_enabled is not None:
            # 获取 mount_id 用于事件发布
            mount_row = await conn.fetchrow(
                "SELECT id FROM document_kb_mounts WHERE document_id=$1 AND kb_id=$2",
                doc_id, kb_id,
            )
            mount_id = str(mount_row["id"]) if mount_row else None

            await conn.execute(
                "UPDATE document_kb_mounts SET is_enabled=$1 WHERE document_id=$2 AND kb_id=$3",
                body.is_enabled, doc_id, kb_id,
            )

            # 发布 MountEnabledChanged 事件（通过 outbox → relay → worker 异步链路）
            if mount_id:
                from src.doc.events import mount_enabled_changed_event
                event = mount_enabled_changed_event(
                    mount_id=mount_id,
                    is_enabled=body.is_enabled,
                    tenant_id=ctx.tenant_id,
                    trace_id=ctx.request_id,
                )
                payload = event.to_outbox_dict()
                await conn.execute(
                    """INSERT INTO outbox (id, event_type, payload, tenant_id, trace_id, status, created_at)
                       VALUES ($1, $2, $3::jsonb, $4, $5, 'pending', now())""",
                    event.event_id, payload["event_type"], payload["payload"],
                    payload["tenant_id"], payload["trace_id"],
                )
        return {"document_id": doc_id, "kb_id": kb_id, "is_enabled": body.is_enabled}
    finally:
        await conn.close()


# ── Trigger Parse（通过 outbox → relay → worker 异步链路执行） ──


@router.post("/documents/{doc_id}/trigger-parse", response_model=TriggerParseResponse)
async def trigger_parse(doc_id: str, ctx: RequestContext = Depends(get_request_context)):
    conn = await asyncpg.connect(_dsn())
    try:
        mount_rows = await conn.fetch(
            "SELECT id, kb_id FROM document_kb_mounts WHERE document_id=$1", doc_id)
        if not mount_rows:
            raise HTTPException(404, "doc:not_found")

        mount_id = str(mount_rows[0]["id"])
        kb_id = str(mount_rows[0]["kb_id"])

        # 检查 KB 状态
        kb_status = await conn.fetchval(
            "SELECT status FROM knowledge_bases WHERE id=$1", kb_id)
        if kb_status == "reindexing":
            raise HTTPException(409, "doc:kb_reindexing")

        # 查文档的 storage_path 和 filename
        doc_row = await conn.fetchrow(
            "SELECT filename, storage_path FROM documents WHERE id=$1", doc_id)
        if not doc_row:
            raise HTTPException(404, "doc:not_found")

        filename = doc_row["filename"]

        # 写入 outbox + 初始化 ingest_execution
        from src.doc.events import document_mounted_event
        import json as _json
        event = document_mounted_event(
            document_id=doc_id, mount_id=mount_id, kb_id=kb_id,
            tenant_id=ctx.tenant_id,
        )
        payload = event.to_outbox_dict()
        await conn.execute(
            "INSERT INTO outbox (id, event_type, payload, tenant_id, trace_id, status, created_at) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7)",
            str(uuid.uuid4()), payload["event_type"],
            _json.dumps(event.payload, default=str),
            payload["tenant_id"], payload["trace_id"],
            payload["status"], datetime.now(timezone.utc),
        )
        await conn.execute(
            "INSERT INTO ingest_executions (mount_id, document_id, kb_id, parse_status, execution_epoch, updated_at) "
            "VALUES ($1,$2,$3,'queued',1,$4) "
            "ON CONFLICT (mount_id) DO UPDATE SET parse_status='queued', execution_epoch=ingest_executions.execution_epoch+1, updated_at=$4",
            mount_id, doc_id, kb_id, datetime.now(timezone.utc),
        )
        # 读取当前 execution_epoch（outbox_relay 分发时将查询）
        current_epoch = await conn.fetchval(
            "SELECT execution_epoch FROM ingest_executions WHERE mount_id=$1", mount_id)
    finally:
        await conn.close()

    # outbox_relay 负责从 outbox 中取出 DocumentMounted 事件并分发 Celery 任务。
    # API 进程不直接调 ingest_document_task.delay()——遵循单一分发路径原则。
    return TriggerParseResponse(mount_id=mount_id, parse_status="queued")


# ── PATCH /documents/{doc_id} — 重命名文档（P1 #19） ─────────────

class DocRenameRequest(BaseModel):
    filename: str

@router.patch("/documents/{doc_id}")
async def rename_document(doc_id: str, body: DocRenameRequest,
                          ctx: RequestContext = Depends(get_request_context)):
    """重命名文档（仅改显示名，不改存储路径）。"""
    conn = await asyncpg.connect(_dsn())
    try:
        row = await conn.fetchrow(
            "SELECT id, filename FROM documents WHERE id=$1 AND tenant_id=$2",
            doc_id, ctx.tenant_id)
        if not row:
            raise HTTPException(404, "doc:not_found")

        new_name = body.filename.strip()
        if not new_name:
            raise HTTPException(422, "common:validation_error — filename must not be empty")

        await conn.execute(
            "UPDATE documents SET filename=$1 WHERE id=$2", new_name, doc_id)
        return {"id": str(row["id"]), "filename": new_name,
                "old_filename": row["filename"]}
    finally:
        await conn.close()


# ── POST /documents/batch/delete — 批量删除（P1 #27） ─────────────

class BatchDeleteRequest(BaseModel):
    items: List[dict]  # [{"doc_id": "...", "kb_id": "..."}]

class BatchDeleteResponse(BaseModel):
    results: List[dict]  # [{"doc_id": "...", "kb_id": "...", "status": "deleted|failed", "error": "..."}]

@router.post("/documents/batch/delete", response_model=BatchDeleteResponse)
async def batch_delete_documents(body: BatchDeleteRequest,
                                 ctx: RequestContext = Depends(get_request_context)):
    """批量删除文档——逐资源独立权限校验、独立执行、独立审计。

    设计依据 §13.4.4：某文档权限不足则该文档单独失败，不影响其余。
    """
    from src.permission.authz import check
    from src.doc.service import delete_document_from_kb

    results = []
    for item in body.items:
        doc_id = item.get("doc_id", "")
        kb_id = item.get("kb_id", "")
        try:
            # 逐资源独立权限校验（经 P-AUTHC 门面）
            decision = check(ctx, "doc:unmount", "document", doc_id,
                           channel_kb=kb_id)
            if decision.get("decision") != "allow":
                results.append({"doc_id": doc_id, "kb_id": kb_id,
                              "status": "failed", "error": "auth:forbidden"})
                continue

            # 独立执行删除
            delete_document_from_kb(
                user_id=ctx.user_id, doc_id=doc_id, kb_id=kb_id,
                tenant_id=ctx.tenant_id, purge=False,
                request_id=ctx.request_id, credential=ctx.credential,
            )
            results.append({"doc_id": doc_id, "kb_id": kb_id, "status": "deleted"})
        except Exception as exc:
            results.append({"doc_id": doc_id, "kb_id": kb_id,
                          "status": "failed", "error": str(exc)[:200]})

    return BatchDeleteResponse(results=results)


# ── POST /documents/batch/parse — 批量解析（P1 #28） ──────────────

class BatchParseRequest(BaseModel):
    mount_ids: List[str]  # list of mount_id to trigger parsing

class BatchParseResponse(BaseModel):
    results: List[dict]

@router.post("/documents/batch/parse", response_model=BatchParseResponse)
async def batch_trigger_parse(body: BatchParseRequest,
                              ctx: RequestContext = Depends(get_request_context)):
    """批量触发文档解析——逐 mount 独立权限校验、独立执行。

    设计依据 §13.4.2：按需而非自动；§13.4.4：某文档失败不影响其余。
    """
    from src.permission.authz import check

    results = []
    for mount_id in body.mount_ids:
        try:
            # 从 mount 反查 kb_id（权限校验需要 kb_id）
            conn = await asyncpg.connect(_dsn())
            try:
                mount_row = await conn.fetchrow(
                    "SELECT document_id, kb_id FROM document_kb_mounts WHERE id=$1",
                    mount_id)
            finally:
                await conn.close()

            if not mount_row:
                results.append({"mount_id": mount_id, "status": "failed",
                              "error": "doc:not_found"})
                continue

            doc_id = str(mount_row["document_id"])
            kb_id = str(mount_row["kb_id"])

            # 独立权限校验
            decision = check(ctx, "kb:write", "kb", kb_id)
            if decision.get("decision") != "allow":
                results.append({"mount_id": mount_id, "status": "failed",
                              "error": "auth:forbidden"})
                continue

            # 独立触发解析
            from src.doc.service import trigger_parse
            trigger_parse(mount_id=mount_id, kb_id=kb_id,
                         tenant_id=ctx.tenant_id)
            results.append({"mount_id": mount_id, "status": "queued",
                          "document_id": doc_id})
        except Exception as exc:
            results.append({"mount_id": mount_id, "status": "failed",
                          "error": str(exc)[:200]})

    return BatchParseResponse(results=results)


# ── helpers ──

def _kb_to_dict(row) -> KBResponse:
    return KBResponse(
        id=str(row["id"]), tenant_id=row["tenant_id"],
        name=row["name"], description=row["description"] or "",
        owner_id=row["owner_id"], status=row["status"],
        created_at=row["created_at"].isoformat(),
    )


def _doc_to_dict(row) -> DocResponse:
    return DocResponse(
        document_id=str(row["doc_id"]), mount_id=str(row["mount_id"]),
        filename=row["filename"], file_size=row["file_size"] or 0,
        mime_type=row["mime_type"] or "", parse_status=row["parse_status"] or "not_parsed",
        is_enabled=row["is_enabled"] if row["is_enabled"] is not None else True,
        uploaded_by=row["uploaded_by"] or "",
        mounted_at=row["mounted_at"].isoformat() if row["mounted_at"] else "",
        created_at=row["created_at"].isoformat() if row["created_at"] else "",
    )

"""Phase 4 目录 + 批量操作 + 文档重命名 REST 端点。"""

import uuid
from datetime import datetime, timezone
from typing import List, Optional

import asyncpg
from fastapi import APIRouter, HTTPException, Depends, Body
from pydantic import BaseModel

from src.config import Settings
from src.api.deps import get_request_context
from src.permission.context import RequestContext

router = APIRouter(prefix="/api/v1", tags=["directories"])

def _dsn(): return Settings().database_url.replace("postgresql+asyncpg://", "postgresql://")


# ── Models ──

class DirResponse(BaseModel):
    id: str; name: str; directory_type: str; bound_kb_id: Optional[str] = None
    parent_id: Optional[str] = None; doc_count: int = 0

class DirCreate(BaseModel):
    name: str; parent_id: Optional[str] = None; kb_id: str

class DirUpdate(BaseModel):
    name: Optional[str] = None

class DocMoveRequest(BaseModel):
    document_id: str

class DocRenameRequest(BaseModel):
    filename: str

class BatchParseRequest(BaseModel):
    mount_ids: List[str]


# ── 目录树 ──

@router.get("/knowledge-bases/{kb_id}/directories", response_model=List[DirResponse])
async def list_directories(kb_id: str, ctx: RequestContext = Depends(get_request_context)):
    conn = await asyncpg.connect(_dsn())
    try:
        rows = await conn.fetch("""
            SELECT d.id, d.name, d.directory_type, d.bound_kb_id, d.parent_id,
                   coalesce((SELECT count(*) FROM document_directory_entry WHERE directory_id=d.id),0) as doc_count
            FROM directories d
            WHERE (d.bound_kb_id=$1 OR d.id IN (
                SELECT directory_id FROM document_directory_entry e
                JOIN document_kb_mounts m ON m.document_id=e.document_id WHERE m.kb_id=$1
            ))
            ORDER BY d.directory_type DESC, d.name ASC
        """, kb_id)
        return [_dir_to_dict(r) for r in rows]
    finally:
        await conn.close()


@router.post("/directories", response_model=DirResponse, status_code=201)
async def create_directory(body: DirCreate, ctx: RequestContext = Depends(get_request_context)):
    dir_id = str(uuid.uuid4())
    conn = await asyncpg.connect(_dsn())
    try:
        await conn.execute(
            "INSERT INTO directories (id, tenant_id, name, parent_id, directory_type, created_by) "
            "VALUES ($1,$2,$3,$4,'manual',$5)",
            dir_id, ctx.tenant_id, body.name, body.parent_id, ctx.user_id)
        return DirResponse(id=dir_id, name=body.name, directory_type="manual",
                          parent_id=body.parent_id, doc_count=0)
    finally:
        await conn.close()


@router.patch("/directories/{dir_id}", response_model=DirResponse)
async def rename_directory(dir_id: str, body: DirUpdate, ctx: RequestContext = Depends(get_request_context)):
    conn = await asyncpg.connect(_dsn())
    try:
        r = await conn.fetchrow("SELECT * FROM directories WHERE id=$1", dir_id)
        if not r: raise HTTPException(404, "doc:not_found")
        if r["directory_type"] != "manual": raise HTTPException(400, "Cannot rename kb_bound directory")
        new_name = body.name or r["name"]
        await conn.execute("UPDATE directories SET name=$1 WHERE id=$2", new_name, dir_id)
        return DirResponse(id=dir_id, name=new_name, directory_type=r["directory_type"],
                          bound_kb_id=str(r["bound_kb_id"]) if r["bound_kb_id"] else None,
                          parent_id=str(r["parent_id"]) if r["parent_id"] else None)
    finally:
        await conn.close()


@router.delete("/directories/{dir_id}")
async def delete_directory(dir_id: str, ctx: RequestContext = Depends(get_request_context)):
    conn = await asyncpg.connect(_dsn())
    try:
        r = await conn.fetchrow("SELECT * FROM directories WHERE id=$1", dir_id)
        if not r: raise HTTPException(404, "doc:not_found")
        if r["directory_type"] == "kb_bound": raise HTTPException(400, "Cannot delete kb_bound directory")
        # Check empty
        n = await conn.fetchval("SELECT count(*) FROM document_directory_entry WHERE directory_id=$1", dir_id)
        if n > 0: raise HTTPException(409, f"Directory has {n} documents — move or remove them first")
        await conn.execute("DELETE FROM directories WHERE id=$1", dir_id)
        return {"status": "deleted"}
    finally:
        await conn.close()


# ── 文档移入/移出目录 ──

@router.post("/directories/{dir_id}/documents")
async def add_doc_to_dir(dir_id: str, body: DocMoveRequest, ctx: RequestContext = Depends(get_request_context)):
    conn = await asyncpg.connect(_dsn())
    try:
        await conn.execute(
            "INSERT INTO document_directory_entry (document_id, directory_id) VALUES ($1,$2) ON CONFLICT DO NOTHING",
            body.document_id, dir_id)
        return {"status": "added"}
    finally:
        await conn.close()


@router.delete("/directories/{dir_id}/documents/{doc_id}")
async def remove_doc_from_dir(dir_id: str, doc_id: str, ctx: RequestContext = Depends(get_request_context)):
    conn = await asyncpg.connect(_dsn())
    try:
        await conn.execute("DELETE FROM document_directory_entry WHERE document_id=$1 AND directory_id=$2", doc_id, dir_id)
        return {"status": "removed"}
    finally:
        await conn.close()


# ── 文档重命名 ──

@router.patch("/documents/{doc_id}")
async def rename_document(doc_id: str, body: DocRenameRequest, ctx: RequestContext = Depends(get_request_context)):
    conn = await asyncpg.connect(_dsn())
    try:
        await conn.execute("UPDATE documents SET filename=$1 WHERE id=$2", body.filename, doc_id)
        return {"status": "renamed", "filename": body.filename}
    finally:
        await conn.close()


# ── 批量删除 ──
# 已移除：此端点与 kb_routes.py:batch_delete_documents 重复，且缺少权限检查。
# 批量删除统一使用 kb_routes.py 的 POST /documents/batch/delete（含逐资源 doc:unmount 校验）。

# ── 批量解析 ──

@router.post("/documents/batch/parse")
async def batch_parse(body: BatchParseRequest, ctx: RequestContext = Depends(get_request_context)):
    """批量触发解析——通过 outbox 发布 DocumentMounted 事件。

    outbox_relay 负责取事件并分发 ingest_document_task 到 Celery worker。
    API 进程不直接调 Celery delay()——遵循单一分发路径原则。
    """
    import json as _json
    from src.doc.events import document_mounted_event

    results = []
    for mid in body.mount_ids:
        try:
            c = await asyncpg.connect(_dsn())
            try:
                r = await c.fetchrow("SELECT document_id, kb_id FROM document_kb_mounts WHERE id=$1", mid)
                if r:
                    doc_id, kb_id = str(r["document_id"]), str(r["kb_id"])

                    # 更新 ingest_execution 状态为 queued（worker 接手后才设 processing）
                    await c.execute(
                        "INSERT INTO ingest_executions (mount_id, document_id, kb_id, parse_status, execution_epoch, updated_at) "
                        "VALUES ($1,$2,$3,'queued',1,$4) "
                        "ON CONFLICT (mount_id) DO UPDATE SET parse_status='queued', execution_epoch=ingest_executions.execution_epoch+1, updated_at=$4",
                        mid, doc_id, kb_id, datetime.now(timezone.utc))

                    # 发布 DocumentMounted 到 outbox（outbox_relay 将分发）
                    event = document_mounted_event(
                        document_id=doc_id, mount_id=mid, kb_id=kb_id,
                        tenant_id=ctx.tenant_id,
                    )
                    outbox_payload = event.to_outbox_dict()
                    await c.execute(
                        "INSERT INTO outbox (id, event_type, payload, tenant_id, trace_id, status, created_at) "
                        "VALUES ($1,$2,$3,$4,$5,$6,$7)",
                        str(uuid.uuid4()), outbox_payload["event_type"],
                        _json.dumps(event.payload, default=str),
                        outbox_payload["tenant_id"], outbox_payload["trace_id"],
                        outbox_payload["status"], datetime.now(timezone.utc),
                    )

                    results.append({"mount_id": mid, "status": "queued"})
                else:
                    results.append({"mount_id": mid, "status": "not_found"})
            finally:
                await c.close()
        except Exception as e:
            results.append({"mount_id": mid, "status": "failed", "error": str(e)[:200]})
    return {"results": results}


def _dir_to_dict(r) -> DirResponse:
    return DirResponse(
        id=str(r["id"]), name=r["name"], directory_type=r["directory_type"],
        bound_kb_id=str(r["bound_kb_id"]) if r.get("bound_kb_id") else None,
        parent_id=str(r["parent_id"]) if r.get("parent_id") else None,
        doc_count=r.get("doc_count", 0) or 0,
    )

"""Phase 3 对话 REST 端点。"""

import uuid
from datetime import datetime, timezone
from typing import List, Optional

import asyncpg
from fastapi import APIRouter, HTTPException, Depends, Query
from pydantic import BaseModel

from src.config import Settings
from src.api.deps import get_request_context
from src.permission.context import RequestContext

router = APIRouter(prefix="/api/v1", tags=["chat"])


def _dsn():
    return Settings().database_url.replace("postgresql+asyncpg://", "postgresql://")


# ── Models ──

class ConversationCreate(BaseModel):
    kb_ids: List[str] = []

class ConversationUpdate(BaseModel):
    title: Optional[str] = None

class ConversationResponse(BaseModel):
    id: str
    tenant_id: str
    user_id: str
    bound_kb_ids: List[str]
    title: str = ""
    turn_count: int = 0
    created_at: str


# ── List ──

@router.get("/conversations", response_model=List[ConversationResponse])
async def list_conversations(ctx: RequestContext = Depends(get_request_context)):
    conn = await asyncpg.connect(_dsn())
    try:
        rows = await conn.fetch("""
            SELECT c.id, c.tenant_id, c.user_id, c.bound_kb_ids, c.created_at,
                   coalesce((SELECT max(turn_index) FROM conversation_turns WHERE conversation_id=c.id), 0) as turn_count,
                   coalesce((SELECT user_question FROM conversation_turns WHERE conversation_id=c.id ORDER BY turn_index ASC LIMIT 1), '新会话') as title
            FROM conversations c
            WHERE c.tenant_id=$1 AND c.user_id=$2
            ORDER BY c.created_at DESC
            LIMIT 50
        """, ctx.tenant_id, ctx.user_id)

        return [_conv_to_dict(r) for r in rows]
    finally:
        await conn.close()


# ── Create ──

@router.post("/conversations", response_model=ConversationResponse, status_code=201)
async def create_conversation(body: ConversationCreate, ctx: RequestContext = Depends(get_request_context)):
    conv_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    conn = await asyncpg.connect(_dsn())
    try:
        await conn.execute(
            "INSERT INTO conversations (id, tenant_id, user_id, bound_kb_ids, created_at) "
            "VALUES ($1,$2,$3,$4,$5)",
            conv_id, ctx.tenant_id, ctx.user_id, body.kb_ids, now,
        )
        return ConversationResponse(
            id=conv_id, tenant_id=ctx.tenant_id, user_id=ctx.user_id,
            bound_kb_ids=body.kb_ids, title="新会话", turn_count=0,
            created_at=now.isoformat(),
        )
    finally:
        await conn.close()


# ── Delete ──

@router.delete("/conversations/{conv_id}")
async def delete_conversation(conv_id: str, ctx: RequestContext = Depends(get_request_context)):
    conn = await asyncpg.connect(_dsn())
    try:
        row = await conn.fetchrow("SELECT * FROM conversations WHERE id=$1", conv_id)
        if not row:
            raise HTTPException(404, "doc:not_found")
        await conn.execute("DELETE FROM conversation_turns WHERE conversation_id=$1", conv_id)
        await conn.execute("DELETE FROM conversations WHERE id=$1", conv_id)
        return {"status": "deleted", "conversation_id": conv_id}
    finally:
        await conn.close()


# ── Rename ──

@router.patch("/conversations/{conv_id}", response_model=ConversationResponse)
async def update_conversation(conv_id: str, body: ConversationUpdate, ctx: RequestContext = Depends(get_request_context)):
    """重命名会话（title 字段）。"""
    conn = await asyncpg.connect(_dsn())
    try:
        row = await conn.fetchrow("SELECT * FROM conversations WHERE id=$1", conv_id)
        if not row:
            raise HTTPException(404, "doc:not_found")

        # Store the title in a separate column or use a metadata column
        # For now, we store it by updating the conversation table's title-like field
        if body.title is not None:
            await conn.execute(
                "UPDATE conversations SET title=$1 WHERE id=$2",
                body.title, conv_id,
            )

        # Fetch updated row
        updated = await conn.fetchrow("SELECT * FROM conversations WHERE id=$1", conv_id)
        # Get turn count and title the same way list does
        turn_count = await conn.fetchval(
            "SELECT coalesce(max(turn_index), 0) FROM conversation_turns WHERE conversation_id=$1", conv_id
        )
        return ConversationResponse(
            id=str(updated["id"]), tenant_id=updated["tenant_id"], user_id=updated["user_id"],
            bound_kb_ids=updated["bound_kb_ids"] or [],
            title=body.title or "新会话",
            turn_count=turn_count or 0,
            created_at=updated["created_at"].isoformat() if updated["created_at"] else "",
        )
    finally:
        await conn.close()


# ── Get turns ──

class TurnResponse(BaseModel):
    id: str
    turn_index: int
    user_question: str
    resolved_query: str
    answer: str = ""
    chunk_ids: List[str] = []
    created_at: str


@router.get("/conversations/{conv_id}/turns", response_model=List[TurnResponse])
async def list_turns(conv_id: str, ctx: RequestContext = Depends(get_request_context)):
    conn = await asyncpg.connect(_dsn())
    try:
        rows = await conn.fetch(
            "SELECT id, turn_index, user_question, resolved_query, answer, retrieved_chunk_ids, created_at "
            "FROM conversation_turns WHERE conversation_id=$1 ORDER BY turn_index ASC",
            conv_id,
        )
        return [
            TurnResponse(
                id=str(r["id"]), turn_index=r["turn_index"],
                user_question=r["user_question"], resolved_query=r["resolved_query"],
                answer=r["answer"] or "", chunk_ids=r["retrieved_chunk_ids"] or [],
                created_at=r["created_at"].isoformat() if r["created_at"] else "",
            ) for r in rows
        ]
    finally:
        await conn.close()


# ── helper ──

def _conv_to_dict(row) -> ConversationResponse:
    kb_ids = row["bound_kb_ids"] or []
    return ConversationResponse(
        id=str(row["id"]), tenant_id=row["tenant_id"], user_id=row["user_id"],
        bound_kb_ids=[str(x) for x in kb_ids],
        title=row["title"] or "新会话",
        turn_count=row["turn_count"] or 0,
        created_at=row["created_at"].isoformat() if row["created_at"] else "",
    )

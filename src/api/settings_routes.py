"""Phase 5 设置 REST 端点 — 模型 + 检索配置 + Prompt 模板。"""

import uuid
from typing import List, Optional
import asyncpg
from fastapi import APIRouter, HTTPException, Depends, Body
from pydantic import BaseModel
from src.config import Settings
from src.api.deps import get_request_context
from src.permission.context import RequestContext

router = APIRouter(prefix="/api/v1", tags=["settings"])


def _dsn(): return Settings().database_url.replace("postgresql+asyncpg://", "postgresql://")


# ── Models ──

class ModelItem(BaseModel):
    model_id: str; model_type: str; provider: str; model_name: str; base_url: str; is_default: bool

class RetrievalConfigModel(BaseModel):
    scope_type: str = "kb"; top_k: int = 10
    retrieval_mode: str = "hybrid"; fusion_method: str = "rrf"
    synthesis_mode: str = "compact"; rerank_model_id: str = ""
    strict: bool = False
    oversample_factor: float = 1.5; min_results: int = 3
    refetch_max_rounds: int = 2; haystack_pipeline_name: str = "query_v1"
    dense_weight: float = 0.5; sparse_weight: float = 0.5

class RetrievalConfigPatch(BaseModel):
    top_k: Optional[int] = None; retrieval_mode: Optional[str] = None
    fusion_method: Optional[str] = None; synthesis_mode: Optional[str] = None
    rerank_model_id: Optional[str] = None
    strict: Optional[bool] = None; oversample_factor: Optional[float] = None
    min_results: Optional[int] = None; refetch_max_rounds: Optional[int] = None
    haystack_pipeline_name: Optional[str] = None
    dense_weight: Optional[float] = None; sparse_weight: Optional[float] = None

class PromptItem(BaseModel):
    id: str; prompt_id: str; version: str; template_text: str; description: str; is_active: bool


# ── 模型列表 ──

@router.get("/models", response_model=List[ModelItem])
async def list_models(ctx: RequestContext = Depends(get_request_context)):
    conn = await asyncpg.connect(_dsn())
    try:
        rows = await conn.fetch("SELECT * FROM model_registry ORDER BY model_type, model_id")
        return [ModelItem(model_id=r["model_id"], model_type=r["model_type"], provider=r["provider"],
                         model_name=r["model_name"], base_url=r["base_url"] or "", is_default=r["is_default"]) for r in rows]
    finally:
        await conn.close()


@router.patch("/models/{model_id}/set-default", response_model=ModelItem)
async def set_default_model(model_id: str, ctx: RequestContext = Depends(get_request_context)):
    """将指定模型设为该类型的默认模型。需要 kb:manage 权限（系统管理员/租户管理员）。"""
    from src.permission.authz import check

    # ★ 权限检查：修改模型默认配置是系统级操作，需要 kb:manage 权限
    # 使用 system-wide resource "config" 进行判定
    decision = check(ctx, "kb:manage", "kb", "config")
    if decision.get("decision") != "allow":
        raise HTTPException(status_code=403, detail="auth:forbidden — 您没有修改模型配置的权限（需要 kb:manage）")

    conn = await asyncpg.connect(_dsn())
    try:
        # Get the target model
        target = await conn.fetchrow("SELECT * FROM model_registry WHERE model_id=$1", model_id)
        if not target:
            raise HTTPException(404, detail="model not found")

        model_type = target["model_type"]

        # Deactivate all models of the same type
        await conn.execute(
            "UPDATE model_registry SET is_default=false WHERE model_type=$1", model_type)

        # Activate the target
        await conn.execute(
            "UPDATE model_registry SET is_default=true WHERE model_id=$1", model_id)

        # Return updated model
        updated = await conn.fetchrow("SELECT * FROM model_registry WHERE model_id=$1", model_id)
        return ModelItem(
            model_id=updated["model_id"], model_type=updated["model_type"],
            provider=updated["provider"], model_name=updated["model_name"],
            base_url=updated["base_url"] or "", is_default=updated["is_default"])
    finally:
        await conn.close()


# ── 检索配置 CRUD ──

@router.get("/configs/retrieval", response_model=RetrievalConfigModel)
async def get_retrieval_config(kb_id: str, ctx: RequestContext = Depends(get_request_context)):
    from src.platform.config.service import resolve_retrieval_config
    rc = resolve_retrieval_config(kb_id=kb_id, tenant_id=ctx.tenant_id)
    return RetrievalConfigModel(top_k=rc.top_k, retrieval_mode=rc.retrieval_mode,
        fusion_method=rc.fusion_method, synthesis_mode=rc.synthesis_mode,
        rerank_model_id=rc.rerank_model_id,
        strict=rc.strict, oversample_factor=rc.oversample_factor,
        min_results=rc.min_results, refetch_max_rounds=rc.refetch_max_rounds,
        haystack_pipeline_name=rc.haystack_pipeline_name,
        dense_weight=rc.dense_weight,
        sparse_weight=rc.sparse_weight)


@router.patch("/configs/retrieval", response_model=RetrievalConfigModel)
async def update_retrieval_config(kb_id: str, body: RetrievalConfigPatch,
                                  ctx: RequestContext = Depends(get_request_context)):
    """更新检索配置。需要 kb:manage 权限。"""
    from src.permission.authz import check

    # ★ 权限检查：修改检索配置需要对该 KB 的 kb:manage 权限
    decision = check(ctx, "kb:manage", "kb", kb_id)
    if decision.get("decision") != "allow":
        raise HTTPException(status_code=403, detail="auth:forbidden — 您没有修改此知识库检索配置的权限（需要 kb:manage）")

    conn = await asyncpg.connect(_dsn())
    try:
        existing = await conn.fetchrow(
            "SELECT * FROM retrieval_configs WHERE scope_type='kb' AND scope_id=$1", kb_id)
        if existing:
            # Update fields
            updates = []; vals = []; i = 1
            for f in ["top_k","retrieval_mode","fusion_method","synthesis_mode",
                      "rerank_model_id","strict","oversample_factor","min_results",
                      "refetch_max_rounds","haystack_pipeline_name",
                      "dense_weight","sparse_weight"]:
                v = getattr(body, f, None)
                if v is not None:
                    updates.append(f"{f}=${i}"); vals.append(v); i += 1
            if updates:
                vals.append(kb_id)
                await conn.execute(f"UPDATE retrieval_configs SET {', '.join(updates)} WHERE scope_type='kb' AND scope_id=${i}", *vals)
        else:
            await conn.execute(
                "INSERT INTO retrieval_configs (id, scope_type, scope_id, top_k, retrieval_mode, "
                "fusion_method, synthesis_mode, rerank_model_id, "
                "strict, oversample_factor, min_results, refetch_max_rounds, haystack_pipeline_name) "
                "VALUES ($1,'kb',$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)",
                str(uuid.uuid4()), kb_id, body.top_k or 10, body.retrieval_mode or "hybrid",
                body.fusion_method or "rrf", body.synthesis_mode or "compact",
                body.rerank_model_id or "",
                body.strict or False, body.oversample_factor or 1.5, body.min_results or 3,
                body.refetch_max_rounds or 2, body.haystack_pipeline_name or "query_v1")
        # Return resolved
        from src.platform.config.service import resolve_retrieval_config
        rc = resolve_retrieval_config(kb_id=kb_id, tenant_id=ctx.tenant_id)
        return RetrievalConfigModel(top_k=rc.top_k, retrieval_mode=rc.retrieval_mode,
            fusion_method=rc.fusion_method, synthesis_mode=rc.synthesis_mode,
            rerank_model_id=rc.rerank_model_id,
            strict=rc.strict, oversample_factor=rc.oversample_factor,
            min_results=rc.min_results, refetch_max_rounds=rc.refetch_max_rounds,
            haystack_pipeline_name=rc.haystack_pipeline_name)
    finally:
        await conn.close()


# ── Prompt 模板列表 ──

@router.get("/prompts", response_model=List[PromptItem])
async def list_prompts(ctx: RequestContext = Depends(get_request_context)):
    conn = await asyncpg.connect(_dsn())
    try:
        rows = await conn.fetch("SELECT * FROM prompt_templates ORDER BY prompt_id, version")
        return [PromptItem(id=str(r["id"]), prompt_id=r["prompt_id"], version=r["version"],
                          template_text=r["template_text"], description=r["description"] or "",
                          is_active=r["is_active"]) for r in rows]
    finally:
        await conn.close()


@router.get("/config")
async def app_config(ctx: RequestContext = Depends(get_request_context)):
    """返回前端需要的动态配置（外部服务地址等）。

    外部工具 URL（Grafana, Langfuse, Cerbos, Admin Console）仅对管理员用户返回。
    普通用户仅获取 OTel collector URL（用于前端链路追踪）。
    """
    s = Settings()
    is_admin = bool({"system_admin", "admin"} & set(ctx.roles))
    return {
        "grafana_url": s.grafana_url if is_admin else "",
        "langfuse_url": s.langfuse_public_url if is_admin else "",
        "cerbos_url": s.cerbos_public_url if is_admin else "",
        "admin_console_url": s.admin_console_url if is_admin else "",
        "otel_collector_url": s.otel_endpoint,
    }


@router.patch("/prompts/{prompt_id}/activate")
async def activate_prompt(prompt_id: str, ctx: RequestContext = Depends(get_request_context)):
    """激活指定版本的 Prompt 模板。需要 kb:manage 权限。"""
    from src.permission.authz import check

    # ★ 权限检查：修改 Prompt 激活状态需要 kb:manage 权限
    decision = check(ctx, "kb:manage", "kb", "config")
    if decision.get("decision") != "allow":
        raise HTTPException(status_code=403, detail="auth:forbidden — 您没有修改 Prompt 配置的权限（需要 kb:manage）")

    conn = await asyncpg.connect(_dsn())
    try:
        # Deactivate all versions of this prompt
        await conn.execute("UPDATE prompt_templates SET is_active=false WHERE id=$1", prompt_id)
        # Activate
        await conn.execute("UPDATE prompt_templates SET is_active=true WHERE id=$1", prompt_id)
        return {"status": "activated", "prompt_id": prompt_id}
    finally:
        await conn.close()

"""设置 REST 端点 — 模型 + 检索配置 + Prompt 模板。"""

import uuid
from typing import List, Literal, Optional
import asyncpg
from fastapi import APIRouter, HTTPException, Depends, Body
from pydantic import BaseModel, Field
from src.config import Settings
from src.api.deps import get_request_context
from src.permission.context import RequestContext

# 检索/合成模式合法值（与 GET /system/enums 一致；非法值 422 而非静默回退）
RETRIEVAL_MODES = Literal["hybrid", "vector_only", "keyword_only"]
FUSION_METHODS = Literal["rrf", "weighted_sum"]
SYNTHESIS_MODES = Literal["auto", "compact", "refine", "tree_summarize", "no_synthesis"]

router = APIRouter(prefix="/api/v1", tags=["settings"])


def _dsn(): return Settings().database_url.replace("postgresql+asyncpg://", "postgresql://")


# ── Models ──

class ModelItem(BaseModel):
    model_id: str; model_type: str; provider: str; model_name: str; base_url: str; is_default: bool

class ModelItemOut(ModelItem):
    """模型列表/详情响应（api_key 永不回显明文，只出掩码与生效源）。"""
    has_key: bool
    key_source: str            # db | env | none
    effective_model_name: str  # resolve 后实际生效的模型名
    effective_base_url: str    # resolve 后实际生效的 base_url
    is_local: bool             # 本地进程内直载（FlagEmbedding 加载器固定，仅登记）

class ModelCreate(BaseModel):
    model_id: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9._-]+$")
    model_type: Literal["llm", "embedding", "reranker"]
    provider: str = Field("ollama", max_length=32)
    model_name: str = Field(..., min_length=1, max_length=128)
    base_url: str = Field("", max_length=256)
    api_key: str = Field("", max_length=256)  # 留空 = 回落 env 秘钥
    is_default: bool = False

class ModelPatch(BaseModel):
    provider: Optional[str] = Field(None, max_length=32)
    model_name: Optional[str] = Field(None, max_length=128)
    base_url: Optional[str] = Field(None, max_length=256)
    is_default: Optional[bool] = None

class ApiKeyRequest(BaseModel):
    api_key: str = Field("", max_length=256)  # 空串 = 清除 DB key → 回落 env

class ModelTestResult(BaseModel):
    model_id: str
    ok: bool
    latency_ms: int
    detail: str

class RetrievalConfigModel(BaseModel):
    # 响应模型模式字段保持 str：避免 DB 历史非法值导致序列化 500；
    # 写入口（PATCH）已用 Literal 校验。
    scope_type: str = "kb"; top_k: int = 10
    retrieval_mode: str = "hybrid"; fusion_method: str = "rrf"
    synthesis_mode: str = "compact"; rerank_model_id: str = ""
    strict: bool = False
    oversample_factor: float = 1.5; min_results: int = 3
    refetch_max_rounds: int = 2; haystack_pipeline_name: str = "query_v1"
    dense_weight: float = 0.5; sparse_weight: float = 0.5
    min_score: float = 0.0
    refine_batch_size: int = 2; tree_summarize_batch_size: int = 5
    max_answer_length: int = 3000; compress_target_length: int = 1000
    doc_preview_max_chars: int = 1000

class RetrievalConfigPatch(BaseModel):
    top_k: Optional[int] = None
    retrieval_mode: Optional[RETRIEVAL_MODES] = None
    fusion_method: Optional[FUSION_METHODS] = None
    synthesis_mode: Optional[SYNTHESIS_MODES] = None
    rerank_model_id: Optional[str] = None
    strict: Optional[bool] = None; oversample_factor: Optional[float] = None
    min_results: Optional[int] = None; refetch_max_rounds: Optional[int] = None
    haystack_pipeline_name: Optional[str] = None
    dense_weight: Optional[float] = None; sparse_weight: Optional[float] = None
    min_score: Optional[float] = None
    refine_batch_size: Optional[int] = None
    tree_summarize_batch_size: Optional[int] = None
    max_answer_length: Optional[int] = None
    compress_target_length: Optional[int] = None
    doc_preview_max_chars: Optional[int] = None

class PromptItem(BaseModel):
    id: str; prompt_id: str; version: str; template_text: str; description: str; is_active: bool

class PromptItemPatch(BaseModel):
    template_text: str
    description: Optional[str] = None


# ── 模型管理 ──
#
# 权威源：model_registry 表。api_key 为空时回落对应类型 env 秘钥
# （见 src/platform/model/registry.py _env_api_key_for_type）。
# api_key 永不回显明文——响应只出 has_key + key_source(db|env|none)。

def _model_item_out(r) -> ModelItemOut:
    """由 model_registry 行组装响应：掩码秘钥 + 生效源 + 生效模型名/base_url。"""
    from src.platform.model.registry import _env_api_key_for_type
    from src.config import Settings as _S
    s = _S()
    mtype = r["model_type"] or "llm"
    db_key = (r["api_key"] or "").strip()
    env_key = _env_api_key_for_type(mtype)
    key_source = "db" if db_key else ("env" if env_key else "none")
    default_url = s.embedding_base_url if mtype == "embedding" else s.llm_base_url
    default_name = s.embedding_model if mtype == "embedding" else s.llm_model
    return ModelItemOut(
        model_id=r["model_id"], model_type=mtype,
        provider=r["provider"] or "ollama",
        model_name=r["model_name"],
        base_url=r["base_url"] or "",
        is_default=r["is_default"],
        has_key=bool(db_key),
        key_source=key_source,
        effective_model_name=r["model_name"] or default_name,
        effective_base_url=r["base_url"] or default_url,
        # 本地进程内直载：FlagEmbedding 加载器固定（BGE-M3 一次前向稠密+稀疏），
        # 模型名锁死，管理面仅登记用途（编辑/删除不适用）。
        is_local=(r["provider"] or "") in ("local", "sentence_transformers"),
    )


def _require_model_manage(ctx) -> None:
    """系统级模型配置写权限（kb:manage on config）。"""
    from src.permission.authz import check
    decision = check(ctx, "kb:manage", "kb", "config")
    if decision.get("decision") != "allow":
        raise HTTPException(status_code=403, detail="auth:forbidden — 您没有修改模型配置的权限（需要 kb:manage）")


async def _fetch_model(conn, model_id: str):
    return await conn.fetchrow("SELECT * FROM model_registry WHERE model_id=$1", model_id)


@router.get("/models", response_model=List[ModelItemOut])
async def list_models(ctx: RequestContext = Depends(get_request_context)):
    conn = await asyncpg.connect(_dsn())
    try:
        rows = await conn.fetch("SELECT * FROM model_registry ORDER BY model_type, model_id")
        return [_model_item_out(r) for r in rows]
    finally:
        await conn.close()


@router.post("/models", response_model=ModelItemOut, status_code=201)
async def create_model(body: ModelCreate, ctx: RequestContext = Depends(get_request_context)):
    """创建模型注册项（kb:manage）。api_key 留空 = 回落 env 秘钥。"""
    _require_model_manage(ctx)
    conn = await asyncpg.connect(_dsn())
    try:
        existing = await conn.fetchrow(
            "SELECT 1 FROM model_registry WHERE model_id=$1 AND model_type=$2",
            body.model_id, body.model_type)
        if existing:
            raise HTTPException(409, detail=f"model already exists: {body.model_id} ({body.model_type})")
        if body.is_default:
            await conn.execute(
                "UPDATE model_registry SET is_default=false WHERE model_type=$1", body.model_type)
        await conn.execute(
            """INSERT INTO model_registry
               (model_id, model_type, provider, model_name, base_url, api_key, is_default)
               VALUES ($1,$2,$3,$4,$5,$6,$7)""",
            body.model_id, body.model_type, body.provider, body.model_name,
            body.base_url, body.api_key, body.is_default)
        created = await _fetch_model(conn, body.model_id)
        return _model_item_out(created)
    finally:
        await conn.close()


@router.patch("/models/{model_id}", response_model=ModelItemOut)
async def update_model(model_id: str, body: ModelPatch,
                       ctx: RequestContext = Depends(get_request_context)):
    """更新模型非秘钥字段（kb:manage）。"""
    _require_model_manage(ctx)
    conn = await asyncpg.connect(_dsn())
    try:
        target = await _fetch_model(conn, model_id)
        if not target:
            raise HTTPException(404, detail="model not found")
        sets = []
        params = []
        if body.provider is not None:
            sets.append("provider=$%d" % (len(params) + 1)); params.append(body.provider)
        if body.model_name is not None:
            sets.append("model_name=$%d" % (len(params) + 1)); params.append(body.model_name)
        if body.base_url is not None:
            sets.append("base_url=$%d" % (len(params) + 1)); params.append(body.base_url)
        if body.is_default is not None:
            sets.append("is_default=$%d" % (len(params) + 1)); params.append(body.is_default)
            if body.is_default:
                await conn.execute(
                    "UPDATE model_registry SET is_default=false WHERE model_type=$1",
                    target["model_type"])
        if sets:
            params.append(model_id)
            await conn.execute(
                "UPDATE model_registry SET %s WHERE model_id=$%d" % (", ".join(sets), len(params)),
                *params)
        updated = await _fetch_model(conn, model_id)
        return _model_item_out(updated)
    finally:
        await conn.close()


@router.put("/models/{model_id}/api-key", response_model=ModelItemOut)
async def set_model_api_key(model_id: str, body: ApiKeyRequest,
                            ctx: RequestContext = Depends(get_request_context)):
    """设置/清除模型 api_key（kb:manage）。空串 = 清除 DB key → 回落 env 秘钥。"""
    _require_model_manage(ctx)
    conn = await asyncpg.connect(_dsn())
    try:
        target = await _fetch_model(conn, model_id)
        if not target:
            raise HTTPException(404, detail="model not found")
        await conn.execute(
            "UPDATE model_registry SET api_key=$1 WHERE model_id=$2", body.api_key, model_id)
        updated = await _fetch_model(conn, model_id)
        return _model_item_out(updated)
    finally:
        await conn.close()


@router.delete("/models/{model_id}", status_code=204)
async def delete_model(model_id: str, ctx: RequestContext = Depends(get_request_context)):
    """删除模型注册项（kb:manage）。默认模型与仍被 retrieval_configs 引用的模型不可删。"""
    _require_model_manage(ctx)
    conn = await asyncpg.connect(_dsn())
    try:
        target = await _fetch_model(conn, model_id)
        if not target:
            raise HTTPException(404, detail="model not found")
        if target["is_default"]:
            raise HTTPException(409, detail="cannot delete the default model; set another default first")
        used = await conn.fetchval(
            "SELECT 1 FROM retrieval_configs WHERE rerank_model_id=$1 LIMIT 1", model_id)
        if used:
            raise HTTPException(409, detail="model is referenced by retrieval_configs.rerank_model_id")
        await conn.execute("DELETE FROM model_registry WHERE model_id=$1", model_id)
    finally:
        await conn.close()


@router.post("/models/{model_id}/test", response_model=ModelTestResult)
async def test_model_connection(model_id: str, ctx: RequestContext = Depends(get_request_context)):
    """连接测试：按 model_type/provider 做最小连通性探测（kb:manage）。

    本地/无 base_url 模型返回 ok（不做网络探测）；OpenAI 兼容端点做最小调用。
    """
    _require_model_manage(ctx)
    from src.platform.model.registry import resolve_model
    cfg = resolve_model(model_id)
    if cfg is None:
        raise HTTPException(404, detail="model not found")
    import time, httpx
    t0 = time.time()
    try:
        provider = (cfg.provider or "").lower()
        if provider in ("sentence_transformers", "local", "infinity") or not cfg.base_url:
            return ModelTestResult(model_id=model_id, ok=True, latency_ms=0,
                                   detail="local model (no remote probe)")
        from openai import OpenAI
        base = cfg.base_url.rstrip("/")
        client = OpenAI(base_url=base, api_key=cfg.api_key or "no-key", timeout=10.0)
        if cfg.model_type == "embedding":
            client.embeddings.create(model=cfg.model_name, input="test")
        elif cfg.model_type == "reranker":
            r = httpx.get(f"{base}/models", timeout=10.0)
            r.raise_for_status()
        else:
            client.chat.completions.create(
                model=cfg.model_name,
                messages=[{"role": "user", "content": "hi"}],
                max_tokens=1,
            )
        return ModelTestResult(model_id=model_id, ok=True,
                               latency_ms=int((time.time() - t0) * 1000), detail="ok")
    except Exception as exc:
        return ModelTestResult(model_id=model_id, ok=False,
                               latency_ms=int((time.time() - t0) * 1000),
                               detail=str(exc)[:200])


@router.patch("/models/{model_id}/set-default", response_model=ModelItemOut)
async def set_default_model(model_id: str, ctx: RequestContext = Depends(get_request_context)):
    """将指定模型设为该类型的默认模型。需要 kb:manage 权限（系统管理员/租户管理员）。"""
    _require_model_manage(ctx)
    conn = await asyncpg.connect(_dsn())
    try:
        target = await _fetch_model(conn, model_id)
        if not target:
            raise HTTPException(404, detail="model not found")
        model_type = target["model_type"]
        await conn.execute(
            "UPDATE model_registry SET is_default=false WHERE model_type=$1", model_type)
        await conn.execute(
            "UPDATE model_registry SET is_default=true WHERE model_id=$1", model_id)
        updated = await _fetch_model(conn, model_id)
        return _model_item_out(updated)
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
        sparse_weight=rc.sparse_weight,
        min_score=rc.min_score,
        refine_batch_size=rc.refine_batch_size,
        tree_summarize_batch_size=rc.tree_summarize_batch_size,
        max_answer_length=rc.max_answer_length,
        compress_target_length=rc.compress_target_length, doc_preview_max_chars=rc.doc_preview_max_chars)


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
                      "dense_weight","sparse_weight","min_score",
                      "refine_batch_size","tree_summarize_batch_size",
                      "max_answer_length","compress_target_length",
                      "doc_preview_max_chars"]:
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
                "strict, oversample_factor, min_results, refetch_max_rounds, haystack_pipeline_name, "
                "tree_summarize_batch_size) "
                "VALUES ($1,'kb',$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)",
                str(uuid.uuid4()), kb_id, body.top_k or 10, body.retrieval_mode or "hybrid",
                body.fusion_method or "rrf", body.synthesis_mode or "compact",
                body.rerank_model_id or "",
                body.strict or False, body.oversample_factor or 1.5, body.min_results or 3,
                body.refetch_max_rounds or 2, body.haystack_pipeline_name or "query_v1",
                body.tree_summarize_batch_size or 5)
        # Return resolved
        from src.platform.config.service import resolve_retrieval_config
        rc = resolve_retrieval_config(kb_id=kb_id, tenant_id=ctx.tenant_id)
        return RetrievalConfigModel(top_k=rc.top_k, retrieval_mode=rc.retrieval_mode,
            fusion_method=rc.fusion_method, synthesis_mode=rc.synthesis_mode,
            rerank_model_id=rc.rerank_model_id,
            strict=rc.strict, oversample_factor=rc.oversample_factor,
            min_results=rc.min_results, refetch_max_rounds=rc.refetch_max_rounds,
            haystack_pipeline_name=rc.haystack_pipeline_name,
            dense_weight=rc.dense_weight, sparse_weight=rc.sparse_weight,
            min_score=rc.min_score,
            refine_batch_size=rc.refine_batch_size, max_answer_length=rc.max_answer_length,
            compress_target_length=rc.compress_target_length, doc_preview_max_chars=rc.doc_preview_max_chars)
    finally:
        await conn.close()


# ── 系统枚举 ──

@router.get("/system/enums")
async def get_system_enums():
    """返回前端需要的所有枚举值列表。前后端枚举的单一权威来源。"""
    from src.ingest.service import VALID_CHUNKING_STRATEGIES
    return {
        "chunking_strategies": VALID_CHUNKING_STRATEGIES,
        "synthesis_modes": ["auto", "compact", "refine", "tree_summarize", "no_synthesis"],
        "retrieval_modes": ["hybrid", "vector_only", "keyword_only"],
        "fusion_methods": ["rrf", "weighted_sum"],
    }


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
    """激活指定版本的 Prompt 模板。需要 kb:manage 权限。

    激活语义：同一 prompt_id 下仅目标版本为 is_active=true，其余版本置 false。
    """
    from src.permission.authz import check

    # ★ 权限检查：修改 Prompt 激活状态需要 kb:manage 权限
    decision = check(ctx, "kb:manage", "kb", "config")
    if decision.get("decision") != "allow":
        raise HTTPException(status_code=403, detail="auth:forbidden — 您没有修改 Prompt 配置的权限（需要 kb:manage）")

    conn = await asyncpg.connect(_dsn())
    try:
        # 先取目标行的 prompt_id，将同 prompt_id 其它版本全部置 false
        row = await conn.fetchrow("SELECT prompt_id FROM prompt_templates WHERE id=$1", prompt_id)
        if not row:
            raise HTTPException(status_code=404, detail="prompt:not_found")
        await conn.execute(
            "UPDATE prompt_templates SET is_active=false WHERE prompt_id=$1 AND id<>$2",
            row["prompt_id"], prompt_id)
        # 再激活目标版本
        await conn.execute("UPDATE prompt_templates SET is_active=true WHERE id=$1", prompt_id)
        return {"status": "activated", "prompt_id": prompt_id}
    finally:
        await conn.close()


@router.patch("/prompts/{prompt_id}", response_model=PromptItem)
async def update_prompt(prompt_id: str, body: PromptItemPatch,
                        ctx: RequestContext = Depends(get_request_context)):
    """更新指定版本 Prompt 模板的 template_text / description。需要 kb:manage 权限。

    编辑后立即对生成生效——合成逻辑经 resolve_prompt 读取该版本的模板文本。
    """
    from src.permission.authz import check

    # ★ 权限检查：修改 Prompt 模板内容需要 kb:manage 权限（与 activate 一致）
    decision = check(ctx, "kb:manage", "kb", "config")
    if decision.get("decision") != "allow":
        raise HTTPException(status_code=403, detail="auth:forbidden — 您没有修改 Prompt 配置的权限（需要 kb:manage）")

    conn = await asyncpg.connect(_dsn())
    try:
        row = await conn.fetchrow(
            "UPDATE prompt_templates SET template_text=$1, "
            "description=COALESCE($2, description) "
            "WHERE id=$3 RETURNING id, prompt_id, version, template_text, description, is_active",
            body.template_text, body.description, prompt_id)
        if not row:
            raise HTTPException(status_code=404, detail="prompt:not_found")
        return PromptItem(id=str(row["id"]), prompt_id=row["prompt_id"],
                          version=row["version"], template_text=row["template_text"],
                          description=row["description"] or "",
                          is_active=row["is_active"])
    finally:
        await conn.close()

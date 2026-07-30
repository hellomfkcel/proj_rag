"""P-MODEL：模型与 Prompt 注册模块。

封装 Generator/Embedder/Ranker，调用方不感知底层 provider。
所有 URL/模型名/API Key 来自 .env → Settings → DB model_registry。

支持的 provider（自动检测）：
  - ollama:         本地 Ollama，endpoint 用 /api/embed
  - vllm:           企业部署 vLLM，OpenAI 兼容 /v1/chat + /v1/embeddings
  - deepseek:       DeepSeek API，OpenAI 兼容
  - openai:         OpenAI API
  - openai_compat:  任何 OpenAI 兼容服务

切换模型只需改 .env 或 model_registry 表 — 零代码改动。
"""

import asyncio
import asyncpg
import time as _time
from typing import Any, Dict, List, Optional
from dataclasses import dataclass

from src.config import Settings
from src.platform.obs.logger import get_logger

log = get_logger(__name__)


@dataclass
class ModelConfig:
    model_name: str
    base_url: str = ""
    api_key: str = ""
    provider: str = "ollama"
    model_type: str = "llm"


# ── DB helpers ──────────────────────────────────────────────────

def _get_db_dsn() -> str:
    return Settings().database_url.replace("postgresql+asyncpg://", "postgresql://")


def _run_async(coro):
    try:
        return asyncio.run(coro)
    except RuntimeError:
        import nest_asyncio
        nest_asyncio.apply()
        return asyncio.run(coro)


# ── resolve_model ───────────────────────────────────────────────

def resolve_model(model_id: str) -> ModelConfig:
    """解析模型配置。

    优先级：DB model_registry 表 → Settings 环境变量。
    """
    s = Settings()

    async def _query():
        conn = await asyncpg.connect(_get_db_dsn())
        try:
            row = await conn.fetchrow(
                "SELECT model_name, base_url, api_key, provider, model_type "
                "FROM model_registry WHERE model_id = $1 AND is_default = true "
                "LIMIT 1", model_id)
            if not row:
                row = await conn.fetchrow(
                    "SELECT model_name, base_url, api_key, provider, model_type "
                    "FROM model_registry WHERE model_id = $1 LIMIT 1", model_id)
            if row:
                mtype = row["model_type"] or "llm"
                # embedding 类型默认用 embedding_base_url，llm 类型默认用 llm_base_url
                default_url = s.embedding_base_url if mtype == "embedding" else s.llm_base_url
                return ModelConfig(
                    model_name=row["model_name"],
                    base_url=row["base_url"] or default_url,
                    api_key=row["api_key"] or s.llm_api_key,
                    provider=row["provider"] or "ollama",
                    model_type=mtype,
                )
        finally:
            await conn.close()
        return None

    db_result = _run_async(_query())
    if db_result:
        return db_result

    # fallback: Settings 环境变量
    if model_id == "qwen3-embed":
        return ModelConfig(s.embedding_model, s.embedding_base_url, s.llm_api_key, "ollama", "embedding")
    return ModelConfig(s.llm_model, s.llm_base_url, s.llm_api_key, "ollama", "llm")


# ── invoke_embedding（provider-aware）────────────────────────────

def invoke_embedding(texts: List[str], mode: str = "document") -> List[List[float]]:
    """文本向量化（自动判定 Ollama /api/embed 或 OpenAI兼容 /v1/embeddings）。

    自动产生 OTel span 上报到 Tempo + Langfuse trace 上报。
    """
    import requests, time as _time

    s = Settings()
    cfg = resolve_model("qwen3-embed")
    base = (cfg.base_url or s.embedding_base_url).rstrip("/")
    is_openai_compat = any(kw in base.lower() for kw in ("vllm", "deepseek", "openai.com", "openai.azure"))

    # OTel span（如果 tracing 已初始化）
    span = None
    try:
        from opentelemetry import trace
        tracer = trace.get_tracer("rag-v14")
        span = tracer.start_span("invoke_embedding")
        span.set_attribute("model", cfg.model_name)
        span.set_attribute("batch_size", len(texts))
        span.set_attribute("provider", "ollama" if not is_openai_compat else cfg.provider)
    except Exception:
        pass

    _start = _time.time()
    embeddings = []

    # Ollama /api/embed supports batch input — send all texts at once
    if not is_openai_compat and len(texts) > 1:
        # Batch all texts into one request (Ollama supports list input)
        resp = requests.post(
            f"{base}/api/embed",
            json={"model": cfg.model_name, "input": texts},
            timeout=120,
        )
        resp.raise_for_status()
        embeddings = resp.json()["embeddings"]
    else:
        for text in texts:
            if is_openai_compat:
                resp = requests.post(
                    f"{base}/embeddings",
                    json={"model": cfg.model_name, "input": text},
                    headers={"Authorization": f"Bearer {cfg.api_key or s.llm_api_key}"},
                    timeout=60,
                )
                resp.raise_for_status()
                embeddings.append(resp.json()["data"][0]["embedding"])
            else:
                resp = requests.post(
                    f"{base}/api/embed",
                    json={"model": cfg.model_name, "input": text},
                    timeout=60,
                )
                resp.raise_for_status()
                embeddings.append(resp.json()["embeddings"][0])

    if span:
        span.set_attribute("elapsed_ms", int((_time.time() - _start) * 1000))
        span.set_attribute("dim", len(embeddings[0]) if embeddings else 0)
        span.end()

    # Langfuse trace
    try:
        trace_generation(
            trace_name=f"embedding-{cfg.model_name}",
            prompt=f"batch:{len(texts)} texts",
            completion=f"{len(embeddings)} vectors x {len(embeddings[0]) if embeddings else 0}d",
            model=cfg.model_name,
            metadata={"batch_size": len(texts), "mode": mode,
                     "provider": "ollama" if not is_openai_compat else cfg.provider},
        )
    except Exception:
        pass

    return embeddings


# ── invoke_llm（已有 OpenAI SDK — 天然兼容所有 provider）────────

def invoke_llm(prompt: str, model_id: Optional[str] = None) -> str:
    """调用 LLM 生成（OpenAI 兼容 API → Ollama / vLLM / DeepSeek / OpenAI）。

    自动产生 OTel span 上报到 Tempo + Langfuse trace。
    """
    from openai import OpenAI
    import time as _wall

    if model_id is None:
        model_id = Settings().llm_model
    cfg = resolve_model(model_id)
    base = (cfg.base_url or Settings().llm_base_url).rstrip("/")

    # OTel span
    span = None
    try:
        from opentelemetry import trace
        tracer = trace.get_tracer("rag-v14")
        span = tracer.start_span("invoke_llm")
        span.set_attribute("model", cfg.model_name)
        span.set_attribute("model_id", model_id)
        span.set_attribute("provider", cfg.provider)
    except Exception:
        pass

    client = OpenAI(base_url=base, api_key=cfg.api_key or Settings().llm_api_key, timeout=120.0)

    _start = _wall.time()
    resp = client.chat.completions.create(
        model=cfg.model_name,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=4096,
        temperature=0.1,
    )
    answer = (resp.choices[0].message.content or "").strip()
    _elapsed = _wall.time() - _start

    if span:
        span.set_attribute("elapsed_ms", int(_elapsed * 1000))
        span.set_attribute("answer_len", len(answer))
        span.end()

    try:
        trace_generation(
            trace_name=f"llm-{model_id}",
            prompt=prompt, completion=answer, model=cfg.model_name,
            metadata={"elapsed_ms": int(_elapsed * 1000)},
        )
    except Exception:
        pass

    return answer


# ── invoke_rerank ───────────────────────────────────────────────

_reranker_cache: Dict[str, Any] = {}
_DEFAULT_RERANK_MODEL = "BAAI/bge-reranker-v2-m3"


def _get_reranker(model_name: str = _DEFAULT_RERANK_MODEL):
    """Get or create a reranker instance by model name (cached)."""
    if model_name not in _reranker_cache:
        from FlagEmbedding import FlagReranker
        _reranker_cache[model_name] = FlagReranker(model_name, use_fp16=True)
    return _reranker_cache[model_name]


def invoke_rerank(query: str, documents: List[str], model_name: str = "") -> List[str]:
    """Re-rank documents using BGE Reranker (P1-6: dynamic model support).

    Args:
        query: The search query.
        documents: List of document contents to re-rank.
        model_name: Reranker model name (default: BAAI/bge-reranker-v2-m3).
                   Can be overridden via P-CONFIG rerank_model_id.
    """
    effective_model = model_name or _DEFAULT_RERANK_MODEL

    # OTel span
    span = None
    try:
        from opentelemetry import trace
        span = trace.get_tracer("rag-v14").start_span("invoke_rerank")
        span.set_attribute("doc_count", len(documents))
        span.set_attribute("model", effective_model)
    except Exception:
        pass

    if not documents:
        if span: span.end()
        return documents

    import time as _wall
    _start = _wall.time()
    try:
        ranker = _get_reranker(effective_model)
        scores = ranker.compute_score([[query, d] for d in documents], normalize=True)
        scored = sorted(zip(documents, scores), key=lambda x: x[1], reverse=True)
        result = [doc for doc, _ in scored]
        if span:
            span.set_attribute("elapsed_ms", int((_wall.time() - _start) * 1000))
            span.end()
        return result
    except Exception as exc:
        if span: span.end()
        log.warning("rerank_failed_fallback_to_noop", error=str(exc), model=effective_model)
        return documents


# ── resolve_prompt ──────────────────────────────────────────────

def resolve_prompt(prompt_id: str = "default", version: str = "v1") -> str:
    """从 prompt_templates 表按版本读取。"""

    async def _query():
        conn = await asyncpg.connect(_get_db_dsn())
        try:
            row = await conn.fetchrow(
                "SELECT template_text FROM prompt_templates "
                "WHERE prompt_id=$1 AND version=$2 AND is_active=true", prompt_id, version)
            if row:
                return row["template_text"]
            row = await conn.fetchrow(
                "SELECT template_text FROM prompt_templates "
                "WHERE prompt_id=$1 AND is_active=true ORDER BY version DESC LIMIT 1", prompt_id)
            if row:
                return row["template_text"]
        finally:
            await conn.close()
        return ""

    result = _run_async(_query())
    if result:
        return result
    # 无 DB 匹配时返回空字符串，让调用方自行选择 fallback 模板。
    # chat/service.py 中每个 synthesis 模式都有各自的 _DEFAULT_*_TEMPLATE
    # 作为 or 回退，此处的硬编码默认值会短路调用方的变量绑定
    # (compact 模板用 {{ documents }}，refine 用 {{ current_doc }})。
    return ""


def create_embedder(model_id: str = "bge-m3", mode: str = "document") -> Any:
    """创建 Haystack Embedder 实例（经 P-MODEL 防腐层）。

    mode="document" → SentenceTransformersDocumentEmbedder（摄入用）
    mode="query"    → SentenceTransformersTextEmbedder（查询用）

    返回的 Embedder 实例可用于 Haystack Pipeline。
    调用方不直接 import Haystack 模型类。
    """
    from sentence_transformers import SentenceTransformer
    from haystack.components.embedders import (
        SentenceTransformersDocumentEmbedder,
        SentenceTransformersTextEmbedder,
    )

    model_config = resolve_model(model_id)
    model_name = model_config.model_name

    if mode == "query":
        return SentenceTransformersTextEmbedder(model=model_name)
    else:
        return SentenceTransformersDocumentEmbedder(model=model_name)


def create_ranker(model_id: str = "bge-reranker-v2-m3", top_k: int = 10) -> Any:
    """创建 Haystack Ranker 实例（经 P-MODEL 防腐层）。

    返回的 Ranker 实例可用于 Haystack 查询 Pipeline 的重排序节点。
    调用方不直接 import Haystack 模型类。
    """
    from haystack.components.rankers import SentenceTransformersRanker

    model_config = resolve_model(model_id)
    return SentenceTransformersRanker(
        model=model_config.model_name,
        top_k=top_k,
    )


def build_prompt(template: str, variables: Dict[str, Any]) -> str:
    """使用 Jinja2 渲染 prompt 模板（等价于 Haystack PromptBuilder）。

    变量示例：
        variables = {
            "query": "用户问题",
            "documents": [{"content": "文档内容", "meta": {...}}, ...],
        }

    模板可访问所有变量及 Jinja2 标准过滤器。
    与 Haystack PromptBuilder 兼容——模板可在两者间互换。
    """
    from jinja2 import Environment, BaseLoader, TemplateSyntaxError

    try:
        env = Environment(loader=BaseLoader())
        tpl = env.from_string(template)
        return tpl.render(**variables)
    except TemplateSyntaxError as e:
        log.error("prompt_template_syntax_error", error=str(e))
        raise ValueError(f"Invalid prompt template: {e}") from e
    except Exception as e:
        log.error("prompt_render_failed", error=str(e))
        raise


# ── Langfuse 集成 ───────────────────────────────────────────────

_langfuse_initialized = False


def init_langfuse():
    global _langfuse_initialized
    if _langfuse_initialized:
        return

    s = Settings()
    if not s.langfuse_public_key or not s.langfuse_secret_key:
        log.info("langfuse_skipped", reason="keys not set")
        _langfuse_initialized = True
        return

    try:
        import langfuse
        lc = langfuse.Langfuse(public_key=s.langfuse_public_key,
                              secret_key=s.langfuse_secret_key, host=s.langfuse_host or None)
        lc.start_as_current_observation(as_type="span", name="langfuse-init")
        log.info("langfuse_initialized", host=s.langfuse_host, version="v4")
        _langfuse_initialized = True
    except Exception as exc:
        log.warning("langfuse_init_failed", error=str(exc))
        _langfuse_initialized = True


def trace_generation(trace_name: str, prompt: str, completion: str, model: str,
                     metadata: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """记录一次生成调用到 Langfuse v4（fail-open，flush 后立即返回）。"""
    import uuid
    import time as _time

    s = Settings()
    if not s.langfuse_public_key:
        return None

    try:
        import langfuse
        client = langfuse.Langfuse(
            public_key=s.langfuse_public_key,
            secret_key=s.langfuse_secret_key,
            host=s.langfuse_host or None,
            flush_interval=1,
        )
        trace_id = uuid.uuid4().hex
        with client.start_as_current_observation(
            as_type="generation",
            name=trace_name,
            trace_context={"trace_id": trace_id},
            model=model,
            input=prompt[:10000] if prompt else "",
            output=completion[:10000] if completion else "",
            metadata=metadata or {},
        ):
            pass
        client.flush()
        # 短等待确保异步发送（不阻塞太久）
        _time.sleep(0.3)
        return trace_id
    except Exception:
        return None

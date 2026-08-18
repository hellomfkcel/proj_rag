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
import os
import time as _time
from typing import Any, Dict, Generator, List, Optional
from dataclasses import dataclass

from src.config import Settings
from src.platform.obs.logger import get_logger

log = get_logger(__name__)


def _get_device(required_mb: int = 0) -> str:
    """检测最佳可用推理设备：CUDA GPU → CPU 回退，含显存检查。

    返回值可直接用于 BGEM3FlagModel、FlagReranker 等 FlagEmbedding 模型的
    ``devices`` 参数。调用方无需自行检测 CUDA 可用性。

    required_mb=0 (默认):
        仅检查 CUDA 是否可用，不检查显存余量（向后兼容）。
    required_mb>0:
        额外检查 GPU 空闲显存是否 ≥ required_mb。
        不足时自动降级 CPU，并记录 WARNING 日志。
    """
    try:
        import torch
        if not torch.cuda.is_available():
            return "cpu"

        if required_mb > 0:
            free_bytes, total_bytes = torch.cuda.mem_get_info()
            free_mb = free_bytes / (1024 * 1024)
            total_mb = total_bytes / (1024 * 1024)
            if free_mb < required_mb:
                log.warning(
                    "gpu_memory_insufficient_fallback_cpu",
                    free_mb=int(free_mb),
                    required_mb=required_mb,
                    total_mb=int(total_mb),
                )
                return "cpu"

        return "cuda"
    except ImportError:
        return "cpu"


def _get_device_string() -> str:
    """返回人类可读的设备描述字符串，供日志/span attribute 使用。"""
    device = _get_device()
    if device == "cuda":
        try:
            import torch
            name = torch.cuda.get_device_name(0)
            free_bytes, total_bytes = torch.cuda.mem_get_info()
            free_gb = free_bytes / (1024**3)
            total_gb = total_bytes / (1024**3)
            return f"cuda ({name}, {free_gb:.1f}/{total_gb:.1f} GiB free)"
        except Exception:
            return "cuda"
    return "cpu"


@dataclass
class ModelConfig:
    model_name: str
    base_url: str = ""
    api_key: str = ""
    provider: str = "ollama"
    model_type: str = "llm"


class ModelOutputError(Exception):
    """P-MODEL 生成的输出异常（如空输出）。

    错误码语义：model:empty_output —— LLM 返回了空 content。
    常见根因：推理类模型（如 deepseek 系列）的 reasoning_content
    吃满 max_tokens 预算，导致最终 content 为空（finish_reason=length）。
    该异常不携带任何 prompt / 生成内容 / credential。
    """


@dataclass
class LLMStreamChunk:
    """invoke_llm_stream 的流式产出单元。

    - reasoning: 推理阶段增量（thinking_content），可为空字符串；
    - content:    答案阶段增量，可为空字符串。
    两者同一 chunk 只会有其一非空（不同阶段）。
    """

    reasoning: str = ""
    content: str = ""


# ── DB helpers ──────────────────────────────────────────────────

def _get_db_dsn() -> str:
    return Settings().database_url.replace("postgresql+asyncpg://", "postgresql://")


def _run_async(coro):
    """统一委托 P-platform run_async_safe（兼容同步/异步调用方）。

    裸 asyncio.run 在已有 event loop 上下文（FastAPI async 端点）会抛
    RuntimeError；run_async_safe 在无 loop 时直接 run、有 loop 时线程池桥接。
    """
    from src.platform.async_utils import run_async_safe
    return run_async_safe(coro)


# ── resolve_model ───────────────────────────────────────────────

# 各模型类型 api_key 的 env 兜底键。本地部署（ollama/vllm/sentence_transformers）无需 key。
_MODEL_TYPE_ENV_API_KEY = {
    "llm": "LLM_API_KEY",
    "embedding": "EMBEDDING_API_KEY",
    "reranker": "RERANK_API_KEY",
}

# 需要真实 api_key 的外部 provider（缺 key 时告警，不阻断）
_KEY_REQUIRED_PROVIDERS = ("deepseek", "openai", "azure", "anthropic")


def _env_api_key_for_type(model_type: str) -> str:
    """返回该模型类型的 env 秘钥兜底（无则空串）。

    llm 用 Settings().llm_api_key（含本地 ollama 默认哨兵）；embedding/reranker
    按各自 env 键读取。使 .env 秘钥轮换后无需改 DB 即生效。
    """
    import os
    s = Settings()
    key_name = _MODEL_TYPE_ENV_API_KEY.get(model_type or "llm")
    if not key_name:
        return ""
    if key_name == "LLM_API_KEY":
        return s.llm_api_key
    return os.getenv(key_name) or ""


def resolve_model(model_id: str) -> ModelConfig:
    """解析模型配置。

    优先级：DB model_registry 表 → Settings 环境变量（秘钥按类型回落）。
    DB 是唯一权威源；api_key 为空时回落对应类型 env 秘钥，使 .env 秘钥轮换生效。
    可观测：resolve 时记录生效秘钥来源（db/env/none，不落 key 明文）。
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
                # embedding 类型默认用 embedding_base_url，其余用 llm_base_url
                default_url = s.embedding_base_url if mtype == "embedding" else s.llm_base_url
                db_key = (row["api_key"] or "").strip()
                env_key = _env_api_key_for_type(mtype)
                effective_key = db_key or env_key
                source = "db" if db_key else ("env" if env_key else "none")
                provider = row["provider"] or "ollama"
                log.debug(
                    "model_resolved",
                    model_id=model_id, model_type=mtype, provider=provider,
                    model_name=row["model_name"], key_source=source,
                    base_url=(row["base_url"] or default_url),
                )
                if not effective_key and provider in _KEY_REQUIRED_PROVIDERS:
                    log.warning(
                        "model_missing_api_key",
                        model_id=model_id, model_type=mtype, provider=provider,
                    )
                return ModelConfig(
                    model_name=row["model_name"],
                    base_url=row["base_url"] or default_url,
                    api_key=effective_key,
                    provider=provider,
                    model_type=mtype,
                )
        finally:
            await conn.close()
        return None

    db_result = _run_async(_query())
    if db_result:
        return db_result

    # fallback: Settings 环境变量（秘钥按类型回落）
    if model_id == "qwen3-embed":
        return ModelConfig(
            s.embedding_model, s.embedding_base_url,
            _env_api_key_for_type("embedding"), "ollama", "embedding",
        )
    return ModelConfig(s.llm_model, s.llm_base_url, s.llm_api_key, "ollama", "llm")


def resolve_managed_model_name(model_type: str, env_key: str = "", default: str = "") -> str:
    """解析某类型"默认模型"的 model_name（供 embedding_service 等运行时消费）。

    优先级：model_registry 该类型 is_default=true 行 → env_key → default。
    使管理台改默认 embedding/reranker 模型后，embedding_service 重启即生效。
    """
    import os

    async def _query():
        conn = await asyncpg.connect(_get_db_dsn())
        try:
            row = await conn.fetchrow(
                "SELECT model_name FROM model_registry "
                "WHERE model_type = $1 AND is_default = true LIMIT 1", model_type)
            if row:
                return row["model_name"] or ""
            # 兼容历史数据：reranker 类型可能存在 rerank 旧值
            if model_type == "reranker":
                row = await conn.fetchrow(
                    "SELECT model_name FROM model_registry "
                    "WHERE model_type = 'rerank' AND is_default = true LIMIT 1")
                if row:
                    return row["model_name"] or ""
            return ""
        finally:
            await conn.close()

    try:
        name = _run_async(_query())
    except Exception:
        name = ""
    if name:
        return name
    if env_key:
        env_val = os.getenv(env_key)
        if env_val:
            return env_val
    return default


def _resolve_default_reranker() -> Optional[ModelConfig]:
    """Resolve the default reranker model from model_registry DB table.

    Returns None if no default reranker is configured.
    """
    async def _query():
        conn = await asyncpg.connect(_get_db_dsn())
        try:
            row = await conn.fetchrow(
                "SELECT model_name, base_url, api_key, provider, model_type "
                "FROM model_registry WHERE model_type='reranker' AND is_default=true "
                "LIMIT 1")
            if row:
                db_key = (row["api_key"] or "").strip()
                env_key = _env_api_key_for_type("reranker")
                return ModelConfig(
                    model_name=row["model_name"],
                    base_url=row["base_url"] or "",
                    api_key=db_key or env_key,
                    provider=row["provider"] or "",
                    model_type=row["model_type"] or "reranker",
                )
        finally:
            await conn.close()
        return None

    try:
        return _run_async(_query())
    except Exception:
        return None


# ── invoke_embedding（provider-aware）────────────────────────────

def invoke_embedding(texts: List[str], mode: str = "document") -> List[List[float]]:
    """文本向量化（自动判定 Ollama /api/embed 或 OpenAI兼容 /v1/embeddings）。

    自动产生 OTel span 上报到 Tempo + Langfuse observation（包围实际计算，非后置记录）。
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

    # Langfuse observation — 在计算开始前创建，使 duration 反映实际耗时
    langfuse_obs = _start_langfuse_observation(
        trace_name=f"embedding-{cfg.model_name}",
        model=cfg.model_name,
        input_data=f"batch:{len(texts)} texts",
        metadata={"batch_size": len(texts), "mode": mode,
                  "provider": "ollama" if not is_openai_compat else cfg.provider},
    )

    _start = _time.time()
    embeddings = []

    try:
        # Ollama /api/embed supports batch input — send all texts at once
        if not is_openai_compat and len(texts) > 1:
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
    finally:
        # 结束 Langfuse observation（无论成功或失败）
        _end_langfuse_observation(
            langfuse_obs,
            output_data=f"{len(embeddings)} vectors x {len(embeddings[0]) if embeddings else 0}d",
        )

    if span:
        span.set_attribute("elapsed_ms", int((_time.time() - _start) * 1000))
        span.set_attribute("dim", len(embeddings[0]) if embeddings else 0)
        span.end()

    return embeddings


# ── invoke_llm（已有 OpenAI SDK — 天然兼容所有 provider）────────

def invoke_llm(prompt: str, model_id: Optional[str] = None,
               max_tokens: Optional[int] = None, temperature: Optional[float] = None,
               thinking: Optional[str] = None) -> str:
    """调用 LLM 生成（OpenAI 兼容 API → Ollama / vLLM / DeepSeek / OpenAI）。

    生成参数（max_tokens / temperature）缺省取 Settings（env 可覆盖），
    支持按调用覆盖——所有生成调用必须经此门面。

    thinking: "enabled" | "disabled" | None —— 透传给 DeepSeek 思考模式开关
    （extra_body={"thinking": {"type": ...}}）。None = 不发该参数（保持 provider 默认）。

    自动产生 OTel span 上报到 Tempo + Langfuse observation（包围实际计算，非后置记录）。

    空输出守卫：推理类模型（deepseek 等）reasoning_content 吃满 max_tokens 预算时
    会返回空 content（finish_reason=length）。此时记录元数据日志（不落 prompt/生成内容）
    并抛 ModelOutputError，绝不静默返回空串。
    """
    from openai import OpenAI
    import time as _wall

    s = Settings()
    if model_id is None:
        model_id = s.llm_model
    effective_max_tokens = max_tokens or s.llm_max_tokens
    effective_temperature = temperature if temperature is not None else s.llm_temperature
    cfg = resolve_model(model_id)
    base = (cfg.base_url or s.llm_base_url).rstrip("/")

    # OTel span
    span = None
    try:
        from opentelemetry import trace
        tracer = trace.get_tracer("rag-v14")
        span = tracer.start_span("invoke_llm")
        span.set_attribute("model", cfg.model_name)
        span.set_attribute("model_id", model_id)
        span.set_attribute("provider", cfg.provider)
        span.set_attribute("max_tokens", effective_max_tokens)
    except Exception:
        pass

    # Langfuse observation — 在计算开始前创建，使 duration 反映实际耗时
    langfuse_obs = _start_langfuse_observation(
        trace_name=f"llm-{model_id}",
        model=cfg.model_name,
        input_data=prompt[:10000],
        metadata={},
    )

    client = OpenAI(base_url=base, api_key=cfg.api_key or s.llm_api_key, timeout=120.0)

    _start = _wall.time()
    answer = ""
    finish_reason = ""
    reasoning_tokens = None
    try:
        extra_body = None
        if thinking in ("enabled", "disabled"):
            extra_body = {"thinking": {"type": thinking}}
        resp = client.chat.completions.create(
            model=cfg.model_name,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=effective_max_tokens,
            temperature=effective_temperature,
            extra_body=extra_body,
        )
        _choice = resp.choices[0]
        finish_reason = _choice.finish_reason or ""
        _msg = _choice.message
        answer = (_msg.content or "").strip()
        try:
            reasoning_tokens = (resp.usage or None) and getattr(
                resp.usage.completion_tokens_details, "reasoning_tokens", None)
        except Exception:
            reasoning_tokens = None
    finally:
        _elapsed = _wall.time() - _start
        _end_langfuse_observation(
            langfuse_obs,
            output_data=answer[:10000] if answer else "",
            metadata={"elapsed_ms": int(_elapsed * 1000)},
        )

    if not answer:
        # 空输出守卫：只记元数据，绝不落 prompt / 生成内容 / credential
        log.warning(
            "llm_empty_output",
            finish_reason=finish_reason,
            reasoning_tokens=reasoning_tokens,
            model=cfg.model_name,
            model_id=model_id,
            max_tokens=effective_max_tokens,
            elapsed_ms=int(_elapsed * 1000),
        )
        if span:
            span.set_attribute("elapsed_ms", int(_elapsed * 1000))
            span.set_attribute("answer_len", 0)
            span.set_attribute("finish_reason", finish_reason)
            span.end()
        raise ModelOutputError(
            f"LLM returned empty content (model={cfg.model_name}, "
            f"finish_reason={finish_reason!r}, max_tokens={effective_max_tokens}, "
            f"reasoning_tokens={reasoning_tokens})"
        )

    if span:
        span.set_attribute("elapsed_ms", int(_elapsed * 1000))
        span.set_attribute("answer_len", len(answer))
        span.end()

    return answer


# ── invoke_llm_stream（真流式生成）──────────────────────────────

def invoke_llm_stream(prompt: str, model_id: Optional[str] = None,
                      max_tokens: Optional[int] = None, temperature: Optional[float] = None,
                      thinking: Optional[str] = None) -> Generator[LLMStreamChunk, None, None]:
    """流式调用 LLM（OpenAI 兼容 → Ollama / vLLM / DeepSeek / OpenAI），逐 chunk 产出 LLMStreamChunk。

    与 invoke_llm 同一 P-MODEL 门面语义，供 B-CHAT 逐推理段/逐 token 回传。
    thinking: "enabled" | "disabled" | None —— 同 invoke_llm，透传 DeepSeek 思考模式开关。

    观测契约与 invoke_llm 一致：OTel span + Langfuse observation 包围整个流，
    结束后补记 elapsed_ms / answer_len / finish_reason / reasoning_tokens。
    空 content 守卫同 invoke_llm：推理吃满 max_tokens 时 content 为空 → 抛 ModelOutputError。

    reasoning_content 为 DeepSeek 非标准字段；SDK（requirements 1.40 / dev 2.50）模型
    extra="allow" 保留未知字段，故经 getattr(delta, "reasoning_content", None) 读取。
    """
    from openai import OpenAI
    import time as _wall

    s = Settings()
    if model_id is None:
        model_id = s.llm_model
    effective_max_tokens = max_tokens or s.llm_max_tokens
    effective_temperature = temperature if temperature is not None else s.llm_temperature
    cfg = resolve_model(model_id)
    base = (cfg.base_url or s.llm_base_url).rstrip("/")

    # OTel span（生成器首迭代时创建，span 覆盖整个流时长）
    span = None
    try:
        from opentelemetry import trace
        tracer = trace.get_tracer("rag-v14")
        span = tracer.start_span("invoke_llm_stream")
        span.set_attribute("model", cfg.model_name)
        span.set_attribute("model_id", model_id)
        span.set_attribute("provider", cfg.provider)
        span.set_attribute("max_tokens", effective_max_tokens)
    except Exception:
        pass

    # Langfuse observation（开始前创建，duration 反映实际耗时）
    langfuse_obs = _start_langfuse_observation(
        trace_name=f"llm-{model_id}",
        model=cfg.model_name,
        input_data=prompt[:10000],
        metadata={},
    )

    client = OpenAI(base_url=base, api_key=cfg.api_key or s.llm_api_key, timeout=120.0)

    _start = _wall.time()
    answer = ""
    finish_reason = ""
    reasoning_tokens = None
    try:
        extra_body = None
        if thinking in ("enabled", "disabled"):
            extra_body = {"thinking": {"type": thinking}}
        resp = client.chat.completions.create(
            model=cfg.model_name,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=effective_max_tokens,
            temperature=effective_temperature,
            stream=True,
            stream_options={"include_usage": True},
            extra_body=extra_body,
        )
        for chunk in resp:
            # 末 chunk 携 usage（stream_options include_usage=True）；SDK 版本间可能把
            # usage 附在含 choice 的 chunk 上，故每个 chunk 都检查，避免漏读。
            if chunk.usage:
                try:
                    reasoning_tokens = getattr(
                        chunk.usage.completion_tokens_details, "reasoning_tokens", None)
                except Exception:
                    reasoning_tokens = None
            if not chunk.choices:
                continue
            _choice = chunk.choices[0]
            if _choice.finish_reason:
                finish_reason = _choice.finish_reason
            _delta = _choice.delta
            rc = getattr(_delta, "reasoning_content", None)
            if rc:
                yield LLMStreamChunk(reasoning=rc)
            if _delta.content:
                answer += _delta.content
                yield LLMStreamChunk(content=_delta.content)

        if not answer:
            # 空输出守卫：只记元数据，绝不落 prompt / 生成内容 / credential
            log.warning(
                "llm_empty_output",
                finish_reason=finish_reason,
                reasoning_tokens=reasoning_tokens,
                model=cfg.model_name,
                model_id=model_id,
                max_tokens=effective_max_tokens,
                elapsed_ms=int((_wall.time() - _start) * 1000),
            )
            raise ModelOutputError(
                f"LLM returned empty content (model={cfg.model_name}, "
                f"finish_reason={finish_reason!r}, max_tokens={effective_max_tokens}, "
                f"reasoning_tokens={reasoning_tokens})"
            )
    finally:
        _elapsed = _wall.time() - _start
        _end_langfuse_observation(
            langfuse_obs,
            output_data=answer[:10000] if answer else "",
            metadata={"elapsed_ms": int(_elapsed * 1000)},
        )
        if span:
            span.set_attribute("elapsed_ms", int(_elapsed * 1000))
            span.set_attribute("answer_len", len(answer))
            span.set_attribute("finish_reason", finish_reason)
            span.set_attribute("reasoning_tokens", reasoning_tokens or 0)
            span.end()


# ── invoke_rerank ───────────────────────────────────────────────

_reranker_cache: Dict[str, Any] = {}
_DEFAULT_RERANK_MODEL_NAME = "BAAI/bge-reranker-v2-m3"


def _resolve_local_model_path(model_id: str) -> str:
    """在本地缓存中查找模型路径，优先 HF cache → ModelScope cache。

    找到本地路径后可直接传给 FlagEmbedding 模型构造函数，
    配合 local_files_only=True 避免联网校验。
    未找到时返回原始 model_id（回退到在线下载）。
    """
    import os as _os

    # HF cache: ~/.cache/huggingface/hub/models--{org}--{name}/snapshots/{hash}
    org, name = model_id.split("/", 1) if "/" in model_id else ("BAAI", model_id)
    hf_snapshots = _os.path.expanduser(
        f"~/.cache/huggingface/hub/models--{org}--{name}/snapshots"
    )
    if _os.path.isdir(hf_snapshots):
        try:
            versions = sorted(_os.listdir(hf_snapshots), reverse=True)
            for v in versions:
                p = _os.path.join(hf_snapshots, v)
                cfg = _os.path.join(p, "config.json")
                if _os.path.isfile(cfg):
                    return p
        except Exception:
            pass

    # ModelScope cache: ~/.cache/modelscope/hub/{org}/{name}
    ms_path = _os.path.expanduser(f"~/.cache/modelscope/hub/{org}/{name}")
    if _os.path.isdir(ms_path):
        return ms_path

    return model_id


def _get_reranker(model_name: str = _DEFAULT_RERANK_MODEL_NAME):
    """Get or create a reranker instance by model name (cached).

    GPU 显存需求约 1500 MiB（fp16 权重 ~1.1 GB + 推理临时空间），
    空闲显存不足时自动降级 CPU。

    ★ 模型常驻：共享服务是唯一模型持有者，worker 经 HTTP 调用，
    不重复加载 → 无资源争夺。

    模型加载：优先从本地 HF/ModelScope 缓存加载（local_files_only=True），
    避免首次调用时联网校验超时。
    """
    global _reranker_cache

    if model_name not in _reranker_cache:
        from FlagEmbedding import FlagReranker

        model_path = _resolve_local_model_path(model_name)
        _reranker_cache[model_name] = FlagReranker(
            model_path,
            use_fp16=True,
            devices=_get_device(required_mb=1500),
            local_files_only=(model_path != model_name),
        )
    return _reranker_cache[model_name]


def invoke_rerank(query: str, documents: List[str], model_name: str = "") -> List[str]:
    """Re-rank documents using BGE Reranker.

    Model resolution order:
    1. model_name parameter (from P-CONFIG rerank_model_id → resolve_model)
    2. DB model_registry (reranker type, is_default=true)
    3. Fallback: BAAI/bge-reranker-v2-m3
    """
    effective_model = model_name

    # If no model specified, try to resolve from DB model_registry
    if not effective_model:
        try:
            # Find default reranker from model_registry
            cfg = _resolve_default_reranker()
            if cfg:
                effective_model = cfg.model_name
                log.info("rerank_model_from_db", model_name=effective_model)
        except Exception:
            pass

    if not effective_model:
        effective_model = _DEFAULT_RERANK_MODEL_NAME

    # OTel span
    span = None
    try:
        from opentelemetry import trace
        span = trace.get_tracer("rag-v14").start_span("invoke_rerank")
        span.set_attribute("doc_count", len(documents))
        span.set_attribute("model", effective_model)
        span.set_attribute("device", _get_device_string())
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


def _start_langfuse_observation(
    trace_name: str,
    model: str = "",
    input_data: str = "",
    metadata: Optional[Dict[str, Any]] = None,
) -> Any:
    """创建 Langfuse generation observation 并返回（用于包围实际计算）。

    返回 Langfuse observation 对象，调用方负责在计算完成后调用
    observation.update(output=...) 然后 observation.__exit__()。

    若 Langfuse 未配置或初始化失败，返回 None（调用方须检查）。
    """
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
        otel_trace_id = _get_otel_trace_id()
        enriched_meta = dict(metadata or {})
        enriched_meta.setdefault("otel_trace_id", otel_trace_id)
        enriched_meta.setdefault("service_name", os.getenv("OTEL_SERVICE_NAME", "rag-v14"))

        obs_ctx = client.start_as_current_observation(
            as_type="generation",
            name=trace_name,
            trace_context={"trace_id": otel_trace_id},
            model=model,
            input=input_data if input_data else None,
            metadata=enriched_meta,
        )
        observation = obs_ctx.__enter__()
        # 返回 (client, observation, otel_trace_id) 供 _end_langfuse_observation 使用
        return (client, observation, otel_trace_id)
    except Exception:
        return None


def _end_langfuse_observation(
    obs_tuple: Any,
    output_data: str = "",
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """结束 Langfuse observation，设置 output 并 flush（fail-open）。"""
    if obs_tuple is None:
        return

    try:
        client, observation, _ = obs_tuple
        if output_data:
            observation.update(output=output_data)
        if metadata:
            observation.update(metadata=metadata)
        observation.__exit__(None, None, None)
        client.flush()
    except Exception:
        pass


def trace_generation(trace_name: str, prompt: str, completion: str, model: str,
                     metadata: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """记录一次生成调用到 Langfuse v4（fail-open，flush 后立即返回）。

    ★ 使用当前 OTel span 的 trace_id 作为 Langfuse trace_id，
    确保 Langfuse 中的模型观测与 Tempo/Grafana 中的调用链可互跳。
    """
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

        # ★ 使用 OTel trace_id，而非随机 UUID，确保跨系统追踪可关联
        otel_trace_id = _get_otel_trace_id()

        # 合并业务上下文到 metadata
        enriched_meta = dict(metadata or {})
        enriched_meta.setdefault("otel_trace_id", otel_trace_id)
        enriched_meta.setdefault("service_name", os.getenv("OTEL_SERVICE_NAME", "rag-v14"))

        with client.start_as_current_observation(
            as_type="generation",
            name=trace_name,
            trace_context={"trace_id": otel_trace_id},
            model=model,
            input=prompt[:10000] if prompt else "",
            output=completion[:10000] if completion else "",
            metadata=enriched_meta,
        ):
            pass
        client.flush()
        # 短等待确保异步发送（不阻塞太久）
        _time.sleep(0.3)
        return otel_trace_id
    except Exception:
        return None


def _get_otel_trace_id() -> str:
    """从当前 OTel span context 获取 trace_id，格式化为 32 位十六进制字符串。

    统一委托 P-OBS 门面（get_current_trace_id），避免可观测逻辑散落各模块。
    无活跃 span 时返回空字符串（由 Langfuse 侧自行回退）。
    """
    from src.platform.obs.tracing import get_current_trace_id
    return get_current_trace_id()

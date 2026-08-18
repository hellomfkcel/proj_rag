"""BGE-M3 统一文本嵌入器（查询侧）——单次 encode 产出稠密 + 稀疏向量。

复用摄入侧 bge_m3_embedder._get_model() 全局单例，同一进程内不重复加载模型。

可观测性：
- Haystack Pipeline 自动为 Component.run() 创建 OTel span。
- 额外在 encode 调用处创建 Langfuse generation observation。
"""

import math
import time as _time
from typing import Any, Dict, List

from haystack import component


@component
class BGE_M3TextEmbedder:
    """BGE-M3 查询嵌入器——稠密 + 稀疏一次产出。

    输出接口:
      - embedding: List[float]      1024d L2 归一化稠密向量
      - sparse_embedding: Dict[str, float]  稀疏词权重
    """

    @component.output_types(
        embedding=List[float], sparse_embedding=Dict[str, float]
    )
    def run(self, text: str) -> Dict[str, Any]:
        # ── Langfuse observation ──
        langfuse_obs = _start_query_observation(text)

        _start = _time.time()
        try:
            from src.services.embedding_client import embed_query
            embedding, sparse_embedding, _ = embed_query(text)
        finally:
            elapsed_ms = int((_time.time() - _start) * 1000)
            _end_query_observation(langfuse_obs, elapsed_ms)

        return {"embedding": embedding, "sparse_embedding": sparse_embedding}


# ══════════════════════════════════════════════════════════════════
# Langfuse 可观测性（fail-open）
# ══════════════════════════════════════════════════════════════════

def _start_query_observation(text: str) -> Any:
    try:
        from src.platform.model.registry import _start_langfuse_observation
        return _start_langfuse_observation(
            trace_name="embedding-bge-m3",
            model="BAAI/bge-m3",
            input_data=text[:500],
            metadata={
                "mode": "query",
                "provider": "bge-m3-flagembedding",
                "service_name": _safe_service_name(),
            },
        )
    except Exception:
        return None


def _end_query_observation(obs_tuple: Any, elapsed_ms: int) -> None:
    if obs_tuple is None:
        return
    try:
        from src.platform.model.registry import _end_langfuse_observation
        _end_langfuse_observation(
            obs_tuple,
            output_data="1024d vector",
            metadata={"elapsed_ms": elapsed_ms},
        )
    except Exception:
        pass


def _safe_service_name() -> str:
    import os
    return os.getenv("OTEL_SERVICE_NAME", "retrieval-worker")

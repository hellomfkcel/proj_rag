"""BGE-M3 统一文档嵌入器（摄入侧）——一次 encode 产出稠密 + 稀疏向量。

替代 OllamaDocumentEmbedder + BGE_M3SparseEmbedder 两个组件。
BGE-M3 能同时输出 dense_vecs (1024d) 和 lexical_weights (稀疏词权重)，
一次模型前向传播完成，消除串行两次调用（HTTP + GPU）的延迟叠加。

设计决策：
- 模型以模块级全局单例加载（同 registry.py _reranker_cache 模式），
  进程生命周期内所有 Pipeline 实例共享同一份模型权重。
  避免当前"每次 Pipeline.loads() → 新建 Component → 重新加载 6.4GB 模型"的缺陷。
- batch_size=512：控制单次 GPU 峰值分配在 ~80 MiB 以内，消除 OOM。
- 稠密向量 L2 归一化后写入 Document.embedding，与 Milvus IP(内积) 距离兼容。

可观测性：
- Haystack Pipeline 自动为每个 Component.run() 创建 OTel span
  (name="haystack.component.run", attributes 含 component name/type/I/O)。
- 额外在 encode 调用处创建 Langfuse generation observation，
  包含 batch_size/elapsed_ms/batch_index 等元数据，与 Tempo trace 关联。
"""

import math
import os as _os
import time as _time
from dataclasses import replace
from typing import Any, Dict, List, Optional

from haystack import component, Document

# ══════════════════════════════════════════════════════════════════
# 模块级全局单例（同 registry.py _reranker_cache 模式）
# + 空闲超时自动卸载
# ══════════════════════════════════════════════════════════════════

_model: Any = None


def _get_model():
    """获取 BGE-M3 全局单例——惰性加载，进程生命周期内常驻。

    每次 Pipeline.loads() 创建新的 Component 实例时，
    不会重新加载模型——所有实例共享此单例。

    ★ 模型常驻：加载后进程生命周期内不卸载（用户指令——共享 embedding-service
    应常驻模型，避免每次查询重载 20-40s）。共享服务是唯一模型持有者，
    worker 经 HTTP 调用，不重复加载 → 无资源争夺。
    """
    global _model

    if _model is None:
        from FlagEmbedding import BGEM3FlagModel

        model_path = "BAAI/bge-m3"
        hf_cache = _os.path.expanduser(
            "~/.cache/huggingface/hub/models--BAAI--bge-m3/snapshots"
        )
        if _os.path.isdir(hf_cache):
            try:
                versions = sorted(_os.listdir(hf_cache), reverse=True)
                for v in versions:
                    p = _os.path.join(hf_cache, v)
                    cfg = _os.path.join(p, "config.json")
                    if _os.path.isfile(cfg):
                        import json
                        with open(cfg) as f:
                            if json.load(f).get("model_type"):
                                model_path = p
                                break
            except Exception:
                pass

        from src.platform.model.registry import _get_device

        _model = BGEM3FlagModel(
            model_path,
            use_fp16=True,
            devices=_get_device(required_mb=2500),
            local_files_only=True,
        )

    return _model


# ══════════════════════════════════════════════════════════════════
# Haystack @component
# ══════════════════════════════════════════════════════════════════

@component
class BGE_M3DocumentEmbedder:
    """BGE-M3 统一文档嵌入器——稠密 + 稀疏一次产出。

    在 Pipeline YAML 中同时替代:
      - OllamaDocumentEmbedder (dense_embedder)
      - BGE_M3SparseEmbedder (sparse_embedder)

    Pipeline 连接:
      sender: embedder.documents → receiver: perm_enricher.documents

    Document 输出:
      - doc.embedding: List[float]  1024d L2 归一化稠密向量
      - doc.sparse_embedding: Dict[str, float]  稀疏词权重(BM25 风格)
    """

    def __init__(self, batch_size: int = 64):
        """batch_size: 每次 HTTP 请求携带的文本数量（客户端主动合批，2026-08-16）。

        由 512 降为 64：embedding_client 按此值切块、逐块一次请求，降低单请求
        排队/超时；服务端（Infinity 动态 batching / 本地 BGE-M3）仍可在内部聚合。
        """
        self.batch_size = batch_size

    @component.output_types(documents=List[Document])
    def run(self, documents: List[Document]) -> Dict[str, Any]:
        texts = [doc.content for doc in documents]

        # ── Langfuse observation ──
        langfuse_obs = _start_embedding_observation(len(texts), self.batch_size)

        _start = _time.time()
        try:
            from src.services.embedding_client import embed_documents
            all_dense, all_sparse, _ = embed_documents(
                texts, batch_size=self.batch_size, normalize=True
            )
        finally:
            elapsed_ms = int((_time.time() - _start) * 1000)
            _end_embedding_observation(langfuse_obs, len(all_dense), 0, elapsed_ms)

        # 写回 Document（使用 replace 避免 mutation）
        for i, doc in enumerate(documents):
            documents[i] = replace(
                doc,
                embedding=all_dense[i] if i < len(all_dense) else None,
                sparse_embedding=all_sparse[i] if i < len(all_sparse) else {},
            )
        return {"documents": documents}


# ══════════════════════════════════════════════════════════════════
# Langfuse 可观测性（fail-open）
# ══════════════════════════════════════════════════════════════════

def _start_embedding_observation(total_texts: int, batch_size: int) -> Any:
    """创建 Langfuse generation observation（摄入嵌入）。"""
    try:
        from src.platform.model.registry import _start_langfuse_observation
        return _start_langfuse_observation(
            trace_name="embedding-bge-m3",
            model="BAAI/bge-m3",
            input_data=f"batch:{total_texts} texts (bs={batch_size})",
            metadata={
                "batch_size": total_texts,
                "sub_batch_size": batch_size,
                "mode": "document",
                "provider": "bge-m3-flagembedding",
                "service_name": _safe_service_name(),
            },
        )
    except Exception:
        return None


def _end_embedding_observation(
    obs_tuple: Any, vector_count: int, batch_count: int, elapsed_ms: int
) -> None:
    """结束 Langfuse observation。"""
    if obs_tuple is None:
        return
    try:
        from src.platform.model.registry import _end_langfuse_observation
        _end_langfuse_observation(
            obs_tuple,
            output_data=f"{vector_count} vectors x 1024d",
            metadata={"elapsed_ms": elapsed_ms, "batch_count": batch_count},
        )
    except Exception:
        pass


def _safe_service_name() -> str:
    import os
    return os.getenv("OTEL_SERVICE_NAME", "ingestion-worker")

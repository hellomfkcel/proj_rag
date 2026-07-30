"""B-RETRIEVE：检索模块。

提供：
- retrieve     Haystack 查询 Pipeline：prefilter 注入（L1）
               + 过采样补检索（L2）+ 混合检索（dense+sparse+RRF）+ rerank

不做：不做生成编排、不拥有对话状态、不做权限判定、不做事后过滤。
"""

from typing import Any, Dict, List, Optional

from src.platform.task.pipeline_runner import run_pipeline_sync
from src.platform.obs.logger import get_logger

log = get_logger(__name__)


def retrieve(
    query: str,
    kb_ids: List[str],
    tenant_id: str,
    ctx_token: str = "",
    top_k: int = 10,
    oversample_factor: float = 1.5,
    min_results: int = 3,
    refetch_max_rounds: int = 2,
    strict: bool = False,
    retrieval_mode: str = "hybrid",
    rerank_model_id: str = "",
    fusion_method: str = "rrf",
    dense_weight: float = 0.5,
    sparse_weight: float = 0.5,
) -> Dict[str, Any]:
    """执行 Haystack 查询 Pipeline：dense + sparse 混合检索 + RRF/weighted_sum 融合 + rerank。

    三层检索链路：
    L1: prefilter 编译注入（6 条件 MetadataFilter，在 Pipeline 外编译后注入 MilvusRetriever）
    L2: 过采样 k×1.5 + 补检索最多 refetch_max_rounds 轮
    L3: strict 库逐条复核（调用 /v1/filter，批次 ≤200，失败整批拒绝）

    retrieval_mode: hybrid / vector_only / keyword_only
    fusion_method: rrf / weighted_sum
    rerank_model_id: 空字符串 = 使用默认 BGE-Reranker
    """
    from haystack import Document
    from milvus_haystack.filters import parse_filters
    from src.permission.context import build_context, resolve_ctx_token
    from src.permission.authz import get_prefilter, compile_filter

    # 从 ctx_token 提取 credential 后重建 RequestContext（生产路径）
    if ctx_token:
        credential = resolve_ctx_token(ctx_token)
        ctx = build_context(credential, enforce_jwt=True)
    else:
        raise ValueError("ctx_token is required for retrieval tasks")

    # ── L1: prefilter ──
    pf = get_prefilter(ctx)
    if pf.get("suspended"):
        return {"documents": [], "status": "suspended"}

    pf_kbs = pf.get("kbs", "__ALL__")
    if pf_kbs == "__ALL__":
        candidate_kbs = kb_ids
    else:
        pf_kb_set = set(pf_kbs) if isinstance(pf_kbs, list) else set()
        candidate_kbs = [kb for kb in kb_ids if kb in pf_kb_set]

    if not candidate_kbs:
        return {"documents": [], "status": "empty_candidates"}

    # For now we process one KB at a time (design allows multi-KB in future)
    kb_id = candidate_kbs[0]

    # Compile 6-condition filter for this KB → dict → Milvus expression string
    from src.permission.authz import _compile_filter_expr
    flt = compile_filter(pf, ctx, kb_id)
    filter_expr = _compile_filter_expr(flt)

    # ── Resolve rerank model (P1-6: dynamic rerank_model_id) ──
    # YAML connections auto-route joiner → ranker documents, only pass query
    ranker_kwargs: Dict[str, Any] = {"query": query}
    if rerank_model_id:
        try:
            from src.platform.model.registry import resolve_model
            rerank_cfg = resolve_model(rerank_model_id)
            ranker_kwargs["model_name"] = rerank_cfg.model_name
            log.info("rerank_model_resolved", model_id=rerank_model_id, model_name=rerank_cfg.model_name)
        except Exception:
            log.warning("rerank_model_resolve_failed", model_id=rerank_model_id)

    # ── Select pipeline based on retrieval_mode (P1-7) ──
    if retrieval_mode == "vector_only":
        # 检索专用 Pipeline（不含 generator，生成由 B-CHAT 的 _synthesize_* 负责）
        _pipeline_name = "retrieval_v1"
        _pipeline_input: Dict[str, Any] = {
            "text_embedder": {"text": query},
            "retriever": {"filters": filter_expr},
        }
        _docs_key = "retriever"
    elif retrieval_mode == "keyword_only":
        # P1-6: Independent keyword-only pipeline (query_v2.yaml)
        _pipeline_name = "query_v2"
        _pipeline_input = {
            "sparse_embedder": {"text": query},
            "sparse_retriever": {"filters": filter_expr},
            "ranker": ranker_kwargs,
        }
        _docs_key = "ranker"
    else:  # hybrid (default) — select pipeline by fusion_method
        if fusion_method == "weighted_sum":
            _pipeline_name = "query_v5"
        else:
            _pipeline_name = "query_v4"
        # YAML connections auto-route embedder outputs → retriever inputs.
        # Only pass text to embedders and filters to retrievers (no duplicate query_embedding).
        # joiner weights: 运行时动态可调，无需重建 pipeline
        _pipeline_input = {
            "text_embedder": {"text": query},
            "sparse_embedder": {"text": query},
            "dense_retriever": {"filters": filter_expr},
            "sparse_retriever": {"filters": filter_expr},
            "joiner": {"dense_weight": dense_weight, "sparse_weight": sparse_weight},
            "ranker": ranker_kwargs,
        }
        _docs_key = "ranker"

    log.info("retrieval_pipeline_selected", mode=retrieval_mode,
             fusion=fusion_method, pipeline=_pipeline_name)

    # ── Run Haystack Query Pipeline ──
    all_docs: List[Document] = []
    k_prime = int(top_k * oversample_factor)

    try:
        result = run_pipeline_sync(_pipeline_name, _pipeline_input)

        # 优先取 hierarchical_merger 输出（合并后的父块），fallback 到 ranker/retriever 输出
        docs = (result.get("hierarchical_merger", {}).get("documents", []) or
                result.get("ranker", {}).get("documents", []) or
                result.get(_docs_key, {}).get("documents", []))
        all_docs.extend(docs)

    except Exception as exc:
        log.warning("pipeline_query_failed", error=str(exc), kb_id=kb_id)
        return {"documents": [], "status": "pipeline_error"}

    # ── L2: refetch if too few results (same pipeline and filter) ──
    # §15.2: "Never relax filter conditions during refetch" — 过滤器不变。
    # 因此当首轮结果为 0 时，refetch 必然也是 0，直接跳过。
    # Refetch 仅在 0 < len(all_docs) < min_results 时有意义：
    # ANN 近似搜索的非确定性可能在补检索中返回不同排列，从而填满 min_results。
    round_count = 0
    while 0 < len(all_docs) < min_results and round_count < refetch_max_rounds:
        round_count += 1
        try:
            # Build refetch input without embedding (pipeline connection handles it)
            refetch_input = {k: v for k, v in _pipeline_input.items()}
            result2 = run_pipeline_sync(_pipeline_name, refetch_input)
            docs2 = (result2.get("hierarchical_merger", {}).get("documents", []) or
                     result2.get("ranker", {}).get("documents", []) or
                     result2.get(_docs_key, {}).get("documents", []))
            # Deduplicate by content
            seen_contents = {d.content for d in all_docs}
            for d in docs2:
                if d.content not in seen_contents:
                    all_docs.append(d)
                    seen_contents.add(d.content)
        except Exception as exc:
            log.warning("pipeline_refetch_failed", round=round_count, error=str(exc))
            break

    # ── L3: strict 逐条权限复核 ──
    if strict and all_docs:
        from src.permission.authz import filter_items as l3_filter
        doc_kb_pairs = [(d.meta.get("document_id", ""), d.meta.get("kb_id", kb_id))
                       for d in all_docs]
        allowed_pairs = l3_filter(ctx, doc_kb_pairs)
        allowed_doc_ids = {doc_id for doc_id, _ in allowed_pairs}
        all_docs = [d for d in all_docs if d.meta.get("document_id", "") in allowed_doc_ids]

    # ── Truncate to top_k ──
    all_docs = all_docs[:top_k]

    return {
        "documents": all_docs,
        "chunk_ids": [d.id for d in all_docs],
        "count": len(all_docs),
    }

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

    # ── Select pipeline template based on retrieval_mode + fusion_method ──
    # The same pipeline structure is used for all KBs; only the filter (MetadataFilter)
    # differs per KB to enforce tenant+bucket isolation (6-condition filter §15.1).
    if retrieval_mode == "vector_only":
        _pipeline_name = "retrieval_v1"
        _pipeline_input_template: Dict[str, Any] = {
            "query_embedder": {"text": query},
        }
        _docs_key = "retriever"
    elif retrieval_mode == "keyword_only":
        _pipeline_name = "query_v2"
        _pipeline_input_template = {
            "query_embedder": {"text": query},
        }
        _docs_key = "ranker"
    else:  # hybrid (default) — select pipeline by fusion_method
        if fusion_method == "weighted_sum":
            _pipeline_name = "query_v5"
            _joiner_input: Dict[str, Any] = {"dense_weight": dense_weight, "sparse_weight": sparse_weight}
        else:
            _pipeline_name = "query_v4"
            _joiner_input = {}
        _pipeline_input_template = {
            "query_embedder": {"text": query},
            "joiner": _joiner_input,
        }
        _docs_key = "ranker"

    # ── Resolve rerank model (P1-6: dynamic rerank_model_id) ──
    ranker_kwargs: Dict[str, Any] = {"query": query}
    if rerank_model_id:
        try:
            from src.platform.model.registry import resolve_model
            rerank_cfg = resolve_model(rerank_model_id)
            ranker_kwargs["model_name"] = rerank_cfg.model_name
            log.info("rerank_model_resolved", model_id=rerank_model_id, model_name=rerank_cfg.model_name)
        except Exception:
            log.warning("rerank_model_resolve_failed", model_id=rerank_model_id)

    # ── Run Haystack Query Pipeline for each candidate KB ──
    # §15.2: 最终候选 KB = prefilter.kbs ∩ 业务候选。
    # 每个 KB 使用独立的 6-condition filter（不同 kb_id），
    # 但共享同一 Pipeline 模板（嵌入/融合/rerank 结构相同）。
    all_docs: List[Document] = []
    k_prime = int(top_k * oversample_factor)

    from src.permission.authz import _compile_filter_expr

    for kb_id in candidate_kbs:
        # Compile 6-condition filter for this KB
        flt = compile_filter(pf, ctx, kb_id)
        filter_expr = _compile_filter_expr(flt)

        # Build KB-specific pipeline input with this KB's filter
        _pipeline_input = dict(_pipeline_input_template)
        if retrieval_mode == "vector_only":
            _pipeline_input["retriever"] = {"filters": filter_expr}
        elif retrieval_mode == "keyword_only":
            _pipeline_input["sparse_retriever"] = {"filters": filter_expr}
            _pipeline_input["ranker"] = ranker_kwargs
        else:  # hybrid
            _pipeline_input["dense_retriever"] = {"filters": filter_expr}
            _pipeline_input["sparse_retriever"] = {"filters": filter_expr}
            _pipeline_input["ranker"] = ranker_kwargs

        try:
            result = run_pipeline_sync(_pipeline_name, _pipeline_input)

            docs = (result.get("hierarchical_merger", {}).get("documents", []) or
                    result.get("ranker", {}).get("documents", []) or
                    result.get(_docs_key, {}).get("documents", []))
            all_docs.extend(docs)

        except Exception as exc:
            log.warning("pipeline_query_failed", error=str(exc), kb_id=kb_id)

    if not all_docs:
        return {"documents": [], "status": "empty_results", "chunk_ids": [], "count": 0}

    # ── L2: refetch if too few results (same pipeline, same filters per KB) ──
    # §15.3: "Never relax filter conditions during refetch" — 过滤器不变。
    # Refetch 仅在 0 < len(all_docs) < min_results 时有意义：
    # ANN 近似搜索的非确定性可能在补检索中返回不同排列。
    round_count = 0
    while 0 < len(all_docs) < min_results and round_count < refetch_max_rounds:
        round_count += 1
        for kb_id in candidate_kbs:
            flt = compile_filter(pf, ctx, kb_id)
            filter_expr = _compile_filter_expr(flt)
            refetch_input = dict(_pipeline_input_template)
            if retrieval_mode == "vector_only":
                refetch_input["retriever"] = {"filters": filter_expr}
            elif retrieval_mode == "keyword_only":
                refetch_input["sparse_retriever"] = {"filters": filter_expr}
                refetch_input["ranker"] = ranker_kwargs
            else:
                refetch_input["dense_retriever"] = {"filters": filter_expr}
                refetch_input["sparse_retriever"] = {"filters": filter_expr}
                refetch_input["ranker"] = ranker_kwargs
            try:
                result2 = run_pipeline_sync(_pipeline_name, refetch_input)
                docs2 = (result2.get("hierarchical_merger", {}).get("documents", []) or
                         result2.get("ranker", {}).get("documents", []) or
                         result2.get(_docs_key, {}).get("documents", []))
                seen_contents = {d.content for d in all_docs}
                for d in docs2:
                    if d.content not in seen_contents:
                        all_docs.append(d)
                        seen_contents.add(d.content)
            except Exception as exc:
                log.warning("pipeline_refetch_failed", round=round_count, kb_id=kb_id, error=str(exc))
            if len(all_docs) >= min_results:
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

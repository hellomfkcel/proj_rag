"""B-RETRIEVE：检索模块。

提供：
- retrieve     Haystack 查询 Pipeline：prefilter 注入（L1）
               + 过采样补检索（L2）+ 混合检索（dense+sparse+RRF）+ rerank

不做：不做生成编排、不拥有对话状态、不做权限判定、不做事后过滤。
"""

from copy import deepcopy
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
    min_score: float = 0.0,
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
    from src.permission.context import build_context, resolve_ctx_token
    from src.permission.authz import get_prefilter, compile_filter

    # 从 ctx_token 提取 credential 后重建 RequestContext（生产路径）
    if ctx_token:
        from src.config import Settings as _Settings
        credential = resolve_ctx_token(ctx_token)
        ctx = build_context(credential, enforce_jwt=True, jwks_url=_Settings().jwt_jwks_url)
    else:
        raise ValueError("ctx_token is required for retrieval tasks")

    # ── 检索 span 属性（写入当前 span，让 Tempo 携带 query/kb/principal/结果）──
    from opentelemetry import trace as _otel_trace2

    def _set_span_attrs(status: str, count: int = 0) -> None:
        _sp = _otel_trace2.get_current_span()
        if _sp.is_recording():
            _sp.set_attribute("retrieve.status", status)
            _sp.set_attribute("retrieve.result_count", count)
            _sp.set_attribute("retrieve.tenant_id", ctx.tenant_id)
            _sp.set_attribute("retrieve.principals", ",".join(sorted(ctx.principals)))
            _sp.set_attribute("retrieve.kb_ids", ",".join(kb_ids))
            _sp.set_attribute("retrieve.query", query)

    # ── L1: prefilter ──
    pf = get_prefilter(ctx)
    if pf.get("suspended"):
        log.warning("retrieve_suspended", tenant_id=ctx.tenant_id,
                    principals=list(ctx.principals), reason=pf.get("reason"))
        _set_span_attrs("suspended")
        return {"documents": [], "status": "suspended"}

    pf_kbs = pf.get("kbs", "__ALL__")
    if pf_kbs == "__ALL__":
        candidate_kbs = kb_ids
    else:
        pf_kb_set = set(pf_kbs) if isinstance(pf_kbs, list) else set()
        candidate_kbs = [kb for kb in kb_ids if kb in pf_kb_set]

    log.info("retrieve_prefilter",
             tenant_id=ctx.tenant_id, principals=list(ctx.principals),
             allow_stamps=pf.get("allow_stamps"), deny_stamps=pf.get("deny_stamps"),
             version=pf.get("version"), requested_kbs=kb_ids,
             candidate_kbs=candidate_kbs)

    if not candidate_kbs:
        log.warning("retrieve_empty_candidates",
                    tenant_id=ctx.tenant_id, principals=list(ctx.principals),
                    requested_kbs=kb_ids, pf_kbs=pf_kbs)
        _set_span_attrs("empty_candidates")
        return {"documents": [], "status": "empty_candidates"}

    # ── P0-2: 查询嵌入只编一次（与 KB 无关，确定性纯函数）──
    # 复用 BGE_M3TextEmbedder 组件（保留 Langfuse observation）。
    # 组件在管线外直接调用时不产生 haystack.component.run span，
    # 故显式包一层 OTel span 保持"查询嵌入"在 trace 中可见。
    from src.retrieve.components.bge_m3_text_embedder import BGE_M3TextEmbedder
    from opentelemetry import trace as _otel_trace

    _tracer = _otel_trace.get_tracer("retrieve.service")
    with _tracer.start_as_current_span("query_embedding", attributes={"mode": "query"}) as _emb_span:
        query_emb = BGE_M3TextEmbedder().run(text=query)
        _emb_span.set_attribute("query_len", len(query))
    dense_emb: List[float] = query_emb["embedding"]
    sparse_emb: Dict[str, float] = query_emb["sparse_embedding"]

    # ── Select pipeline template based on retrieval_mode + fusion_method ──
    # 三模式统一为"无 embedder"管线，embeddings 作为 run 输入传入（P0-2）。
    # §15.1: 每个 KB 使用独立的 6-condition filter（不同 kb_id），共享同一管线模板。
    # P1-1: k_prime = k × 1.5 过采样下推为 Milvus limit。
    k_prime = int(top_k * oversample_factor)

    if retrieval_mode == "vector_only":
        _pipeline_name = "retrieval_v2"
        _retriever_key = "retriever"
        _base_input: Dict[str, Any] = {
            "retriever": {"query_embedding": dense_emb},
        }
    elif retrieval_mode == "keyword_only":
        _pipeline_name = "query_v7"
        _retriever_key = "sparse_retriever"
        _base_input = {
            "sparse_retriever": {"query_sparse_embedding": sparse_emb},
        }
    else:  # hybrid (default) — Milvus 原生 hybrid_search，服务端 RRF/加权融合
        _pipeline_name = "query_v6"
        _retriever_key = "hybrid_retriever"
        _base_input = {
            "hybrid_retriever": {
                "query_embedding": dense_emb,
                "query_sparse_embedding": sparse_emb,
                "fusion_method": fusion_method,
                "dense_weight": dense_weight,
                "sparse_weight": sparse_weight,
            }
        }

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

    def _build_pipeline_input(filter_expr: str) -> Dict[str, Any]:
        """按 KB 构建管线输入：embeddings 固定，filter + top_k 每 KB 注入。"""
        pin = deepcopy(_base_input)
        pin[_retriever_key]["filters"] = filter_expr
        pin[_retriever_key]["top_k"] = k_prime
        if retrieval_mode in ("keyword_only", "hybrid"):
            pin["ranker"] = ranker_kwargs
        return pin

    # ── Run Haystack Query Pipeline for each candidate KB ──
    # §15.2: 最终候选 KB = prefilter.kbs ∩ 业务候选。
    all_docs: List[Document] = []

    from src.permission.authz import _compile_filter_expr

    for kb_id in candidate_kbs:
        # Compile 6-condition filter for this KB
        flt = compile_filter(pf, ctx, kb_id)
        filter_expr = _compile_filter_expr(flt)

        try:
            result = run_pipeline_sync(_pipeline_name, _build_pipeline_input(filter_expr))
            docs = (result.get("hierarchical_merger", {}).get("documents", []) or
                    result.get("ranker", {}).get("documents", []) or
                    result.get(_retriever_key, {}).get("documents", []))
            all_docs.extend(docs)
            # 每 KB 检索结果：filter_expr 即六条件编译结果（含 allow_stamps json_contains），
            # 命中 0 时可直接看出"空 allow_stamps 被排除"等原因。
            log.info("retrieve_kb_run", kb_id=kb_id, filter_expr=filter_expr,
                     hit_count=len(docs), mode=retrieval_mode)

        except Exception as exc:
            log.warning("pipeline_query_failed", error=str(exc), kb_id=kb_id,
                        filter_expr=filter_expr)

    if not all_docs:
        log.warning("retrieve_empty",
                    kb_ids=candidate_kbs, tenant_id=ctx.tenant_id,
                    principals=list(ctx.principals),
                    reason="no_chunks_match_prefilter_or_milvus_zero",
                    hint="likely empty allow_stamps or KB not authorized; check retrieve_kb_run filter_expr / stamp_empty_visibility")
        _set_span_attrs("empty_results")
        return {"documents": [], "status": "empty_results", "chunk_ids": [], "count": 0}

    # ── L2: refetch if too few results (same pipeline, same filters per KB) ──
    # §15.3: "Never relax filter conditions during refetch" — 过滤器不变。
    # Refetch 仅在 0 < len(all_docs) < min_results 时有意义：
    # ANN 近似搜索的非确定性可能在补检索中返回不同排列。
    # P0-2: refetch 复用已算好的 embeddings，不再重复编码查询。
    round_count = 0
    while 0 < len(all_docs) < min_results and round_count < refetch_max_rounds:
        round_count += 1
        log.info("retrieve_refetch", round=round_count,
                 current=len(all_docs), min_results=min_results)
        for kb_id in candidate_kbs:
            flt = compile_filter(pf, ctx, kb_id)
            filter_expr = _compile_filter_expr(flt)
            try:
                result2 = run_pipeline_sync(_pipeline_name, _build_pipeline_input(filter_expr))
                docs2 = (result2.get("hierarchical_merger", {}).get("documents", []) or
                         result2.get("ranker", {}).get("documents", []) or
                         result2.get(_retriever_key, {}).get("documents", []))
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

    # ── Score filter: 丢弃 rerank 得分低的不相关文档 ──
    if min_score > 0 and all_docs:
        filtered = [d for d in all_docs
                    if d.meta.get("rerank_score", 999) >= min_score]
        dropped = len(all_docs) - len(filtered)
        if dropped > 0:
            log.info("rerank_score_filter_dropped", dropped=dropped,
                     remaining=len(filtered), min_score=min_score)
        all_docs = filtered

    # ── Truncate to top_k ──
    all_docs = all_docs[:top_k]
    _set_span_attrs("ok", len(all_docs))
    log.info("retrieve_done", count=len(all_docs), kb_ids=candidate_kbs,
             mode=retrieval_mode, refetch_rounds=round_count)

    return {
        "documents": all_docs,
        "chunk_ids": [d.id for d in all_docs],
        "count": len(all_docs),
    }

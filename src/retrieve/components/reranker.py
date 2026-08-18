"""BGEReranker — Haystack @component wrapping P-MODEL invoke_rerank.

Uses BGE Reranker v2-m3 (via FlagEmbedding) to re-rank retrieved documents.
Falls back to no-op (identity sort) if reranker fails to load.
"""

from typing import Any, Dict, List
from haystack import component, Document


@component
class BGEReranker:
    """Re-rank documents using BGE Reranker v2-m3.

    Delegates to P-MODEL invoke_rerank which handles model loading and
    fail-open fallback (returns input order if reranker unavailable).
    """

    def __init__(self, top_k: int = 10, min_score: float | None = None):
        self.top_k = top_k
        self.min_score = min_score  # None = 不过滤，向后兼容

    @component.output_types(documents=List[Document])
    def run(self, query: str, documents: List[Document], model_name: str = "") -> Dict[str, Any]:
        """Re-rank documents by relevance to query.

        If fewer than 2 documents, returns them as-is.
        Otherwise invokes the reranker via embedding_client (HTTP or local).
        Attaches rerank_score to doc.meta; drops docs below min_score threshold.

        model_name 为可选的管线输入（per-KB rerank_model_id 解析出的模型名，
        空则用 embedding_service 托管默认重排模型）。
        """
        if len(documents) <= 1:
            return {"documents": documents}

        from src.services.embedding_client import rerank

        contents = [d.content for d in documents]
        reranked_contents, scores, _ = rerank(
            query, contents, top_k=self.top_k, model_name=model_name)

        # Map reranked contents back to Document objects, preserving order
        content_to_doc = {d.content: d for d in documents}
        result = []
        seen = set()
        for c, score in zip(reranked_contents, scores):
            if c not in seen:
                doc = content_to_doc.get(c, documents[len(result)])
                doc.meta["rerank_score"] = round(score, 4)
                # 阈值过滤
                if self.min_score is not None and score < self.min_score:
                    continue
                result.append(doc)
                seen.add(c)

        result = result[:self.top_k]
        return {"documents": result}

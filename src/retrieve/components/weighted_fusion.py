"""WeightedFusionJoiner — 自定义 Haystack @component：加权融合 + RRF。

替代 Haystack DocumentJoiner，解决两个限制：
1. 权重可通过 run() 参数动态传入（每次查询可调）
2. 对每个 retriever 的结果独立做 min-max 归一化后再加权

实现融合契约（v14.md §0.2.4）：
  fuse(dense_results, sparse_results) → list[Document]
"""

from dataclasses import replace
from typing import Any, Dict, Iterable, List, Optional

from typing_extensions import Annotated

from haystack import component, Document


@component
class WeightedFusionJoiner:
    """双路检索结果加权融合。

    - fusion_mode="weighted_sum": 分别对两路结果做 min-max 归一化 → 加权求和
    - fusion_mode="rrf":         Reciprocal Rank Fusion（k=60），也支持权重
    - 权重通过 run() 参数传入，可每次查询动态调整
    - 使用 Variadic 输入：YAML 中 dense_retriever 和 sparse_retriever 都可连到 joiner.documents
    """

    def __init__(
        self,
        fusion_mode: str = "weighted_sum",
        top_k: int = 30,
        sort_by_score: bool = True,
    ):
        """
        :param fusion_mode: "weighted_sum" | "rrf"
        :param top_k: 融合后最多保留条数
        :param sort_by_score: 是否按最终分数降序排列
        """
        if fusion_mode not in ("weighted_sum", "rrf"):
            raise ValueError(f"fusion_mode must be 'weighted_sum' or 'rrf', got '{fusion_mode}'")
        self.fusion_mode = fusion_mode
        self.top_k = top_k
        self.sort_by_score = sort_by_score

    @component.output_types(documents=List[Document])
    def run(
        self,
        documents: Annotated[Iterable[List[Document]], "__haystack__variadic_t"],
        dense_weight: float = 0.5,
        sparse_weight: float = 0.5,
        top_k: Optional[int] = None,
    ) -> Dict[str, Any]:
        """融合多路检索结果。

        :param documents:     variadic 输入，documents[0]=dense, documents[1]=sparse
        :param dense_weight:  dense 路权重（仅 weighted_sum 模式生效，默认 0.5）
        :param sparse_weight: sparse 路权重（仅 weighted_sum 模式生效，默认 0.5）
        :param top_k:         覆盖 init 的 top_k
        """
        lists = list(documents) if documents else []
        list_a = lists[0] if len(lists) > 0 else []
        list_b = lists[1] if len(lists) > 1 else []

        if self.fusion_mode == "weighted_sum":
            output = self._weighted_sum(list_a, list_b, dense_weight, sparse_weight)
        else:
            output = self._rrf(list_a, list_b, dense_weight, sparse_weight)

        if self.sort_by_score:
            output = sorted(output, key=lambda d: d.score or 0.0, reverse=True)

        effective_top_k = top_k or self.top_k
        output = output[:effective_top_k]

        return {"documents": output}

    # ── weighted_sum ─────────────────────────────────────────

    def _weighted_sum(
        self,
        list_a: List[Document],
        list_b: List[Document],
        w_a: float,
        w_b: float,
    ) -> List[Document]:
        """Min-max 归一化后加权求和，避免量纲差异导致一路压制另一路。"""
        norm_a = self._minmax_normalize(list_a)
        norm_b = self._minmax_normalize(list_b)

        seen: Dict[str, Document] = {}
        scores: Dict[str, float] = {}

        for doc, score in zip(list_a, norm_a):
            seen[doc.id] = doc
            scores[doc.id] = scores.get(doc.id, 0.0) + score * w_a

        for doc, score in zip(list_b, norm_b):
            seen[doc.id] = doc
            scores[doc.id] = scores.get(doc.id, 0.0) + score * w_b

        for doc_id, doc in seen.items():
            seen[doc_id] = replace(doc, score=scores[doc_id])

        return list(seen.values())

    @staticmethod
    def _minmax_normalize(docs: List[Document]) -> List[float]:
        """Min-max 归一化到 [0, 1]。单元素或全同分时保留原始值。"""
        if not docs:
            return []
        raw = [d.score if d.score is not None else 0.0 for d in docs]
        lo, hi = min(raw), max(raw)
        if hi == lo:
            return [1.0 if raw else 0.0 for _ in raw]
        return [(s - lo) / (hi - lo) for s in raw]

    # ── RRF ──────────────────────────────────────────────────

    def _rrf(
        self,
        list_a: List[Document],
        list_b: List[Document],
        w_a: float,
        w_b: float,
    ) -> List[Document]:
        """Weighted Reciprocal Rank Fusion（k=60，支持权重调偏）。"""
        k = 60
        seen: Dict[str, Document] = {}
        scores: Dict[str, float] = {}
        total_w = w_a + w_b
        wa = w_a / total_w if total_w > 0 else 0.5
        wb = w_b / total_w if total_w > 0 else 0.5

        for rank, doc in enumerate(list_a):
            seen[doc.id] = doc
            scores[doc.id] = scores.get(doc.id, 0.0) + wa / (k + rank + 1)

        for rank, doc in enumerate(list_b):
            seen[doc.id] = doc
            scores[doc.id] = scores.get(doc.id, 0.0) + wb / (k + rank + 1)

        for doc_id, doc in seen.items():
            seen[doc_id] = replace(doc, score=scores[doc_id])

        return list(seen.values())

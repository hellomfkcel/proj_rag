"""SemanticDocumentSplitter — Haystack @component for semantic-aware chunking.

Splits documents at semantic boundaries where sentence similarity drops
below a threshold. Uses the same embedding model as retrieval for
consistency (BGE-M3 via P-MODEL).

Design: v14.md §12.2, §14.4 — semantic strategy
"""

from typing import Any, Dict, List, Optional
import numpy as np

from haystack import component, Document


@component
class SemanticDocumentSplitter:
    """Split documents at semantic boundaries using embedding similarity.

    Algorithm:
    1. Split text into sentences
    2. Embed each sentence via P-MODEL
    3. Compute cosine similarity between adjacent sentences
    4. Split where similarity < breakpoint_threshold
    5. Merge short segments to meet min_chunk_size

    Key parameters:
    - breakpoint_threshold_percentile: percentile of similarities below which to split
      (lower = fewer splits, higher = more splits). Default 50 (median).
    - min_chunk_size: minimum characters per chunk (merge small segments)
    - buffer_size: number of sentences to group before checking similarity
    """

    def __init__(
        self,
        breakpoint_threshold_percentile: int = 50,
        min_chunk_size: int = 100,
        buffer_size: int = 1,
    ):
        self.breakpoint_threshold_percentile = breakpoint_threshold_percentile
        self.min_chunk_size = min_chunk_size
        self.buffer_size = buffer_size

    @component.output_types(documents=List[Document])
    def run(self, documents: List[Document]) -> Dict[str, Any]:
        from src.platform.obs.logger import get_logger
        log = get_logger(__name__)

        result_docs: List[Document] = []

        for doc in documents:
            text = doc.content
            if not text or len(text) < self.min_chunk_size:
                result_docs.append(doc)
                continue

            # ── Step 1: Split into sentences ──
            sentences = self._split_sentences(text)
            if len(sentences) <= 1:
                result_docs.append(doc)
                continue

            # ── Step 2: Embed sentences via embedding client ──
            # 自动路由到 HTTP 服务（EMBEDDING_SERVICE_URL 设置时）
            # 或本地 BGE-M3 全局单例（默认）。dense_vecs 与 _cosine_similarity 兼容。
            try:
                from src.services.embedding_client import embed_documents
                embeddings_raw, _, _ = embed_documents(
                    sentences, batch_size=64, normalize=False
                )
                embeddings = embeddings_raw
            except Exception as exc:
                log.warning("semantic_split_embed_failed", error=str(exc))
                # Fallback: return document as-is
                result_docs.append(doc)
                continue

            # ── Step 3: Compute adjacent similarities ──
            similarities = []
            for i in range(len(embeddings) - 1):
                sim = self._cosine_similarity(embeddings[i], embeddings[i + 1])
                similarities.append(sim)

            if not similarities:
                result_docs.append(doc)
                continue

            # ── Step 4: Determine breakpoints ──
            threshold = np.percentile(similarities, self.breakpoint_threshold_percentile)
            breakpoints = [0]
            for i, sim in enumerate(similarities):
                if sim < threshold:
                    breakpoints.append(i + 1)  # split after sentence i
            breakpoints.append(len(sentences))

            # ── Step 5: Build chunks (merge short ones) ──
            chunks: List[str] = []
            current: List[str] = []
            current_len = 0

            for bp_idx in range(len(breakpoints) - 1):
                start = breakpoints[bp_idx]
                end = breakpoints[bp_idx + 1]
                segment = " ".join(sentences[start:end])
                seg_len = len(segment)

                if current_len + seg_len < self.min_chunk_size:
                    current.append(segment)
                    current_len += seg_len
                else:
                    if current:
                        chunks.append(" ".join(current))
                    current = [segment]
                    current_len = seg_len

            if current:
                chunks.append(" ".join(current))

            # ── Step 6: Create Document objects ──
            for i, chunk_text in enumerate(chunks):
                chunk = Document(
                    content=chunk_text,
                    meta={
                        **doc.meta,
                        "chunk_index": i,
                        "chunk_count": len(chunks),
                        "split_strategy": "semantic",
                    },
                )
                result_docs.append(chunk)

        return {"documents": result_docs}

    def _split_sentences(self, text: str) -> List[str]:
        """Split text into sentences (Chinese + English aware)."""
        import re
        # Split on sentence-ending punctuation for Chinese and English
        # Chinese: 。！？\n
        # English: . ! ?
        pattern = r'(?<=[。！？\.\!\?\n])\s*'
        sentences = re.split(pattern, text)
        return [s.strip() for s in sentences if s.strip()]

    def _cosine_similarity(self, a: List[float], b: List[float]) -> float:
        """Cosine similarity between two vectors."""
        a_arr = np.array(a)
        b_arr = np.array(b)
        dot = np.dot(a_arr, b_arr)
        norm_a = np.linalg.norm(a_arr)
        norm_b = np.linalg.norm(b_arr)
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return float(dot / (norm_a * norm_b))

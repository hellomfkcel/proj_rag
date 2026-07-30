"""HierarchicalMerger — Haystack @component for parent-child chunk merging.

When documents are split into small chunks for embedding but the LLM needs
larger context, this component merges sibling chunks back by document_id,
reconstructing the parent document context around each retrieved chunk.

Design reference: RAG系统设计v14.md §15.5
"""

from typing import Any, Dict, List, Optional
from haystack import component, Document
from src.platform.obs.logger import get_logger

log = get_logger(__name__)


@component
class HierarchicalMerger:
    """Merge small sibling chunks back into parent context windows.

    For each retrieved chunk, looks up neighboring chunks from the same
    document and concatenates their content to provide richer context
    for the LLM while keeping the embedding search precise.

    When merge is not possible (single-chunk docs, missing metadata),
    returns documents unchanged.
    """

    def __init__(
        self,
        window_size: int = 3,       # chunks before + after the target
        max_total_chunks: int = 10,  # max chunks per merged document
        dedup_by: str = "content",   # deduplicate by content or id
    ):
        self.window_size = window_size
        self.max_total_chunks = max_total_chunks
        self.dedup_by = dedup_by

    @component.output_types(documents=List[Document])
    def run(self, documents: List[Document]) -> Dict[str, Any]:
        """Merge chunk siblings into parent context.

        For each retrieved document, look up neighbor chunks from the same
        parent document and build a merged context.
        """
        if len(documents) <= 1:
            return {"documents": documents}

        from pymilvus import connections, Collection

        # Group chunks by document_id
        doc_groups: Dict[str, List[Document]] = {}
        for d in documents:
            doc_id = d.meta.get("document_id", "")
            if not doc_id:
                continue
            if doc_id not in doc_groups:
                doc_groups[doc_id] = []
            doc_groups[doc_id].append(d)

        if not doc_groups:
            return {"documents": documents}

        try:
            from src.config import Settings
            s = Settings()
            connections.connect("merger", host=s.milvus_host, port=str(s.milvus_port))
            col = Collection("rag_documents")
            col.load()

            merged: List[Document] = []
            seen_content: set = set()

            for doc_id, chunks in doc_groups.items():
                # Get all chunk IDs for this document from Milvus
                try:
                    results = col.query(
                        expr=f'document_id == "{doc_id}"',
                        output_fields=["content", "document_id"],
                        limit=self.max_total_chunks * 2,
                    )
                except Exception:
                    # Can't query — use original chunks as-is
                    for c in chunks:
                        if c.content not in seen_content:
                            merged.append(c)
                            seen_content.add(c.content)
                    continue

                # For each retrieved chunk, find its neighbors
                for chunk in chunks:
                    chunk_content = chunk.content
                    if chunk_content in seen_content:
                        continue
                    seen_content.add(chunk_content)

                    # Find this chunk's position in the full doc
                    chunk_idx = -1
                    for idx, r in enumerate(results):
                        if r.get("id") == chunk.id or r.get("content", "")[:100] == chunk_content[:100]:
                            chunk_idx = idx
                            break

                    # Build window around this chunk
                    start = max(0, chunk_idx - self.window_size)
                    end = min(len(results), chunk_idx + self.window_size + 1)
                    window = results[start:end]

                    # Merge window content
                    merged_content_parts = []
                    for w in window:
                        wc = w.get("content", "")
                        if wc and wc not in seen_content:
                            merged_content_parts.append(wc)
                            seen_content.add(wc)

                    if merged_content_parts:
                        merged_doc = Document(
                            content="\n\n".join(merged_content_parts[:self.max_total_chunks]),
                            meta={
                                **chunk.meta,
                                "merged_from": len(merged_content_parts),
                                "window_start": start,
                                "window_end": end,
                            },
                        )
                        merged_doc.id = chunk.id
                        merged.append(merged_doc)
                    elif chunk_content not in [m.content for m in merged]:
                        merged.append(chunk)

            connections.disconnect("merger")
            log.debug("hierarchical_merge_complete",
                     input_count=len(documents),
                     output_count=len(merged))
            return {"documents": merged}

        except Exception as exc:
            log.warning("hierarchical_merge_failed", error=str(exc))
            return {"documents": documents}

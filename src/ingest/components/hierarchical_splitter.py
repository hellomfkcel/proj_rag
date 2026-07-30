"""HierarchicalDocumentSplitter — Haystack @component for multi-level chunking.

Creates parent-child relationships between chunks at different granularities:
- Level 0 (parent): coarse chunks for overview/summary
- Level 1 (child): fine chunks for detailed retrieval

Each child chunk has a `parent_id` referencing its parent.
This enables the HierarchicalMerger on the query side to reconstruct context.

Design: v14.md §12.2, §14.4 — hierarchical strategy
"""

import uuid
from typing import Any, Dict, List, Optional

from haystack import component, Document
from haystack.components.preprocessors import DocumentSplitter


@component
class HierarchicalDocumentSplitter:
    """Multi-level document splitter with parent-child relationships.

    Pipeline:
    1. Split document into parent chunks (large, e.g. 1024 chars)
    2. Split each parent into child chunks (small, e.g. 256 chars)
    3. Each child gets parent_id → parent chunk
    4. Both parents and children are output (parents for merging, children for retrieval)
    """

    def __init__(
        self,
        parent_split_length: int = 1024,
        parent_split_overlap: int = 128,
        child_split_length: int = 256,
        child_split_overlap: int = 32,
        parent_split_by: str = "sentence",
        child_split_by: str = "sentence",
    ):
        self.parent_split_length = parent_split_length
        self.parent_split_overlap = parent_split_overlap
        self.child_split_length = child_split_length
        self.child_split_overlap = child_split_overlap
        self.parent_split_by = parent_split_by
        self.child_split_by = child_split_by

    @component.output_types(documents=List[Document], parent_documents=List[Document])
    def run(self, documents: List[Document]) -> Dict[str, Any]:
        from src.platform.obs.logger import get_logger
        log = get_logger(__name__)

        all_children: List[Document] = []
        all_parents: List[Document] = []

        for doc in documents:
            text = doc.content
            if not text:
                continue

            # ── Level 0: Create parent chunks (coarse) ──
            parent_splitter = DocumentSplitter(
                split_by=self.parent_split_by,
                split_length=self.parent_split_length,
                split_overlap=self.parent_split_overlap,
            )
            parent_docs = parent_splitter.run(
                documents=[Document(content=text, meta=doc.meta)]
            )["documents"]

            for p_idx, parent in enumerate(parent_docs):
                parent_id = f"{doc.id or uuid.uuid4().hex[:12]}_p{p_idx}"
                parent.meta["level"] = 0
                parent.meta["parent_id"] = None
                parent.meta["chunk_index"] = p_idx
                parent.meta["split_strategy"] = "hierarchical"
                # Store identifier for child reference
                parent.meta["_hier_id"] = parent_id
                all_parents.append(parent)

                # ── Level 1: Create child chunks from each parent ──
                if len(parent.content) > self.child_split_length:
                    child_splitter = DocumentSplitter(
                        split_by=self.child_split_by,
                        split_length=self.child_split_length,
                        split_overlap=self.child_split_overlap,
                    )
                    child_docs = child_splitter.run(
                        documents=[Document(content=parent.content, meta=parent.meta)]
                    )["documents"]

                    for c_idx, child in enumerate(child_docs):
                        child.meta["level"] = 1
                        child.meta["parent_id"] = parent_id
                        child.meta["chunk_index"] = c_idx
                        child.meta["split_strategy"] = "hierarchical"
                        # Inherit document-level metadata
                        child.meta["document_id"] = doc.meta.get("document_id", "")
                        child.meta["kb_id"] = doc.meta.get("kb_id", "")
                        child.meta["tenant_id"] = doc.meta.get("tenant_id", "")
                        all_children.append(child)
                else:
                    # Parent is small enough to be its own child
                    parent.meta["level"] = 1
                    parent.meta["parent_id"] = parent_id
                    all_children.append(parent)

        log.info("hierarchical_split_complete",
                 doc_count=len(documents),
                 parent_count=len(all_parents),
                 child_count=len(all_children))

        return {
            "documents": all_children,         # goes to embedder → writer
            "parent_documents": all_parents,    # also written for hierarchical retrieval
        }

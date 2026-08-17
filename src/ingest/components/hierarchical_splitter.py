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
from langchain_text_splitters import RecursiveCharacterTextSplitter

# CJK-aware separators matching RecursiveDocumentSplitter
_CJK_SEPARATORS = ["\n\n", "\n", "。", "！", "？", "；", "，", "、", " ", ""]


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
        parent_split_by: str = "word",
        child_split_by: str = "word",
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

        # documents 输出包含两级 chunk（父块 + 子块），父块与子块同批嵌入/写库/盖戳
        # （§14.5.5：父块不因"是上层"而被跳过，与子块同源同新）。
        all_chunks: List[Document] = []   # → documents（level 0 + level 1）
        all_parents: List[Document] = []  # → parent_documents（仅 level 0，向后兼容）

        for doc in documents:
            text = doc.content
            if not text:
                continue

            # ── Level 0: Create parent chunks (coarse) ──
            parent_splitter = RecursiveCharacterTextSplitter(
                chunk_size=self.parent_split_length,
                chunk_overlap=self.parent_split_overlap,
                separators=_CJK_SEPARATORS,
                keep_separator=True,
            )
            parent_texts = parent_splitter.split_text(text)

            for p_idx, p_text in enumerate(parent_texts):
                parent_id = f"{doc.id or uuid.uuid4().hex[:12]}_p{p_idx}"
                # 显式确定性 id（§14.3：(mount_id, chunk_index) 唯一约束）。
                # Haystack 默认 id 是内容哈希——重叠切分产生的相同内容子块会撞主键。
                parent = Document(content=p_text, meta=dict(doc.meta), id=parent_id)
                parent.meta["level"] = 0
                parent.meta["parent_id"] = None
                parent.meta["chunk_index"] = p_idx
                parent.meta["split_strategy"] = "hierarchical"
                parent.meta["_hier_id"] = parent_id

                # ── Level 1: Create child chunks from each parent ──
                if len(parent.content) > self.child_split_length:
                    # 父块较大 → 父块入 level 0，子块入 level 1
                    all_parents.append(parent)
                    all_chunks.append(parent)
                    child_splitter = RecursiveCharacterTextSplitter(
                        chunk_size=self.child_split_length,
                        chunk_overlap=self.child_split_overlap,
                        separators=_CJK_SEPARATORS,
                        keep_separator=True,
                    )
                    child_texts = child_splitter.split_text(parent.content)

                    for c_idx, c_text in enumerate(child_texts):
                        child = Document(
                            content=c_text,
                            meta=dict(parent.meta),
                            id=f"{parent_id}_c{c_idx}",
                        )
                        child.meta["level"] = 1
                        child.meta["parent_id"] = parent_id
                        child.meta["chunk_index"] = c_idx
                        child.meta["split_strategy"] = "hierarchical"
                        child.meta["document_id"] = doc.meta.get("document_id", "")
                        child.meta["kb_id"] = doc.meta.get("kb_id", "")
                        child.meta["tenant_id"] = doc.meta.get("tenant_id", "")
                        all_chunks.append(child)
                else:
                    # 父块足够小 → 自身即 chunk（level 1，parent of itself），只存一次
                    parent.meta["level"] = 1
                    parent.meta["parent_id"] = parent_id
                    parent.meta["chunk_index"] = 0
                    all_chunks.append(parent)

        log.info("hierarchical_split_complete",
                 doc_count=len(documents),
                 parent_count=len(all_parents),
                 child_count=len(all_chunks) - len(all_parents))

        return {
            "documents": all_chunks,          # 父块 + 子块 → embedder → writer
            "parent_documents": all_parents,   # 仅 level 0（向后兼容）
        }

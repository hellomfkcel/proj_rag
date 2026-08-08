"""RecursiveDocumentSplitter — Haystack @component using langchain's RecursiveCharacterTextSplitter.

Replaces Haystack's DocumentSplitter which uses NLTK (English-centric) for sentence/word
tokenization and fails to split CJK text properly. RecursiveCharacterTextSplitter uses a
priority-ordered separator list with character-level fallback, producing uniform chunks
for all languages.

Separator priority (CJK-aware):
    "\n\n" → "\n" → "。" → "！" → "？" → "；" → "，" → "、" → " " → ""
"""

from __future__ import annotations

from typing import Any, Dict, List

from haystack import component, Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

# CJK-aware separator list: structural → sentence → clause → word → character
_CJK_SEPARATORS = ["\n\n", "\n", "。", "！", "？", "；", "，", "、", " ", ""]


@component
class RecursiveDocumentSplitter:
    """Split documents using RecursiveCharacterTextSplitter with CJK-aware separators.

    Drop-in replacement for haystack's DocumentSplitter. Produces uniform chunks
    respecting the chunk_size parameter regardless of input language.
    """

    def __init__(
        self,
        chunk_size: int = 256,
        chunk_overlap: int = 32,
    ):
        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap
        self._splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            separators=_CJK_SEPARATORS,
            keep_separator=True,
        )

    @component.output_types(documents=List[Document])
    def run(self, documents: List[Document]) -> Dict[str, Any]:
        all_chunks: List[Document] = []

        for doc in documents:
            text = doc.content
            if not text:
                continue

            splits = self._splitter.split_text(text)
            for i, chunk_text in enumerate(splits):
                chunk_meta = dict(doc.meta)
                chunk_meta["chunk_index"] = i
                chunk_meta["chunk_count"] = len(splits)
                all_chunks.append(Document(content=chunk_text, meta=chunk_meta))

        return {"documents": all_chunks}

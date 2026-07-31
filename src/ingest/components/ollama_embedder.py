"""OllamaDocumentEmbedder — Haystack @component for ingestion side.

Delegates to P-MODEL invoke_embedding (Ollama qwen3-embedding:0.6b).
Replaces FastembedDocumentEmbedder which doesn't support our model.
"""

from dataclasses import replace
from typing import Any, Dict, List
from haystack import component, Document


@component
class OllamaDocumentEmbedder:
    """Document embedder that calls Ollama via P-MODEL."""

    def __init__(self, model: str = "qwen3-embedding:0.6b"):
        # Model name is normally provided by Pipeline YAML via ${EMBEDDING_MODEL}
        # env var placeholder; this default is a fallback for direct instantiation.
        self.model = model

    @component.output_types(documents=List[Document])
    def run(self, documents: List[Document]) -> Dict[str, Any]:
        from src.platform.model.registry import invoke_embedding

        texts = [doc.content for doc in documents]
        embeddings = invoke_embedding(texts, mode="document")

        # 使用 dataclasses.replace() 替代直接属性赋值（doc.embedding = emb）。
        # 直接 mutation 可能导致共享 Document 实例的并行管道分支出现未预期行为。
        # 见: https://docs.haystack.deepset.ai/docs/custom-components#requirements
        for i, (doc, emb) in enumerate(zip(documents, embeddings)):
            documents[i] = replace(doc, embedding=emb)

        return {"documents": documents}

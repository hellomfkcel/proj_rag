"""OllamaDocumentEmbedder — Haystack @component for ingestion side.

Delegates to P-MODEL invoke_embedding (Ollama qwen3-embedding:0.6b).
Replaces FastembedDocumentEmbedder which doesn't support our model.
"""

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

        for doc, emb in zip(documents, embeddings):
            doc.embedding = emb

        return {"documents": documents}

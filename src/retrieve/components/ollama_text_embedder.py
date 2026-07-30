"""OllamaTextEmbedder — Haystack @component for query side.

Delegates to P-MODEL invoke_embedding (Ollama qwen3-embedding:0.6b).
Replaces FastembedTextEmbedder which doesn't support our model.
"""

from typing import Any, Dict, List
from haystack import component


@component
class OllamaTextEmbedder:
    """Text embedder that calls Ollama via P-MODEL."""

    def __init__(self, model: str = "qwen3-embedding:0.6b"):
        # Model name is normally provided by Pipeline YAML via ${EMBEDDING_MODEL}
        # env var placeholder; this default is a fallback for direct instantiation.
        self.model = model

    @component.output_types(embedding=List[float])
    def run(self, text: str) -> Dict[str, Any]:
        from src.platform.model.registry import invoke_embedding

        embeddings = invoke_embedding([text], mode="query")
        return {"embedding": embeddings[0]}

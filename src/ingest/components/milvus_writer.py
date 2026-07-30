"""MilvusDocumentStoreWriter — Haystack @component wrapping direct PyMilvus writes.

milvus-haystack 0.0.18 is incompatible with pymilvus 2.5.6 (Collection.indices API changed).
This component bypasses the broken write_documents() and uses pymilvus directly.
"""

from typing import Any, Dict, List

from haystack import Document, component
from pymilvus import MilvusClient


# BGE-M3 embedding dimensions (dense=1024, sparse=250002)
DENSE_DIM = 1024


@component
class MilvusDocumentStoreWriter:
    """Write Haystack Documents to Milvus via MilvusClient API.

    Uses the recommended MilvusClient API (pymilvus >= 2.6) instead of the
    deprecated ORM-style Collection/connections API.
    """

    def __init__(
        self,
        collection_name: str = "rag_documents",
        milvus_host: str = "localhost",
        milvus_port: str = "19530",
    ):
        self._collection_name = collection_name
        self._milvus_host = milvus_host
        self._milvus_port = str(milvus_port)
        self._client: Any = None

    def _get_client(self) -> MilvusClient:
        if self._client is None:
            self._client = MilvusClient(
                uri=f"http://{self._milvus_host}:{self._milvus_port}"
            )
        return self._client

    @component.output_types(documents=List[Document])
    def run(self, documents: List[Document]):
        client = self._get_client()

        # Prepare data rows for insertion
        data: List[Dict[str, Any]] = []
        for doc in documents:
            dense_embedding = doc.embedding
            sparse_embedding = doc.sparse_embedding if doc.sparse_embedding else {}

            # Convert sparse_embedding (dict {token_id: weight}) to SparseRow
            if sparse_embedding:
                sparse_row = {int(k): float(v) for k, v in sparse_embedding.items()}
            else:
                sparse_row = {}

            row = {
                "id": doc.id,
                "content": doc.content,
                "vector": dense_embedding if dense_embedding else [],
                "sparse_vector": sparse_row,
                "document_id": doc.meta.get("document_id", ""),
                "mount_id": doc.meta.get("mount_id", ""),
                "kb_id": doc.meta.get("kb_id", ""),
                "tenant_id": doc.meta.get("tenant_id", ""),
                "allow_stamps": doc.meta.get("allow_stamps", []),  # Python list → Milvus JSON array
                "deny_stamps": doc.meta.get("deny_stamps", []),    # Python list → Milvus JSON array
                "vis_version": int(doc.meta.get("vis_version", 0)),
                "retrievable": bool(doc.meta.get("retrievable", True)),
            }
            data.append(row)

        if data:
            try:
                client.insert(self._collection_name, data)
            except Exception as exc:
                # Fallback: try upsert (MilvusClient handles flush internally)
                try:
                    client.upsert(self._collection_name, data)
                except Exception:
                    # Re-raise original error for task retry
                    raise exc

        return {"documents": documents}

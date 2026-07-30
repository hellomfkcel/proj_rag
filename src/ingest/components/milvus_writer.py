"""MilvusDocumentStoreWriter — Haystack @component wrapping direct PyMilvus writes.

milvus-haystack 0.0.18 is incompatible with pymilvus 2.5.6 (Collection.indices API changed).
This component bypasses the broken write_documents() and uses pymilvus directly.
"""

from typing import Any, Dict, List

from haystack import Document, component
from pymilvus import Collection, connections, DataType


COLLECTION_SCHEMA = {
    "id": DataType.VARCHAR,
    "content": DataType.VARCHAR,
    "vector": DataType.FLOAT_VECTOR,
    "sparse_vector": DataType.SPARSE_FLOAT_VECTOR,
}

# BGE-M3 embedding dimensions (dense=1024, sparse=250002)
DENSE_DIM = 1024


@component
class MilvusDocumentStoreWriter:
    """Write Haystack Documents to Milvus via direct pymilvus API.

    Bypasses milvus-haystack's broken write_documents() in pymilvus 2.5.x.
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
        self._collection: Any = None

    def _get_collection(self) -> Collection:
        if self._collection is None:
            # Connect to Milvus
            connections.connect(
                alias="default",
                host=self._milvus_host,
                port=self._milvus_port,
            )
            self._collection = Collection(self._collection_name)
        return self._collection

    @component.output_types(documents=List[Document])
    def run(self, documents: List[Document]):
        col = self._get_collection()

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
                col.insert(data)
                col.flush()
            except Exception as exc:
                # Fallback: try upsert-style insert
                try:
                    col.upsert(data)
                    col.flush()
                except Exception:
                    # Re-raise original error for task retry
                    raise exc

        return {"documents": documents}

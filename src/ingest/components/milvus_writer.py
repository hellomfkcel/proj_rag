"""MilvusDocumentStoreWriter — Haystack @component wrapping direct PyMilvus writes.

milvus-haystack 0.0.18 is incompatible with pymilvus 2.5.6 (Collection.indices API changed).
This component bypasses the broken write_documents() and uses pymilvus directly.
"""

from typing import Any, Dict, List

from haystack import Document, component
from pymilvus import MilvusClient, DataType, CollectionSchema, FieldSchema


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

    def _ensure_collection(self, client: MilvusClient) -> None:
        """确保 collection 存在，不存在则创建（含完整 schema）。

        设计依据：docs/RAG系统设计v14.md §14.3 摄入 Pipeline 结构
                  + §14.5.1 chunk payload 字段契约

        使用显式 CollectionSchema + FieldSchema 创建 collection，
        原因：MilvusClient.create_collection() 简捷 API 存在两个问题——
        (a) auto_id=False 默认创建 INT64 主键（不是 VARCHAR），
            导致 DataNotMatchException: {id} field should be a int64；
        (b) enable_sparse_vector=True 仅启用 collection 级功能标记，
            不创建 sparse_vector 字段，导致 MilvusException:
            fieldName(sparse_vector) not found。

        字段契约：
        - id (VARCHAR, PK, max_length=256) — chunk 唯一标识
        - vector (FLOAT_VECTOR, 1024d, IP) — BGE-M3 稠密向量
        - sparse_vector (SPARSE_FLOAT_VECTOR, IP) — BGE-M3 稀疏向量
        - 动态字段 (enable_dynamic_field=True):
          content, document_id, mount_id, kb_id, tenant_id,
          allow_stamps, deny_stamps, vis_version, retrievable
        """
        if client.has_collection(self._collection_name):
            # 验证 collection schema 是否与当前代码预期一致
            desc = client.describe_collection(self._collection_name)
            pk_field = None
            has_sparse = False
            for f in desc.get("fields", []):
                if f.get("is_primary"):
                    pk_field = f
                if f.get("name") == "sparse_vector":
                    has_sparse = True
            if pk_field and pk_field.get("data_type", "") == "Int64":
                # Schema 不兼容：旧 collection 使用了 INT64 主键 → 重建
                client.drop_collection(self._collection_name)
            elif not has_sparse:
                # Schema 不兼容：缺少 sparse_vector 显式字段 → 重建
                client.drop_collection(self._collection_name)
            else:
                # Schema 兼容，无需重建。确保 collection 已加载到内存——
                # Milvus 重启后 collection 会回到 NotLoad 状态。
                # load_collection 已加载时是快速空操作（幂等）。
                client.load_collection(self._collection_name)
                return

        # 显式 schema：确保 id 为 VARCHAR，sparse_vector 为 SPARSE_FLOAT_VECTOR
        fields = [
            FieldSchema(
                name="id", dtype=DataType.VARCHAR,
                is_primary=True, max_length=256,
            ),
            FieldSchema(
                name="vector", dtype=DataType.FLOAT_VECTOR,
                dim=DENSE_DIM,
            ),
            FieldSchema(
                name="sparse_vector", dtype=DataType.SPARSE_FLOAT_VECTOR,
            ),
        ]
        schema = CollectionSchema(
            fields, enable_dynamic_field=True,
        )
        client.create_collection(
            collection_name=self._collection_name,
            schema=schema,
            metric_type="IP",
        )

        # 创建索引（pymilvus 2.6.x 使用 prepare_index_params API）
        index_params = client.prepare_index_params()
        index_params.add_index(
            field_name="vector",
            index_type="HNSW",
            metric_type="IP",
            params={"M": 16, "efConstruction": 200},
        )
        index_params.add_index(
            field_name="sparse_vector",
            index_type="SPARSE_INVERTED_INDEX",
            metric_type="IP",
        )
        client.create_index(self._collection_name, index_params)

        # 加载 collection 到内存（MilvusClient API 不会自动 load）。
        # load_collection 在 collection 已加载时是快速空操作（幂等）。
        # 必须在 insert 之前或之后调用——insert 不需要 load，但后续的
        # query/search 操作（盖戳管道、检索管道）必须 load。
        client.load_collection(self._collection_name)

    @component.output_types(documents=List[Document])
    def run(self, documents: List[Document]):
        client = self._get_client()

        # 确保 collection 存在（幂等，首次自动创建）
        self._ensure_collection(client)

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

            # 确保 collection 已加载到内存，以便后续盖戳管道和检索管道
            # 能正常执行 query/search 操作。
            # load_collection 已加载时是快速空操作（幂等）。
            client.load_collection(self._collection_name)

        return {"documents": documents}

"""MilvusDocumentStoreWriter — Haystack @component wrapping direct PyMilvus writes.

milvus-haystack 0.0.18 is incompatible with pymilvus 2.5.6 (Collection.indices API changed).
This component bypasses the broken write_documents() and uses pymilvus directly.
"""

from typing import Any, Dict, List, Set

from haystack import Document, component
from pymilvus import MilvusClient, DataType, CollectionSchema, FieldSchema


# BGE-M3 embedding dimensions (dense=1024, sparse=250002)
DENSE_DIM = 1024

# Milvus 动态字段单值最大 65536 字节，content 超过 60000 字符时截断
MAX_CONTENT_LENGTH = 60000

# 已告警过的标量索引失败字段（进程级去重，避免每次 ingest 重复告警）
_WARNED_INDEX_FIELDS: Set[str] = set()

# 显式 schema 字段（含全部 L1 prefilter 过滤字段 + 层级元数据）。
# content 保持动态字段（保留截断安全网，规避显式 VARCHAR 字节上限不确定性）。
REQUIRED_EXPLICIT_FIELDS: Set[str] = {
    "id", "vector", "sparse_vector",
    "document_id", "mount_id", "kb_id", "tenant_id",
    "allow_stamps", "deny_stamps", "vis_version", "retrievable",
    "parent_id", "level", "chunk_index",
}


def build_schema() -> CollectionSchema:
    """构建 rag_documents 集合 schema（显式字段 + 动态字段兜底）。

    字段契约：docs/RAG系统设计v14.md §14.5.1 chunk payload。
    过滤热字段显式化后可建 INVERTED 标量索引加速 L1 prefilter；
    enable_dynamic_field=True 保留其余 meta（如 content）动态写入。
    """
    fields = [
        FieldSchema(name="id", dtype=DataType.VARCHAR, is_primary=True, max_length=256),
        FieldSchema(name="vector", dtype=DataType.FLOAT_VECTOR, dim=DENSE_DIM),
        FieldSchema(name="sparse_vector", dtype=DataType.SPARSE_FLOAT_VECTOR),
        # ── L1 prefilter 过滤字段（显式化，可建索引）──
        FieldSchema(name="document_id", dtype=DataType.VARCHAR, max_length=256),
        FieldSchema(name="mount_id", dtype=DataType.VARCHAR, max_length=256),
        FieldSchema(name="kb_id", dtype=DataType.VARCHAR, max_length=256),
        FieldSchema(name="tenant_id", dtype=DataType.VARCHAR, max_length=64),
        FieldSchema(name="allow_stamps", dtype=DataType.JSON),
        FieldSchema(name="deny_stamps", dtype=DataType.JSON),
        FieldSchema(name="vis_version", dtype=DataType.INT64),
        FieldSchema(name="retrievable", dtype=DataType.BOOL),
        # ── 层级分块元数据（§12.2/§14.3，merger 排序/开窗用）──
        FieldSchema(name="parent_id", dtype=DataType.VARCHAR, max_length=256),
        FieldSchema(name="level", dtype=DataType.INT64),
        FieldSchema(name="chunk_index", dtype=DataType.INT64),
    ]
    return CollectionSchema(fields, enable_dynamic_field=True)


def ensure_indices(client: MilvusClient, collection_name: str) -> None:
    """幂等确保索引存在——并发安全的索引补齐。

    并发场景：Writer A 创建 collection + 索引，Writer B 在索引建成前到达。
    Writer B 看到 collection 已存在但索引缺失 → load_collection 报 code=700。
    此函数用 describe_index 检查每个字段的索引状态并幂等补齐缺失的索引，
    避免竞态导致的 ingest 失败和重试。

    索引策略：
    - vector:        HNSW (IP) —— 稠密 ANN
    - sparse_vector: SPARSE_INVERTED_INDEX (IP) —— 稀疏 ANN
    - 过滤热字段:     INVERTED 标量索引 —— 加速 L1 六条件 prefilter
      （json_contains 由 allow_stamps/deny_stamps 的 JSON INVERTED 索引加速）
    """
    VECTOR_FIELDS = [
        ("vector", "HNSW", "IP", {"M": 16, "efConstruction": 200}),
        ("sparse_vector", "SPARSE_INVERTED_INDEX", "IP", {}),
    ]
    SCALAR_FIELDS = [
        "tenant_id", "kb_id", "document_id", "mount_id", "parent_id",
        "vis_version", "retrievable",
        "allow_stamps", "deny_stamps",
    ]

    for field_name, index_type, metric_type, params in VECTOR_FIELDS:
        try:
            existing = client.describe_index(collection_name, field_name)
            if existing and existing.get("index_name"):
                continue  # 索引已存在，跳过
        except Exception:
            pass  # 索引不存在，需要创建

        index_params = client.prepare_index_params()
        index_params.add_index(
            field_name=field_name,
            index_type=index_type,
            metric_type=metric_type,
            params=params,
        )
        client.create_index(collection_name, index_params)

    for field_name in SCALAR_FIELDS:
        try:
            existing = client.describe_index(collection_name, field_name)
            if existing and existing.get("index_name"):
                continue
        except Exception:
            pass
        try:
            index_params = client.prepare_index_params()
            index_params.add_index(
                field_name=field_name,
                index_type="INVERTED",
            )
            client.create_index(collection_name, index_params)
        except Exception as exc:
            # Milvus 2.4 对 JSON 字段（allow_stamps/deny_stamps）不支持 INVERTED 索引。
            # 索引缺失只影响过滤性能，不影响权限语义 —— 失败不阻断 ingest。
            # 已实测确认（2026-08-16），日志仅告警一次避免刷屏。
            if field_name not in _WARNED_INDEX_FIELDS:
                _WARNED_INDEX_FIELDS.add(field_name)
                import logging
                logging.getLogger(__name__).warning(
                    "scalar_index_create_failed",
                    extra={"field": field_name, "error": str(exc)[:200]},
                )


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

        schema 演进：filter 热字段（tenant_id/kb_id/allow_stamps/...）已显式化
        以便建立 INVERTED 标量索引。旧动态-only 集合缺少这些显式字段 → 自动重建。
        """
        if client.has_collection(self._collection_name):
            # 验证 collection schema 是否与当前代码预期一致
            desc = client.describe_collection(self._collection_name)
            present = {f.get("name") for f in desc.get("fields", [])}
            pk_field = next((f for f in desc.get("fields", []) if f.get("is_primary")), None)
            pk_type = pk_field.get("type", pk_field.get("data_type")) if pk_field else None
            if pk_type in (DataType.INT64, "Int64", 5):
                # Schema 不兼容：旧 collection 使用了 INT64 主键 → 重建
                client.drop_collection(self._collection_name)
            elif not present.issuperset(REQUIRED_EXPLICIT_FIELDS):
                # Schema 不兼容：缺少显式过滤字段/层级字段 → 重建
                missing = REQUIRED_EXPLICIT_FIELDS - present
                import logging
                logging.getLogger(__name__).warning(
                    "collection_schema_rebuild",
                    extra={"missing_fields": sorted(missing)},
                )
                client.drop_collection(self._collection_name)
            else:
                # Schema 兼容，无需重建。
                # 确保索引存在——并发场景下 collection 可能是其他 writer 刚创建的，
                # 索引可能尚未建成。用 describe_index 检查并幂等补齐。
                ensure_indices(client, self._collection_name)
                # 确保 collection 已加载到内存——
                # Milvus 重启后 collection 会回到 NotLoad 状态。
                # load_collection 已加载时是快速空操作（幂等）。
                client.load_collection(self._collection_name)
                return

        # 显式 schema：确保 id 为 VARCHAR，sparse_vector 为 SPARSE_FLOAT_VECTOR
        # 过滤字段显式化 + 动态字段兜底（content 等其余 meta）
        client.create_collection(
            collection_name=self._collection_name,
            schema=build_schema(),
            metric_type="IP",
        )

        # 创建索引 + 加载 collection（复用 ensure_indices，幂等安全）
        ensure_indices(client, self._collection_name)

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
                # 层级元数据（非层级策略缺省值，merger 按 level/chunk_index 排序开窗）
                "parent_id": doc.meta.get("parent_id") or "",
                "level": int(doc.meta.get("level", 0)),
                "chunk_index": int(doc.meta.get("chunk_index", 0)),
            }

            # 安全网：Milvus 动态字段上限 65536 字节
            content = row.get("content", "")
            if len(content) > MAX_CONTENT_LENGTH:
                original_len = len(content)
                row["content"] = (
                    content[:MAX_CONTENT_LENGTH]
                    + f"\n\n[...文档过长已截断，原文: {original_len}字符]"
                )
                import logging
                _log = logging.getLogger("MilvusDocumentStoreWriter")
                _log.warning(
                    "content_truncated_for_milvus",
                    extra={
                        "document_id": row.get("document_id"),
                        "mount_id": row.get("mount_id"),
                        "chunk_id": row.get("id"),
                        "original_length": original_len,
                        "truncated_length": MAX_CONTENT_LENGTH,
                    },
                )

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

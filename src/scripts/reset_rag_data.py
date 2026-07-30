"""清理知识库数据 — 保留 KB 定义和配置，清空文档/向量/挂载/执行记录。

运行方式：
  conda activate rag_dev_v14
  python -m src.scripts.reset_rag_data
"""

import sys
sys.path = [p for p in sys.path if 'ros' not in p]

import asyncio
import asyncpg
from pymilvus import connections, Collection, utility

from src.config import Settings
from src.platform.obs.logger import get_logger

log = get_logger(__name__)


def _dsn():
    return Settings().database_url.replace("postgresql+asyncpg://", "postgresql://")


async def reset_postgres():
    """清空业务数据，保留 knowledge_bases / retrieval_configs / chunking_configs / model_registry。"""
    conn = await asyncpg.connect(_dsn())
    try:
        tables_to_clear = [
            "outbox",
            "conversation_turns",
            "conversations",
            "ingest_executions",
            "document_kb_mounts",
            "documents",
            "resource_registry",
            "mount_registry",
        ]
        for tbl in tables_to_clear:
            try:
                await conn.execute(f"DELETE FROM {tbl}")
                log.info("postgres_table_cleared", table=tbl)
            except Exception as e:
                log.warning("postgres_table_clear_failed", table=tbl, error=str(e))

        # 重新注册 seed KB（保留 KB 定义）
        kb_rows = await conn.fetch("SELECT id, owner_id FROM knowledge_bases")
        for row in kb_rows:
            try:
                await conn.execute(
                    "INSERT INTO resource_registry (resource_type, resource_id, owner) "
                    "VALUES ('kb', $1, $2) ON CONFLICT (resource_type, resource_id) DO UPDATE SET retired=false",
                    str(row["id"]), f"user:{row['owner_id']}",
                )
            except Exception:
                pass
        log.info("resource_registry_reseeded", kb_count=len(kb_rows))
    finally:
        await conn.close()


def reset_milvus():
    """删除并重建 rag_documents 集合。"""
    s = Settings()
    connections.connect("default", host=s.milvus_host, port=str(s.milvus_port))

    if utility.has_collection("rag_documents"):
        utility.drop_collection("rag_documents")
        log.info("milvus_collection_dropped")

    # 重建（与 MilvusWriter 中的 schema 一致）
    from pymilvus import CollectionSchema, FieldSchema, DataType

    pk = FieldSchema(name="id", dtype=DataType.VARCHAR, is_primary=True, max_length=256)
    content = FieldSchema(name="content", dtype=DataType.VARCHAR, max_length=65535)
    vector = FieldSchema(name="vector", dtype=DataType.FLOAT_VECTOR, dim=1024)
    sparse = FieldSchema(name="sparse_vector", dtype=DataType.SPARSE_FLOAT_VECTOR)
    doc_id = FieldSchema(name="document_id", dtype=DataType.VARCHAR, max_length=256)
    mount_id = FieldSchema(name="mount_id", dtype=DataType.VARCHAR, max_length=256)
    kb_id = FieldSchema(name="kb_id", dtype=DataType.VARCHAR, max_length=256)
    tenant_id = FieldSchema(name="tenant_id", dtype=DataType.VARCHAR, max_length=64)
    allow_stamps = FieldSchema(name="allow_stamps", dtype=DataType.JSON)
    deny_stamps = FieldSchema(name="deny_stamps", dtype=DataType.JSON)
    vis_version = FieldSchema(name="vis_version", dtype=DataType.INT64)
    retrievable = FieldSchema(name="retrievable", dtype=DataType.BOOL)

    schema = CollectionSchema(
        fields=[pk, content, vector, sparse, doc_id, mount_id, kb_id,
                tenant_id, allow_stamps, deny_stamps, vis_version, retrievable],
        enable_dynamic_field=False,
    )

    Collection(name="rag_documents", schema=schema)

    # 创建索引
    col = Collection("rag_documents")
    col.create_index("vector", {
        "metric_type": "IP",
        "index_type": "IVF_FLAT",
        "params": {"nlist": 128},
    })
    col.create_index("sparse_vector", {
        "metric_type": "IP",
        "index_type": "SPARSE_INVERTED_INDEX",
        "params": {"drop_ratio_build": 0.2},
    })
    col.load()

    connections.disconnect("default")
    log.info("milvus_collection_created", msg="rag_documents ready")


def main():
    print("=== Resetting RAG Data ===")
    print()

    print("[1/2] Clearing PostgreSQL tables...")
    asyncio.run(reset_postgres())

    print("[2/2] Recreating Milvus collection...")
    reset_milvus()

    print()
    print("Done. Knowledge base definitions and configs are preserved.")
    print("You can now upload documents and test the full pipeline.")


if __name__ == "__main__":
    main()

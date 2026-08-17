"""清理知识库数据 — 保留 KB 定义和配置，清空文档/向量/挂载/执行记录。

运行方式：
  conda activate rag_dev_v14
  python -m src.scripts.reset_rag_data
"""

import sys
sys.path = [p for p in sys.path if 'ros' not in p]

import asyncio
import asyncpg
from pymilvus import MilvusClient

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
    """删除并重建 rag_documents 集合（schema/索引与 MilvusWriter 严格一致）。"""
    s = Settings()
    client = MilvusClient(uri=f"http://{s.milvus_host}:{s.milvus_port}")

    if client.has_collection("rag_documents"):
        client.drop_collection("rag_documents")
        log.info("milvus_collection_dropped")

    # 复用 writer 的 schema + 索引构造——单一事实来源，杜绝 reset 与 writer 不一致
    from src.ingest.components.milvus_writer import build_schema, ensure_indices

    client.create_collection(
        collection_name="rag_documents",
        schema=build_schema(),
        metric_type="IP",
    )
    ensure_indices(client, "rag_documents")
    client.load_collection("rag_documents")
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

"""一次性修复：将 Milvus 中 tenant_id 为空的 chunk 补全为正确值。

运行方式：
  conda activate rag_dev_v14
  python -m src.scripts.fix_tenant_id

原理：
- 查询 Milvus 中所有 tenant_id="" 的 chunk
- 从 document_kb_mounts 反查正确的 tenant_id
- 直接更新 Milvus 中的 tenant_id 字段

背景：
  存量 chunk 在 MilvusWriter 写入时 tenant_id 字段尚未包含在 schema 中
  （或 meta 中未传入），导致默认为空。检索过滤器要求 tenant_id == "tenant-dev"，
  因此所有检索结果为零。
"""

import sys
sys.path = [p for p in sys.path if 'ros' not in p]

import asyncio
import asyncpg
from pymilvus import connections, Collection

from src.config import Settings
from src.platform.obs.logger import get_logger

log = get_logger(__name__)


def _dsn():
    return Settings().database_url.replace("postgresql+asyncpg://", "postgresql://")


async def _get_tenant_for_kb(kb_id: str) -> str:
    """从 knowledge_bases 表获取 KB 的 tenant_id。"""
    conn = await asyncpg.connect(_dsn())
    try:
        row = await conn.fetchrow(
            "SELECT tenant_id FROM knowledge_bases WHERE id=$1", kb_id)
        return row["tenant_id"] if row else "tenant-dev"
    finally:
        await conn.close()


async def _get_tenant_for_document(doc_id: str) -> str:
    """从 documents 表获取文档的 tenant_id。"""
    conn = await asyncpg.connect(_dsn())
    try:
        row = await conn.fetchrow(
            "SELECT tenant_id FROM documents WHERE id=$1", doc_id)
        return row["tenant_id"] if row else "tenant-dev"
    finally:
        await conn.close()


async def fix_tenant_ids():
    """主修复逻辑。"""
    s = Settings()

    # 1. Query Milvus for chunks with empty tenant_id
    connections.connect("default", host=s.milvus_host, port=str(s.milvus_port))
    col = Collection("rag_documents")
    col.load()

    results = col.query(
        expr='tenant_id == ""',
        output_fields=["*"],
        limit=10000,
    )

    if not results:
        log.info("fix_tenant_id_no_empty_chunks",
                 msg="All chunks already have non-empty tenant_id. Nothing to fix.")
        connections.disconnect("default")
        return

    log.info("fix_tenant_id_start", empty_count=len(results))

    # 2. Group by kb_id to batch-lookup tenants
    kb_tenants: dict = {}
    doc_tenants: dict = {}
    fixed = 0

    for batch_start in range(0, len(results), 500):
        batch = results[batch_start:batch_start + 500]
        upsert_rows = []

        for row in batch:
            kb_id = row.get("kb_id", "")
            doc_id = row.get("document_id", "")

            # Determine correct tenant_id — prefer KB tenant, fall back to doc tenant
            if kb_id not in kb_tenants:
                kb_tenants[kb_id] = await _get_tenant_for_kb(kb_id)
            correct_tenant = kb_tenants[kb_id]

            if not correct_tenant and doc_id:
                if doc_id not in doc_tenants:
                    doc_tenants[doc_id] = await _get_tenant_for_document(doc_id)
                correct_tenant = doc_tenants[doc_id]

            if not correct_tenant:
                correct_tenant = "tenant-dev"  # 最终兜底

            upsert_rows.append({
                "id": row["id"],
                "content": row["content"],
                "vector": row["vector"],
                "sparse_vector": row.get("sparse_vector", {}),
                "document_id": row.get("document_id", ""),
                "mount_id": row.get("mount_id", ""),
                "kb_id": row.get("kb_id", ""),
                "tenant_id": correct_tenant,
                "allow_stamps": row.get("allow_stamps", []),
                "deny_stamps": row.get("deny_stamps", []),
                "vis_version": row.get("vis_version", 1),
                "retrievable": row.get("retrievable", True),
            })

        if upsert_rows:
            col.upsert(upsert_rows)
            col.flush()
            fixed += len(upsert_rows)
            log.info("fix_tenant_id_batch", batch_size=len(upsert_rows), total_fixed=fixed)

    connections.disconnect("default")
    log.info("fix_tenant_id_complete", fixed=fixed,
             msg=f"Updated {fixed} chunks with correct tenant_id. Retrieval should now work.")


def main():
    asyncio.run(fix_tenant_ids())


if __name__ == "__main__":
    main()

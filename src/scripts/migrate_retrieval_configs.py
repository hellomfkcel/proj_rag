"""补全 retrieval_configs 缺失列（幂等，可重复执行）。

背景：scripts/init.sql 的 retrieval_configs 定义曾缺 dense_weight/sparse_weight
（以及 min_score/refine_batch_size/tree_summarize_batch_size/max_answer_length/
compress_target_length/doc_preview_max_chars），导致设置页保存"稀疏权重"时
UPDATE 引用不存在的列 → 500（诊断报告_前端检索配置与管线权限_20260816.md P0）。

本脚本以 ALTER TABLE ... ADD COLUMN IF NOT EXISTS 幂等补齐，兼容：
- 全新库（init.sql 已含全部列 → 全部 no-op）
- 既有库（补缺失列）

运行方式：
  conda activate rag_dev_v14
  python -m src.scripts.migrate_retrieval_configs
"""

import asyncio
import os
import sys

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, _project_root)

from src.config import Settings


# 代码期望存在、但历史 DDL/既有库可能缺失的列（与 RetrievalConfig 对齐）
_MISSING_COLUMNS = [
    ("dense_weight", "FLOAT DEFAULT 0.5"),
    ("sparse_weight", "FLOAT DEFAULT 0.5"),
    ("min_score", "FLOAT DEFAULT 0.0"),
    ("refine_batch_size", "INTEGER DEFAULT 2"),
    ("tree_summarize_batch_size", "INTEGER DEFAULT 5"),
    ("max_answer_length", "INTEGER DEFAULT 3000"),
    ("compress_target_length", "INTEGER DEFAULT 1000"),
    ("doc_preview_max_chars", "INTEGER DEFAULT 1000"),
]


async def main() -> None:
    import asyncpg
    s = Settings()
    dsn = s.database_url.replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(dsn)
    try:
        added = []
        for col, ddl in _MISSING_COLUMNS:
            await conn.execute(
                f"ALTER TABLE retrieval_configs ADD COLUMN IF NOT EXISTS {col} {ddl}"
            )
            added.append(col)
        print(f"migrate_retrieval_configs: ensured columns -> {added}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())

"""读取 scripts/init.sql 并在 PostgreSQL 上执行。

幂等（所有 DDL 使用 IF NOT EXISTS / ON CONFLICT DO NOTHING）。

用法: make db-init  或  python -m src.scripts.init_db
"""

import asyncio
import asyncpg
import os
import sys

# 确保项目根目录在 sys.path
_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, _project_root)

from src.config import Settings


async def main():
    s = Settings()
    dsn = s.database_url.replace("postgresql+asyncpg://", "postgresql://")

    sql_path = os.path.join(_project_root, "scripts", "init.sql")
    if not os.path.exists(sql_path):
        print(f"ERROR: init.sql not found at {sql_path}")
        sys.exit(1)

    print(f"Connecting to PostgreSQL ...")
    conn = await asyncpg.connect(dsn)

    try:
        sql = open(sql_path, "r", encoding="utf-8").read()
        # Skip transaction wrapper lines — asyncpg executes as single statement
        # but init.sql uses BEGIN/COMMIT which breaks across multiple execute() calls.
        # We execute the entire SQL as one script.
        await conn.execute(sql)
        print("DB init complete — all tables created.")

        # 基础配置 seed（model_registry + prompt_templates）：
        # 新建库后必须有，否则模型管理页空白、生成缺 prompt 模板。
        # 幂等（表空才写模型 / ON CONFLICT DO NOTHING），每次 start.sh start 自动执行。
        from src.scripts.seed_base_config import seed_base_config
        await seed_base_config(conn)
        print("Base config seed complete.")
    except Exception as e:
        print(f"ERROR during init: {e}")
        raise
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())

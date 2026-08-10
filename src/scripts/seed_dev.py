"""写入开发期测试数据。

创建：
- 测试租户 tenant-dev
- 三个角色：admin (system_admin) / reader (普通用户) / writer (kb:write)
- 一个 seed KB + 在 resource_registry 注册

用法: make db-seed  或  python -m src.scripts.seed_dev
"""

import asyncio
import asyncpg
import os
import sys
import uuid
from datetime import datetime, timezone

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, _project_root)

from src.config import Settings


async def main():
    s = Settings()
    dsn = s.database_url.replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(dsn)

    now = datetime.now(timezone.utc)
    tenant_id = "tenant-dev"

    print(f"Seeding dev data into tenant: {tenant_id}")

    try:
        # ── 1. 创建 seed 知识库 ──
        kb_id = str(uuid.uuid4())
        await conn.execute("""
            INSERT INTO knowledge_bases (id, tenant_id, name, description, owner_id, status, created_at)
            VALUES ($1, $2, '开发测试KB', '开发环境 seed 知识库', 'admin', 'active', $3)
            ON CONFLICT (tenant_id, name) DO NOTHING
        """, kb_id, tenant_id, now)

        # 取实际的 kb_id（幂等：可能已存在）
        existing = await conn.fetchrow(
            "SELECT id FROM knowledge_bases WHERE tenant_id=$1 AND name='开发测试KB'", tenant_id)
        if existing:
            kb_id = str(existing["id"])

        print(f"  KB: 开发测试KB (id={kb_id[:8]}...)")

        # ── 2. 创建 chunking / retrieval 配置 ──
        await conn.execute("""
            INSERT INTO chunking_configs (kb_id, version, haystack_strategy, split_length, split_overlap, language)
            VALUES ($1, 'v1', 'sentence', 256, 32, 'zh')
            ON CONFLICT DO NOTHING
        """, kb_id)

        await conn.execute("""
            INSERT INTO retrieval_configs (scope_type, scope_id, top_k, retrieval_mode, fusion_method, strict, oversample_factor, min_results, refetch_max_rounds, haystack_pipeline_name)
            VALUES ('kb', $1, 10, 'hybrid', 'rrf', false, 1.5, 3, 2, 'query_v4')
            ON CONFLICT DO NOTHING
        """, kb_id)

        # ── 3. 创建 kb_bound 目录 ──
        await conn.execute("""
            INSERT INTO directories (id, tenant_id, name, directory_type, bound_kb_id, created_by)
            VALUES ($1, $2, '开发测试KB', 'kb_bound', $3, 'admin')
            ON CONFLICT DO NOTHING
        """, str(uuid.uuid4()), tenant_id, kb_id)

        # ── 4. 注册资源镜像（P-AUTHC 内部表） ──
        for owner, role_name in [
            ("user:admin", "system_admin"),
            ("user:reader", "user"),
            ("user:writer", "user"),
        ]:
            await conn.execute("""
                INSERT INTO resource_registry (resource_type, resource_id, owner)
                VALUES ('kb', $1, $2)
                ON CONFLICT (resource_type, resource_id) DO UPDATE SET retired=false, owner=$2
            """, kb_id, owner)

        print(f"  Resource registry: admin / reader / writer registered on KB")

        # ── 5. 写入主模型注册（如果 model_registry 为空） ──
        model_count = await conn.fetchval("SELECT count(*) FROM model_registry")
        if model_count == 0:
            models = [
                ("qwen3-8b", "llm", "ollama", "qwen3:8b", "http://localhost:11434", True),
                ("deepseek-chat", "llm", "deepseek", "deepseek-v4-flash", "https://api.deepseek.com/v1", False),
                ("qwen3-embed", "embedding", "ollama", "qwen3-embedding:0.6b", "http://localhost:11434", True),
                ("bge-m3", "embedding", "sentence_transformers", "BAAI/bge-m3", "", True),
                ("bge-reranker-v2-m3", "reranker", "sentence_transformers", "BAAI/bge-reranker-v2-m3", "", True),
            ]
            for model_id, mtype, provider, model_name, base_url, is_default in models:
                await conn.execute("""
                    INSERT INTO model_registry (model_id, model_type, provider, model_name, base_url, is_default)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    ON CONFLICT DO NOTHING
                """, model_id, mtype, provider, model_name, base_url, is_default)
            print(f"  Model registry: {len(models)} models seeded")

        # ── 6. 合成角色 Prompt 模板（幂等，模板与 chat/service.py 内联默认一致） ──
        # 合成逻辑经 resolve_prompt(prompt_id, "v1") 读取，DB 有数据即生效（§16.3）；
        # 前端设置页可直接编辑这些模板，编辑后立即影响生成行为。
        prompt_templates = [
            ("compact", "你是企业知识库助手，请基于以下文档内容回答问题。\n"
             "如文档中没有相关信息，请如实说明，不要编造。\n\n"
             "{% for doc in documents %}[来源 {{ loop.index }}] {{ doc.content }}\n{% endfor %}\n"
             "问题：{{ query }}", "compact 单次合成"),
            ("refine_init", "你是企业知识库助手。请基于以下文档内容回答用户问题。\n"
             "如文档中没有足够信息，请如实说明。\n\n[文档内容]\n{{ current_doc }}\n\n问题：{{ query }}",
             "refine 首轮初始答案"),
            ("refine", "你是企业知识库助手。你之前生成了以下答案：\n\n[已有答案]\n{{ existing_answer }}\n\n"
             "现在你得到了新的参考文档。请基于新文档的信息，对已有答案进行补充和完善。\n"
             "如果新文档中有原答案未涵盖的重要信息，请补充。\n"
             "如果新文档的信息与原答案矛盾，请修正。\n"
             "如果新文档没有新增信息，保持原答案不变。\n\n[新文档]\n{{ current_doc }}\n\n问题：{{ query }}",
             "refine 迭代完善"),
            ("summarize", "请为以下文档片段提取与用户问题相关的关键信息。输出简洁的要点列表，"
             "每个要点不超过 2 句话。\n\n{% for doc in documents %}[文档 {{ loop.index }}] {{ doc.content }}\n{% endfor %}\n"
             "问题：{{ query }}\n\n关键信息要点：", "tree_summarize 分批摘要"),
            ("merge", "你是企业知识库助手。以下是多篇文档的要点摘要。请基于这些摘要回答用户问题。"
             "如摘要中没有相关信息，请如实说明。\n\n{{ summaries }}\n\n问题：{{ query }}", "tree_summarize 汇总合成"),
        ]
        for pid, text, desc in prompt_templates:
            await conn.execute("""
                INSERT INTO prompt_templates (prompt_id, version, template_text, description, is_active)
                VALUES ($1, 'v1', $2, $3, true)
                ON CONFLICT (prompt_id, version) DO NOTHING
            """, pid, text, desc)
        print(f"  Prompt templates: {len(prompt_templates)} role templates ensured")

        print("Seed complete — 开发环境已就绪。")

    except Exception as e:
        print(f"ERROR during seed: {e}")
        raise
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())

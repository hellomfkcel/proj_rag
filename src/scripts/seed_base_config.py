"""基础配置 seed：model_registry + prompt_templates。

与 dev 数据 seed（seed_dev 的 KB/配置/资源注册）分离：
- 本模块是**配置类** seed，新建库后必须有（模型管理页/生成 prompt 依赖它）；
- 由 init_db（每次 start.sh start 建表后）自动调用，幂等；
- seed_dev 复用本模块，避免两处维护同一份模型/模板清单。

用法: from src.scripts.seed_base_config import seed_base_config
"""

import asyncpg

# 主模型注册（model_registry）：model_id / type / provider / model_name / base_url / is_default
# 若表为空则整批写入；已有数据不覆盖（运营可在管理台编辑）。
MODEL_SEEDS = [
    ("qwen3-8b", "llm", "ollama", "qwen3:8b", "http://localhost:11434", True),
    ("deepseek-chat", "llm", "deepseek", "deepseek-v4-flash", "https://api.deepseek.com/v1", False),
    ("qwen3-embed", "embedding", "ollama", "qwen3-embedding:0.6b", "http://localhost:11434", True),
    ("bge-m3", "embedding", "sentence_transformers", "BAAI/bge-m3", "", True),
    ("bge-reranker-v2-m3", "reranker", "sentence_transformers", "BAAI/bge-reranker-v2-m3", "", True),
]

# Prompt 模板（prompt_templates）：与 chat/service.py 内联默认一致；
# 合成逻辑经 resolve_prompt(prompt_id, "v1") 读取，DB 有数据即生效。
PROMPT_TEMPLATES = [
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


async def seed_base_config(conn: asyncpg.Connection) -> None:
    """幂等写入基础配置：model_registry（空才写）+ prompt_templates（冲突不覆盖）。

    必须在表已创建后调用（init_db 执行 DDL 后 / seed_dev 流程中）。
    """
    # ── model_registry（表空则整批写入） ──
    model_count = await conn.fetchval("SELECT count(*) FROM model_registry")
    if model_count == 0:
        for model_id, mtype, provider, model_name, base_url, is_default in MODEL_SEEDS:
            await conn.execute(
                """
                INSERT INTO model_registry (model_id, model_type, provider, model_name, base_url, is_default)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT DO NOTHING
                """,
                model_id, mtype, provider, model_name, base_url, is_default,
            )
        print(f"  Model registry: {len(MODEL_SEEDS)} models seeded")
    else:
        print(f"  Model registry: 已存在 {model_count} 行，跳过 seed")

    # ── prompt_templates（幂等，冲突不覆盖） ──
    for pid, text, desc in PROMPT_TEMPLATES:
        await conn.execute(
            """
            INSERT INTO prompt_templates (prompt_id, version, template_text, description, is_active)
            VALUES ($1, 'v1', $2, $3, true)
            ON CONFLICT (prompt_id, version) DO NOTHING
            """,
            pid, text, desc,
        )
    print(f"  Prompt templates: {len(PROMPT_TEMPLATES)} role templates ensured")

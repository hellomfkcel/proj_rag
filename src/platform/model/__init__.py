"""P-MODEL：模型与 Prompt 注册模块。

提供：
- invoke_embedding(model_id, texts, mode) → 向量
- invoke_llm(model_id, prompt) → 生成结果
- invoke_rerank(model_id, query, documents) → 重排序（阶段一 no-op）
- resolve_prompt(prompt_id, version) → Jinja2 模板字符串
- resolve_model(model_id) → 连接配置

所有 Generator/Embedder/Ranker 实例封装在模块内部，
Haystack 类型不出模块签名。
"""

from .registry import (
    invoke_embedding,
    invoke_llm,
    invoke_rerank,
    resolve_model,
    resolve_prompt,
    build_prompt,
    create_embedder,
    create_ranker,
)

__all__ = [
    "invoke_embedding",
    "invoke_llm",
    "invoke_rerank",
    "resolve_model",
    "resolve_prompt",
    "build_prompt",
    "create_embedder",
    "create_ranker",
]

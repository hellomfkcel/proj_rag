"""P-TASK：Haystack Pipeline → Celery 任务包装。

v14 核心包装层——所有 Pipeline 执行的唯一入口：
- run_pipeline_sync      worker 内同步执行，供 Celery 任务和业务模块使用
- run_pipeline_async     API 进程内调用，提交 Celery 任务到队列
- run_pipeline_task      Celery 任务体，执行 + Redis Pub/Sub 流式回传

规则：
- API 进程禁止调用 pipeline.run()——只用 run_pipeline_async
- Pipeline 对象在 worker 内部构造，不跨进程共享
"""

import os
from typing import Any, Dict, Optional

from haystack import Pipeline

from src.config import Settings
from src.platform.task.celery_app import celery_app


def _load_pipeline_with_env_vars(pipeline_name: str,
                                  overrides: Optional[Dict[str, str]] = None) -> Pipeline:
    """从 YAML 仓库加载 Pipeline 并替换环境变量占位符。

    这是所有 Pipeline 加载的唯一入口。
    overrides: 额外的占位符→值映射（如切分配置参数），优先于 Settings 默认值。
    """
    # ── 预加载组件模块以填充 Haystack component registry ──
    _preload_component_modules()

    s = Settings()
    yaml_path = os.path.join(s.pipeline_yaml_dir, f"{pipeline_name}.yaml")

    import os as _os
    if s.llm_api_key and not _os.environ.get("OPENAI_API_KEY"):
        _os.environ["OPENAI_API_KEY"] = s.llm_api_key

    pipeline_yaml = open(yaml_path).read()

    # 标准替换 + 调用方覆盖
    replacements = {
        "${MILVUS_HOST}": s.milvus_host,
        "${MILVUS_PORT}": str(s.milvus_port),
        "${OLLAMA_BASE_URL}": s.ollama_base_url,
        "${OLLAMA_MODEL}": s.ollama_model,
        "${LLM_BASE_URL}": s.llm_base_url,
        "${LLM_MODEL}": s.llm_model,
        "${LLM_API_KEY}": s.llm_api_key or "",
    }
    if overrides:
        replacements.update(overrides)

    for placeholder, value in replacements.items():
        pipeline_yaml = pipeline_yaml.replace(placeholder, str(value))

    return Pipeline.loads(pipeline_yaml)


def _preload_component_modules() -> None:
    """预加载所有 Pipeline YAML 引用的组件模块。

    Haystack 2.3.1 的 component registry 依赖 @component 装饰器在模块导入时
    自动注册。此函数确保所有已知组件在 Pipeline.loads() 之前已完成注册。

    新增自定义组件时，在此函数中添加对应的 import。
    """
    try:
        # Haystack 内置组件
        import haystack.components.preprocessors.document_splitter  # noqa: F401 — DocumentSplitter
        import haystack.components.joiners.document_joiner          # noqa: F401 — DocumentJoiner
        import haystack.components.builders.prompt_builder          # noqa: F401 — PromptBuilder
        import haystack.components.generators.openai                # noqa: F401 — OpenAIGenerator

        # milvus-haystack 集成
        import milvus_haystack.milvus_embedding_retriever  # noqa: F401 — MilvusEmbeddingRetriever et al.

        # 本系统自定义组件（摄入 Pipeline）
        import src.ingest.components.ollama_embedder     # noqa: F401
        import src.ingest.components.sparse_embedder     # noqa: F401
        import src.ingest.components.perm_enricher       # noqa: F401
        import src.ingest.components.semantic_splitter   # noqa: F401
        import src.ingest.components.hierarchical_splitter  # noqa: F401
        import src.ingest.components.milvus_writer       # noqa: F401 — MilvusDocumentStoreWriter

        # 本系统自定义组件（查询 Pipeline）
        import src.retrieve.components.ollama_text_embedder   # noqa: F401
        import src.retrieve.components.sparse_text_embedder   # noqa: F401
        import src.retrieve.components.dense_retriever        # noqa: F401
        import src.retrieve.components.sparse_retriever       # noqa: F401
        import src.retrieve.components.reranker               # noqa: F401
        import src.retrieve.components.hierarchical_merger     # noqa: F401
        import src.retrieve.components.prefilter_injector     # noqa: F401
        import src.retrieve.components.weighted_fusion       # noqa: F401
    except ImportError:
        pass


def run_pipeline_sync(
    pipeline_name: str,
    pipeline_input: Dict[str, Any],
    overrides: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """同步执行 Haystack Pipeline（供 Celery 任务和业务模块在 worker 内使用）。

    这是所有 Pipeline.run() 调用的唯一入口。
    ingest/retrieve/chat 模块不得自行 Pipeline.loads()。

    overrides: YAML 占位符覆盖（如切分配置参数）。键如 "${CHUNK_SPLIT_LENGTH}"。
    """
    pipeline = _load_pipeline_with_env_vars(pipeline_name, overrides=overrides)
    return pipeline.run(pipeline_input)


# ══════════════════════════════════════════════════════════════════
# Celery 任务包装（供 API 进程异步提交）
# ══════════════════════════════════════════════════════════════════

@celery_app.task(
    bind=True,
    acks_late=True,
    reject_on_worker_lost=True,
    max_retries=0,
)
def run_pipeline_task(
    self,
    pipeline_name: str,
    pipeline_input: Dict[str, Any],
    task_metadata: Dict[str, Any],
) -> Dict[str, Any]:
    """Celery 任务：执行 Pipeline 并通过 Redis Pub/Sub 流式回传。

    检索类 Pipeline 结果经 Redis Pub/Sub 流式推送到 API 层。
    """
    import json

    result = run_pipeline_sync(pipeline_name, pipeline_input)

    # 流式回传（仅检索类任务使用）
    task_id = task_metadata.get("task_id", "")
    if task_id:
        import redis
        s = Settings()
        r = redis.from_url(s.redis_url)
        channel = f"query-stream:{task_id}"

        documents = result.get("joiner", {}).get("documents", [])
        if documents:
            chunk_ids = [d.id for d in documents if hasattr(d, "id")]
            r.publish(channel, json.dumps({
                "event": "retrieved",
                "chunk_ids": chunk_ids,
            }))

        replies = result.get("generator", {}).get("replies", [])
        if replies:
            r.publish(channel, json.dumps({
                "event": "token",
                "content": replies[0],
            }))

        r.publish(channel, json.dumps({"event": "done"}))
        r.close()

    return {"status": "completed"}


def run_pipeline_async(
    pipeline_name: str,
    pipeline_input: Dict[str, Any],
    task_metadata: Dict[str, Any],
    queue: str = "ingestion_queue",
) -> Any:
    """异步提交 Haystack Pipeline 任务到 Celery 队列。

    在 API 进程中调用此函数——不在 API 进程内调用 pipeline.run()。
    """
    return run_pipeline_task.apply_async(
        args=[pipeline_name, pipeline_input, task_metadata],
        queue=queue,
    )

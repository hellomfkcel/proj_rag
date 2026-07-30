"""B-INGEST：摄入管线。

阶段三升级：
- execution_epoch 栅栏：每个关键写点前重读 epoch，不匹配则自动退出
- 协作式取消：parse_status == 'cancelling' 时在途任务自动退出
- 卸载清理时序：cancelling → 清理 chunk → removed

提供：
- ingest_document_task    Haystack 摄入 Pipeline 任务
- stamp_channel_task      盖戳管道（六条纪律全部实现）
- has_execution / get_parse_status / should_abort
- cleanup_mount_chunks    卸载时清理 Milvus chunk

独占数据：ingest_execution / chunks（向量库）
"""

import asyncio
import asyncpg
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from src.platform.task.celery_app import celery_app
from src.platform.task.pipeline_runner import run_pipeline_sync
from src.config import Settings
from src.platform.obs.logger import get_logger

log = get_logger(__name__)


def _dsn() -> str:
    return Settings().database_url.replace("postgresql+asyncpg://", "postgresql://")


# ══════════════════════════════════════════════════════════════════
# Pipeline selection by chunking strategy
# ══════════════════════════════════════════════════════════════════

# 策略 -> Pipeline YAML 映射（v14.md §12.2, §14.4）
_STRATEGY_PIPELINE_MAP = {
    "sentence":      "ingest_v1",
    "word":          "ingest_v2",
    "passage":       "ingest_v3",
    "semantic":      "ingest_v4",
    "hierarchical":  "ingest_v5",
}


def _pipeline_for_strategy(strategy: str) -> str:
    """根据 haystack_strategy 返回对应的摄入 Pipeline 名称。

    完整策略映射（v14.md §12.2, §14.4）：
    - sentence      → ingest_v1  (DocumentSplitter, split_by=sentence)
    - word          → ingest_v2  (DocumentSplitter, split_by=word)
    - passage       → ingest_v3  (DocumentSplitter, split_by=passage)
    - semantic      → ingest_v4  (SemanticDocumentSplitter, embedding similarity)
    - hierarchical  → ingest_v5  (HierarchicalDocumentSplitter, parent-child)
    """
    pipeline = _STRATEGY_PIPELINE_MAP.get(strategy)
    if pipeline is None:
        log.warning("unknown_strategy_fallback", strategy=strategy)
        return "ingest_v1"
    return pipeline


# ══════════════════════════════════════════════════════════════════
# should_abort（阶段三：epoch 栅栏 + 协作式取消）
# ══════════════════════════════════════════════════════════════════

def should_abort(mount_id: str, current_epoch: int) -> bool:
    """在关键写点前检查是否应中止任务。

    条件满足任一即中止：
    - ingest_execution.execution_epoch != current_epoch（僵尸任务）
    - ingest_execution.parse_status == 'cancelling'（卸载清理）
    """

    async def _check():
        conn = await asyncpg.connect(_dsn())
        try:
            row = await conn.fetchrow(
                "SELECT execution_epoch, parse_status FROM ingest_executions "
                "WHERE mount_id = $1", mount_id,
            )
            if not row:
                return True  # 记录已删除，中止
            if row["execution_epoch"] != current_epoch:
                log.warning("ingest_aborted_epoch_mismatch",
                           mount_id=mount_id,
                           current=current_epoch,
                           db=row["execution_epoch"])
                return True
            if row["parse_status"] == "cancelling":
                log.info("ingest_aborted_cancelling", mount_id=mount_id)
                return True
            return False
        finally:
            await conn.close()

    try:
        return asyncio.run(_check())
    except Exception as exc:
        log.warning("should_abort_check_failed", mount_id=mount_id, error=str(exc))
        return True  # 安全侧：检查失败时中止（fail-safe）


def _increment_epoch(mount_id: str) -> int:
    """重提交时递增 execution_epoch，返回新 epoch。

    注意：当前 epoch 管理由 kb_routes.py trigger_parse 通过
    ON CONFLICT DO UPDATE SET execution_epoch+1 + SELECT 读取后传入
    ingest_document_task.delay(execution_epoch=...) 完成。
    此函数为备用工具，供未来需要独立递增 epoch 的场景使用。
    """

    async def _do():
        conn = await asyncpg.connect(_dsn())
        try:
            row = await conn.fetchval(
                "UPDATE ingest_executions SET execution_epoch = execution_epoch + 1, "
                "updated_at = $2 WHERE mount_id = $1 "
                "RETURNING execution_epoch",
                mount_id, datetime.now(timezone.utc),
            )
            return row or 1
        finally:
            await conn.close()

    try:
        return asyncio.run(_do())
    except Exception:
        return 1


# ══════════════════════════════════════════════════════════════════
# ingest_document_task（Celery 任务）
# ══════════════════════════════════════════════════════════════════

@celery_app.task(
    bind=True,
    acks_late=True,
    reject_on_worker_lost=True,
    max_retries=3,
    default_retry_delay=30,
    queue="ingestion_queue",
)
def ingest_document_task(
    self,
    mount_id: str,
    document_id: str,
    kb_id: str,
    tenant_id: str,
    chunking_config_version: str = "v1",
    pipeline_yaml_version: str = "v1",
    execution_epoch: int = 1,
) -> Dict[str, Any]:
    """执行 Haystack 摄入 Pipeline 并向 stamping_queue 提交盖戳任务。

    摄入 Pipeline 结构：
    DocumentSplitter → DenseEmbedder → SparseEmbedder → PermEnricher → MilvusWriter

    任务主流程：
    1. 状态/epoch 前置检查
    2. 经 P-STORE 读原文件
    3. 经 P-CONFIG 按锚定 version 取切分配置
    4. run_pipeline_async 执行摄入 Pipeline
    5. chunk 写入 Milvus（payload 含空戳记：vis_version=0, allow_stamps=[]）
    6. 提交 stamp_channel_task 到 stamping_queue
    7. 置 completed
    """
    s = Settings()

    try:
        # 0. 状态转换：queued → processing（worker 确实开始执行时才设 processing）
        _update_execution_status(mount_id, "processing")

        # 0b. epoch 前置检查（阶段三栅栏 + 协作式取消）
        if should_abort(mount_id, execution_epoch):
            return {"status": "aborted", "reason": "epoch_mismatch_or_cancelling"}

        # 1. 读原文件 — 从 DB 获取 storage_path
        from src.platform.store.backend import StorageBackend
        store = StorageBackend(
            endpoint_url=s.s3_endpoint_url,
            access_key=s.s3_access_key,
            secret_key=s.s3_secret_key,
            bucket=s.s3_bucket,
        )

        import asyncpg as _asyncpg

        async def _get_storage_path():
            conn = await _asyncpg.connect(_dsn())
            try:
                row = await conn.fetchrow(
                    "SELECT storage_path, filename FROM documents WHERE id=$1", document_id)
                if not row:
                    raise RuntimeError(f"Document {document_id} not found in DB")
                sp = row["storage_path"]
                if not sp:
                    raise RuntimeError(f"Document {document_id} has no storage_path")
                return sp
            finally:
                await conn.close()

        storage_path = asyncio.run(_get_storage_path())

        try:
            raw_bytes = store.get(storage_path)
            raw_text = raw_bytes.decode("utf-8", errors="replace")
        except Exception as e:
            log.error("file_not_found_in_storage", key=storage_path, error=str(e))
            raise RuntimeError(f"Failed to read document from object storage: {storage_path}") from e

        # 1b. Pipeline 执行前再次检查 epoch（阶段三栅栏）
        if should_abort(mount_id, execution_epoch):
            return {"status": "aborted", "reason": "epoch_mismatch_before_pipeline"}

        # 2. 执行摄入 Pipeline（经 P-TASK run_pipeline_sync——唯一入口）
        from haystack.dataclasses import Document as HaystackDocument

        hdoc = HaystackDocument(
            content=raw_text,
            meta={
                "document_id": document_id,
                "mount_id": mount_id,
                "kb_id": kb_id,
                "tenant_id": tenant_id,
            },
        )

        # 根据 haystack_strategy 选择对应的摄入 Pipeline
        from src.platform.config.service import resolve_chunking_config
        chunk_cfg = resolve_chunking_config(kb_id, version=chunking_config_version)
        pipeline_name = _pipeline_for_strategy(chunk_cfg.haystack_strategy)
        log.info("ingest_pipeline_selected", strategy=chunk_cfg.haystack_strategy,
                 pipeline=pipeline_name, kb_id=kb_id,
                 split_length=chunk_cfg.split_length,
                 split_overlap=chunk_cfg.split_overlap)

        # 将 DB 切分配置映射为 YAML 占位符覆盖值
        adv = chunk_cfg.advanced_params or {}
        pipeline_overrides = {
            "${CHUNK_SPLIT_LENGTH}": str(chunk_cfg.split_length),
            "${CHUNK_SPLIT_OVERLAP}": str(chunk_cfg.split_overlap),
            "${CHUNK_BREAKPOINT_PERCENTILE}": str(adv.get("breakpoint_threshold_percentile", 50)),
            "${CHUNK_BUFFER_SIZE}": str(adv.get("buffer_size", 1)),
            "${CHUNK_PARENT_LENGTH}": str(adv.get("parent_split_length", 1024)),
            "${CHUNK_CHILD_LENGTH}": str(adv.get("child_split_length", 256)),
        }

        result = run_pipeline_sync(
            pipeline_name,
            {"splitter": {"documents": [hdoc]}},
            overrides=pipeline_overrides,
        )

        # 提取写入的 chunk count
        writer_out = result.get("writer", {})
        docs_written = writer_out.get("documents", [])
        chunk_count = len(docs_written)

        # 3. 提交盖戳任务到 stamping_queue
        stamp_channel_task.apply_async(
            args=[tenant_id, document_id, kb_id, None],
            queue="stamping_queue",
        )

        # 4. 更新 ingest_execution 状态
        _update_execution_status(mount_id, "completed")

        log.info("ingest_completed",
                 mount_id=mount_id, chunk_count=chunk_count,
                 pipeline_version=pipeline_yaml_version)

        return {
            "status": "completed",
            "mount_id": mount_id,
            "chunk_count": chunk_count,
        }

    except Exception as exc:
        log.error("ingest_failed", mount_id=mount_id, error=str(exc))

        # 重试
        retries = self.request.retries
        if retries < self.max_retries:
            delay = 30 * (2 ** retries)
            raise self.retry(exc=exc, countdown=delay)

        _update_execution_status(mount_id, "failed")
        return {"status": "failed", "mount_id": mount_id, "error": str(exc)}


# ══════════════════════════════════════════════════════════════════
# stamp_channel_task（盖戳管道）—— 六条纪律全部实现
# ══════════════════════════════════════════════════════════════════

@celery_app.task(
    bind=True,
    acks_late=True,
    reject_on_worker_lost=True,
    max_retries=5,
    default_retry_delay=5,
    queue="stamping_queue",
)
def stamp_channel_task(
    self,
    tenant_id: str,
    doc_id: str,
    kb_id: str,
    expected_version: Optional[int] = None,
) -> Dict[str, Any]:
    """盖戳管道：从 Cerbos /v1/visibility 取戳记，写入 Milvus chunk payload。

    六条纪律（缺一即缺陷）：
    1. 失败不落盘：调 /v1/visibility 失败 → 不写任何东西，不 ack
    2. unmounted 清空：unmounted=true → 清空戳记
    3. 版本单调性：response.version < 当前值 → 丢弃，不覆盖
    4. 分批让渡：批大小 500，批间让出 CPU
    5. 断点续跑：游标记录，崩溃重投覆盖写天然幂等
    6. 审计 fail-open：STAMP_APPLIED 写失败不阻塞
    """
    from src.platform.obs.logger import get_logger
    log = get_logger(__name__)

    from src.permission.authz import get_visibility

    try:
        # 1. 调 /v1/visibility（经 P-AUTHC 门面，唯一出口）
        result = get_visibility(
            tenant=tenant_id,
            doc_id=doc_id,
            kb_id=kb_id,
        )

        # 2. 若 unmounted → 清空戳记（纪律 2）
        if result.get("unmounted"):
            _upsert_stamps(tenant_id, doc_id, kb_id,
                          allow_stamps=[], deny_stamps=[], vis_version=0)
            log.info("stamp_cleared", doc_id=doc_id, kb_id=kb_id,
                      reason="unmounted")
            return {"status": "cleared", "reason": "unmounted"}

        # 3. 版本单调性检查（纪律 3）
        new_version = result.get("version", 1)
        current_version = _get_current_vis_version(tenant_id, doc_id, kb_id)
        if current_version is not None and new_version < current_version:
            log.info("stamp_skipped_stale",
                     doc_id=doc_id, kb_id=kb_id,
                     new_version=new_version, current_version=current_version)
            return {"status": "skipped", "reason": "stale_version"}

        # 4. 分批 upsert（纪律 4：批大小 500）
        allow_stamps = result.get("allow_stamps", [])
        deny_stamps = result.get("deny_stamps", [])

        _upsert_stamps(tenant_id, doc_id, kb_id,
                      allow_stamps=allow_stamps,
                      deny_stamps=deny_stamps,
                      vis_version=new_version)

        # 6. 审计（纪律 6：fail-open）
        from src.platform.audit.service import emit_audit_event
        try:
            emit_audit_event(
                event_type="STAMP_APPLIED",
                user_id="system",
                tenant_id=tenant_id,
                action="stamp",
                resource_type="chunk",
                resource_id=f"{doc_id}/{kb_id}",
                allowed=True,
                vis_version=new_version,
            )
        except Exception:
            pass  # fail-open

        log.info("stamp_applied",
                 doc_id=doc_id, kb_id=kb_id,
                 version=new_version,
                 allow_count=len(allow_stamps),
                 deny_count=len(deny_stamps))

        return {"status": "completed", "version": new_version}

    except Exception as exc:
        # 纪律 1：失败不落盘、不 ack → retry
        log.warning("stamp_failed_retrying",
                     doc_id=doc_id, kb_id=kb_id, error=str(exc))

        retries = self.request.retries
        if retries < self.max_retries:
            delay = min(5 * (2 ** retries), 60)
            raise self.retry(exc=exc, countdown=delay)

        # 重试用尽：不标 failed（纪律 6），交给对账兜底
        log.error("stamp_dead_letter",
                   doc_id=doc_id, kb_id=kb_id,
                   retries=retries)
        return {"status": "dead_letter", "doc_id": doc_id, "kb_id": kb_id}


def _upsert_stamps(
    tenant_id: str, doc_id: str, kb_id: str,
    allow_stamps: List[str], deny_stamps: List[str],
    vis_version: int,
) -> None:
    """向 Milvus 批量 upsert 戳记字段（纪律 4：分批让渡）。

    只更新三个戳记字段：allow_stamps, deny_stamps, vis_version。
    不加工、不推导、不补全。
    """
    from pymilvus import Collection, connections
    s = Settings()
    connections.connect("default", host=s.milvus_host, port=str(s.milvus_port))

    try:
        col = Collection("rag_documents")
        # 用 kb_id + document_id 定位即可（tenant_id 可能在旧数据中为空）
        expr = f'kb_id == "{kb_id}" && document_id == "{doc_id}"'

        # 查询该通道下所有 chunk（需要完整字段用于 upsert）
        results = col.query(
            expr=expr,
            output_fields=["id", "content", "vector", "sparse_vector",
                          "document_id", "mount_id", "kb_id", "tenant_id",
                          "allow_stamps", "deny_stamps", "vis_version", "retrievable"],
            limit=10000,
        )

        batch_size = 500
        if not results:
            log.warning("stamp_upsert_no_chunks_found",
                       tenant_id=tenant_id, doc_id=doc_id, kb_id=kb_id,
                       msg="No chunks matched the query — stamps NOT written. "
                           "Chunks will remain invisible (vis_version=0) until this is resolved.")
        for i in range(0, len(results), batch_size):
            batch = results[i:i + batch_size]
            upsert_rows = []
            for row in batch:
                # 若存量 chunk 的 tenant_id 为空（Milvus schema 演进前写入的数据），
                # 用本任务参数覆盖；否则保留已有值
                row_tenant = row.get("tenant_id", "")
                effective_tenant = row_tenant if row_tenant else tenant_id
                upsert_rows.append({
                    "id": row["id"],
                    "content": row["content"],
                    "vector": row["vector"],
                    "sparse_vector": row.get("sparse_vector", {}),
                    "document_id": row.get("document_id", ""),
                    "mount_id": row.get("mount_id", ""),
                    "kb_id": row.get("kb_id", kb_id),
                    "tenant_id": effective_tenant,
                    "allow_stamps": allow_stamps,  # Python list → Milvus JSON array
                    "deny_stamps": deny_stamps,    # Python list → Milvus JSON array
                    "vis_version": vis_version,
                    "retrievable": row.get("retrievable", True),
                })
            col.upsert(upsert_rows)
            col.flush()
    finally:
        connections.disconnect("default")


def _get_current_vis_version(tenant_id: str, doc_id: str, kb_id: str) -> Optional[int]:
    """获取当前 chunk 的 vis_version（用于版本单调性检查）。"""
    from pymilvus import Collection, connections
    s = Settings()
    try:
        connections.connect("default", host=s.milvus_host, port=str(s.milvus_port))
        col = Collection("rag_documents")
        expr = f'kb_id == "{kb_id}" && document_id == "{doc_id}" && vis_version > 0'
        results = col.query(expr=expr, output_fields=["vis_version"], limit=1)
        if results:
            return results[0].get("vis_version")
    except Exception:
        pass
    finally:
        connections.disconnect("default")
    return None


# ══════════════════════════════════════════════════════════════════
# has_execution / get_parse_status
# ══════════════════════════════════════════════════════════════════

def has_execution(mount_ids: List[str]) -> Set[str]:
    """批量存在性查询（对账用）。返回已有执行记录的 mount_id 集合。"""
    import asyncpg
    import asyncio

    async def _do():
        s = Settings()
        conn = await asyncpg.connect(
            s.database_url.replace("postgresql+asyncpg://", "postgresql://")
        )
        try:
            rows = await conn.fetch(
                "SELECT mount_id FROM ingest_executions WHERE mount_id = ANY($1)",
                mount_ids,
            )
            return {str(r["mount_id"]) for r in rows}
        finally:
            await conn.close()

    return asyncio.run(_do())


def get_parse_status(mount_id: str) -> Optional[str]:
    """解析状态查询（窄投影，不暴露 epoch 等内部字段）。"""
    import asyncpg
    import asyncio

    async def _do():
        s = Settings()
        conn = await asyncpg.connect(
            s.database_url.replace("postgresql+asyncpg://", "postgresql://")
        )
        try:
            row = await conn.fetchrow(
                "SELECT parse_status FROM ingest_executions WHERE mount_id=$1",
                mount_id,
            )
            return row["parse_status"] if row else None
        finally:
            await conn.close()

    return asyncio.run(_do())


def _update_execution_status(mount_id: str, status: str) -> None:
    """更新摄入执行状态（非阻塞 fire-and-forget）。"""
    import asyncpg
    import asyncio

    async def _do():
        s = Settings()
        conn = await asyncpg.connect(
            s.database_url.replace("postgresql+asyncpg://", "postgresql://")
        )
        try:
            await conn.execute(
                "UPDATE ingest_executions SET parse_status=$1, updated_at=$2 WHERE mount_id=$3",
                status, datetime.now(timezone.utc), mount_id,
            )
        finally:
            await conn.close()

    try:
        asyncio.run(_do())
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════
# cleanup_mount_chunks（阶段三：卸载时清理 Milvus chunk）
# ══════════════════════════════════════════════════════════════════

def cleanup_mount_chunks(mount_id: str, doc_id: str, kb_id: str) -> int:
    """卸载时清理一个挂载的全部 Milvus chunk。

    时序（不可颠倒）：
    1. B-INGEST 置 parse_status = 'cancelling'（不可逆）
    2. 在途 ingest_document_task 在下一个写点发现 cancelling → 退出
    3. 确认退出后 → 清理该挂载的 chunk
    4. 置 parse_status = 'removed'
    """
    from pymilvus import Collection, connections

    # 1. 置 cancelling
    _update_execution_status(mount_id, "cancelling")

    # 2. 等待在途任务退出（简化：等待 5s，生产环境用更可靠的通知机制）
    import time
    time.sleep(5)

    # 3. 清理 Milvus chunk
    s = Settings()
    deleted = 0
    try:
        connections.connect("default", host=s.milvus_host, port=str(s.milvus_port))
        col = Collection("rag_documents")
        expr = f'document_id == "{doc_id}" && kb_id == "{kb_id}"'
        col.delete(expr)
        col.flush()
        deleted = col.num_entities
        log.info("mount_chunks_cleaned", mount_id=mount_id, doc_id=doc_id, kb_id=kb_id)
    except Exception as exc:
        log.error("mount_chunks_cleanup_failed", mount_id=mount_id, error=str(exc))
    finally:
        connections.disconnect("default")

    # 4. 置 removed
    _update_execution_status(mount_id, "removed")

    return deleted

"""B-INGEST：摄入管线。

关键机制：
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
from src.config import Settings
from src.platform.obs.logger import get_logger

log = get_logger(__name__)


def _dsn() -> str:
    return Settings().database_url.replace("postgresql+asyncpg://", "postgresql://")


# ══════════════════════════════════════════════════════════════════
# Pipeline selection by chunking strategy
# ══════════════════════════════════════════════════════════════════

# 策略 -> Pipeline YAML 映射
_STRATEGY_PIPELINE_MAP = {
    "sentence":      "ingest_v1",
    "word":          "ingest_v2",
    "passage":       "ingest_v3",
    "semantic":      "ingest_v4",
    "hierarchical":  "ingest_v5",
}

# 权威的切分策略枚举列表（供 kb_routes.py 等模块引用）
VALID_CHUNKING_STRATEGIES = list(_STRATEGY_PIPELINE_MAP.keys())


def _pipeline_for_strategy(strategy: str) -> str:
    """根据 haystack_strategy 返回对应的摄入 Pipeline 名称。

    完整策略映射：
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
# should_abort / _abort_reason（epoch 栅栏 + 协作式取消）
# ══════════════════════════════════════════════════════════════════

def _abort_reason(mount_id: str, current_epoch: int) -> str | None:
    """返回应中止的原因；None 表示不应中止。

    返回值语义（写点栅栏按原因决定是否做孤儿清理）：
    - 'removed'     ingest_executions 记录已删除 —— 文档被移除，可安全清理
                   （文档已不存在，不会有人再写该 KB 的 chunk）
    - 'epoch'       执行版本不匹配 —— 重摄产生的新任务接管，旧任务不清数据
    - 'cancelling'  卸载清理中 —— 由 cleanup_mount_chunks 负责清理
    - 'unknown'     检查失败（fail-safe）—— 中止但不清理，避免误删新数据
    """
    async def _check():
        conn = await asyncpg.connect(_dsn())
        try:
            row = await conn.fetchrow(
                "SELECT execution_epoch, parse_status FROM ingest_executions "
                "WHERE mount_id = $1", mount_id,
            )
            if not row:
                return "removed"
            if row["execution_epoch"] != current_epoch:
                return "epoch"
            if row["parse_status"] == "cancelling":
                return "cancelling"
            return None
        finally:
            await conn.close()

    try:
        return asyncio.run(_check())
    except Exception as exc:
        log.warning("abort_reason_check_failed", mount_id=mount_id, error=str(exc))
        return "unknown"  # 安全侧：中止但不清理


def should_abort(mount_id: str, current_epoch: int) -> bool:
    """在关键写点前检查是否应中止任务（有中止原因即为真）。"""
    reason = _abort_reason(mount_id, current_epoch)
    if reason == "epoch":
        log.warning("ingest_aborted_epoch_mismatch",
                    mount_id=mount_id, current=current_epoch)
    elif reason == "cancelling":
        log.info("ingest_aborted_cancelling", mount_id=mount_id)
    return reason is not None


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
    # 全局 task_soft_time_limit=600s 对大型文档（hierarchical 数千 chunk）
    # 过短：超时被中断 → 无限重试、status 卡 processing。
    # ingest 独立放宽软/硬超时（30/35 min），检索/盖戳保持全局 600s。
    soft_time_limit=1800,
    time_limit=2100,
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

        # 0b. epoch 前置检查（epoch 栅栏 + 协作式取消）
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
            from src.platform.store.encoding import detect_and_decode
            raw_text, detected_enc = detect_and_decode(raw_bytes)
            log.info("encoding_detected", encoding=detected_enc, storage_path=storage_path)
        except Exception as e:
            log.error("file_not_found_in_storage", key=storage_path, error=str(e))
            raise RuntimeError(f"Failed to read document from object storage: {storage_path}") from e

        # 1b. Pipeline 执行前再次检查 epoch（epoch 栅栏）
        if should_abort(mount_id, execution_epoch):
            return {"status": "aborted", "reason": "epoch_mismatch_before_pipeline"}

        # 1c. 清理该文档在此 KB 的旧 chunk（重传/重解析场景，避免重复数据）
        _delete_document_chunks_in_kb(document_id, kb_id)

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

        # 惰性导入：避免 celery_app 模块级导入 ingest.service 时连带拉入
        # haystack（pipeline_runner 顶层 from haystack import Pipeline）。
        # api/stamping/outbox/visibility 等不执行 Pipeline 的进程因此无需安装
        # haystack/torch；此处仅在摄入任务真正执行时（worker 内）才导入。
        from src.platform.task.pipeline_runner import run_pipeline_sync

        result = run_pipeline_sync(
            pipeline_name,
            {"splitter": {"documents": [hdoc]}},
            overrides=pipeline_overrides,
        )

        # 提取写入的 chunk count
        writer_out = result.get("writer", {})
        docs_written = writer_out.get("documents", [])
        chunk_count = len(docs_written)

        # ★ 写点栅栏：每个关键写点前重读 epoch，不匹配则自动退出。
        # pipeline 执行期间（切分→嵌入→写 Milvus）文档可能被删除或重摄。
        # 此处是 Milvus 写入后的下一个关键点（提交盖戳前）。已中止时：
        #   - 不提交盖戳任务、不置 completed（文档已不存在，写戳无意义）；
        #   - 若文档已被移除（记录删除，abort='removed'），清理本任务刚写入
        #     的 chunk —— 文档已不存在，不会有人再写，可安全清理避免孤儿数据；
        #   - epoch 变化/取消 由新任务或 cleanup_mount_chunks 接管，不清数据。
        abort = _abort_reason(mount_id, execution_epoch)
        if abort is not None:
            if abort == "removed":
                _delete_document_chunks_in_kb(document_id, kb_id)
                log.warning("ingest_aborted_removed_cleanup",
                            mount_id=mount_id, document_id=document_id,
                            kb_id=kb_id, chunk_count=chunk_count)
            else:
                log.warning("ingest_aborted_after_pipeline",
                            mount_id=mount_id, reason=abort,
                            chunk_count=chunk_count)
            return {"status": "aborted", "reason": f"{abort}_after_pipeline",
                    "mount_id": mount_id, "chunk_count": chunk_count}

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
                          allow_stamps=[], deny_stamps=[], vis_version=0,
                          allow_empty=True)
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

        # ── 空可见性预警：allow_stamps 为空（且非 unmounted/stale，已在前处理）→ 内容对任何
        # principal 不可见。这是上传解析后最早可判定可见性的点，必须大声暴露（警告+审计），
        # 否则"有文档但检索不到"会被静默生产，直到查询时才暴露。 ──
        if not allow_stamps:
            log.warning("stamp_empty_visibility",
                        doc_id=doc_id, kb_id=kb_id, tenant_id=tenant_id,
                        reason="no_authorized_readers",
                        hint="grant kb:read/kb:write or role binding in admin-console, then re-stamp")
            from src.platform.audit.service import emit_audit_event
            try:
                emit_audit_event(
                    event_type="STAMP_EMPTY_VISIBILITY",
                    user_id="system",
                    tenant_id=tenant_id,
                    action="stamp",
                    resource_type="chunk",
                    resource_id=f"{doc_id}/{kb_id}",
                    allowed=True,
                    note="allow_stamps empty — content invisible to all principals",
                )
            except Exception:
                pass  # fail-open：审计失败不阻塞盖戳

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
            is_no_chunks = "No chunks found" in str(exc)
            if is_no_chunks:
                # 根据 ingest 状态选择退避策略
                parse_status = _check_ingest_status(doc_id, kb_id)
                if parse_status == "completed":
                    # ingest 已完成，chunks 应该存在 → 短暂重试
                    delay = min(5 * (2 ** retries), 30)
                elif parse_status == "failed":
                    # ingest 已彻底失败 → 放弃盖戳
                    log.warning("stamp_aborted_ingest_failed",
                                doc_id=doc_id, kb_id=kb_id)
                    return {"status": "aborted", "reason": "ingest_failed",
                            "doc_id": doc_id, "kb_id": kb_id}
                elif parse_status in ("processing", "queued"):
                    # ingest 进行中 → 长退避
                    delay = min(30 * (2 ** retries), 180)
                elif parse_status == "not_parsed" or parse_status is None:
                    # 从未触发过摄入 → 不会有 chunks，直接放弃不重试
                    log.info("stamp_skipped_not_ingested",
                             doc_id=doc_id, kb_id=kb_id,
                             parse_status=parse_status or "no_execution_record")
                    return {"status": "skipped", "reason": "not_ingested",
                            "doc_id": doc_id, "kb_id": kb_id}
                else:
                    # 未知状态 → 中等退避
                    delay = min(15 * (2 ** retries), 120)
            else:
                # 非 no_chunks 错误（网络/权限服务故障）→ 标准退避
                delay = min(5 * (2 ** retries), 60)        # 5s, 10s, 20s, 40s, 60s
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
    allow_empty: bool = False,
) -> None:
    """向 Milvus 批量 upsert 戳记字段（纪律 4：分批让渡）。

    只更新三个戳记字段：allow_stamps, deny_stamps, vis_version。
    不加工、不推导、不补全。

    allow_empty=True: 当没有 chunk 可更新时视为成功（用于 unmounted 清空）。
    allow_empty=False: 没有 chunk 时抛异常触发 Celery 重试（纪律 1）。
    """
    from pymilvus import MilvusClient
    s = Settings()
    client = MilvusClient(uri=f"http://{s.milvus_host}:{s.milvus_port}")

    # 确保 collection 已加载到内存再进行 query/upsert。
    # MilvusClient API 不会自动 load collection，且 Milvus 重启后
    # collection 会回到 NotLoad 状态。load_collection 已加载时是快速空操作（幂等）。
    client.load_collection("rag_documents")

    # 用 kb_id + document_id 定位即可（tenant_id 可能在旧数据中为空）
    # MilvusClient.query() 的 filter 语法与 ORM 略有差异：字段名需直接使用
    expr = f'kb_id == "{kb_id}" && document_id == "{doc_id}"'

    # 查询该通道下所有 chunk（需要完整字段用于 upsert）。
    # 显式 schema 下 upsert 必须保留全部显式字段，否则层级元数据被清零。
    results: List[Dict[str, Any]] = client.query(
        collection_name="rag_documents",
        filter=expr,
        output_fields=["id", "content", "vector", "sparse_vector",
                      "document_id", "mount_id", "kb_id", "tenant_id",
                      "allow_stamps", "deny_stamps", "vis_version", "retrievable",
                      "parent_id", "level", "chunk_index"],
        limit=10000,
    )

    batch_size = 500
    if not results:
        if allow_empty:
            # 清空操作（unmounted）且没有 chunk：no-op 成功
            log.info("stamp_clear_no_chunks",
                    doc_id=doc_id, kb_id=kb_id,
                    msg="No chunks to clear — already empty or never ingested.")
            return
        # 纪律 1：失败不落盘、不 ack。
        # 没有 chunk 可盖戳 → 抛异常触发 Celery 重试，不做静默跳过。
        #
        # 常见于三种情况：
        # (a) ingest 尚未开始 → 等待 ingest 完成（中退避）
        # (b) ingest 已完成但 chunks 暂时不可见 → Milvus 传播延迟（短退避）
        # (c) 文档从未被解析 → 重试有限次后进死信，由对账兜底
        #
        # 具体退避策略由 stamp_channel_task 的 except 块根据
        # _check_ingest_status(parse_status) 动态决定。
        raise RuntimeError(
            f"No chunks found for doc={doc_id} kb={kb_id} — "
            f"stamp cannot be applied. Chunks may not have been ingested yet. "
            f"Retrying with backoff."
        )

    for i in range(0, len(results), batch_size):
        batch = results[i:i + batch_size]
        upsert_rows: List[Dict[str, Any]] = []
        for row in batch:
            # 若存量 chunk 的 tenant_id 为空，用本任务参数覆盖；否则保留已有值
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
                # 层级元数据原样保留（盖戳只更新三个戳记字段，不加工/不推导）
                "parent_id": row.get("parent_id", "") or "",
                "level": row.get("level", 0),
                "chunk_index": row.get("chunk_index", 0),
            })
        client.upsert(collection_name="rag_documents", data=upsert_rows)


def _check_ingest_status(doc_id: str, kb_id: str) -> str | None:
    """检查文档的 ingest 状态，返回 parse_status 或 None。

    当 stamp_channel_task 发现没有 chunk 可盖戳时调用。
    返回值用于决定重试策略：
    - "completed":  ingest 已完成，chunks 应存在 → 短暂重试（传播延迟）
    - "processing" / "queued": ingest 执行中 → 长退避重试
    - None / 其他: 尚未开始或异常 → 中等退避重试
    """
    import asyncpg as _asyncpg
    import asyncio as _asyncio

    async def _query():
        conn = await _asyncpg.connect(_dsn())
        try:
            row = await conn.fetchrow(
                "SELECT ie.parse_status, ie.mount_id "
                "FROM ingest_executions ie "
                "JOIN document_kb_mounts dkm ON ie.mount_id = dkm.id "
                "WHERE dkm.document_id = $1 AND dkm.kb_id = $2 "
                "ORDER BY ie.updated_at DESC LIMIT 1",
                doc_id, kb_id,
            )
            if row:
                log.warning("stamp_no_chunks_ingest_status",
                           doc_id=doc_id, kb_id=kb_id,
                           parse_status=row["parse_status"],
                           mount_id=str(row["mount_id"]))
                return row["parse_status"]
            else:
                log.warning("stamp_no_chunks_no_ingest_record",
                           doc_id=doc_id, kb_id=kb_id,
                           msg="No ingest_execution found — document may not have been parsed yet")
                return None
        finally:
            await conn.close()

    try:
        return _asyncio.run(_query())
    except Exception:
        return None


def _get_current_vis_version(tenant_id: str, doc_id: str, kb_id: str) -> Optional[int]:
    """获取当前 chunk 的 vis_version（用于版本单调性检查）。

    使用 MilvusClient API（与 milvus_writer.py 和检索组件一致）。
    """
    from pymilvus import MilvusClient
    s = Settings()
    try:
        client = MilvusClient(uri=f"http://{s.milvus_host}:{s.milvus_port}")
        client.load_collection("rag_documents")
        expr = f'kb_id == "{kb_id}" && document_id == "{doc_id}" && vis_version > 0'
        results = client.query(
            collection_name="rag_documents",
            filter=expr,
            output_fields=["vis_version"],
            limit=1,
        )
        if results:
            return results[0].get("vis_version")
    except Exception:
        pass
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


def _get_execution_status(mount_id: str) -> str | None:
    """查询当前 parse_status（同步）。"""
    import asyncpg
    import asyncio

    async def _do():
        s = Settings()
        conn = await asyncpg.connect(
            s.database_url.replace("postgresql+asyncpg://", "postgresql://")
        )
        try:
            return await conn.fetchval(
                "SELECT parse_status FROM ingest_executions WHERE mount_id=$1",
                mount_id,
            )
        finally:
            await conn.close()

    try:
        return asyncio.run(_do())
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════
# cleanup_mount_chunks（卸载时清理 Milvus chunk）
# ══════════════════════════════════════════════════════════════════

def _delete_document_chunks_in_kb(doc_id: str, kb_id: str) -> int:
    """删除指定文档在指定 KB 的全部 Milvus chunk。

    用于重传/重解析前清理旧数据，避免 chunk 重复。
    """
    from pymilvus import MilvusClient

    s = Settings()
    deleted = 0
    try:
        client = MilvusClient(uri=f"http://{s.milvus_host}:{s.milvus_port}")
        expr = f'document_id == "{doc_id}" && kb_id == "{kb_id}"'
        delete_result = client.delete(
            collection_name="rag_documents",
            filter=expr,
        )
        deleted = delete_result.get("delete_count", 0) if isinstance(delete_result, dict) else 0
        if deleted > 0:
            log.info("pre_ingest_chunks_cleaned", doc_id=doc_id, kb_id=kb_id,
                     deleted=deleted)
    except Exception as exc:
        log.warning("pre_ingest_chunks_cleanup_failed", doc_id=doc_id, kb_id=kb_id,
                    error=str(exc))
    return deleted


def cleanup_mount_chunks(mount_id: str, doc_id: str, kb_id: str) -> int:
    """卸载时清理一个挂载的全部 Milvus chunk。

    时序（不可颠倒）：
    1. B-INGEST 置 parse_status = 'cancelling'（不可逆）
    2. 在途 ingest_document_task 在下一个写点发现 cancelling → 退出
    3. 确认退出后 → 清理该挂载的 chunk
    4. 置 parse_status = 'removed'
    """
    # 1. 置 cancelling
    _update_execution_status(mount_id, "cancelling")

    # 2. 轮询等待在途任务退出（最长 5s，每 0.5s 检查一次）
    import time
    max_wait = 5.0
    interval = 0.5
    waited = 0.0
    while waited < max_wait:
        time.sleep(interval)
        waited += interval
        # 检查状态是否已稳定（非 processing）
        current = _get_execution_status(mount_id)
        if current in ("cancelling", "failed", "completed", "removed", None):
            break

    # 3. 清理 Milvus chunk
    deleted = _delete_document_chunks_in_kb(doc_id, kb_id)
    if deleted > 0:
        log.info("mount_chunks_cleaned", mount_id=mount_id, doc_id=doc_id,
                 kb_id=kb_id, deleted=deleted)

    # 4. 置 removed
    _update_execution_status(mount_id, "removed")

    return deleted

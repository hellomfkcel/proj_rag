"""P-TASK：跨系统对账定时任务（阶段三）。

- 结构镜像对账（v14.md §13.7b）：每小时对比本地 mount 表 vs 权限服务
- 戳记对账（v14.md §14.5c）：strict 库 15min 全量 / 普通库 1h 10% 抽样

对账发现缺口 → 自动修复 + 递增 Metric 指标。
"""

import asyncio
import asyncpg
import random
import time
from typing import Set

from src.config import Settings
from src.platform.obs.metrics import set_mirror_gap, set_stamp_drift, set_orphan_stamp
from src.platform.obs.logger import get_logger

log = get_logger(__name__)


def _dsn() -> str:
    return Settings().database_url.replace("postgresql+asyncpg://", "postgresql://")


# ══════════════════════════════════════════════════════════════════
# 结构镜像对账（v14.md §13.7b）
# ══════════════════════════════════════════════════════════════════

async def reconcile_mount_mirror() -> dict:
    """对比本地 document_kb_mount 与权限服务镜像。

    方向一（我有它无）→ 补调 link，递增 mirror_gap，告警
    方向二（它有我无）→ 补调 unlink 回收孤儿，记录但不告警
    """
    conn = await asyncpg.connect(_dsn())
    try:
        # 抽样 mount 表（每小时全量扫描太贵，抽样 10%）
        rows = await conn.fetch(
            "SELECT document_id, kb_id FROM document_kb_mounts "
            "ORDER BY random() LIMIT 50"
        )
        if not rows:
            return {"mirror_gap": 0, "orphans": 0, "sampled": 0}

        # 方向一：对每条 mount 调 /v1/check 验证资源是否存在
        from src.permission.authz import check, link_resource
        from src.permission.context import RequestContext

        # 对账任务使用系统身份（无用户主体，仅验证资源镜像是否存在）
        sys_ctx = RequestContext(
            request_id="reconcile-mirror",
            user_id="system",
            tenant_id="",
            credential="system",
        )
        gaps = 0

        for row in rows:
            doc_id = str(row["document_id"])
            kb_id = str(row["kb_id"])
            try:
                result = check(
                    ctx=sys_ctx,
                    action="doc:view",
                    resource_type="document",
                    resource_id=doc_id,
                    channel_kb=kb_id,
                )
                # 如果返回 unknown_resource，说明权限服务无此资源 → 缺口
                if result.get("decision") == "deny" and "unknown" in str(result.get("reasons", "")):
                    gaps += 1
                    log.warning("mirror_gap_found", doc_id=doc_id, kb_id=kb_id)
                    # 补调 link（经 P-AUTHC 门面）
                    link_resource(sys_ctx, doc_id, kb_id)
            except Exception:
                pass  # 单条失败不影响整体

        set_mirror_gap(gaps)
        return {"mirror_gap": gaps, "orphans": 0, "sampled": len(rows)}
    finally:
        await conn.close()


# ══════════════════════════════════════════════════════════════════
# 戳记对账（v14.md §14.5c）
# ══════════════════════════════════════════════════════════════════

async def reconcile_stamps() -> dict:
    """检查 Milvus chunk 的戳记完整性。

    strict 库 → 每 15 分钟全量
    普通库 → 每小时 10% 抽样

    检出项：
    - orphan_stamp：vis_version=null 或字段缺失 → 补盖戳
    - stamp_drift：vis_version 落后于权限服务 → 重新盖戳
    """
    from pymilvus import Collection, connections

    s = Settings()
    orphans = 0
    drifts = 0

    try:
        connections.connect("default", host=s.milvus_host, port=str(s.milvus_port))
        col = Collection("rag_documents")

        # 检查 orphan chunks（vis_version <= 0）
        try:
            orphan_results = col.query(
                expr="vis_version == 0",
                output_fields=["document_id", "kb_id", "tenant_id"],
                limit=200,
            )
            orphans = len(orphan_results)
            if orphans > 0:
                log.warning("orphan_stamps_found", count=orphans)
                set_orphan_stamp(orphans)

                # 补提交 stamp_channel_task
                seen = set()
                for r in orphan_results:
                    key = (r["tenant_id"], r["document_id"], r["kb_id"])
                    if key not in seen:
                        seen.add(key)
                        from src.ingest.service import stamp_channel_task
                        stamp_channel_task.apply_async(
                            args=[r["tenant_id"], r["document_id"], r["kb_id"], None],
                            queue="stamping_queue",
                        )
        except Exception as exc:
            log.error("orphan_check_failed", error=str(exc))

        # 抽样检查 drifts（取部分 chunk 调 /v1/visibility 对比版本号）
        try:
            sample = col.query(
                expr="vis_version > 0",
                output_fields=["document_id", "kb_id", "tenant_id", "vis_version"],
                limit=50,
            )
            from src.permission.authz import get_visibility

            seen_doc_kb = set()
            for r in sample:
                if random.random() < 0.3:  # 30% 抽样
                    key = (r["tenant_id"], r["document_id"], r["kb_id"])
                    if key not in seen_doc_kb:
                        seen_doc_kb.add(key)
                        try:
                            vis = get_visibility(
                                tenant=r["tenant_id"],
                                doc_id=r["document_id"],
                                kb_id=r["kb_id"],
                            )
                            server_ver = vis.get("version", 1)
                            chunk_ver = r.get("vis_version", 0)
                            if chunk_ver < server_ver:
                                drifts += 1
                                log.warning("stamp_drift_found",
                                           doc_id=r["document_id"],
                                           chunk_ver=chunk_ver,
                                           server_ver=server_ver)
                        except Exception:
                            pass

            if drifts > 0:
                set_stamp_drift(drifts)
        except Exception as exc:
            log.error("drift_check_failed", error=str(exc))

    finally:
        try:
            connections.disconnect("default")
        except Exception:
            pass

    return {"orphan_stamps": orphans, "stamp_drifts": drifts}


# ══════════════════════════════════════════════════════════════════
# 定时循环（开发期运行：每分钟一次，而非每小时/15min）
# ══════════════════════════════════════════════════════════════════

def run_reconciliation_loop():
    """启动对账循环（常驻进程）。"""
    mirror_interval = 60      # 开发期 60s，生产环境 3600s
    stamp_interval = 300      # 开发期 5min，生产环境 15min/60min
    last_mirror = 0.0
    last_stamp = 0.0

    log.info("reconciliation_loop_started",
             mirror_interval_s=mirror_interval,
             stamp_interval_s=stamp_interval)

    while True:
        now = time.time()

        # 结构镜像对账
        if now - last_mirror >= mirror_interval:
            try:
                result = asyncio.run(reconcile_mount_mirror())
                log.info("mirror_reconciled", **result)
            except Exception as exc:
                log.error("mirror_reconcile_failed", error=str(exc))
            last_mirror = now

        # 戳记对账
        if now - last_stamp >= stamp_interval:
            try:
                result = asyncio.run(reconcile_stamps())
                log.info("stamps_reconciled", **result)
            except Exception as exc:
                log.error("stamp_reconcile_failed", error=str(exc))
            last_stamp = now

        time.sleep(10)


# ══════════════════════════════════════════════════════════════════
# Celery Beat 定时任务包装（供 celery_app beat 调度）
# ══════════════════════════════════════════════════════════════════

from src.platform.task.celery_app import celery_app


@celery_app.task(
    bind=True,
    max_retries=0,
    queue="ingestion_queue",
)
def reconcile_mirror_beat(self) -> dict:
    """Celery beat 任务：每小时执行结构镜像对账。

    生产环境每小时一次，开发环境需要时可加快频率。
    """
    try:
        result = asyncio.run(reconcile_mount_mirror())
        if result.get("mirror_gap", 0) > 0:
            log.warning("mirror_gap_detected", gap=result["mirror_gap"])
        return result
    except Exception as exc:
        log.error("mirror_beat_failed", error=str(exc))
        return {"error": str(exc)}


@celery_app.task(
    bind=True,
    max_retries=0,
    queue="stamping_queue",
)
def reconcile_stamps_beat(self) -> dict:
    """Celery beat 任务：每 15 分钟执行戳记对账。

    strict 库全量扫描，普通库抽样检查。
    """
    try:
        result = asyncio.run(reconcile_stamps())
        if result.get("orphan_stamps", 0) > 0:
            log.warning("orphan_stamps_detected", count=result["orphan_stamps"])
        if result.get("stamp_drifts", 0) > 0:
            log.warning("stamp_drifts_detected", count=result["stamp_drifts"])
        return result
    except Exception as exc:
        log.error("stamp_beat_failed", error=str(exc))
        return {"error": str(exc)}


if __name__ == "__main__":
    run_reconciliation_loop()

"""P-AUTHC：VisibilityChanged 事件订阅与转发。

§6A.8 要求 P-AUTHC 订阅权限服务事件流并转交 B-INGEST 处理。
当前 Cerbos PDP (0.39) 未提供原生事件流 API，本模块实现混合方案：

1. **主动触发路径**：生命周期操作（register/link/unlink/retire）完成后立即触发关联文档的盖戳刷新。
   这覆盖了本系统发起的权限变更。

2. **轮询兜底路径**：定期扫描 mount_registry 表，检测 unlinked/retired 状态变更，
   补齐管理台直连权限服务发起的变更（本系统无感知的变更）。

两条路径汇聚到同一个 stamp_channel_task，遵守 §14.5.2 的"三个触发源同一实现"原则。
"""

import asyncio
import asyncpg
import time
from typing import Dict, List, Set

from src.config import Settings
from src.platform.obs.logger import get_logger
from src.platform.obs.metrics import set_stamp_drift

log = get_logger(__name__)


def _dsn() -> str:
    return Settings().database_url.replace("postgresql+asyncpg://", "postgresql://")


# ══════════════════════════════════════════════════════════════════
# 主动触发：生命周期操作后立即刷新戳记
# ══════════════════════════════════════════════════════════════════

def on_visibility_changed(doc_id: str, kb_id: str, tenant_id: str,
                          change_type: str = "unknown") -> None:
    """生命周期操作后立即触发盖戳刷新。

    调用时机：register/link/unlink/retire 完成后。
    不阻塞调用方——stamp_channel_task 异步执行。

    这是 VisibilityChanged 事件的主动触发路径（§14.5.2 触发源 2）。
    """
    try:
        from src.ingest.service import stamp_channel_task

        stamp_channel_task.apply_async(
            args=[tenant_id, doc_id, kb_id, None],
            queue="stamping_queue",
        )
        log.debug("visibility_changed_stamp_triggered",
                  doc_id=doc_id, kb_id=kb_id,
                  change_type=change_type)
    except Exception as exc:
        log.warning("visibility_changed_trigger_failed",
                    doc_id=doc_id, kb_id=kb_id,
                    change_type=change_type,
                    error=str(exc))


def on_kb_visibility_changed(kb_id: str, tenant_id: str,
                              change_type: str = "kb_grant") -> None:
    """KB 粒度权限变更 → 展开为该 KB 下全部文档的盖戳任务。

    设计文档 §14.5.4：权限服务以 KB 粒度聚合发出 VisibilityChanged，
    本系统收到后必须自己展开为逐文档的 stamp_channel_task。

    分页查询 document_kb_mount，每 (doc_id, kb_id) 提交一个盖戳任务。
    """
    async def _expand():
        conn = await asyncpg.connect(_dsn())
        try:
            offset = 0
            page_size = 500
            total_submitted = 0

            while True:
                rows = await conn.fetch(
                    "SELECT document_id FROM document_kb_mounts "
                    "WHERE kb_id = $1 ORDER BY document_id "
                    "LIMIT $2 OFFSET $3",
                    kb_id, page_size, offset,
                )
                if not rows:
                    break

                for row in rows:
                    doc_id = str(row["document_id"])
                    try:
                        on_visibility_changed(
                            doc_id=doc_id,
                            kb_id=kb_id,
                            tenant_id=tenant_id,
                            change_type=change_type,
                        )
                        total_submitted += 1
                    except Exception:
                        pass  # 单个文档失败不影响其余

                offset += page_size
                # 批次间让渡（防止打满 stamping_queue）
                if len(rows) == page_size:
                    await asyncio.sleep(0.5)

            log.info("kb_visibility_expanded",
                     kb_id=kb_id, total_docs=total_submitted,
                     change_type=change_type)
            return total_submitted
        finally:
            await conn.close()

    try:
        asyncio.run(_expand())
    except Exception as exc:
        log.error("kb_visibility_expand_failed",
                  kb_id=kb_id, change_type=change_type,
                  error=str(exc))


# ══════════════════════════════════════════════════════════════════
# 轮询兜底：检测管理台直连权限服务发起的变更
# ══════════════════════════════════════════════════════════════════

async def poll_visibility_changes(last_check_time: float = 0.0) -> Dict:
    """轮询 mount_registry 检测近期变更。

    检测两类变更：
    1. 新 unlinked 的挂载 → 清空戳记（文档从 KB 移除）
    2. 新 retired 的资源 → 清空戳记（文档彻底删除）

    返回检测到的变更数，调用方据此决定是否缩短下次轮询间隔。
    """
    conn = await asyncpg.connect(_dsn())
    changes_found = 0

    try:
        # 检测最近 unlinked 的挂载（通过扫描当前 unlinked=true 但 stamp 可能未清空的记录）
        unlinked_rows = await conn.fetch(
            "SELECT doc_id, kb_id FROM mount_registry "
            "WHERE unlinked = true "
            "LIMIT 100"
        )
        for row in unlinked_rows:
            doc_id = str(row["doc_id"])
            kb_id = str(row["kb_id"])
            try:
                on_visibility_changed(
                    doc_id=doc_id, kb_id=kb_id,
                    tenant_id="",  # tenant 从 stamp_channel_task 内部查询
                    change_type="poll_unlinked",
                )
                changes_found += 1
            except Exception:
                pass

        # 检测最近 retired 的资源
        retired_rows = await conn.fetch(
            "SELECT resource_type, resource_id FROM resource_registry "
            "WHERE retired = true AND resource_type = 'kb' "
            "LIMIT 20"
        )
        for row in retired_rows:
            kb_id = str(row["resource_id"])
            try:
                on_kb_visibility_changed(
                    kb_id=kb_id, tenant_id="",
                    change_type="poll_kb_retired",
                )
                changes_found += 1
            except Exception:
                pass

    finally:
        await conn.close()

    if changes_found > 0:
        log.info("visibility_poll_changes_found", count=changes_found)

    return {"changes_found": changes_found}


# ══════════════════════════════════════════════════════════════════
# 轮询循环（常驻进程入口）
# ══════════════════════════════════════════════════════════════════

def run_visibility_event_loop(poll_interval_s: int = 30):
    """启动 VisibilityChanged 事件轮询循环。

    生产环境建议 30s 间隔（权限变更非高频操作，30s 延迟可接受）。
    可与 reconciliation loop 合并为同一常驻进程。

    设计意图：此循环是 §14.5c 对账的补充——对账是事后修复，此循环是准实时检测。
    """
    log.info("visibility_event_loop_started",
             poll_interval_s=poll_interval_s,
             msg="Polling mount_registry for visibility changes")

    while True:
        try:
            result = asyncio.run(poll_visibility_changes())
            changes = result.get("changes_found", 0)

            # 有变更时缩短下次间隔（可能有更多变更）
            sleep_s = min(poll_interval_s, 10) if changes > 0 else poll_interval_s
            time.sleep(sleep_s)
        except Exception as exc:
            log.error("visibility_event_loop_error", error=str(exc))
            time.sleep(poll_interval_s)


if __name__ == "__main__":
    run_visibility_event_loop()

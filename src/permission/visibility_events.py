"""P-AUTHC：VisibilityChanged 事件订阅与转发。

§6A.8 要求 P-AUTHC 订阅权限服务事件流并转交 B-INGEST 处理。

实现方案：
1. **Redis Pub/Sub 路径**（remote 模式）：订阅权限服务发布的 "visibility_changed" 频道，
   接收外部权限变更事件（管理台 ACL 授予/回收/角色绑定/封禁等操作）。
2. **主动触发路径**（local 模式）：生命周期操作完成后立即触发关联文档的盖戳刷新。
3. **轮询兜底路径**（local/remote 均可）：定期扫描本地表，补齐未覆盖的变更。

三条路径汇聚到同一个 stamp_channel_task，遵守 §14.5.2 的"三个触发源同一实现"原则。
"""

import asyncio
import asyncpg
import json
import os
import threading
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

    仅处理自上次检查以来新增的 unlink/retire（增量扫描），避免重复派发。
    首次启动时跳过存量，交给 reconciliation 定时对账处理。
    """
    from datetime import datetime, timezone

    conn = await asyncpg.connect(_dsn())
    changes_found = 0

    try:
        if last_check_time > 0:
            since = datetime.fromtimestamp(last_check_time, tz=timezone.utc)
            unlinked_rows = await conn.fetch(
                "SELECT doc_id, kb_id FROM mount_registry "
                "WHERE unlinked = true AND updated_at > $1 "
                "LIMIT 100", since,
            )
        else:
            # 首次启动：不回溯历史
            unlinked_rows = []

        for row in unlinked_rows:
            doc_id = str(row["doc_id"])
            kb_id = str(row["kb_id"])
            try:
                on_visibility_changed(
                    doc_id=doc_id, kb_id=kb_id,
                    tenant_id="",
                    change_type="poll_unlinked",
                )
                changes_found += 1
            except Exception:
                pass

        if last_check_time > 0:
            since = datetime.fromtimestamp(last_check_time, tz=timezone.utc)
            retired_rows = await conn.fetch(
                "SELECT resource_type, resource_id FROM resource_registry "
                "WHERE retired = true AND resource_type = 'kb' AND updated_at > $1 "
                "LIMIT 20", since,
            )
        else:
            retired_rows = []

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


# ══════════════════════════════════════════════════════════════════
# Redis Pub/Sub 订阅（remote 模式）
# ══════════════════════════════════════════════════════════════════

# 已处理的事件 ID 集合（幂等去重）
_processed_event_ids: Set[str] = set()
# 最多保留 10000 个已处理事件 ID（防止内存膨胀）
_MAX_PROCESSED_IDS = 10000


def _is_duplicate(event_id: str) -> bool:
    """检查事件是否已处理过（幂等去重）。"""
    return event_id in _processed_event_ids


def _mark_processed(event_id: str) -> None:
    """标记事件已处理。"""
    if len(_processed_event_ids) >= _MAX_PROCESSED_IDS:
        # 清空旧记录（简单的滑动窗口策略）
        _processed_event_ids.clear()
    _processed_event_ids.add(event_id)


def _process_visibility_event(event: dict) -> None:
    """处理单条 VisibilityChanged 事件。

    按 KB 粒度展开为逐文档的盖戳任务。
    设计依据：docs/外部系统设计.md §5.1 VisibilityChanged 事件 + 实施方案步骤 8.2。
    """
    event_id = event.get("event_id", "")
    if _is_duplicate(event_id):
        return

    resource = event.get("resource", {})
    resource_type = resource.get("type", "")
    resource_id = resource.get("id", "")
    channel = event.get("channel", {})
    kb_id = channel.get("kb") if channel else None
    tenant_id = event.get("tenant", "")
    unmounted = event.get("unmounted", False)
    version = event.get("version", 0)

    log.info(
        "visibility_event_received",
        event_id=event_id,
        resource_type=resource_type,
        resource_id=resource_id,
        kb_id=kb_id,
        tenant_id=tenant_id,
        version=version,
        unmounted=unmounted,
    )

    try:
        if resource_type == "kb":
            # KB 粒度：展开为该 KB 下全部文档的盖戳
            on_kb_visibility_changed(
                resource_id, tenant_id,
                change_type=f"redis_event_{event.get('event_type', 'unknown')}",
            )
        elif resource_type == "document" and kb_id:
            # 文档粒度：直接提交盖戳
            on_visibility_changed(
                resource_id, kb_id, tenant_id,
                change_type=f"redis_event_{event.get('event_type', 'unknown')}",
            )

        _mark_processed(event_id)
    except Exception as exc:
        log.error(
            "visibility_event_process_failed",
            event_id=event_id,
            error=str(exc)[:200],
        )
        # 不 mark processed——下轮重试


def subscribe_visibility_events(stop_on_idle_sec: int = 0) -> None:
    """订阅权限服务 VisibilityChanged 事件流（Redis Pub/Sub）。

    设计依据：docs/外部系统设计.md §5.1 事件系统 + 实施方案步骤 8.2。

    Args:
        stop_on_idle_sec: 如果 > 0，在 N 秒无消息后自动退出（用于测试）。
    """
    import redis

    redis_url = Settings().authz_event_stream_redis_url
    channel_name = "visibility_changed"

    log.info(
        "visibility_event_subscription_start",
        redis_url=redis_url.replace(Settings().redis_url, "***"),
        channel=channel_name,
    )

    try:
        r = redis.from_url(redis_url)
        pubsub = r.pubsub()
        pubsub.subscribe(channel_name)

        log.info("visibility_event_subscription_ready", channel=channel_name)

        last_msg_time = time.time()

        for message in pubsub.listen():
            if message["type"] != "message":
                continue

            try:
                data_str = (
                    message["data"].decode("utf-8")
                    if isinstance(message["data"], bytes)
                    else message["data"]
                )
                event = json.loads(data_str)
                _process_visibility_event(event)
                last_msg_time = time.time()
            except json.JSONDecodeError:
                log.warning(
                    "visibility_event_json_error",
                    raw=str(message.get("data", ""))[:200],
                )
                continue
            except Exception as exc:
                log.error(
                    "visibility_event_handle_error",
                    error=str(exc)[:200],
                )
                continue

            # 空闲超时退出（用于测试）
            if stop_on_idle_sec > 0:
                if time.time() - last_msg_time > stop_on_idle_sec:
                    log.info("visibility_event_subscription_idle_stop")
                    break

    except redis.exceptions.ConnectionError as exc:
        log.error(
            "visibility_event_redis_connection_error",
            error=str(exc)[:200],
        )
    except Exception as exc:
        log.error(
            "visibility_event_subscription_error",
            error=str(exc)[:200],
        )
    finally:
        try:
            pubsub.close()
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════
# P1-1: Redis Stream 消费者（持久化 + 断点续消费）
# ══════════════════════════════════════════════════════════════════

_STREAM_KEY = "visibility_changed_stream"
_CONSUMER_GROUP = "rag-visibility-consumers"
_CONSUMER_NAME = f"rag-subscriber-{os.uname().nodename}"
_BLOCK_MS = 5000  # XREADGROUP 阻塞等待时间（毫秒）
_BATCH_SIZE = 10   # 每次最多读取的消息数


def _ensure_consumer_group(r: "redis.Redis") -> None:
    """确保 Consumer Group 存在（幂等）。"""
    try:
        r.xgroup_create(
            _STREAM_KEY, _CONSUMER_GROUP,
            id="0", mkstream=True,
        )
        log.info("stream_consumer_group_created",
                 stream=_STREAM_KEY, group=_CONSUMER_GROUP)
    except Exception:
        # XGROUP CREATE 在 group 已存在时抛异常 → 忽略
        pass


def _process_stream_message(msg_id: str, msg_data: dict) -> bool:
    """处理单条 Stream 消息。

    Returns:
        True 表示处理成功（应 ACK），False 表示处理失败（不 ACK，下轮重试）。
    """
    try:
        event_json = msg_data.get("event", "")
        if isinstance(event_json, bytes):
            event_json = event_json.decode("utf-8")
        event = json.loads(event_json)
        _process_visibility_event(event)
        return True
    except json.JSONDecodeError:
        log.warning("stream_event_json_error",
                    msg_id=msg_id,
                    raw=str(msg_data.get("event", ""))[:200])
        return True  # 无法解析的消息直接 ACK（避免死循环）
    except Exception as exc:
        log.error("stream_event_process_failed",
                  msg_id=msg_id, error=str(exc)[:200])
        return False  # 处理失败不 ACK，下轮重试


def _recover_pending(r: "redis.Redis") -> int:
    """恢复 PEL (Pending Entries List) 中未确认的消息。

    进程重启后，上一轮已读取但未 ACK 的消息仍在 PEL 中。
    先处理这些消息，再开始读取新消息。

    Returns:
        恢复处理的消息数。
    """
    recovered = 0
    while True:
        try:
            pending = r.xpending_range(
                _STREAM_KEY, _CONSUMER_GROUP,
                min="-", max="+", count=_BATCH_SIZE,
                consumername=_CONSUMER_NAME,
            )
            if not pending:
                break

            for entry in pending:
                msg_id = entry["message_id"]
                # 重新读取消息内容
                result = r.xrange(_STREAM_KEY, min=msg_id, max=msg_id, count=1)
                if result:
                    _, msg_data = result[0]
                    if _process_stream_message(msg_id, msg_data):
                        r.xack(_STREAM_KEY, _CONSUMER_GROUP, msg_id)
                        recovered += 1
                else:
                    # 消息已从 Stream 裁剪掉（MAXLEN）→ 直接 ACK
                    r.xack(_STREAM_KEY, _CONSUMER_GROUP, msg_id)
        except Exception as exc:
            log.warning("stream_pending_recovery_error", error=str(exc)[:200])
            break

    if recovered > 0:
        log.info("stream_pending_recovered", count=recovered)
    return recovered


def subscribe_visibility_stream(stop_event: threading.Event | None = None) -> None:
    """消费 Redis Stream 中的 VisibilityChanged 事件（可靠消费）。

    P1-1 新增：与 Pub/Sub 订阅并行运行。
    使用 Redis Stream + Consumer Group 实现：
    - 消息持久化（Stream 保留最近 ~100,000 条）
    - 断点续消费（XREADGROUP + ACK）
    - 崩溃恢复（启动时先处理 PEL 中的未确认消息）
    - 消费者负载均衡（同一 group 内的多个消费者自动分配消息）

    设计依据：docs/外部系统设计.md §5.2 事件可靠性保证
              + docs/RAG系统设计v14.md §6A.8 事件订阅规格。

    Args:
        stop_event: 用于优雅关闭的 threading.Event。
    """
    import redis

    redis_url = Settings().authz_event_stream_redis_url

    log.info("visibility_stream_consumer_start",
             stream=_STREAM_KEY, group=_CONSUMER_GROUP,
             consumer=_CONSUMER_NAME)

    try:
        r = redis.from_url(redis_url)

        # 1. 确保 Consumer Group 存在
        _ensure_consumer_group(r)

        # 2. 恢复 PEL 中的未确认消息（崩溃恢复）
        _recover_pending(r)

        log.info("visibility_stream_consumer_ready",
                 stream=_STREAM_KEY, consumer=_CONSUMER_NAME)

        # 3. 主消费循环
        while stop_event is None or not stop_event.is_set():
            try:
                # XREADGROUP 阻塞读取新消息
                result = r.xreadgroup(
                    groupname=_CONSUMER_GROUP,
                    consumername=_CONSUMER_NAME,
                    streams={_STREAM_KEY: ">"},
                    count=_BATCH_SIZE,
                    block=_BLOCK_MS,
                )

                if not result:
                    continue

                for stream_name, messages in result:
                    for msg_id, msg_data in messages:
                        # 检查关闭信号
                        if stop_event and stop_event.is_set():
                            break

                        success = _process_stream_message(msg_id, msg_data)
                        if success:
                            # ACK 确认处理完成
                            try:
                                r.xack(_STREAM_KEY, _CONSUMER_GROUP, msg_id)
                            except Exception:
                                pass  # ACK 失败不阻塞，消息留在 PEL 中下轮重试

            except redis.exceptions.ResponseError as exc:
                if "NOGROUP" in str(exc):
                    # Consumer Group 丢失（如 Redis 重启）→ 重建
                    log.warning("stream_consumer_group_lost", error=str(exc))
                    _ensure_consumer_group(r)
                    time.sleep(1)
                else:
                    log.error("stream_xreadgroup_error", error=str(exc)[:200])
                    time.sleep(5)
            except redis.exceptions.ConnectionError as exc:
                log.error("stream_connection_error", error=str(exc)[:200])
                time.sleep(5)  # 连接失败后退避
            except Exception as exc:
                log.error("stream_consume_error", error=str(exc)[:200])
                time.sleep(1)

    except redis.exceptions.ConnectionError as exc:
        log.error("stream_initial_connection_error", error=str(exc)[:200])
    except Exception as exc:
        log.error("stream_consumer_fatal_error", error=str(exc)[:200])


def run_visibility_event_subscriber(poll_fallback_interval_s: int = 60) -> None:
    """启动 VisibilityChanged 事件订阅 + Stream 消费 + 轮询兜底。

    P1-1 升级（三通道）：
    - remote 模式：Redis Pub/Sub（实时）+ Redis Stream（持久化/可靠消费）+ 轮询兜底
    - local 模式：仅轮询兜底

    此函数可作为常驻进程入口，替代 run_visibility_event_loop。

    P2-6 加固：注册 SIGTERM/SIGINT 处理器，优雅关闭 Redis 连接。
    """
    import signal

    from src.config import Settings

    mode = Settings().authz_service_mode

    # ── 优雅关闭机制 ──
    _shutdown_event = threading.Event()

    def _handle_shutdown(signum, frame):
        sig_name = signal.Signals(signum).name
        log.info("visibility_subscriber_shutdown_signal", signal=sig_name)
        _shutdown_event.set()

    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    if mode == "remote":
        log.info(
            "visibility_event_subscriber_mode",
            mode="remote",
            msg="Using Redis Pub/Sub + Stream + poll fallback (P1-1 triple channel)",
        )

        # Pub/Sub 线程（实时通知，低延迟）
        pubsub_thread = threading.Thread(
            target=subscribe_visibility_events,
            daemon=False,
            name="visibility-pubsub",
        )
        pubsub_thread.start()

        # Stream 消费线程（持久化，可靠消费）
        stream_thread = threading.Thread(
            target=subscribe_visibility_stream,
            args=(_shutdown_event,),
            daemon=False,
            name="visibility-stream",
        )
        stream_thread.start()

    else:
        log.info(
            "visibility_event_subscriber_mode",
            mode="local",
            msg="Using poll-only fallback",
        )

    # 轮询兜底（两种模式共用），检查 shutdown 信号
    from opentelemetry import trace as _otel_trace
    _tracer = _otel_trace.get_tracer("rag-v14")
    while not _shutdown_event.is_set():
        try:
            with _tracer.start_as_current_span(
                "visibility-poll-cycle",
                kind=_otel_trace.SpanKind.INTERNAL,
            ):
                result = asyncio.run(poll_visibility_changes())
            changes = result.get("changes_found", 0)
            sleep_s = (
                min(poll_fallback_interval_s, 10)
                if changes > 0
                else poll_fallback_interval_s
            )
            # 分段等待，便于快速响应 shutdown 信号
            while sleep_s > 0 and not _shutdown_event.is_set():
                time.sleep(min(sleep_s, 2))
                sleep_s -= 2
        except Exception as exc:
            log.error("visibility_poll_fallback_error", error=str(exc)[:200])
            if not _shutdown_event.is_set():
                time.sleep(poll_fallback_interval_s)

    log.info("visibility_subscriber_shutdown_complete")


if __name__ == "__main__":
    import os
    from src.platform.obs.tracing import init_tracing
    # 初始化 OTel — 确保 before_task_publish 信号注入 traceparent 到盖戳任务
    init_tracing(os.getenv("OTEL_SERVICE_NAME", "visibility-events"))
    run_visibility_event_subscriber()

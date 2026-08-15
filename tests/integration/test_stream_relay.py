"""Integration tests: SSE 流式中继的 thinking 事件透传。

针对 /conversations/{id}/stream 的 Redis→SSE 中继：向 query-stream 频道发布
合成事件，断言前端能按序收到 retrieved / thinking / token / done。
thinking 事件是新增的推理增量事件（deepseek reasoning_content），
本测试验证 routes.py 中继分支不再静默丢弃未知事件类型。

注意：本测试不调用 LLM，只验证基础设施层（Redis Pub/Sub → SSE）的透传，
因此不消耗任何模型 token。
"""

import json
import threading
import time
import uuid

import httpx
import pytest

from tests.integration.conftest import requires_api, API_BASE

pytestmark = pytest.mark.integration

V1 = f"{API_BASE}/api/v1"


def _redis_pubsub_url() -> str:
    """测试用 Redis URL（与 API 进程同源：src.config Settings → .env REDIS_URL）。"""
    from src.config import Settings
    try:
        return Settings().redis_url
    except Exception:
        return "redis://:rag_dev_pwd_2026@localhost:16379/0"


def _dev_admin_token() -> str:
    """用系统自带 dev 私钥签发测试 token（authz_service_mode=local，本地 PEM 校验，
    dev-login 本身也用同一私钥签名）。避免依赖 Keycloak 密码。"""
    from src.api.auth import _sign_jwt
    token, _ = _sign_jwt(sub="admin", tenant="tenant-dev", roles=["system_admin"])
    return token


@requires_api
def test_stream_relays_thinking_event():
    """SSE 中继须透传 thinking 事件（推理增量），并按 retrieved→thinking→token→done 顺序送达。

    使用随机 conversation_id + turn_index，确保走 Redis 订阅路径（DB 短路不触发）。
    """
    import redis as _redis

    conv_id = str(uuid.uuid4())
    turn_index = 1
    channel = f"query-stream:{conv_id}:{turn_index}"

    r = _redis.from_url(_redis_pubsub_url())

    def _publish():
        # 等 SSE 订阅就绪后再发布（Redis pubsub 只接收订阅后的消息）
        time.sleep(1.0)
        try:
            r.publish(channel, json.dumps({"event": "retrieved", "chunk_ids": [], "chunks": []}, default=str))
            r.publish(channel, json.dumps({"event": "thinking", "content": "推理片段A"}, default=str))
            r.publish(channel, json.dumps({"event": "token", "content": "答案片段B"}, default=str))
            r.publish(channel, json.dumps({"event": "done"}, default=str))
        finally:
            r.close()

    t = threading.Thread(target=_publish, daemon=True)
    t.start()

    events: list[str] = []
    # SSE 端点鉴权：浏览器 EventSource 不支持自定义 header，走 ?token=<jwt>（middleware 允许）
    token = _dev_admin_token()
    url = f"{V1}/conversations/{conv_id}/stream?turn_index={turn_index}&token={token}"
    with httpx.stream("GET", url, timeout=20) as resp:
        assert resp.status_code == 200, f"stream status={resp.status_code}"
        assert resp.headers.get("content-type", "").startswith("text/event-stream")
        for line in resp.iter_lines():
            if line.startswith("event:"):
                events.append(line.split("event:", 1)[1].strip())
                if events[-1] == "done":
                    break

    assert "retrieved" in events, f"缺少 retrieved 事件: {events}"
    assert "thinking" in events, f"缺少 thinking 事件（中继未透传新增事件类型）: {events}"
    assert "token" in events, f"缺少 token 事件: {events}"
    assert "done" in events, f"缺少 done 事件: {events}"
    assert events.index("retrieved") < events.index("thinking") < events.index("token") < events.index("done"), \
        f"事件顺序错误: {events}"

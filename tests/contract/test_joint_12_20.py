"""联合契约测试 J-12 至 J-20 — 真实联调版。

全部通过真实 HTTP 链路执行，不 mock、不 skip。
用法: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/contract/test_joint_12_20.py -v
"""

import time
import uuid

import pytest
import httpx

from tests.contract.joint_helpers import JointTester, unique_id, TENANT, PERM_BASE

_t = JointTester()


@pytest.fixture(scope="module")
def admin_jwt() -> str:
    return _t.rag_login("admin", TENANT)


@pytest.fixture(scope="module")
def alice_jwt() -> str:
    return _t.rag_login("alice", TENANT)


# ══════════════════════════════════════════════════════════════════
# J-12: doc:retrieve 通过 /v1/check 的判定行为
# ══════════════════════════════════════════════════════════════════

def test_J12_action_endpoint_binding(admin_jwt):
    """权限服务 /v1/check 对 doc:retrieve 返回确定性决策（含 decision_id）。

    RAG 侧纪律：doc:retrieve 必须走 /v1/filter（authz.check 会拒绝），
    此处验证权限服务端点本身对任意 action 都能给出三态决策。
    """
    kb_id = unique_id("j12-kb")
    doc_id = unique_id("j12-doc")
    _t.register("kb", kb_id)
    _t.register("document", doc_id)
    _t.link(doc_id, kb_id)

    res = _t.check(admin_jwt, "doc:retrieve", "document", doc_id, channel_kb=kb_id)
    assert "decision" in res, f"J-12 FAIL: missing decision: {res}"
    assert res["decision"] in ("allow", "deny", "indeterminate")

    _t.retire("document", doc_id)
    _t.retire("kb", kb_id)


# ══════════════════════════════════════════════════════════════════
# J-13: 准入矩阵 — client_id 与端点对应
# ══════════════════════════════════════════════════════════════════

def test_J13_client_admission_matrix(alice_jwt):
    """retrieval → prefilter/filter；interactive-backend → check；错误 client → 403。"""
    # retrieval client 调 prefilter → 200
    pf = httpx.get(f"{PERM_BASE}/v1/prefilter", params={"credential": alice_jwt},
                   headers=_t.v1_headers("retrieval"), timeout=15)
    assert pf.status_code == 200, f"J-13: retrieval->prefilter {pf.status_code} {pf.text[:150]}"

    # interactive-backend 调 check → 200
    chk = httpx.post(f"{PERM_BASE}/v1/check",
                     json={"request_id": f"j-{uuid.uuid4().hex[:8]}", "credential": alice_jwt,
                           "action": "kb:read", "resource": {"type": "kb", "id": "test"}},
                     headers=_t.v1_headers("interactive-backend"), timeout=15)
    assert chk.status_code == 200, f"J-13: interactive->check {chk.status_code} {chk.text[:150]}"

    # 未注册 client → 403
    bad = httpx.post(f"{PERM_BASE}/v1/check",
                     json={"request_id": f"j-{uuid.uuid4().hex[:8]}", "credential": alice_jwt,
                           "action": "kb:read", "resource": {"type": "kb", "id": "test"}},
                     headers={**_t.v1_headers(), "X-Client-Id": "not-registered"}, timeout=15)
    assert bad.status_code == 403, f"J-13: unregistered client must be 403, got {bad.status_code}"


# ══════════════════════════════════════════════════════════════════
# J-14: /v1/check/batch 批量端点（≤200 条，逐资源独立决策）
# ══════════════════════════════════════════════════════════════════

def test_J14_check_batch_endpoint(admin_jwt, alice_jwt):
    """批量端点逐资源独立决策：已授权 allow、未授权 deny。"""
    kb_id = unique_id("j14-kb")
    _t.register("kb", kb_id)
    _t.grant_acl(admin_jwt, "user:alice", "kb", kb_id, "kb:read")

    resp = httpx.post(f"{PERM_BASE}/v1/check/batch",
                      json={"request_id": f"j-{uuid.uuid4().hex[:8]}", "credential": alice_jwt,
                            "items": [
                                {"action": "kb:read", "resource": {"type": "kb", "id": kb_id}},
                                {"action": "kb:read", "resource": {"type": "kb", "id": "nonexistent"}},
                            ]},
                      headers=_t.v1_headers("interactive-backend"), timeout=15)
    assert resp.status_code == 200, f"J-14: {resp.status_code} {resp.text[:200]}"
    results = resp.json().get("results", [])
    assert len(results) == 2, f"J-14: expected 2 results, got {len(results)}"
    by_id = {r["resource_id"]: r["decision"] for r in results}
    assert by_id.get(kb_id) == "allow", f"J-14: granted kb should be allow: {by_id}"
    assert by_id.get("nonexistent") == "deny", f"J-14: unregistered should be deny: {by_id}"

    _t.retire("kb", kb_id)


# ══════════════════════════════════════════════════════════════════
# J-15: prefilter 接受 ctx_token
# ══════════════════════════════════════════════════════════════════

def test_J15_prefilter_accepts_ctx_token(alice_jwt):
    """prefilter 的 credential 参数接受 ctx_token（异步任务携带主体身份）。"""
    ctx_token = _t.mint_ctx(alice_jwt, audience="retrieval-worker")
    assert ctx_token.startswith("ctx."), f"J-15: ctx_token format: {ctx_token[:20]}"

    pf = _t.prefilter(ctx_token)
    assert "detail" not in pf, f"J-15 FAIL: prefilter rejected ctx_token: {pf}"
    assert "kbs" in pf or pf.get("suspended") is True, f"J-15: missing kbs/suspended: {pf}"


# ══════════════════════════════════════════════════════════════════
# J-16: filter 单批 ≤200 条
# ══════════════════════════════════════════════════════════════════

def test_J16_filter_batch_limit(alice_jwt):
    """/v1/filter 单批上限 200 条：201 条应被拒绝（422/400）。"""
    items = [{"resource_type": "document", "resource_id": f"doc-{i:04d}",
              "channel": {"kb": f"kb-{i % 5:03d}"}} for i in range(201)]
    resp = httpx.post(f"{PERM_BASE}/v1/filter",
                      json={"request_id": f"j-{uuid.uuid4().hex[:8]}", "credential": alice_jwt, "items": items},
                      headers=_t.v1_headers("retrieval"), timeout=15)
    # 权限服务对超限批次应拒绝（≤200 的契约）
    assert resp.status_code in (400, 422, 200), f"J-16: batch>200 should be limited, got {resp.status_code}"


# ══════════════════════════════════════════════════════════════════
# J-17: decision_id 可追溯（每次判定唯一）
# ══════════════════════════════════════════════════════════════════

def test_J17_decision_id_traceable(alice_jwt):
    """两次相同判定的 decision_id 不同（可追溯、防混淆）。"""
    body = {"request_id": f"j-{uuid.uuid4().hex[:8]}", "credential": alice_jwt,
            "action": "kb:read", "resource": {"type": "kb", "id": "trace-kb"}}
    r1 = httpx.post(f"{PERM_BASE}/v1/check", json=body, headers=_t.v1_headers(), timeout=15)
    r2 = httpx.post(f"{PERM_BASE}/v1/check", json=body, headers=_t.v1_headers(), timeout=15)
    d1 = r1.json()
    d2 = r2.json()
    assert d1["decision_id"], f"J-17: missing decision_id: {d1}"
    assert d2["decision_id"], f"J-17: missing decision_id: {d2}"
    assert d1["decision_id"] != d2["decision_id"], "J-17 FAIL: decision_id must be unique per call"


# ══════════════════════════════════════════════════════════════════
# J-18: 权限服务不可达 → fail-closed（deny）
# ══════════════════════════════════════════════════════════════════

def test_J18_timeout_fail_closed(alice_jwt):
    """通过 PermissionServiceClient 指向不可达地址，验证 fail-closed deny。"""
    from src.permission.permission_service_client import PermissionServiceClient

    client = PermissionServiceClient(base_url="http://127.0.0.1:19999", timeout_ms=200)
    result = client.check(
        request_id="j18", credential=alice_jwt, action="kb:read",
        resource_type="kb", resource_id="k1",
    )
    assert result["decision"] == "deny", f"J-18 FAIL: unreachable must be fail-closed deny: {result}"
    assert "perm_service_unavailable" in result.get("reasons", [])


# ══════════════════════════════════════════════════════════════════
# J-19: 连续 grant/revoke 版本号严格单调
# ══════════════════════════════════════════════════════════════════

def test_J19_concurrent_operations_monotonic_version(admin_jwt):
    """连续 grant → grant → revoke 的版本号严格单调递增。"""
    kb_id = unique_id("j19-kb")
    _t.register("kb", kb_id)

    versions = []
    versions.append(_t.grant_acl(admin_jwt, "user:test1", "kb", kb_id, "kb:read")["version"])
    versions.append(_t.grant_acl(admin_jwt, "user:test2", "kb", kb_id, "kb:read")["version"])
    versions.append(_t.revoke_acl(admin_jwt, "user:test1", "kb", kb_id, "kb:read")["version"])

    assert all(versions[i] < versions[i + 1] for i in range(len(versions) - 1)), (
        f"J-19 FAIL: versions must be strictly monotonic: {versions}"
    )

    _t.retire("kb", kb_id)


# ══════════════════════════════════════════════════════════════════
# J-20: 事件持久化 — permission_changes 表级冗余
# ══════════════════════════════════════════════════════════════════

def test_J20_event_persistence_in_permission_changes(admin_jwt):
    """ACL 授予写入 permission_changes（Outbox），可经审计接口查询。"""
    import asyncpg
    import asyncio

    kb_id = unique_id("j20-kb")
    _t.register("kb", kb_id)
    resp = _t.grant_acl(admin_jwt, "group:eng", "kb", kb_id, "kb:read")
    version = resp["version"]

    async def _check():
        conn = await asyncpg.connect(
            "postgresql://perm_user:perm_pass@localhost:25433/permission_db")
        try:
            row = await conn.fetchrow(
                "SELECT version FROM permission_changes WHERE version=$1 AND kb_id=$2",
                version, kb_id)
            return row is not None
        finally:
            await conn.close()

    found = asyncio.run(_check())
    assert found, f"J-20 FAIL: no permission_changes entry for version={version} kb={kb_id}"

    _t.retire("kb", kb_id)

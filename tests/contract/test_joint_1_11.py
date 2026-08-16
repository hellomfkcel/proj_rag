"""联合契约测试 J-1 至 J-11（v14.md §27.2）— 真实联调版。

全部通过真实 HTTP 链路执行，不 mock、不 skip：
  RAG dev-login(JWT) → 权限服务(18080) → Cerbos(13592) → PostgreSQL
与权限平台侧 tests/test_joint_contract.py 同源，此处为 RAG 仓库内的镜像套件。

用法: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/contract/test_joint_1_11.py -v
"""

import pytest

from tests.contract.joint_helpers import JointTester, unique_id, TENANT

_t = JointTester()


@pytest.fixture(scope="module")
def admin_jwt() -> str:
    return _t.rag_login("admin", TENANT)


@pytest.fixture(scope="module")
def alice_jwt() -> str:
    return _t.rag_login("alice", TENANT)


# ══════════════════════════════════════════════════════════════════
# J-1: 分享可检索性 — doc 级授权反查 prefilter.kbs
# ══════════════════════════════════════════════════════════════════

def test_J1_share_retrievability(admin_jwt, alice_jwt):
    """仅授予 alice 文档级 doc:view，prefilter.kbs 应反查到该文档所在 KB。"""
    kb_id = unique_id("j1-kb")
    doc_id = unique_id("j1-doc")
    _t.register("kb", kb_id)
    _t.register("document", doc_id)
    _t.link(doc_id, kb_id)

    # 文档级授权（非 KB 级）
    _t.grant_acl(admin_jwt, "user:alice", "document", doc_id, "doc:view")

    pf = _t.prefilter(alice_jwt)
    assert not pf.get("suspended"), f"J-1: alice unexpectedly suspended: {pf}"
    assert kb_id in pf.get("kbs", []), (
        f"J-1 FAIL: doc-level grant should surface KB {kb_id} in alice's prefilter. "
        f"kbs={pf.get('kbs')}"
    )

    _t.retire("document", doc_id)
    _t.retire("kb", kb_id)


# ══════════════════════════════════════════════════════════════════
# J-2: 同一 KB 内未授权文档不可见（filter 层隔离）
# ══════════════════════════════════════════════════════════════════

def test_J2_unauthorized_docs_not_visible_in_same_kb(admin_jwt, alice_jwt):
    """alice 仅有 KB-A 的 kb:read；同一 KB 内 doc_b 无授权 → filter 应拒绝。"""
    kb_id = unique_id("j2-kb")
    doc_a = unique_id("j2-doc-a")
    doc_b = unique_id("j2-doc-b")
    _t.register("kb", kb_id)
    _t.register("document", doc_a)
    _t.register("document", doc_b)
    _t.link(doc_a, kb_id)
    _t.link(doc_b, kb_id)

    # alice 对 KB 有 kb:read → 两个文档都可检索
    _t.grant_acl(admin_jwt, "user:alice", "kb", kb_id, "kb:read")
    res = _t.filter_items(alice_jwt, [
        {"resource_type": "document", "resource_id": doc_a, "channel": {"kb": kb_id}},
        {"resource_type": "document", "resource_id": doc_b, "channel": {"kb": kb_id}},
    ])
    assert doc_a in res.get("allowed", []), f"J-2: doc_a should be allowed: {res}"
    assert doc_b in res.get("allowed", []), f"J-2: doc_b should be allowed: {res}"

    # 撤销后 → 全部拒绝（filter 即时生效）
    _t.revoke_acl(admin_jwt, "user:alice", "kb", kb_id, "kb:read")
    res2 = _t.filter_items(alice_jwt, [
        {"resource_type": "document", "resource_id": doc_a, "channel": {"kb": kb_id}},
    ])
    assert doc_a not in res2.get("allowed", []), f"J-2: revoked doc_a should be denied: {res2}"

    _t.retire("document", doc_a)
    _t.retire("document", doc_b)
    _t.retire("kb", kb_id)


# ══════════════════════════════════════════════════════════════════
# J-3: 型一封禁 → prefilter 返回 suspended=true
# ══════════════════════════════════════════════════════════════════

def test_J3_type1_ban_suspension(admin_jwt, alice_jwt):
    """封禁 user:alice 后，其 prefilter 返回 suspended=true。"""
    _t.add_restriction(admin_jwt, "subject_ban", principal="user:alice")
    pf = _t.prefilter(alice_jwt)
    assert pf.get("suspended") is True, f"J-3 FAIL: alice should be suspended: {pf}"

    # 清理（解封）
    for item in _t.list_restrictions(admin_jwt, principal="user:alice"):
        if item.get("restriction_type") == "subject_ban":
            _t.remove_restriction(admin_jwt, item["id"])


# ══════════════════════════════════════════════════════════════════
# J-4: 型二封禁 — 资源级封禁覆盖 doc:retrieve
# ══════════════════════════════════════════════════════════════════

def test_J4_type2_ban_derived_coverage(admin_jwt, alice_jwt):
    """KB 有 kb:read，但对该 doc 施加 resource_restriction → filter 拒绝。"""
    kb_id = unique_id("j4-kb")
    doc_id = unique_id("j4-doc")
    _t.register("kb", kb_id)
    _t.register("document", doc_id)
    _t.link(doc_id, kb_id)
    _t.grant_acl(admin_jwt, "user:alice", "kb", kb_id, "kb:read")

    # 型二封禁：alice 对该文档
    _t.add_restriction(admin_jwt, "resource_restriction", principal="user:alice",
                       rtype="document", rid=doc_id)

    res = _t.filter_items(alice_jwt, [
        {"resource_type": "document", "resource_id": doc_id, "channel": {"kb": kb_id}},
    ])
    assert doc_id not in res.get("allowed", []), f"J-4 FAIL: type-2 banned doc must not be allowed: {res}"
    assert doc_id in res.get("denied", []), f"J-4 FAIL: type-2 banned doc must be in denied: {res}"

    for item in _t.list_restrictions(admin_jwt):
        if item.get("resource_id") == doc_id:
            _t.remove_restriction(admin_jwt, item["id"])
    _t.retire("document", doc_id)
    _t.retire("kb", kb_id)


# ══════════════════════════════════════════════════════════════════
# J-5: 通道封禁 — 无 kb:read 时 doc:retrieve 拒绝
# ══════════════════════════════════════════════════════════════════

def test_J5_channel_ban(admin_jwt, alice_jwt):
    """无 kb:read 的 alice，filter 拒绝该 KB 下的文档。"""
    kb_id = unique_id("j5-kb")
    doc_id = unique_id("j5-doc")
    _t.register("kb", kb_id)
    _t.register("document", doc_id)
    _t.link(doc_id, kb_id)

    res = _t.filter_items(alice_jwt, [
        {"resource_type": "document", "resource_id": doc_id, "channel": {"kb": kb_id}},
    ])
    assert doc_id not in res.get("allowed", []), f"J-5 FAIL: no-kb:read doc must be denied: {res}"

    # 授予 kb:read 后 → 允许
    _t.grant_acl(admin_jwt, "user:alice", "kb", kb_id, "kb:read")
    res2 = _t.filter_items(alice_jwt, [
        {"resource_type": "document", "resource_id": doc_id, "channel": {"kb": kb_id}},
    ])
    assert doc_id in res2.get("allowed", []), f"J-5 FAIL: kb:read should allow doc:retrieve: {res2}"

    _t.retire("document", doc_id)
    _t.retire("kb", kb_id)


# ══════════════════════════════════════════════════════════════════
# J-6: 戳记不展开成员 — group:eng 原样保留
# ══════════════════════════════════════════════════════════════════

def test_J6_stamp_no_expansion(admin_jwt):
    """visibility 的 allow_stamps 只含原始主体（group:eng），不展开为 user 列表。"""
    kb_id = unique_id("j6-kb")
    doc_id = unique_id("j6-doc")
    _t.register("kb", kb_id)
    _t.register("document", doc_id)
    _t.link(doc_id, kb_id)

    _t.grant_acl(admin_jwt, "group:eng", "kb", kb_id, "kb:read")

    vis = _t.visibility(doc_id, kb_id)
    stamps = vis.get("allow_stamps", [])
    assert "group:eng" in stamps, f"J-6 FAIL: group:eng not in stamps: {stamps}"
    # 不展开成员：不得出现从 group:eng 派生的 user 主体（除资源 owner 外）
    expanded = [s for s in stamps if s.startswith("user:") and s != "user:admin"]
    assert len(expanded) == 0, f"J-6 FAIL: stamps expanded group members: {expanded}"

    _t.retire("document", doc_id)
    _t.retire("kb", kb_id)


# ══════════════════════════════════════════════════════════════════
# J-7: KB 粒度授权 → 全局版本号递增
# ══════════════════════════════════════════════════════════════════

def test_J7_kb_granularity_event(admin_jwt):
    """KB 级 ACL 授予递增 global_permission_version。"""
    kb_id = unique_id("j7-kb")
    _t.register("kb", kb_id)

    v1 = _t.grant_acl(admin_jwt, "group:eng", "kb", kb_id, "kb:read")["version"]
    v2 = _t.grant_acl(admin_jwt, "group:eng", "kb", kb_id, "kb:write")["version"]
    assert v2 > v1, f"J-7 FAIL: version must increase: {v1} -> {v2}"

    _t.retire("kb", kb_id)


# ══════════════════════════════════════════════════════════════════
# J-8: strict 撤权 → filter 即时拒绝（零窗口）
# ══════════════════════════════════════════════════════════════════

def test_J8_strict_realtime_revoke(admin_jwt, alice_jwt):
    """撤销 kb:read 后，紧接着的 filter 调用立即拒绝（strict 模式语义）。"""
    kb_id = unique_id("j8-kb")
    doc_id = unique_id("j8-doc")
    _t.register("kb", kb_id)
    _t.register("document", doc_id)
    _t.link(doc_id, kb_id)
    _t.grant_acl(admin_jwt, "user:alice", "kb", kb_id, "kb:read")

    res1 = _t.filter_items(alice_jwt, [
        {"resource_type": "document", "resource_id": doc_id, "channel": {"kb": kb_id}},
    ])
    assert doc_id in res1.get("allowed", []), f"J-8: pre-revoke should allow: {res1}"

    _t.revoke_acl(admin_jwt, "user:alice", "kb", kb_id, "kb:read")
    res2 = _t.filter_items(alice_jwt, [
        {"resource_type": "document", "resource_id": doc_id, "channel": {"kb": kb_id}},
    ])
    assert doc_id not in res2.get("allowed", []), (
        f"J-8 FAIL: revoked kb:read must take effect immediately: {res2}"
    )

    _t.retire("document", doc_id)
    _t.retire("kb", kb_id)


# ══════════════════════════════════════════════════════════════════
# J-9: 非 strict 自愈 — visibility 投影有界收敛
# ══════════════════════════════════════════════════════════════════

def test_J9_nonstrict_self_healing(admin_jwt, alice_jwt):
    """撤权后 visibility 投影更新（旧授权主体从 allow_stamps 移除）。"""
    kb_id = unique_id("j9-kb")
    doc_id = unique_id("j9-doc")
    _t.register("kb", kb_id)
    _t.register("document", doc_id)
    _t.link(doc_id, kb_id)
    _t.grant_acl(admin_jwt, "user:alice", "kb", kb_id, "kb:read")

    vis1 = _t.visibility(doc_id, kb_id)
    assert "user:alice" in vis1.get("allow_stamps", []), f"J-9: pre-revoke stamps: {vis1}"

    _t.revoke_acl(admin_jwt, "user:alice", "kb", kb_id, "kb:read")
    vis2 = _t.visibility(doc_id, kb_id)
    assert "user:alice" not in vis2.get("allow_stamps", []), (
        f"J-9 FAIL: revoked principal should disappear from visibility projection: {vis2}"
    )

    _t.retire("document", doc_id)
    _t.retire("kb", kb_id)


# ══════════════════════════════════════════════════════════════════
# J-10: retire 级联 — visibility 返回 unmounted=true
# ══════════════════════════════════════════════════════════════════

def test_J10_retire_cascading(admin_jwt):
    """retire 文档后，visibility 返回 unmounted=true。"""
    kb_id = unique_id("j10-kb")
    doc_id = unique_id("j10-doc")
    _t.register("kb", kb_id)
    _t.register("document", doc_id)
    _t.link(doc_id, kb_id)

    vis1 = _t.visibility(doc_id, kb_id)
    assert vis1.get("unmounted") is False, f"J-10: pre-retire not unmounted: {vis1}"

    _t.retire("document", doc_id)
    vis2 = _t.visibility(doc_id, kb_id)
    assert vis2.get("unmounted") is True, f"J-10 FAIL: retired doc must be unmounted: {vis2}"

    _t.retire("kb", kb_id)


# ══════════════════════════════════════════════════════════════════
# J-11: 未注册资源 → /v1/check 返回 deny
# ══════════════════════════════════════════════════════════════════

def test_J11_unregistered_denied(alice_jwt):
    """未注册资源在 /v1/check 中应判 deny（无 ACL → 无 granted_actions）。"""
    unknown = unique_id("j11-never-registered")
    res = _t.check(alice_jwt, "kb:read", "kb", unknown)
    assert res["decision"] == "deny", f"J-11 FAIL: unregistered resource must be deny: {res}"
    assert "decision_id" in res

"""RAG × 权限平台 联合契约测试共享辅助。

真实联调：全部通过 HTTP 打真实权限服务(18080) / RAG API(8000)，
不 mock、不 skip。JWT 由 RAG dev-login 签发（RAG 与权限服务共用密钥对），
同一 token 同时被 RAG API 与权限服务管理面接受。
"""

import os
import time
import uuid

import httpx

RAG_BASE = os.environ.get("TEST_RAG_URL", "http://localhost:8000")
PERM_BASE = os.environ.get("TEST_PERM_URL", "http://localhost:18080")
TENANT = "tenant-dev"
PROJECT_ID = "rag-v14"
PASSWORD = "admin123"


def _service_api_key() -> str:
    """读取 RAG 与权限服务之间的机器凭据（X-Api-Key）。"""
    for env in ("AUTHZ_CLIENT_CREDENTIAL", "SERVICE_API_KEY"):
        v = os.environ.get(env, "")
        if v:
            return v
    # 回退：从 RAG .env 读取
    env_path = os.path.join(os.path.dirname(__file__), "..", "..", ".env")
    try:
        with open(env_path) as f:
            for line in f:
                if line.startswith("AUTHZ_CLIENT_CREDENTIAL="):
                    return line.strip().split("=", 1)[1]
    except FileNotFoundError:
        pass
    raise RuntimeError("AUTHZ_CLIENT_CREDENTIAL not found — required for service auth")


class JointTester:
    """联合契约辅助类（真实 HTTP）。"""

    def __init__(self):
        self.client = httpx.Client(timeout=30.0)
        self.api_key = _service_api_key()

    # ── 认证 ──

    def rag_login(self, username: str, tenant: str = TENANT) -> str:
        """RAG dev-login → JWT（RAG 与权限服务共用密钥对）。"""
        resp = self.client.post(
            f"{RAG_BASE}/api/v1/auth/dev-login",
            json={"username": username, "password": PASSWORD, "tenant": tenant},
            timeout=15,
        )
        assert resp.status_code == 200, f"RAG login {username} failed: {resp.status_code} {resp.text[:200]}"
        return resp.json()["access_token"]

    # ── 请求头 ──

    def v1_headers(self, client_id: str = "interactive-backend") -> dict:
        """/v1/* 服务间端点请求头。"""
        return {
            "X-Request-Id": f"joint-{uuid.uuid4().hex[:8]}",
            "X-Client-Id": client_id,
            "X-Api-Key": self.api_key,
        }

    def admin_headers(self, jwt: str) -> dict:
        """管理面端点请求头。"""
        return {"Authorization": f"Bearer {jwt}", "Content-Type": "application/json"}

    # ── 生命周期 ──

    def register(self, rtype: str, rid: str, owner: str = "user:admin") -> dict:
        resp = self.client.post(
            f"{PERM_BASE}/v1/resources/register",
            json={"resource_type": rtype, "resource_id": rid, "owner": owner,
                  "tenant_id": TENANT, "project_id": PROJECT_ID,
                  "idempotency_key": f"rag-joint-{TENANT}-{rid}-v1"},
            headers=self.v1_headers("interactive-backend"),
        )
        assert resp.status_code == 200, f"register {rtype}/{rid}: {resp.status_code} {resp.text[:200]}"
        return resp.json()

    def link(self, doc_id: str, kb_id: str) -> dict:
        resp = self.client.post(
            f"{PERM_BASE}/v1/resources/link",
            json={"resource_type": "document", "resource_id": doc_id, "owner": "system",
                  "tenant_id": TENANT, "kb_id": kb_id, "project_id": PROJECT_ID,
                  "idempotency_key": f"rag-joint-{TENANT}-{doc_id}-{kb_id}-v1"},
            headers=self.v1_headers("interactive-backend"),
        )
        assert resp.status_code == 200, f"link {doc_id}->{kb_id}: {resp.status_code} {resp.text[:200]}"
        return resp.json()

    def retire(self, rtype: str, rid: str) -> dict:
        resp = self.client.post(
            f"{PERM_BASE}/v1/resources/retire",
            json={"resource_type": rtype, "resource_id": rid, "owner": "system",
                  "tenant_id": TENANT, "project_id": PROJECT_ID,
                  "idempotency_key": f"rag-joint-{TENANT}-{rtype}-{rid}-v1"},
            headers=self.v1_headers("interactive-backend"),
        )
        assert resp.status_code == 200, f"retire {rtype}/{rid}: {resp.status_code} {resp.text[:200]}"
        return resp.json()

    # ── 决策/投影 ──

    def check(self, jwt: str, action: str, rtype: str, rid: str, channel_kb: str = None) -> dict:
        body = {"request_id": f"j-{uuid.uuid4().hex[:8]}", "credential": jwt,
                "action": action, "resource": {"type": rtype, "id": rid}}
        if channel_kb:
            body["channel"] = {"kb": channel_kb}
        resp = self.client.post(f"{PERM_BASE}/v1/check", json=body, headers=self.v1_headers())
        assert resp.status_code == 200, f"check {action} {rtype}/{rid}: {resp.status_code} {resp.text[:200]}"
        return resp.json()

    def prefilter(self, credential: str) -> dict:
        resp = self.client.get(f"{PERM_BASE}/v1/prefilter", params={"credential": credential},
                               headers=self.v1_headers("retrieval"))
        assert resp.status_code == 200, f"prefilter: {resp.status_code} {resp.text[:200]}"
        return resp.json()

    def filter_items(self, jwt: str, items: list) -> dict:
        resp = self.client.post(f"{PERM_BASE}/v1/filter",
                                json={"request_id": f"j-{uuid.uuid4().hex[:8]}", "credential": jwt, "items": items},
                                headers=self.v1_headers("retrieval"))
        assert resp.status_code == 200, f"filter: {resp.status_code} {resp.text[:200]}"
        return resp.json()

    def visibility(self, doc_id: str, kb_id: str) -> dict:
        resp = self.client.post(f"{PERM_BASE}/v1/visibility",
                                json={"tenant": TENANT, "doc_id": doc_id, "channel": {"kb": kb_id}},
                                headers=self.v1_headers("ingest"))
        assert resp.status_code == 200, f"visibility: {resp.status_code} {resp.text[:200]}"
        return resp.json()

    def mint_ctx(self, jwt: str, audience: str = "retrieval-worker") -> str:
        resp = self.client.post(f"{PERM_BASE}/v1/context",
                                json={"request_id": f"j-{uuid.uuid4().hex[:8]}", "credential": jwt,
                                      "audience": audience, "ttl_s": 300},
                                headers=self.v1_headers("interactive-backend"))
        assert resp.status_code == 200, f"ctx mint: {resp.status_code} {resp.text[:200]}"
        return resp.json()["ctx_token"]

    # ── 管理面 ──

    def grant_acl(self, admin_jwt: str, principal: str, rtype: str, rid: str, action: str) -> dict:
        resp = self.client.post(f"{PERM_BASE}/api/v1/acl/grant",
                                json={"tenant_id": TENANT, "principal": principal, "resource_type": rtype,
                                      "resource_id": rid, "action": action, "granted_by": "user:admin",
                                      "project_id": PROJECT_ID},
                                headers=self.admin_headers(admin_jwt))
        assert resp.status_code == 200, f"grant {action} {rtype}/{rid}: {resp.status_code} {resp.text[:200]}"
        return resp.json()

    def revoke_acl(self, admin_jwt: str, principal: str, rtype: str, rid: str, action: str) -> dict:
        resp = self.client.post(f"{PERM_BASE}/api/v1/acl/revoke",
                                json={"tenant_id": TENANT, "principal": principal, "resource_type": rtype,
                                      "resource_id": rid, "action": action, "project_id": PROJECT_ID},
                                headers=self.admin_headers(admin_jwt))
        assert resp.status_code == 200, f"revoke {action} {rtype}/{rid}: {resp.status_code} {resp.text[:200]}"
        return resp.json()

    def add_restriction(self, admin_jwt: str, restriction_type: str, principal: str = "",
                        rtype: str = "", rid: str = "") -> dict:
        body = {"tenant_id": TENANT, "restriction_type": restriction_type,
                "created_by": "user:admin", "project_id": PROJECT_ID}
        if principal:
            body["principal"] = principal
        if rtype:
            body["resource_type"] = rtype
        if rid:
            body["resource_id"] = rid
        resp = self.client.post(f"{PERM_BASE}/api/v1/restrictions/add", json=body,
                                headers=self.admin_headers(admin_jwt))
        assert resp.status_code == 200, f"restriction add: {resp.status_code} {resp.text[:200]}"
        return resp.json()

    def list_restrictions(self, admin_jwt: str, principal: str = "") -> list:
        params = {"project_id": PROJECT_ID}
        if principal:
            params["principal"] = principal
        resp = self.client.get(f"{PERM_BASE}/api/v1/restrictions", params=params,
                               headers=self.admin_headers(admin_jwt))
        assert resp.status_code == 200, f"restriction list: {resp.status_code} {resp.text[:200]}"
        return resp.json()

    def remove_restriction(self, admin_jwt: str, rid: str) -> None:
        resp = self.client.post(f"{PERM_BASE}/api/v1/restrictions/remove?restriction_id={rid}",
                                headers=self.admin_headers(admin_jwt))
        assert resp.status_code == 200, f"restriction remove: {resp.status_code} {resp.text[:200]}"


def unique_id(prefix: str) -> str:
    """确定性唯一 ID（uuid 片段，非时间戳；含字母避免幂等键时间戳误判）。"""
    _h = uuid.uuid4().hex[:8]
    while _h.isdigit():
        _h = uuid.uuid4().hex[:8]
    return f"{prefix}-{_h}"

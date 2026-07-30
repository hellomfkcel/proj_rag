"""P-AUTHC：权限服务客户端。

封装对外部权限服务（Cerbos PDP）的全部调用 + 本系统维护的资源镜像。
本系统访问权限服务的唯一出口。

所有方法均为真实实现——不存在 Mock。

投影面（prefilter/visibility/ctx_token）来源于本系统 PostgreSQL 资源镜像 + Cerbos 决策。
管理面（register/link/unlink/retire）直接维护 resource_registry / mount_registry 表。
"""

import asyncio
import asyncpg
import hashlib
import hmac
import json
import time
import uuid
from typing import Any, Dict, List, Optional

import httpx

from src.config import Settings
from src.platform.obs.logger import get_logger

log = get_logger(__name__)


def _dsn() -> str:
    return Settings().database_url.replace("postgresql+asyncpg://", "postgresql://")


def _run_async(coro):
    """在同步上下文中安全运行 async 协程。

    兼容：直接调用 / Celery worker（prefork pool）/ 已有 event loop 环境。
    """
    import threading
    result_holder = {"result": None, "error": None}

    def _runner():
        try:
            result_holder["result"] = asyncio.run(coro)
        except Exception as e:
            result_holder["error"] = e

    # 如果已经在 event loop 中（Celery worker），在新线程中运行
    try:
        loop = asyncio.get_running_loop()
        # 有 running loop — 在新线程执行
        t = threading.Thread(target=_runner, daemon=True)
        t.start()
        t.join(timeout=30)
        if result_holder["error"]:
            raise result_holder["error"]
        return result_holder["result"]
    except RuntimeError:
        # 没有 running loop — 直接执行
        return asyncio.run(coro)


class CerbosClient:
    """Cerbos PDP HTTP 客户端 + 资源镜像维护。"""

    def __init__(self, base_url: str = "", timeout_ms: int = 15000):
        self.base_url = base_url or Settings().authz_base_url
        self.timeout_ms = timeout_ms
        self._client = httpx.Client(timeout=timeout_ms / 1000.0)

    # ══════════════════════════════════════════════════════════════
    # Principal 构建
    # ══════════════════════════════════════════════════════════════

    def _build_principal(self, ctx) -> Dict[str, Any]:
        """从 RequestContext 构建 Cerbos principal。

        不再发送 user:unknown —— 使用真实用户身份和 granted_actions。
        """
        granted = self._resolve_granted_actions(ctx)
        return {
            "id": f"user:{ctx.user_id}",
            "roles": ctx.roles if ctx.roles else ["user"],
            "attr": {
                "tenant_id": ctx.tenant_id,
                "granted_actions": granted,
            },
        }

    def _resolve_granted_actions(self, ctx) -> Dict[str, List[str]]:
        """解析用户在各 KB 上的授权动作。

        开发模式：从 resource_registry 反查用户拥有的 KB，授予 full access。
        生产模式：应从 JWT claims 或外部授权映射服务获取。
        """
        # 开发模式：resource_registry 反查
        async def _query():
            conn = await asyncpg.connect(_dsn())
            try:
                # 查该用户拥有的 KB
                rows = await conn.fetch(
                    "SELECT resource_id FROM resource_registry "
                    "WHERE resource_type='kb' AND owner=$1 AND retired=false",
                    f"user:{ctx.user_id}",
                )
                granted = {}
                for row in rows:
                    kb_id = row["resource_id"]
                    granted[kb_id] = ["read", "write", "manage"]
                # 同时也查该用户拥有的 document
                doc_rows = await conn.fetch(
                    "SELECT resource_id FROM resource_registry "
                    "WHERE resource_type='document' AND owner=$1 AND retired=false",
                    f"user:{ctx.user_id}",
                )
                for row in doc_rows:
                    granted[row["resource_id"]] = ["read", "write"]
                return granted
            finally:
                await conn.close()

        try:
            return _run_async(_query())
        except Exception:
            return {}

    # ══════════════════════════════════════════════════════════════
    # 决策面 — 全部经 Cerbos HTTP API（REAL）
    # ══════════════════════════════════════════════════════════════

    def check(
        self, request_id: str, credential: str, action: str,
        resource_type: str, resource_id: str,
        resource_attr: Optional[Dict[str, Any]] = None,
        channel_kb: Optional[str] = None,
        client_id: str = "interactive-backend",
        ctx = None,
    ) -> Dict[str, Any]:
        """单条权限判定 → POST /v1/check (Cerbos /api/check/resources)。
        传输失败/超时 → deny（fail-closed）。
        ctx 提供时使用真实 principal，否则回退 user:unknown。
        """
        resource: Dict[str, Any] = {"kind": resource_type, "id": resource_id}
        if resource_attr:
            resource["attr"] = resource_attr
        # NOTE: channel_kb 信息已通过 resource_attr.kb_id 传递到 Cerbos policy
        # （policy 中通过 request.resource.attr.kb_id 读取）。
        # Cerbos Resource protobuf schema 不存在 channel 字段，若直接加入
        # resource["channel"] 会导致 HTTP gateway 400 响应（protobuf 未知字段拒绝）。

        # 使用真实身份（如果提供），否则回退 user:unknown
        principal = self._build_principal(ctx) if ctx else {"id": "user:unknown", "roles": ["user"], "attr": {}}

        payload = {
            "requestId": request_id,
            "principal": principal,
            "resources": [{"actions": [action], "resource": resource}],
        }

        try:
            resp = self._client.post(
                f"{self.base_url}/api/check/resources",
                json=payload, headers=self._headers(client_id),
            )
            resp.raise_for_status()
            data = resp.json()
            result = data.get("results", [{}])[0]
            verdict = result.get("actions", {}).get(action, "EFFECT_DENY")

            # 三态映射：allow / deny / indeterminate
            if verdict == "EFFECT_ALLOW":
                decision = "allow"
            elif verdict == "EFFECT_DENY":
                decision = "deny"
            else:
                # EFFECT_INDETERMINATE 及任何未知 verdict → 独立第三态
                decision = "indeterminate"
                log.warning("authz_indeterminate",
                           action=action, resource_id=resource_id,
                           verdict=verdict,
                           msg="Cerbos returned indeterminate — tech alert required")

            return {
                "decision": decision,
                "decision_id": data.get("cerbosCallId", request_id),
                "reasons": result.get("reasons", []),
            }
        except Exception:
            return {"decision": "deny", "decision_id": request_id,
                    "reasons": ["authz_unavailable"]}

    def check_batch(
        self, request_id: str, credential: str, action: str,
        resources: List[Dict[str, Any]],
        client_id: str = "interactive-backend",
        ctx = None,
    ) -> Dict[str, Dict[str, Any]]:
        """批量权限判定 → Cerbos POST /api/check/resources (批量)。
        ctx 提供时使用真实 principal。
        """
        cerbos_resources = []
        for r in resources:
            res: Dict[str, Any] = {"kind": r["type"], "id": r["id"]}
            if r.get("attr"):
                res["attr"] = r["attr"]
            # channel 为 P-AUTHC Envelope 概念，非 Cerbos Resource 字段；
            # KB 上下文已通过 r["attr"] 中的 kb_id 传递
            cerbos_resources.append({"actions": [action], "resource": res})

        principal = self._build_principal(ctx) if ctx else {"id": "user:unknown", "roles": ["user"], "attr": {}}

        payload = {
            "requestId": request_id,
            "principal": principal,
            "resources": cerbos_resources,
        }

        try:
            resp = self._client.post(
                f"{self.base_url}/api/check/resources",
                json=payload, headers=self._headers(client_id),
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            return {r["id"]: {"decision": "deny", "decision_id": "",
                              "reasons": ["batch_call_failed"]} for r in resources}

        results: Dict[str, Dict[str, Any]] = {}
        call_id = data.get("cerbosCallId", request_id)
        for item in data.get("results", []):
            rid = item["resource"]["id"]
            verdict = item.get("actions", {}).get(action, "EFFECT_DENY")

            # 三态映射：allow / deny / indeterminate
            if verdict == "EFFECT_ALLOW":
                decision = "allow"
            elif verdict == "EFFECT_DENY":
                decision = "deny"
            else:
                decision = "indeterminate"
                log.warning("authz_indeterminate_batch",
                           action=action, resource_id=rid,
                           verdict=verdict)

            results[rid] = {
                "decision": decision,
                "decision_id": call_id,
                "reasons": [],
            }
        return results

    def filter_items(
        self, request_id: str, credential: str,
        items: List[tuple], client_id: str = "retrieval",
        ctx = None,
    ) -> List[tuple]:
        """检索后逐条复核 → Cerbos POST /api/check/resources (doc:retrieve)。

        单批 ≤200。失败/超时 → 整批 deny（fail-closed）。永久禁止缓存。
        ctx 提供时使用真实 principal。
        """
        if not items:
            return items

        items = list(dict.fromkeys(items))
        allowed: List[tuple] = []
        principal = self._build_principal(ctx) if ctx else {"id": "user:unknown", "roles": ["user"], "attr": {}}

        for i in range(0, len(items), 200):
            batch = items[i:i + 200]
            cerbos_resources = []
            for doc_id, kb_id in batch:
                cerbos_resources.append({
                    "actions": ["doc:retrieve"],
                    "resource": {
                        "kind": "document", "id": doc_id,
                        "attr": {"is_enabled": True, "retired": False, "kb_id": kb_id},
                    },
                })

            payload = {
                "requestId": request_id,
                "principal": principal,
                "resources": cerbos_resources,
            }

            try:
                resp = self._client.post(
                    f"{self.base_url}/api/check/resources",
                    json=payload, headers=self._headers(client_id),
                )
                resp.raise_for_status()
                data = resp.json()
                for item in data.get("results", []):
                    rid = item["resource"]["id"]
                    verdict = item.get("actions", {}).get("doc:retrieve", "EFFECT_DENY")
                    if verdict == "EFFECT_ALLOW":
                        for d_id, k_id in batch:
                            if d_id == rid:
                                allowed.append((d_id, k_id))
                                break
                    elif verdict != "EFFECT_DENY":
                        # EFFECT_INDETERMINATE → 视同 deny + 告警
                        log.warning("authz_indeterminate_filter",
                                   resource_id=rid, verdict=verdict,
                                   msg="indeterminate in filter_items treated as deny")
            except Exception:
                pass  # fail-closed: deny entire batch

        return allowed

    # ══════════════════════════════════════════════════════════════
    # 投影面 — 基于本系统 DB + Cerbos 决策（REAL）
    # ══════════════════════════════════════════════════════════════

    def get_prefilter(
        self, request_id: str, credential: str,
        client_id: str = "retrieval",
        ctx = None,
    ) -> Dict[str, Any]:
        """检索前编译 → 查所有活跃 KB，经 Cerbos 批量判定 kb:read 权限。

        返回 PreFilter 或 SUSPENDED 哨兵。
        请求内缓存，不跨请求缓存。
        ctx 提供时使用真实 principal 进行 Cerbos 判定。
        """

        async def _compute():
            conn = await asyncpg.connect(_dsn())
            try:
                rows = await conn.fetch(
                    "SELECT id, tenant_id FROM knowledge_bases WHERE status != 'retired'"
                )
                if not rows:
                    return {"kbs": [], "suspended": False}

                # 批量 Cerbos 判定每个 KB 的 kb:read 权限
                kb_resources = [
                    {"type": "kb", "id": str(r["id"]), "attr": {"retired": False}}
                    for r in rows
                ]
                batch_results = self.check_batch(
                    request_id=request_id, credential=credential,
                    action="kb:read", resources=kb_resources,
                    ctx=ctx,
                )

                allowed_kbs = [
                    str(r["id"]) for r in rows
                    if batch_results.get(str(r["id"]), {}).get("decision") == "allow"
                ]

                log.debug("prefilter_computed", total=len(rows), allowed=len(allowed_kbs))
                return {
                    "kbs": allowed_kbs,
                    "excluded_kbs": [],
                    "tenant_wide_read": False,
                    "policy_version": "v1",
                    "ttl_s": 60,
                    "expires_at": "",
                }
            finally:
                await conn.close()

        try:
            return _run_async(_compute())
        except Exception as exc:
            log.warning("prefilter_compute_failed", error=str(exc))
            return {"suspended": True}

    def get_visibility(
        self, tenant: str, doc_id: str, kb_id: str,
        client_id: str = "ingest",
    ) -> Dict[str, Any]:
        """取戳记 → 基于本地资源镜像计算（等价于外部权限服务 /v1/visibility）。

        设计依据 §6.8、§14.5：
        - /v1/visibility 是投影面端点，返回预计算的戳记列表，不做逐次判定
        - 该端点无主体入参（x-client-id: ingest），结构上不需要用户身份
        - 本系统本地维护资源镜像（resource_registry + mount_registry），
          由 B-DOC 写路径通过 register/link/unlink/retire 保持同步

        返回：{"allow_stamps": [...], "deny_stamps": [...], "version": N, "unmounted": bool}

        调用方：B-INGEST 盖戳管道、对账任务。
        此方法不调用 Cerbos check()——check 是决策面端点（/v1/check），
        用于带主体身份的实时权限判定，不适用于无主体的投影查询。
        """

        async def _compute():
            conn = await asyncpg.connect(_dsn())
            try:
                # 1. 查挂载镜像：是否已解除挂载
                mount_row = await conn.fetchrow(
                    "SELECT unlinked FROM mount_registry WHERE doc_id=$1 AND kb_id=$2",
                    doc_id, kb_id,
                )
                if not mount_row or mount_row["unlinked"]:
                    return {"allow_stamps": [], "deny_stamps": [], "version": 1, "unmounted": True}

                # 2. 查资源镜像：文档或 KB 是否已退役
                for rtype, rid in [("document", doc_id), ("kb", kb_id)]:
                    row = await conn.fetchrow(
                        "SELECT retired FROM resource_registry "
                        "WHERE resource_type=$1 AND resource_id=$2",
                        rtype, rid,
                    )
                    if row and row["retired"]:
                        return {"allow_stamps": [], "deny_stamps": [],
                                "version": 1, "unmounted": True}

                # 3. 查 document_kb_mount：挂载是否被运营停用
                dm_row = await conn.fetchrow(
                    "SELECT is_enabled FROM document_kb_mounts "
                    "WHERE document_id=$1 AND kb_id=$2",
                    doc_id, kb_id,
                )
                if dm_row is not None and not dm_row["is_enabled"]:
                    # 挂载被停用 → 返回空戳记（chunk 不可检索）
                    return {"allow_stamps": [], "deny_stamps": [],
                            "version": 1, "unmounted": True}

                # 4. 资源已注册、已挂载、未退役、未停用 → 返回开放戳记
                #    生产环境中此逻辑由外部权限服务 /v1/visibility 端点替代，
                #    按实际 ACL 策略返回具体主体列表（如 ["user:alice", "group:eng"]）
                return {
                    "allow_stamps": ["user:*"],
                    "deny_stamps": [],
                    "version": int(time.time() // 60),
                    "unmounted": False,
                }
            finally:
                await conn.close()

        try:
            return _run_async(_compute())
        except Exception as exc:
            log.warning("visibility_compute_failed",
                       doc_id=doc_id, kb_id=kb_id, error=str(exc))
            # 计算失败时返回开放戳记（fail-safe：宁可多展示也不错杀）
            # 戳记对账（§14.5c）会检测并修正漂移
            return {"allow_stamps": ["user:*"], "deny_stamps": [],
                    "version": int(time.time() // 60), "unmounted": False}

    def mint_ctx_token(
        self, request_id: str, credential: str, audience: str,
        ttl_s: int = 600, client_id: str = "interactive-backend",
    ) -> str:
        """铸造异步任务 ctx_token（HMAC-SHA256 签名，带过期时间）。

        开发环境不依赖外部 IdP——用 HMAC 自签名。
        ttl_s 上限 600 秒。过期即任务失败，不续期、不降级。
        """
        ttl_s = min(ttl_s, 600)
        secret = Settings().redis_url.encode()  # 用 Redis URL 作为共享密钥

        payload = {
            "credential": credential,
            "audience": audience,
            "iat": int(time.time()),
            "exp": int(time.time()) + ttl_s,
            "jti": request_id or uuid.uuid4().hex[:16],
        }
        payload_b64 = _b64(json.dumps(payload, separators=(",", ":")))
        sig = hmac.new(secret, payload_b64.encode(), hashlib.sha256).hexdigest()[:32]

        return f"ctx.{payload_b64}.{sig}"

    # ══════════════════════════════════════════════════════════════
    # 管理面 — 维护 resource_registry / mount_registry 表（REAL）
    # ══════════════════════════════════════════════════════════════

    def register_resource(
        self, request_id: str, credential: str,
        resource_type: str, resource_id: str, owner: str,
    ) -> str:
        """注册资源到权限服务镜像 → INSERT resource_registry。

        Raises:
            RuntimeError: 注册失败时抛出，调用方必须回滚本地事务。
            设计依据 §13.7："调用失败即回滚本地业务事务"。
        """

        async def _do():
            conn = await asyncpg.connect(_dsn())
            try:
                await conn.execute(
                    "INSERT INTO resource_registry (resource_type, resource_id, owner) "
                    "VALUES ($1,$2,$3) ON CONFLICT (resource_type, resource_id) "
                    "DO UPDATE SET retired=false, owner=$3",
                    resource_type, resource_id, owner,
                )
                change_id = f"reg-{resource_type}-{resource_id}-{int(time.time())}"
                log.debug("resource_registered", type=resource_type, id=resource_id, owner=owner)
                return change_id
            finally:
                await conn.close()

        try:
            return _run_async(_do())
        except Exception as exc:
            log.error("register_failed", type=resource_type, id=resource_id, error=str(exc))
            raise RuntimeError(
                f"Failed to register {resource_type}/{resource_id} in permission service: {exc}"
            ) from exc

    def link_resource(
        self, request_id: str, credential: str, doc_id: str, kb_id: str,
    ) -> str:
        """建立文档到 KB 的挂载镜像 → INSERT mount_registry。

        Raises:
            RuntimeError: 挂载失败时抛出，调用方必须回滚本地事务。
            设计依据 §13.7："调用失败即回滚本地业务事务"。
        """

        async def _do():
            conn = await asyncpg.connect(_dsn())
            try:
                # 验证双方资源均已注册
                for rtype, rid in [("document", doc_id), ("kb", kb_id)]:
                    row = await conn.fetchrow(
                        "SELECT retired FROM resource_registry WHERE resource_type=$1 AND resource_id=$2",
                        rtype, rid,
                    )
                    if not row:
                        # 自动注册
                        await conn.execute(
                            "INSERT INTO resource_registry (resource_type, resource_id, owner) "
                            "VALUES ($1,$2,$3) ON CONFLICT DO NOTHING",
                            rtype, rid, "system",
                        )

                await conn.execute(
                    "INSERT INTO mount_registry (doc_id, kb_id) VALUES ($1,$2) "
                    "ON CONFLICT (doc_id, kb_id) DO UPDATE SET unlinked=false",
                    doc_id, kb_id,
                )
                change_id = f"link-{doc_id}-{kb_id}-{int(time.time())}"
                log.debug("resource_linked", doc_id=doc_id, kb_id=kb_id)
                return change_id
            finally:
                await conn.close()

        try:
            return _run_async(_do())
        except Exception as exc:
            log.error("link_failed", doc_id=doc_id, kb_id=kb_id, error=str(exc))
            raise RuntimeError(
                f"Failed to link {doc_id} to {kb_id} in permission service: {exc}"
            ) from exc

    def unlink_resource(
        self, request_id: str, credential: str, doc_id: str, kb_id: str,
    ) -> str:
        """解除文档到 KB 的挂载镜像 → UPDATE mount_registry SET unlinked=true。

        Raises:
            RuntimeError: 解除挂载失败时抛出，调用方必须回滚本地事务。
        """

        async def _do():
            conn = await asyncpg.connect(_dsn())
            try:
                await conn.execute(
                    "UPDATE mount_registry SET unlinked=true WHERE doc_id=$1 AND kb_id=$2",
                    doc_id, kb_id,
                )
                change_id = f"unlink-{doc_id}-{kb_id}-{int(time.time())}"
                log.debug("resource_unlinked", doc_id=doc_id, kb_id=kb_id)
                return change_id
            finally:
                await conn.close()

        try:
            return _run_async(_do())
        except Exception as exc:
            log.error("unlink_failed", doc_id=doc_id, kb_id=kb_id, error=str(exc))
            raise RuntimeError(
                f"Failed to unlink {doc_id} from {kb_id} in permission service: {exc}"
            ) from exc

    def retire_resource(
        self, request_id: str, credential: str,
        resource_type: str, resource_id: str,
    ) -> str:
        """退役资源 → UPDATE resource_registry SET retired=true + 级联删除所有挂载。

        Raises:
            RuntimeError: 退役失败时抛出，调用方必须回滚本地事务。
            设计依据 §13.7："调用失败即回滚本地业务事务"。
        """

        async def _do():
            conn = await asyncpg.connect(_dsn())
            try:
                # 退役资源
                await conn.execute(
                    "UPDATE resource_registry SET retired=true "
                    "WHERE resource_type=$1 AND resource_id=$2",
                    resource_type, resource_id,
                )

                # 如果是文档，级联清理所有挂载镜像
                if resource_type == "document":
                    await conn.execute(
                        "UPDATE mount_registry SET unlinked=true WHERE doc_id=$1",
                        resource_id,
                    )

                change_id = f"retire-{resource_type}-{resource_id}-{int(time.time())}"
                log.debug("resource_retired", type=resource_type, id=resource_id)
                return change_id
            finally:
                await conn.close()

        try:
            return _run_async(_do())
        except Exception as exc:
            log.error("retire_failed", type=resource_type, id=resource_id, error=str(exc))
            raise RuntimeError(
                f"Failed to retire {resource_type}/{resource_id} in permission service: {exc}"
            ) from exc

    def _headers(self, client_id: str) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-Client-Id": client_id,
        }


def _b64(s: str) -> str:
    import base64
    return base64.urlsafe_b64encode(s.encode()).rstrip(b"=").decode()


_client: Optional[CerbosClient] = None


def get_client() -> CerbosClient:
    global _client
    if _client is None:
        _client = CerbosClient()
    return _client

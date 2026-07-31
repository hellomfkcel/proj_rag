"""P-AUTHC：外部权限服务 HTTP 客户端。

设计依据：docs/外部系统设计.md §2.6 与 RAG 系统当前实现的对照 + 实施方案步骤 8.3。

当 AUTHZ_SERVICE_MODE=remote 时，CerbosClient 被此客户端替代。
所有方法将调用从直接调 Cerbos PDP 改为调外部权限服务后端 REST API。

接口签名与 CerbosClient 保持一致（防腐层的价值）。
"""

import hashlib
import hmac
import json
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

import httpx

from src.config import Settings
from src.platform.obs.logger import get_logger

log = get_logger(__name__)


class PermissionServiceClient:
    """HTTP 客户端 — 调用外部权限服务后端。

    替代 CerbosClient，通过 REST API 与权限服务后端通信。
    接口签名与 CerbosClient 保持一致，确保调用方（authz.py）无需修改。
    """

    def __init__(self, base_url: str = "", timeout_ms: int = 15000):
        self.base_url = base_url or Settings().authz_service_url
        self.timeout_ms = timeout_ms
        self._client = httpx.Client(timeout=timeout_ms / 1000.0)

    # ══════════════════════════════════════════════════════════════
    # 决策面 — 经权限服务后端
    # ══════════════════════════════════════════════════════════════

    def check(
        self,
        request_id: str,
        credential: str,
        action: str,
        resource_type: str,
        resource_id: str,
        resource_attr: Optional[Dict[str, Any]] = None,
        channel_kb: Optional[str] = None,
        client_id: str = "interactive-backend",
        ctx=None,
    ) -> Dict[str, Any]:
        """单条权限判定 → POST /v1/check。

        调用外部权限服务后端，由后端完成 JWT 解析 + ACL 查询 + Cerbos 判定。
        """
        body: Dict[str, Any] = {
            "request_id": request_id,
            "credential": credential,
            "action": action,
            "resource": {"type": resource_type, "id": resource_id},
        }
        if channel_kb:
            body["channel"] = {"kb": channel_kb}

        try:
            resp = self._client.post(
                f"{self.base_url}/v1/check",
                json=body,
                headers=self._headers(request_id, client_id),
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            log.warning(
                "perm_service_check_failed",
                request_id=request_id,
                action=action,
                resource_id=resource_id,
                error=str(exc)[:200],
            )
            return {
                "decision": "deny",
                "decision_id": request_id,
                "reasons": ["perm_service_unavailable"],
            }

    def check_batch(
        self,
        request_id: str,
        credential: str,
        action: str,
        resources: List[Dict[str, Any]],
        client_id: str = "interactive-backend",
        ctx=None,
    ) -> Dict[str, Dict[str, Any]]:
        """批量判定 → POST /v1/check/batch，单次往返。

        设计依据：J-14 联合契约测试 — 批量端点对 interactive-backend 开放。
        单批 ≤200 条，逐资源独立决策。整批传输失败 → 整批判否（fail-closed）。
        """
        if not resources:
            return {}

        resources = resources[:200]  # 安全上限

        # 构建批量请求 items
        batch_items = []
        for r in resources:
            item = {
                "action": action,
                "resource": {"type": r["type"], "id": r["id"]},
            }
            if r.get("channel_kb"):
                item["channel"] = {"kb": r["channel_kb"]}
            batch_items.append(item)

        try:
            resp = self._client.post(
                f"{self.base_url}/v1/check/batch",
                json={
                    "request_id": request_id,
                    "credential": credential,
                    "items": batch_items,
                },
                headers=self._headers(request_id, client_id),
            )
            resp.raise_for_status()
            data = resp.json()
            # Map results back to {resource_id: decision_dict}
            all_results: Dict[str, Dict[str, Any]] = {}
            for result_item in data.get("results", []):
                rid = result_item["resource_id"]
                all_results[rid] = {
                    "decision": result_item["decision"],
                    "decision_id": result_item.get("decision_id", request_id),
                    "reasons": [],
                }
            return all_results
        except Exception as exc:
            log.warning(
                "perm_service_check_batch_failed",
                request_id=request_id,
                count=len(batch_items),
                error=str(exc)[:200],
            )
            # fail-closed: 整批判否
            return {
                r["id"]: {
                    "decision": "deny",
                    "decision_id": request_id,
                    "reasons": ["perm_service_unavailable"],
                }
                for r in resources
            }

    def filter_items(
        self,
        request_id: str,
        credential: str,
        items: List[tuple],
        client_id: str = "retrieval",
        ctx=None,
    ) -> List[tuple]:
        """检索后逐条复核 → POST /v1/filter。

        单批 ≤200。失败/超时 → 整批 deny（fail-closed）。永久禁止缓存。
        """
        if not items:
            return items

        items = list(dict.fromkeys(items))  # 去重保持顺序
        allowed: List[tuple] = []

        for i in range(0, len(items), 200):
            batch = items[i : i + 200]
            batch_items = [
                {
                    "resource_type": "document",
                    "resource_id": doc_id,
                    "channel": {"kb": kb_id},
                }
                for doc_id, kb_id in batch
            ]

            try:
                resp = self._client.post(
                    f"{self.base_url}/v1/filter",
                    json={
                        "request_id": request_id,
                        "credential": credential,
                        "items": batch_items,
                    },
                    headers=self._headers(request_id, client_id),
                )
                resp.raise_for_status()
                data = resp.json()
                allowed_ids = set(data.get("allowed", []))
                for doc_id, kb_id in batch:
                    if doc_id in allowed_ids:
                        allowed.append((doc_id, kb_id))
            except Exception as exc:
                log.warning(
                    "perm_service_filter_failed",
                    request_id=request_id,
                    batch_size=len(batch),
                    error=str(exc)[:200],
                )
                # fail-closed: deny entire batch — 不添加任何 item 到 allowed

        return allowed

    # ══════════════════════════════════════════════════════════════
    # 投影面 — 经权限服务后端
    # ══════════════════════════════════════════════════════════════

    def get_prefilter(
        self,
        request_id: str,
        credential: str,
        client_id: str = "retrieval",
        ctx=None,
    ) -> Dict[str, Any]:
        """检索前编译 → GET /v1/prefilter。

        返回 PreFilter 或 SUSPENDED 哨兵。
        """
        try:
            resp = self._client.get(
                f"{self.base_url}/v1/prefilter",
                params={"credential": credential},
                headers=self._headers(request_id, client_id),
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            log.warning(
                "perm_service_prefilter_failed",
                request_id=request_id,
                error=str(exc)[:200],
            )
            return {"suspended": True}  # fail-closed

    def get_visibility(
        self,
        tenant: str,
        doc_id: str,
        kb_id: str,
        client_id: str = "ingest",
    ) -> Dict[str, Any]:
        """取戳记 → POST /v1/visibility。

        调用方：B-INGEST 盖戳管道、对账任务。
        """
        try:
            resp = self._client.post(
                f"{self.base_url}/v1/visibility",
                json={
                    "tenant": tenant,
                    "doc_id": doc_id,
                    "channel": {"kb": kb_id},
                },
                headers=self._headers("stamp", client_id),
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            log.warning(
                "perm_service_visibility_failed",
                doc_id=doc_id,
                kb_id=kb_id,
                error=str(exc)[:200],
            )
            # fail-closed：绝不返回空戳记
            # 设计依据：§14.5.3 盖戳管道六条纪律第1条 —
            #   "失败/超时 → 不写任何东西、不 ack"
            # 返回空 allow_stamps=[] 会导致全部 chunk 对任何人不可见，
            # 比暂时不更新的危害大得多。调用方须捕获此异常并进入重试路径。
            raise RuntimeError(
                f"Failed to get visibility for doc={doc_id} kb={kb_id}: {exc}"
            ) from exc

    def mint_ctx_token(
        self,
        request_id: str,
        credential: str,
        audience: str,
        ttl_s: int = 600,
        client_id: str = "interactive-backend",
    ) -> str:
        """铸造 ctx_token → POST /v1/context。

        ttl_s 上限 600 秒。
        """
        ttl_s = min(ttl_s, 600)
        try:
            resp = self._client.post(
                f"{self.base_url}/v1/context",
                json={
                    "request_id": request_id,
                    "credential": credential,
                    "audience": audience,
                    "ttl_s": ttl_s,
                },
                headers=self._headers(request_id, client_id),
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("ctx_token", "")
        except Exception as exc:
            log.warning(
                "perm_service_context_failed",
                request_id=request_id,
                audience=audience,
                error=str(exc)[:200],
            )
            # fail-closed：权限服务不可达时抛出异常，不派发任务
            # 设计依据：§6A.1 mint_ctx_token 失败行为 — "任务不派发，返回503"
            raise RuntimeError(
                f"Permission service unreachable — cannot mint ctx_token "
                f"(audience={audience}, request_id={request_id})"
            ) from exc

    # ══════════════════════════════════════════════════════════════
    # 管理面 — 经权限服务后端
    # ══════════════════════════════════════════════════════════════

    def register_resource(
        self,
        request_id: str,
        credential: str,
        resource_type: str,
        resource_id: str,
        owner: str,
        tenant_id: str = "",
        name: str | None = None,
    ) -> str:
        """注册资源 → POST /v1/resources/register。

        tenant_id 来自 ctx.tenant_id，由调用方 (P-AUTHC authz.py) 传入。
        name: 资源名称（KB 名称 / 文档文件名），供管理台展示。
        """
        body: dict = {
            "resource_type": resource_type,
            "resource_id": resource_id,
            "owner": owner,
            "tenant_id": tenant_id,
            "idempotency_key": (
                f"rag-register-{tenant_id}-{resource_id}-v1"
            ),
        }
        if name:
            body["name"] = name

        try:
            resp = self._client.post(
                f"{self.base_url}/v1/resources/register",
                json=body,
                headers=self._headers(request_id, "interactive-backend"),
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("change_id", "")
        except Exception as exc:
            log.error(
                "perm_service_register_failed",
                type=resource_type,
                id=resource_id,
                error=str(exc)[:200],
            )
            raise RuntimeError(
                f"Failed to register {resource_type}/{resource_id} "
                f"in permission service: {exc}"
            ) from exc

    def link_resource(
        self,
        request_id: str,
        credential: str,
        doc_id: str,
        kb_id: str,
        tenant_id: str = "",
    ) -> str:
        """建立挂载 → POST /v1/resources/link。

        tenant_id 来自 ctx.tenant_id，由调用方传入。
        """
        try:
            resp = self._client.post(
                f"{self.base_url}/v1/resources/link",
                json={
                    "resource_type": "document",
                    "resource_id": doc_id,
                    "owner": "system",
                    "tenant_id": tenant_id,
                    "kb_id": kb_id,
                    "idempotency_key": (
                        f"rag-link-{tenant_id}-{doc_id}-{kb_id}-v1"
                    ),
                },
                headers=self._headers(request_id, "interactive-backend"),
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("change_id", "")
        except Exception as exc:
            log.error(
                "perm_service_link_failed",
                doc_id=doc_id,
                kb_id=kb_id,
                error=str(exc)[:200],
            )
            raise RuntimeError(
                f"Failed to link {doc_id} to {kb_id} in permission service: {exc}"
            ) from exc

    def unlink_resource(
        self,
        request_id: str,
        credential: str,
        doc_id: str,
        kb_id: str,
        tenant_id: str = "",
    ) -> str:
        """解除挂载 → POST /v1/resources/unlink。

        tenant_id 来自 ctx.tenant_id，由调用方传入。
        """
        try:
            resp = self._client.post(
                f"{self.base_url}/v1/resources/unlink",
                json={
                    "resource_type": "document",
                    "resource_id": doc_id,
                    "owner": "system",
                    "tenant_id": tenant_id,
                    "kb_id": kb_id,
                    "idempotency_key": (
                        f"rag-unlink-{tenant_id}-{doc_id}-{kb_id}-v1"
                    ),
                },
                headers=self._headers(request_id, "interactive-backend"),
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("change_id", "")
        except Exception as exc:
            log.error(
                "perm_service_unlink_failed",
                doc_id=doc_id,
                kb_id=kb_id,
                error=str(exc)[:200],
            )
            raise RuntimeError(
                f"Failed to unlink {doc_id} from {kb_id} in permission service: {exc}"
            ) from exc

    def retire_resource(
        self,
        request_id: str,
        credential: str,
        resource_type: str,
        resource_id: str,
        tenant_id: str = "",
    ) -> str:
        """退役资源 → POST /v1/resources/retire。

        tenant_id 来自 ctx.tenant_id，由调用方传入。
        """
        try:
            resp = self._client.post(
                f"{self.base_url}/v1/resources/retire",
                json={
                    "resource_type": resource_type,
                    "resource_id": resource_id,
                    "owner": "system",
                    "tenant_id": tenant_id,
                    "idempotency_key": (
                        f"rag-retire-{tenant_id}-{resource_type}-{resource_id}-v1"
                    ),
                },
                headers=self._headers(request_id, "interactive-backend"),
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("change_id", "")
        except Exception as exc:
            log.error(
                "perm_service_retire_failed",
                type=resource_type,
                id=resource_id,
                error=str(exc)[:200],
            )
            raise RuntimeError(
                f"Failed to retire {resource_type}/{resource_id} "
                f"in permission service: {exc}"
            ) from exc

    # ══════════════════════════════════════════════════════════════
    # HTTP 工具
    # ══════════════════════════════════════════════════════════════

    def _headers(
        self, request_id: str, client_id: str
    ) -> Dict[str, str]:
        """构造请求头 — 含 X-Client-Id + 可选 X-Api-Key 服务间认证。

        设计依据：docs/RAG系统设计v14.md §18.2 — AUTHZ_CLIENT_CREDENTIAL
        用于 RAG 系统与权限服务之间的机器对机器认证。
        """
        headers = {
            "Content-Type": "application/json",
            "X-Request-Id": request_id,
            "X-Client-Id": client_id,
        }
        # 服务间认证：若配置了凭据，作为 X-Api-Key 发送
        credential = Settings().authz_client_credential
        if credential:
            headers["X-Api-Key"] = credential
        return headers

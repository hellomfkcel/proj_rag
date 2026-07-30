"""P-AUTHC：权限判定门面。

阶段三升级：
- 所有权限调用包裹熔断器（circuitbreaker 库）
- 三态映射 + 四类 fail-closed + Metric 埋点
- 熔断打开时所有调用直接返回 auth:authz_unavailable

提供：
- check / check_batch / filter_items / get_prefilter / compile_filter
- mint_ctx_token / require_permission
- 生命周期端口（register/link/unlink/retire）
"""

import functools
from typing import Any, Dict, List, Optional, Tuple

from circuitbreaker import CircuitBreaker

from src.permission.context import RequestContext
from src.permission.cerbos_client import get_client
from src.platform.obs.logger import get_logger
from src.platform.obs.metrics import record_authz_decision, record_authz_call_failed

logger = get_logger(__name__)

Decision = Dict[str, Any]


# ══════════════════════════════════════════════════════════════════
# 熔断器（v14.md §25.3）
# ══════════════════════════════════════════════════════════════════

_authz_circuit_breaker = CircuitBreaker(
    failure_threshold=10,          # 连续失败 10 次 → 熔断打开
    recovery_timeout=60,           # 60s 后半开探测
    name="authz",
    expected_exception=Exception,
    fallback_function=None,
)


def _with_circuit_breaker(func):
    """装饰器：包裹熔断器 + Metric 埋点。

    熔断打开时返回 E503 auth:authz_unavailable（v14.md §25.3 拒答型降级）。
    """

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        endpoint = _endpoint_for(func.__name__)

        try:
            result = func(*args, **kwargs)

            # Metric: 记录判定结果
            if isinstance(result, dict) and "decision" in result:
                record_authz_decision(endpoint, result["decision"])
            elif isinstance(result, dict):
                for v in result.values():
                    if isinstance(v, dict) and "decision" in v:
                        record_authz_decision(endpoint, v["decision"])

            return result

        except Exception as exc:
            # 区分失败类型
            kind = "connection" if "connection" in str(exc).lower() else \
                   "timeout" if "timeout" in str(exc).lower() else "http_error"
            record_authz_call_failed(endpoint, kind)
            logger.warning("authz_call_failed", endpoint=endpoint,
                          kind=kind, error=str(exc)[:100])
            raise

    # 包裹 circuitbreaker
    cb = CircuitBreaker(
        failure_threshold=10,
        recovery_timeout=60,
        name=f"authz_{func.__name__}",
        expected_exception=Exception,
        fallback_function=lambda *a, _fn=func.__name__, **kw: _circuit_open_fallback(_fn),
    )
    return cb(wrapper)


def _circuit_open_fallback(func_name: str):
    """熔断打开时的 fallback：返回 deny/unavailable。"""
    endpoint = _endpoint_for(func_name)
    logger.error("authz_circuit_open", endpoint=endpoint,
                 message="权限服务熔断器打开，所有调用直接拒绝")

    if func_name in ("filter_items", "get_prefilter"):
        if func_name == "get_prefilter":
            return {"suspended": True}  # 型一封禁 → 跳过检索
        return []  # filter_items → 整批 deny

    return {"decision": "deny", "decision_id": "", "reasons": ["authz_unavailable"]}


def _endpoint_for(func_name: str) -> str:
    return {
        "check": "check",
        "check_batch": "check_batch",
        "filter_items": "filter",
        "get_prefilter": "prefilter",
        "mint_ctx_token": "context",
    }.get(func_name, func_name)


# ══════════════════════════════════════════════════════════════════
# 三态映射
# ══════════════════════════════════════════════════════════════════

def _to_decision(client_result: Dict[str, Any]) -> Decision:
    return {
        "decision": client_result.get("decision", "deny"),
        "decision_id": client_result.get("decision_id", ""),
        "reasons": client_result.get("reasons", []),
    }


# ══════════════════════════════════════════════════════════════════
# check（单条判定，带熔断）
# ══════════════════════════════════════════════════════════════════

@_with_circuit_breaker
def check(
    ctx: RequestContext,
    action: str,
    resource_type: str,
    resource_id: str,
    channel_kb: Optional[str] = None,
) -> Decision:
    """单条权限判定 → /v1/check。

    doc:retrieve → 直接抛异常（必须走 /v1/filter）。
    超时/传输失败 → deny（fail-closed）。
    熔断打开 → 返回 deny + authz_unavailable。
    """
    if action == "doc:retrieve":
        raise ValueError("doc:retrieve must use /v1/filter, not /v1/check")

    client = get_client()
    try:
        result = client.check(
            request_id=ctx.request_id,
            credential=ctx.credential,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            channel_kb=channel_kb,
            ctx=ctx,
        )
        decision = _to_decision(result)
        ctx.authz_decision_ref = decision["decision_id"]
        return decision
    except Exception:
        logger.warning("authz_check_failed", request_id=ctx.request_id, action=action)
        raise  # 让 circuitbreaker 计数


# ══════════════════════════════════════════════════════════════════
# check_batch（批量判定，带熔断）
# ══════════════════════════════════════════════════════════════════

@_with_circuit_breaker
def check_batch(
    ctx: RequestContext,
    action: str,
    resources: List[Tuple[str, str]],
) -> Dict[str, Decision]:
    """批量判定 → /v1/check/batch（单次往返，≤200/批）。

    逐资源独立决策。整批传输失败 → 整批判否（fail-closed）。
    """
    if not resources:
        return {}

    client = get_client()
    batch_input = [{"type": rtype, "id": rid} for rtype, rid in resources]

    all_results: Dict[str, Decision] = {}
    for i in range(0, len(batch_input), 200):
        batch = batch_input[i:i + 200]
        try:
            batch_results = client.check_batch(
                request_id=ctx.request_id, credential=ctx.credential,
                action=action, resources=batch,
                ctx=ctx,
            )
            for rid, result in batch_results.items():
                all_results[rid] = _to_decision(result)
        except Exception:
            logger.warning("authz_batch_check_failed", request_id=ctx.request_id)
            raise
    return all_results


# ══════════════════════════════════════════════════════════════════
# filter_items（层 3，带熔断）
# ══════════════════════════════════════════════════════════════════

@_with_circuit_breaker
def filter_items(
    ctx: RequestContext,
    items: List[Tuple[str, str]],
) -> List[Tuple[str, str]]:
    """检索后逐条复核 → /v1/filter（阶段二接入，阶段三加固熔断）。

    单批 ≤200；传输失败/超时 → 整批 deny；永久禁止缓存。
    熔断打开 → 返回空列表（整批 deny）。
    """
    if not items:
        return items

    client = get_client()
    try:
        return client.filter_items(
            request_id=ctx.request_id, credential=ctx.credential, items=items,
            ctx=ctx,
        )
    except Exception:
        logger.warning("authz_filter_items_failed", request_id=ctx.request_id,
                      item_count=len(items))
        raise


# ══════════════════════════════════════════════════════════════════
# get_prefilter（检索前编译，带熔断）
# ══════════════════════════════════════════════════════════════════

def get_prefilter(ctx: RequestContext) -> Dict[str, Any]:
    """检索前编译过滤条件 → /v1/prefilter。

    返回 PreFilter 或 SUSPENDED 哨兵（suspended: true）。
    请求内缓存，不跨请求缓存。
    失败 → 不允许回退到无过滤查询，必须整体拒答。

    所有模式（开发/生产）统一走 Cerbos PDP 判定，不再有 DB 绕过。
    """

    client = get_client()
    try:
        return client.get_prefilter(
            request_id=ctx.request_id, credential=ctx.credential,
            ctx=ctx,
        )
    except Exception:
        logger.warning("authz_prefilter_failed", request_id=ctx.request_id)
        record_authz_call_failed("prefilter", "connection")
        return {"suspended": True}  # fail-closed


# ══════════════════════════════════════════════════════════════════
# compile_filter（六条件）
# ══════════════════════════════════════════════════════════════════

def compile_filter(pf: Dict[str, Any], ctx: RequestContext, kb_id: str) -> Dict[str, Any]:
    """六条件 → milvus-haystack filter dict（§15.1, §6A.3）。

    条件 1: tenant_id 隔离
    条件 2: kb_id 隔离
    条件 3: allow_stamps MatchAny（json_contains OR）
    条件 4: retrievable 标记
    条件 5: deny_stamps MUST_NOT（NOT json_contains AND）
    条件 6: vis_version > 0（已盖戳）

    返回格式适配 milvus-haystack parse_filters + _compile_filter_expr 扩展。
    """
    if pf.get("suspended"):
        return {"field": "id", "operator": "==", "value": "__SUSPENDED__"}

    # 扩展 principals：加入通配符（user:* 等）
    expanded = list(set(ctx.principals))
    for p in ctx.principals:
        prefix = p.split(":", 1)[0] if ":" in p else ""
        if prefix:
            expanded.append(f"{prefix}:*")
    expanded = list(set(expanded))

    # allow_stamps MatchAny → OR(json_contains(p1), json_contains(p2), ...)
    allow_conds = [{"field": "allow_stamps", "operator": "json_contains", "value": p}
                   for p in expanded]

    # deny_stamps MUST_NOT → AND(NOT json_contains(p1), NOT json_contains(p2), ...)
    deny_conds = [{"field": "deny_stamps", "operator": "not_json_contains", "value": p}
                  for p in expanded]

    conditions = [
        {"field": "tenant_id", "operator": "==", "value": ctx.tenant_id},
        {"field": "kb_id", "operator": "==", "value": kb_id},
        {"field": "retrievable", "operator": "==", "value": True},
        {"field": "vis_version", "operator": ">", "value": 0},
    ]
    if allow_conds:
        conditions.append({"operator": "OR", "conditions": allow_conds})
    for dc in deny_conds:
        conditions.append(dc)  # not_json_contains handled by _compile_filter_expr

    return {"operator": "AND", "conditions": conditions}


def _compile_filter_expr(filter_dict: Dict[str, Any]) -> str:
    """将 compile_filter 输出的 dict 编译为 Milvus 表达式字符串。

    milvus-haystack 的 parse_filters 不支持 json_contains/not_json_contains，
    此函数补充这部分操作符的编译，其余交给 parse_filters。
    """
    from milvus_haystack.filters import parse_filters

    def _compile(node: dict) -> str:
        op = node.get("operator", "")
        if op == "json_contains":
            return f'json_contains({node["field"]}, "{node["value"]}")'
        if op == "not_json_contains":
            return f'not json_contains({node["field"]}, "{node["value"]}")'
        if op in ("AND", "OR"):
            parts = [_compile(c) for c in node.get("conditions", [])]
            sep = " && " if op == "AND" else " || "
            return "(" + sep.join(parts) + ")"
        # 标准操作符交给 milvus-haystack
        return parse_filters(node)

    return _compile(filter_dict)


# ══════════════════════════════════════════════════════════════════
# mint_ctx_token / lifecycle / require_permission
# ══════════════════════════════════════════════════════════════════

def mint_ctx_token(ctx: RequestContext, audience: str = "retrieval-worker", ttl_s: int = 600) -> str:
    client = get_client()
    return client.mint_ctx_token(
        request_id=ctx.request_id, credential=ctx.credential,
        audience=audience, ttl_s=min(ttl_s, 600),
    )


def register_resource(ctx: RequestContext, resource_type: str, resource_id: str, owner: str) -> str:
    result = get_client().register_resource(ctx.request_id, ctx.credential, resource_type, resource_id, owner)
    # KB 注册后触发该 KB 下全部文档的盖戳刷新（新 KB 授权可能影响已有文档可见性）
    if resource_type == "kb":
        try:
            from src.permission.visibility_events import on_kb_visibility_changed
            on_kb_visibility_changed(resource_id, ctx.tenant_id, change_type="kb_registered")
        except Exception:
            pass  # 不影响主流程
    return result

def link_resource(ctx: RequestContext, doc_id: str, kb_id: str) -> str:
    result = get_client().link_resource(ctx.request_id, ctx.credential, doc_id, kb_id)
    # 挂载建立后触发该文档的盖戳刷新（新挂载可能改变可见性）
    try:
        from src.permission.visibility_events import on_visibility_changed
        on_visibility_changed(doc_id, kb_id, ctx.tenant_id, change_type="link")
    except Exception:
        pass  # 不影响主流程
    return result

def unlink_resource(ctx: RequestContext, doc_id: str, kb_id: str) -> str:
    result = get_client().unlink_resource(ctx.request_id, ctx.credential, doc_id, kb_id)
    # 挂载解除后触发盖戳清空（文档从 KB 移除）
    try:
        from src.permission.visibility_events import on_visibility_changed
        on_visibility_changed(doc_id, kb_id, ctx.tenant_id, change_type="unlink")
    except Exception:
        pass  # 不影响主流程
    return result

def retire_resource(ctx: RequestContext, resource_type: str, resource_id: str) -> str:
    result = get_client().retire_resource(ctx.request_id, ctx.credential, resource_type, resource_id)
    # 资源退役后触发相关文档的盖戳清空
    try:
        from src.permission.visibility_events import on_visibility_changed
        if resource_type == "kb":
            from src.permission.visibility_events import on_kb_visibility_changed
            on_kb_visibility_changed(resource_id, ctx.tenant_id, change_type="kb_retired")
        # document retire 的逐文档处理由 B-DOC _purge_document 中的 stamp_channel_task 覆盖
    except Exception:
        pass  # 不影响主流程
    return result


def get_visibility(tenant: str, doc_id: str, kb_id: str) -> Dict[str, Any]:
    """取戳记 → /v1/visibility（经 P-AUTHC 门面，唯一出口）。

    返回：{"allow_stamps": [...], "deny_stamps": [...], "version": N, "unmounted": bool}
    调用方：B-INGEST 盖戳管道、对账任务。

    此门面封装了 get_client().get_visibility() 调用，
    消除 ingest/service.py 和 reconciliation.py 对 CerbosClient 的直接依赖。
    """
    return get_client().get_visibility(tenant=tenant, doc_id=doc_id, kb_id=kb_id)


def require_permission(action: str, resource_type: str):
    from src.permission.context import RequestContext

    def _guard(ctx: RequestContext = None):
        if ctx is None:
            from fastapi import HTTPException
            raise HTTPException(
                status_code=500,
                detail="auth:context_missing — RequestContext not injected by middleware. "
                       "Ensure AuthMiddleware is registered before this route."
            )
        decision = check(ctx, action, resource_type, resource_type)
        if decision["decision"] == "indeterminate":
            from fastapi import HTTPException
            raise HTTPException(
                status_code=503,
                detail="auth:authz_indeterminate — permission service returned indeterminate. "
                       "Tech alert has been triggered."
            )
        if decision["decision"] != "allow":
            from fastapi import HTTPException
            raise HTTPException(status_code=403, detail="auth:forbidden")
        return ctx

    from fastapi import Depends
    return Depends(_guard)

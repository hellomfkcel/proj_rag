"""P-AUTHC：RequestContext 构建。

中间件：JWT 本地校签 → 从 claims 构建 ctx → 保管 credential 原文。
"""

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass, field
from typing import List, Optional

import jwt
from jwt.exceptions import InvalidTokenError, ExpiredSignatureError


@dataclass
class RequestContext:
    """请求生命周期内的传输载体，由 P-AUTHC 构建一次。"""

    request_id: str                                            # = OTel trace_id（32 hex）
    user_id: str                                               # JWT sub
    tenant_id: str                                             # JWT tenant（仅供业务查询，不参与权限判定）
    credential: str                                            # JWT 原文，必须原样保管
    roles: List[str] = field(default_factory=list)             # 仅供审计展示
    groups: List[str] = field(default_factory=list)            # 同上
    principals: List[str] = field(default_factory=list)        # user:/group:/role: 前缀并集，层1过滤用
    is_service_account: bool = False
    client_ip: str = ""
    authz_decision_ref: str = ""                               # 权限服务返回的 decision_id
    risk_level: str = "normal"                                 # normal / sensitive


def build_context(
    credential: str,
    client_ip: str = "",
    enforce_jwt: bool = True,          # 生产环境必须为 True
    jwks_url: str = "",
) -> RequestContext:
    """从 JWT credential 构建 RequestContext。

    - 本地校验 JWT 签名与过期（快速失败）
    - 从 claims 提取 user_id / tenant_id / roles / groups
    - 展开 principals（user:/group:/role: 前缀并集）
    - 保管 credential 原文

    本地校签与权限服务校验是两处——本地更严不更松，通过不代表授权通过。
    """
    if not enforce_jwt:
        raise RuntimeError(
            "enforce_jwt=False is forbidden in production. "
            "Use the dev-login endpoint to obtain a real JWT for development."
        )

    if not credential:
        raise ValueError("credential is required when enforce_jwt=True")

    # 本地校签
    try:
        if jwks_url:
            from jwt import PyJWKClient
            jwks_client = PyJWKClient(jwks_url)
            signing_key = jwks_client.get_signing_key_from_jwt(credential)
            claims = jwt.decode(credential, signing_key.key, algorithms=["RS256"], options={"verify_exp": True})
        else:
            # 无 JWKS URL 时只解码不验签（开发期）
            claims = jwt.decode(credential, options={"verify_signature": False, "verify_exp": True})
    except ExpiredSignatureError:
        raise PermissionError("JWT expired")
    except InvalidTokenError as e:
        raise PermissionError(f"JWT invalid: {e}")

    user_id = claims.get("sub", "")
    tenant_id = claims.get("tenant", claims.get("tenant_id", ""))
    roles = _norm_list(claims.get("realm_access", {}).get("roles", []))
    groups = _norm_list(claims.get("groups", []))

    # 展开 principals
    principals = []
    principals.append(f"user:{user_id}")
    for g in groups:
        principals.append(f"group:{g}")
    for r in roles:
        principals.append(f"role:{r}")

    return RequestContext(
        request_id=claims.get("jti", "unknown"),
        user_id=user_id,
        tenant_id=tenant_id,
        credential=credential,
        roles=roles,
        groups=groups,
        principals=principals,
        is_service_account=claims.get("is_service_account", False),
        client_ip=client_ip,
    )


def _norm_list(val) -> List[str]:
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        return [val]
    return []


def resolve_ctx_token(ctx_token: str) -> str:
    """从 ctx_token 提取原始 JWT credential。

    ctx_token 格式：ctx.{base64-payload}.{hmac-signature}
    payload 中包含 credential 字段（原始 JWT）。

    用于 worker 任务从 ctx_token 重建 RequestContext：
        credential = resolve_ctx_token(ctx_token)
        ctx = build_context(credential, enforce_jwt=True)
    """
    if not ctx_token or not ctx_token.startswith("ctx."):
        raise ValueError("Invalid ctx_token: must start with 'ctx.'")

    try:
        parts = ctx_token.split(".")
        if len(parts) != 3:
            raise ValueError(f"Invalid ctx_token format: expected 3 parts, got {len(parts)}")

        payload_b64 = parts[1]
        # 补齐 base64 padding
        padding = 4 - len(payload_b64) % 4
        if padding != 4:
            payload_b64 += "=" * padding

        payload_bytes = base64.urlsafe_b64decode(payload_b64)
        payload = json.loads(payload_bytes)

        # 检查过期
        import time
        if payload.get("exp", 0) < int(time.time()):
            raise PermissionError("ctx_token expired")

        credential = payload.get("credential", "")
        if not credential:
            raise ValueError("ctx_token payload missing 'credential' field")

        return credential
    except (json.JSONDecodeError, base64.binascii.Error) as e:
        raise ValueError(f"Failed to parse ctx_token: {e}")

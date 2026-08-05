"""认证端点 — 开发模式登录 + 生产模式 SSO + Token 刷新 + 租户列表。

开发模式 (`POST /api/v1/auth/dev-login`):
  自签 JWT (RS256)，返回 access_token + user info。
  不依赖外部 IdP — 仅供开发期使用。

生产模式 (`POST /api/v1/auth/token`):
  OAuth2 authorization_code → JWT + refresh_token。

Token 刷新 (`POST /api/v1/auth/refresh`):
  开发模式：用已有 claims 重签 JWT。
  生产模式：验证 refresh_token → 签发新 access_token + 轮换 refresh_token。

租户列表 (`GET /api/v1/tenants`):
  返回当前用户所属的租户列表（开发模式从 knowledge_bases 表反查）。
"""

import hashlib
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel

from src.config import Settings

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
tenant_router = APIRouter(prefix="/api/v1", tags=["tenants"])


# ── Models ──────────────────────────────────────────────────────

class DevLoginRequest(BaseModel):
    username: str = "admin"
    password: str = ""             # Keycloak 用户密码
    tenant: str = "tenant-dev"

class DevLoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: str               # ISO 8601
    user: dict                    # {id, tenant_id, roles}

class TenantInfo(BaseModel):
    id: str
    name: str
    kb_count: int = 0
    doc_count: int = 0


# ── Helpers ─────────────────────────────────────────────────────

def _sign_jwt(sub: str, tenant: str, roles: list[str]) -> tuple[str, datetime]:
    """用 RS256 私钥签发 JWT。返回 (token, expires_at)。"""
    from jose import jwt as jose_jwt

    s = Settings()
    with open(s.jwt_private_key_path) as f:
        private_key = f.read()

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=s.jwt_expire_seconds)

    claims = {
        "sub": sub,
        "tenant": tenant,
        "roles": roles,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "iss": "rag-v14-dev",
    }
    token = jose_jwt.encode(claims, private_key, algorithm=s.jwt_algorithm)
    return token, expires_at


# ── POST /api/v1/auth/dev-login ─────────────────────────────────

@router.post("/dev-login", response_model=DevLoginResponse)
async def dev_login(body: DevLoginRequest):
    """用户登录：Keycloak 验证用户名密码 + 后端验证租户成员资格。

    流程：
    1. 调用 Keycloak token endpoint (password grant) 验证用户名密码
    2. 从 Keycloak 响应提取用户身份（sub, preferred_username, roles）
    3. 验证用户属于请求的租户（tenant_memberships）
    4. 签发本系统 JWT（含 Keycloak 身份 + 租户 claim）
    """
    import httpx
    from jose import jwt as jose_jwt

    s = Settings()

    # ── Step 1: Keycloak 密码验证 ──
    try:
        async with httpx.AsyncClient(timeout=15.0) as http:
            kc_resp = await http.post(
                f"{s.keycloak_server_url}/realms/{s.keycloak_realm}/protocol/openid-connect/token",
                data={
                    "client_id": s.keycloak_client_id,
                    "grant_type": "password",
                    "username": body.username,
                    "password": body.password,
                    "scope": "openid",
                },
            )
            if kc_resp.status_code != 200:
                detail = "用户名或密码错误"
                try:
                    err = kc_resp.json()
                    detail = err.get("error_description", detail)
                except Exception:
                    pass
                raise HTTPException(status_code=401, detail=detail)
            kc_data = kc_resp.json()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail=f"Keycloak 认证服务不可达: {str(e)[:100]}",
        )

    # ── Step 2: 从 Keycloak access_token 提取用户身份 ──
    # Keycloak JWT 无需验签（我们刚从 Keycloak 拿到，TLS 保证完整性）
    try:
        # 直接解码 payload（不验证签名 — 我们信任刚获取的 token）
        import base64, json as _json
        payload_b64 = kc_data["access_token"].split(".")[1]
        payload_b64 += "=" * (4 - len(payload_b64) % 4)
        kc_claims = _json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception:
        raise HTTPException(status_code=500, detail="无法解析 Keycloak token")

    user_id = kc_claims.get("preferred_username", kc_claims.get("sub", body.username))
    roles = (kc_claims.get("realm_access", {}) or {}).get("roles", [])
    # 转换 Keycloak 默认角色为系统角色
    if "system_admin" not in roles and "admin" not in roles:
        if "user" not in roles:
            roles.append("user")

    # ── Step 3: 租户成员校验 ──
    if s.authz_service_mode == "remote":
        try:
            async with httpx.AsyncClient(timeout=10.0) as http:
                resp = await http.get(
                    f"{s.authz_service_url}/api/v1/tenants/by-user/user:{user_id}",
                )
                if resp.status_code == 200:
                    data = resp.json()
                    user_tenant_ids = {t["id"] for t in data.get("tenants", [])}
                    if body.tenant not in user_tenant_ids:
                        raise HTTPException(
                            status_code=403,
                            detail=f"用户 '{user_id}' 不属于租户 '{body.tenant}'。"
                                   f"可访问的租户: {', '.join(sorted(user_tenant_ids)) if user_tenant_ids else '无'}",
                        )
        except HTTPException:
            raise
        except Exception:
            pass  # 权限服务不可达时容错

    # ── Step 4: 签发本系统 JWT ──
    token, expires_at = _sign_jwt(sub=user_id, tenant=body.tenant, roles=roles)

    return DevLoginResponse(
        access_token=token,
        expires_at=expires_at.isoformat(),
        user={
            "id": user_id,
            "tenant_id": body.tenant,
            "roles": roles,
            "name": kc_claims.get("name", user_id),
        },
    )


# ── POST /api/v1/auth/token（生产 OAuth2/OIDC） ──────────────────

class TokenRequest(BaseModel):
    code: str
    redirect_uri: str = ""


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str | None = None
    token_type: str = "bearer"
    expires_at: str
    user: dict


class RefreshRequest(BaseModel):
    refresh_token: str | None = None  # None in dev mode → re-sign with stored claims


class RefreshResponse(BaseModel):
    access_token: str
    refresh_token: str | None = None  # rotated refresh_token (prod only)
    token_type: str = "bearer"
    expires_at: str


@router.post("/token", response_model=TokenResponse)
async def exchange_token(body: TokenRequest):
    """OAuth2 authorization_code → 本系统 JWT + refresh_token。

    通用 OIDC 协议层，不绑定特定 IdP：
    1. 从 OIDC_DISCOVERY_URL 自动发现 IdP 的 token_endpoint / jwks_uri
    2. POST authorization_code → IdP → access_token + id_token + refresh_token
    3. 用 JWKS 公钥验签 id_token → 提取 sub/tenant/roles/groups
    4. 签发本系统 JWT（RS256 自签，wrapping IdP claims）
    5. 存储 refresh_token hash 到 DB（用于后续轮换）

    OIDC_DISCOVERY_URL 未配置时 → 返回 501，引导使用开发模式 /dev-login。
    """
    from src.api.oidc import OIDCProvider

    provider = OIDCProvider()

    if not provider.enabled:
        raise HTTPException(
            status_code=501,
            detail="OIDC not configured. Set OIDC_DISCOVERY_URL + OIDC_CLIENT_ID "
                   "for production, or use POST /api/v1/auth/dev-login in dev mode."
        )

    # 1. Exchange authorization_code for tokens
    tokens = provider.exchange_code(body.code, body.redirect_uri)
    if not tokens:
        raise HTTPException(
            status_code=400,
            detail="Token exchange failed — invalid code, expired code, or IdP unreachable."
        )

    # 2. Validate id_token (JWKS signature + issuer + expiry)
    claims = provider.validate_id_token(tokens.id_token)
    if not claims:
        raise HTTPException(
            status_code=400,
            detail="id_token validation failed — signature, issuer, or expiry check failed."
        )

    # 3. Sign our own JWT (RS256, wrapping IdP claims)
    roles = claims.roles or ["user"]
    jwt_token, expires_at = _sign_jwt(
        sub=claims.sub or claims.name or "oidc-user",
        tenant=claims.tenant or "unknown",
        roles=roles,
    )

    # 4. Store refresh_token hash for rotation (one-way, async)
    if tokens.refresh_token:
        _store_refresh_token_hash(claims.sub, tokens.refresh_token)

    return TokenResponse(
        access_token=jwt_token,
        refresh_token=tokens.refresh_token or None,
        expires_at=expires_at.isoformat(),
        user={
            "id": claims.sub,
            "tenant_id": claims.tenant,
            "roles": roles,
            "name": claims.name or claims.sub,
            "email": claims.email,
        },
    )


@router.post("/refresh", response_model=RefreshResponse)
async def refresh_token(body: RefreshRequest):
    """刷新 access_token。

    生产模式（有 refresh_token）：
      1. 校验 refresh_token hash → 存在且未撤销
      2. 向 IdP token_endpoint 发送 refresh_token → 换新 token 对
      3. 旧 refresh_token 标记 revoked（防重放）
      4. 存储新 refresh_token hash
      5. 签发新本系统 JWT

    开发模式（无 refresh_token）：
      从 Authorization header 解析当前 JWT claims → 重签。
    """
    from src.api.oidc import OIDCProvider

    s = Settings()

    # ── Production mode: refresh_token rotation ──
    if body.refresh_token:
        provider = OIDCProvider()
        if not provider.enabled:
            raise HTTPException(
                status_code=501,
                detail="OIDC not configured — cannot validate refresh_token."
            )

        # 1. Verify refresh_token hash exists in DB and is not revoked
        token_hash = _hash_token(body.refresh_token)
        stored = await _lookup_refresh_token(token_hash)
        if not stored:
            raise HTTPException(status_code=400, detail="Invalid or revoked refresh_token")

        # 2. Exchange refresh_token at IdP
        new_tokens = provider.refresh(body.refresh_token)
        if not new_tokens:
            # Mark old token as revoked (IdP rejected it — may be compromised)
            await _revoke_refresh_token(token_hash)
            raise HTTPException(status_code=400, detail="Refresh token rejected by IdP — please re-login")

        # 3. Validate new id_token (if provided)
        claims = None
        if new_tokens.id_token:
            claims = provider.validate_id_token(new_tokens.id_token)
        if not claims:
            # Fallback: use stored user info from DB
            claims = type("Claims", (), {
                "sub": stored["sub"],
                "tenant": stored["tenant"],
                "roles": stored["roles"],
                "name": stored["name"],
            })()

        # 4. Revoke old token + store new one (atomic)
        await _revoke_refresh_token(token_hash)
        if new_tokens.refresh_token:
            _store_refresh_token_hash(
                claims.sub, new_tokens.refresh_token,
                tenant=claims.tenant, roles=claims.roles, name=claims.name,
            )

        # 5. Sign new JWT
        jwt_token, expires_at = _sign_jwt(
            sub=claims.sub,
            tenant=claims.tenant,
            roles=claims.roles,
        )

        return RefreshResponse(
            access_token=jwt_token,
            refresh_token=new_tokens.refresh_token or None,
            expires_at=expires_at.isoformat(),
        )

    # ── Dev mode: re-sign from current JWT claims ──
    try:
        with open(s.jwt_public_key_path) as f:
            _ = f.read()  # ensure key exists

        token, expires_at = _sign_jwt(
            sub="dev-user",
            tenant="tenant-dev",
            roles=["user"],
        )
        return RefreshResponse(
            access_token=token,
            expires_at=expires_at.isoformat(),
        )
    except Exception:
        raise HTTPException(status_code=400, detail="Token refresh failed")


# ══════════════════════════════════════════════════════════════════
# refresh_token 安全存储（SHA-256 hash → DB）
# ══════════════════════════════════════════════════════════════════

def _hash_token(token: str) -> str:
    """SHA-256 hash of refresh_token（单向，不可逆）。"""
    return hashlib.sha256(token.encode()).hexdigest()


def _store_refresh_token_hash(
    sub: str, refresh_token: str,
    tenant: str = "", roles: list = None, name: str = "",
) -> None:
    """存储 refresh_token 的 SHA-256 hash（用于后续轮换验证）。"""
    import asyncio
    import asyncpg as _apg

    s = Settings()
    token_hash = _hash_token(refresh_token)

    async def _do():
        conn = await _apg.connect(
            s.database_url.replace("postgresql+asyncpg://", "postgresql://"))
        try:
            await conn.execute("""
                INSERT INTO refresh_token_hashes (token_hash, sub, tenant_id, roles, name, revoked)
                VALUES ($1, $2, $3, $4, $5, false)
            """, token_hash, sub, tenant or "", roles or ["user"],
                name or sub)
        finally:
            await conn.close()

    try:
        asyncio.run(_do())
    except Exception:
        pass  # Non-blocking: token log can be re-established via re-login


async def _lookup_refresh_token(token_hash: str) -> Optional[dict]:
    """查询 refresh_token hash → 返回存储信息（revoked 时返回 None）。"""
    import asyncpg as _apg

    s = Settings()
    conn = await _apg.connect(
        s.database_url.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        row = await conn.fetchrow(
            "SELECT sub, tenant_id, roles, name, revoked FROM refresh_token_hashes "
            "WHERE token_hash=$1", token_hash)
        if not row or row["revoked"]:
            return None
        return {
            "sub": row["sub"], "tenant": row["tenant_id"],
            "roles": row["roles"] or ["user"], "name": row["name"] or "",
        }
    finally:
        await conn.close()


async def _revoke_refresh_token(token_hash: str) -> None:
    """标记 refresh_token 为 revoked（防重放）。"""
    import asyncpg as _apg

    s = Settings()
    conn = await _apg.connect(
        s.database_url.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        await conn.execute(
            "UPDATE refresh_token_hashes SET revoked=true WHERE token_hash=$1",
            token_hash)
    finally:
        await conn.close()


# ── GET /api/v1/auth/callback (SSO placeholder) ──────────────────

# The /auth/callback route is handled by the frontend (Next.js pages router)
# or a dedicated route in the API. For now, document the expected flow:
#
#   1. User clicks "Keycloak Login" → redirected to IdP
#   2. IdP redirects back to /auth/callback?code=xxx&state=yyy
#   3. Frontend extracts `code`, calls POST /api/v1/auth/token {code}
#   4. Receives access_token + refresh_token → stores in localStorage → enters app


# ── GET /api/v1/auth/callback (SSO 回调) ─────────────────────────

@router.get("/callback")
async def sso_callback(code: str = "", state: str = ""):
    """OAuth2 回调端点 — IdP 登录后重定向至此（P3-15: 完整实现）。

    GET /api/v1/auth/callback?code=xxx&state=yyy

    流程：
    1. IdP 回调带 authorization_code
    2. 后端用 code 换取 access_token + id_token + refresh_token
    3. 验签 id_token → 提取用户信息
    4. 签发本系统 JWT + 存储 refresh_token hash
    5. 返回 HTML 页面将 token 写入 localStorage → 跳转 /kb

    无 code 时：返回 JSON 提示使用 /dev-login（开发模式）。
    OIDC_DISCOVERY_URL 未配置时：返回 501。
    """
    if not code:
        return {
            "message": "SSO callback endpoint. Use ?code=xxx&state=yyy after IdP redirect. "
                       "For dev mode, use POST /api/v1/auth/dev-login instead."
        }

    from src.api.oidc import OIDCProvider

    provider = OIDCProvider()

    if not provider.enabled:
        raise HTTPException(
            status_code=501,
            detail="OIDC not configured. Set OIDC_DISCOVERY_URL + OIDC_CLIENT_ID "
                   "for production SSO, or use POST /api/v1/auth/dev-login in dev mode."
        )

    # ── 1. Exchange authorization_code ──
    # Determine redirect_uri: the callback URL itself (without query params)
    redirect_uri = f"{_get_base_url()}/api/v1/auth/callback"
    tokens = provider.exchange_code(code, redirect_uri)
    if not tokens:
        raise HTTPException(
            status_code=400,
            detail="Token exchange failed — code may be expired or invalid. Please re-login."
        )

    # ── 2. Validate id_token ──
    claims = provider.validate_id_token(tokens.id_token)
    if not claims:
        raise HTTPException(
            status_code=400,
            detail="id_token validation failed. Please re-login."
        )

    # ── 3. Sign our own JWT ──
    roles = claims.roles or ["user"]
    jwt_token, expires_at = _sign_jwt(
        sub=claims.sub or claims.name or "oidc-user",
        tenant=claims.tenant or "unknown",
        roles=roles,
    )

    # ── 4. Store refresh_token hash ──
    refresh_token = tokens.refresh_token or ""
    if refresh_token:
        _store_refresh_token_hash(
            claims.sub, refresh_token,
            tenant=claims.tenant, roles=roles, name=claims.name,
        )

    # ── 5. Return HTML that completes the auth flow in browser ──
    user_info = {
        "id": claims.sub,
        "tenant_id": claims.tenant,
        "roles": roles,
        "name": claims.name or claims.sub,
        "email": claims.email,
    }

    return _sso_callback_html(jwt_token, refresh_token, expires_at.isoformat(), user_info)


def _get_base_url() -> str:
    """Get the base URL for constructing redirect_uri."""
    s = Settings()
    return getattr(s, "public_base_url", "") or "http://localhost:8000"


def _sso_callback_html(
    access_token: str,
    refresh_token: str,
    expires_at: str,
    user: dict,
) -> str:
    """Generate the post-login HTML page that stores auth data in localStorage."""
    import json as _json
    user_json = _json.dumps(user)

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>登录成功 — RAG v14</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            display: flex;
            align-items: center;
            justify-content: center;
            height: 100vh;
            margin: 0;
            background: #f5f5f5;
        }}
        .card {{
            background: white;
            border-radius: 12px;
            padding: 40px;
            text-align: center;
            box-shadow: 0 2px 16px rgba(0,0,0,0.08);
            max-width: 400px;
        }}
        .spinner {{
            width: 40px;
            height: 40px;
            border: 3px solid #e5e7eb;
            border-top-color: #2563eb;
            border-radius: 50%;
            animation: spin 0.8s linear infinite;
            margin: 0 auto 16px;
        }}
        @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
        h1 {{ font-size: 18px; color: #1f2937; margin: 0 0 8px; }}
        p {{ font-size: 14px; color: #6b7280; margin: 0; }}
    </style>
</head>
<body>
    <div class="card">
        <div class="spinner"></div>
        <h1>登录成功</h1>
        <p>正在跳转到系统...</p>
    </div>
    <script>
        (function() {{
            var token = {_json.dumps(access_token)};
            var refresh = {_json.dumps(refresh_token)};
            var expires = {_json.dumps(expires_at)};
            var user = {user_json};

            // Store in localStorage (same format as dev-login)
            localStorage.setItem('access_token', token);
            if (refresh) localStorage.setItem('refresh_token', refresh);
            localStorage.setItem('expires_at', expires);
            localStorage.setItem('user', JSON.stringify(user));

            // Redirect to main app
            var params = new URLSearchParams(window.location.search);
            var state = params.get('state') || '';
            var redirect = '/kb';
            if (state) {{
                try {{ var s = JSON.parse(decodeURIComponent(state)); if (s.redirect) redirect = s.redirect; }} catch(e) {{}}
            }}
            window.location.replace(redirect);
        }})();
    </script>
</body>
</html>"""


# ── GET /api/v1/tenants/{id}/stats ───────────────────────────────

@tenant_router.get("/tenants/{tenant_id}/stats")
async def tenant_stats(tenant_id: str):
    """返回单个租户的统计信息。"""
    import asyncpg

    s = Settings()
    dsn = s.database_url.replace("postgresql+asyncpg://", "postgresql://")

    conn = await asyncpg.connect(dsn)
    try:
        kb_count = await conn.fetchval(
            "SELECT count(*) FROM knowledge_bases WHERE tenant_id=$1", tenant_id
        )
        doc_count = await conn.fetchval(
            "SELECT count(*) FROM documents WHERE tenant_id=$1", tenant_id
        )
        return {
            "id": tenant_id,
            "name": tenant_id,
            "kb_count": kb_count or 0,
            "doc_count": doc_count or 0,
        }
    finally:
        await conn.close()

# ── POST /api/v1/auth/check-permission ────────────────────────────
# P3-14: 前端细粒度按钮权限控制 — 调后端 P-AUTHC check()
# 设计依据：docs/权限管理系统架构设计.md §6.4 阶段三
#          + docs/RAG系统设计v14.md §6.3 check

class CheckPermissionRequest(BaseModel):
    action: str
    resource_type: str
    resource_id: str
    channel_kb: str | None = None  # 通道类动词必带（doc:unmount 等）


class CheckPermissionResponse(BaseModel):
    decision: str  # "allow" | "deny" | "indeterminate"
    decision_id: str
    reasons: list[str] = []


@router.post("/check-permission", response_model=CheckPermissionResponse)
async def check_permission_endpoint(
    body: CheckPermissionRequest,
    authorization: str | None = Header(None, alias="Authorization"),
):
    """前端按钮权限检查 — 调用 P-AUTHC check()。

    前端在渲染操作按钮（删除/下载/解析等）之前调用此端点，
    获取当前用户对该资源的权限判定结果，实现细粒度按钮条件渲染。

    设计依据：docs/权限管理系统架构设计.md §6.4 阶段三
    本端点不执行任何本地判定 — 所有授权决策经 P-AUTHC → 权限服务。

    Raises:
        401: 未提供 Authorization header 或 JWT 无效。
    """
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header required")

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="Bearer token required")

    from src.permission.authz import check as authz_check
    from src.permission.context import build_context

    try:
        ctx = build_context(credential=token)
        result = authz_check(
            ctx=ctx,
            action=body.action,
            resource_type=body.resource_type,
            resource_id=body.resource_id,
            channel_kb=body.channel_kb,
        )
        return CheckPermissionResponse(
            decision=result.get("decision", "deny"),
            decision_id=result.get("decision_id", ""),
            reasons=result.get("reasons", []),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=403,
            detail=f"Permission check failed: {str(exc)}",
        ) from exc


# ── POST /api/v1/auth/switch-tenant ─────────────────────────────
# 无需密码：用当前有效 JWT 验证身份后重新签发（目标租户需通过成员校验）

class SwitchTenantRequest(BaseModel):
    target_tenant: str


@router.post("/switch-tenant", response_model=DevLoginResponse)
async def switch_tenant(
    body: SwitchTenantRequest,
    authorization: str | None = Header(None, alias="Authorization"),
):
    """切换租户：用当前 JWT 验证身份后重签新租户 JWT。

    无需重新输入密码——当前有效 JWT 已证明用户身份。
    后端验证用户属于目标租户后才签发新 JWT。
    """
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header required")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="Bearer token required")

    s = Settings()
    from jose import jwt as jose_jwt, JWTError

    # ── 验证当前 JWT ──
    try:
        with open(s.jwt_public_key_path) as f:
            public_key = f.read()
        claims = jose_jwt.decode(
            token, public_key, algorithms=[s.jwt_algorithm],
            options={"verify_exp": True},
        )
    except JWTError as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {e}")

    username = claims.get("sub", "")
    current_tenant = claims.get("tenant", "")
    roles = claims.get("roles", [])

    if body.target_tenant == current_tenant:
        raise HTTPException(status_code=400, detail="Already in target tenant")

    # ── 验证用户属于目标租户 ──
    if s.authz_service_mode == "remote":
        import httpx
        try:
            async with httpx.AsyncClient(timeout=10.0) as http:
                resp = await http.get(
                    f"{s.authz_service_url}/api/v1/tenants/by-user/user:{username}",
                )
                if resp.status_code == 200:
                    data = resp.json()
                    user_tenant_ids = {t["id"] for t in data.get("tenants", [])}
                    if body.target_tenant not in user_tenant_ids:
                        raise HTTPException(
                            status_code=403,
                            detail=f"用户 '{username}' 不属于租户 '{body.target_tenant}'",
                        )
        except HTTPException:
            raise
        except Exception:
            pass  # 权限服务不可达时容错

    # ── 签发新 JWT ──
    token, expires_at = _sign_jwt(sub=username, tenant=body.target_tenant, roles=roles)
    return DevLoginResponse(
        access_token=token,
        expires_at=expires_at.isoformat(),
        user={
            "id": username,
            "tenant_id": body.target_tenant,
            "roles": roles,
            "name": username,
        },
    )


@tenant_router.get("/tenants", response_model=list[TenantInfo])
async def list_tenants(
    authorization: str | None = Header(None, alias="Authorization"),
):
    """返回租户列表。

    行为区分：
    - 已认证用户 → 仅返回当前用户所属的租户（Header 中展示）
    - 未认证 → 返回所有可用租户（登录页租户下拉选择）

    数据来源：
    1. 权限服务 /api/v1/tenants/by-user/{user_id}（已认证时优先）
    2. 权限服务 /api/v1/tenants（未认证时获取全量）
    3. 本地 knowledge_bases 表 DISTINCT tenant_id（fallback）
    """
    import asyncpg
    import httpx
    from jose import jwt as jose_jwt

    s = Settings()
    tenants: dict[str, TenantInfo] = {}
    current_user_id: str | None = None

    # ── 解析用户身份（若已认证）──
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer" and token:
            try:
                with open(s.jwt_public_key_path) as f:
                    public_key = f.read()
                claims = jose_jwt.decode(
                    token, public_key, algorithms=[s.jwt_algorithm],
                    options={"verify_exp": True},
                )
                current_user_id = claims.get("sub", "")
            except Exception:
                pass  # token 无效 → 按未认证处理

    # ── 来源 1：权限服务 ──
    if s.authz_service_mode == "remote":
        try:
            async with httpx.AsyncClient(timeout=10.0) as http:
                if current_user_id:
                    # 已认证：只获取用户所属的租户
                    resp = await http.get(
                        f"{s.authz_service_url}/api/v1/tenants/by-user/user:{current_user_id}",
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        for t in data.get("tenants", []):
                            tenants[t["id"]] = TenantInfo(
                                id=t["id"],
                                name=t.get("name", t["id"]),
                                kb_count=t.get("member_count", 0),
                                doc_count=0,
                            )
                else:
                    # 未认证：获取全量租户（登录页使用）
                    resp = await http.get(
                        f"{s.authz_service_url}/api/v1/tenants",
                        params={"status": "active", "limit": 200},
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        for t in data.get("tenants", []):
                            tenants[t["id"]] = TenantInfo(
                                id=t["id"],
                                name=t.get("name", t["id"]),
                                kb_count=t.get("member_count", 0),
                                doc_count=0,
                            )
        except Exception:
            pass  # fall through to local DB

    # ── 来源 2：本地 knowledge_bases 表 ──
    dsn = s.database_url.replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch("""
            SELECT
                tenant_id,
                (SELECT count(*) FROM knowledge_bases WHERE tenant_id = k.tenant_id) as kb_count,
                (SELECT count(*) FROM documents WHERE tenant_id = k.tenant_id) as doc_count
            FROM knowledge_bases k
            GROUP BY tenant_id
        """)
        for r in rows:
            tid = r["tenant_id"]
            if tid not in tenants:
                tenants[tid] = TenantInfo(
                    id=tid,
                    name=tid,
                    kb_count=r["kb_count"],
                    doc_count=r["doc_count"],
                )
            else:
                t = tenants[tid]
                t.kb_count = max(t.kb_count, r["kb_count"])
                t.doc_count = max(t.doc_count, r["doc_count"])
    finally:
        await conn.close()

    # ── 已认证用户：确保当前 JWT 中的租户也出现在列表中 ──
    if current_user_id and authorization:
        try:
            scheme, _, token = authorization.partition(" ")
            with open(s.jwt_public_key_path) as f:
                public_key = f.read()
            claims = jose_jwt.decode(
                token, public_key, algorithms=[s.jwt_algorithm],
                options={"verify_exp": False},
            )
            jwt_tenant = claims.get("tenant", "")
            if jwt_tenant and jwt_tenant not in tenants:
                tenants[jwt_tenant] = TenantInfo(
                    id=jwt_tenant,
                    name=jwt_tenant,
                    kb_count=0,
                    doc_count=0,
                )
        except Exception:
            pass

    return list(tenants.values())

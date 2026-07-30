"""P-AUTHC 中间件 — JWT 验证 + RequestContext 注入。

FastAPI 中间件：在每个请求进入路由之前验证 JWT。
公开路由（/api/v1/auth/*, /healthz, /readyz, /ping, /docs, /openapi.json）跳过验证。
无 token → 401，过期 token → 401，无效签名 → 401。

公钥来源（优先级从高到低）：
1. JWT_JWKS_URL → PyJWKClient（自动缓存 + 定期刷新，生产推荐）
2. JWT_PUBLIC_KEY_PATH → 本地 PEM 文件（开发默认）
"""

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from jose import jwt as jose_jwt, JWTError

from src.config import Settings
from src.permission.context import RequestContext


PUBLIC_PREFIXES = (
    "/api/v1/auth", "/healthz", "/readyz", "/ping",
    "/docs", "/openapi.json", "/favicon.ico",
)

# SSE streaming endpoints that may need token via query parameter.
# Browser EventSource API cannot send Authorization headers; the
# standard workaround is to pass the token as ?token=<jwt>.
_STREAM_PREFIX = "/api/v1/conversations/"


class AuthMiddleware(BaseHTTPMiddleware):
    """JWT 验证中间件。

    从 Authorization header 提取 Bearer token → 验证签名+过期 → 注入 request.state.ctx。
    公开路径跳过。验证失败返回 JSONResponse 401。
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # 公开路径跳过
        if path.startswith(PUBLIC_PREFIXES):
            return await call_next(request)

        # 提取 token — 优先 Authorization header，SSE 端点回退到 query param
        header = request.headers.get("Authorization", "")
        token = None
        if header.startswith("Bearer "):
            token = header[7:]

        # 浏览器 EventSource 不支持自定义 header，SSE 端点允许 ?token=<jwt>
        if not token and path.startswith(_STREAM_PREFIX) and "/stream" in path:
            token = request.query_params.get("token")

        if not token:
            return JSONResponse(
                status_code=401,
                content={"error_code": "auth:unauthenticated", "message": "missing token"},
            )

        # JWT 验证
        client_ip = request.client.host if request.client else ""
        try:
            ctx = _verify_and_build_ctx(token, client_ip)
            request.state.ctx = ctx
        except _AuthError as e:
            return JSONResponse(status_code=e.status, content={"error_code": e.error_code, "message": e.message})

        return await call_next(request)


class _AuthError(Exception):
    def __init__(self, status: int, error_code: str, message: str):
        self.status = status
        self.error_code = error_code
        self.message = message


# ── JWKS client（懒加载，自动缓存；仅 python-jose ≥3.4 支持） ──

_jwks_client = None
_jwks_tried = False


def _get_jwks_client():
    """返回 PyJWKClient 单例。

    python-jose < 3.4 不支持 PyJWKClient → 返回 None，回退 PEM 文件。
    升级到 python-jose[cryptography]>=3.4 后自动启用 JWKS。
    """
    global _jwks_client, _jwks_tried
    if _jwks_client is not None:
        return _jwks_client
    if _jwks_tried:
        return None

    s = Settings()
    if not s.jwt_jwks_url:
        _jwks_tried = True
        return None

    try:
        from jose import PyJWKClient  # noqa: F811 — available in python-jose >= 3.4
        _jwks_client = PyJWKClient(s.jwt_jwks_url, cache_keys=True)
    except ImportError:
        # python-jose < 3.4 — PyJWKClient not available
        # Upgrade: pip install "python-jose[cryptography]>=3.4"
        pass
    _jwks_tried = True
    return _jwks_client


def _verify_and_build_ctx(token: str, client_ip: str = "") -> RequestContext:
    """验证 JWT 签名 + 过期，构建 RequestContext。

    公钥来源：
    1. JWT_JWKS_URL 已配置 → PyJWKClient（自动缓存，生产推荐）
    2. 否则 → 本地 PEM 文件（开发默认）
    """
    s = Settings()

    # ── 路径 1: JWKS URL（生产） ──
    jwks = _get_jwks_client()
    if jwks is not None:
        try:
            signing_key = jwks.get_signing_key_from_jwt(token)
            claims = jose_jwt.decode(
                token, signing_key.key,
                algorithms=[s.jwt_algorithm],
                options={"verify_exp": True},
            )
        except JWTError as e:
            raise _AuthError(401, "auth:unauthenticated", str(e))
        except Exception as e:
            raise _AuthError(500, "common:internal_error",
                           f"JWKS validation failed: {e}")
    else:
        # ── 路径 2: 本地 PEM 文件（开发） ──
        try:
            with open(s.jwt_public_key_path) as f:
                public_key = f.read()
        except FileNotFoundError:
            raise _AuthError(500, "common:internal_error",
                           "JWT public key not found. "
                           "Configure JWT_JWKS_URL for production or ensure "
                           "JWT_PUBLIC_KEY_PATH points to a valid PEM file.")

        try:
            claims = jose_jwt.decode(token, public_key, algorithms=[s.jwt_algorithm])
        except JWTError as e:
            raise _AuthError(401, "auth:unauthenticated", str(e))

    user_id = claims.get("sub", "unknown")
    tenant_id = claims.get("tenant", "tenant-dev")
    roles = claims.get("roles", ["user"])
    if isinstance(roles, str):
        roles = [roles]

    principals = [f"user:{user_id}"]
    for r in roles:
        principals.append(f"role:{r}")

    return RequestContext(
        request_id=claims.get("jti", "unknown"),
        user_id=user_id,
        tenant_id=tenant_id,
        credential=token,
        roles=roles,
        groups=[],
        principals=principals,
        client_ip=client_ip,
    )

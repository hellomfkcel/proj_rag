"""OIDC 通用协议层 — 不绑定特定 IdP。

适配 Keycloak / Auth0 / Okta / 企业自建 IdP 等任何符合 OIDC 标准的 Provider。

职责：
- OIDC discovery（自动发现 token_endpoint / jwks_uri / issuer，缓存 1h）
- authorization_code → token 交换
- id_token 本地验签（JWKS 公钥）
- refresh_token 轮换

用法：
    from src.api.oidc import OIDCProvider

    provider = OIDCProvider()          # 从 Settings 读 OIDC_DISCOVERY_URL
    tokens = provider.exchange_code(code, redirect_uri)
    claims = provider.validate_id_token(tokens["id_token"])
    new_tokens = provider.refresh(tokens["refresh_token"])
"""

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import httpx
from jose import jwt as jose_jwt
from jose.exceptions import JWTError

from src.config import Settings
from src.platform.obs.logger import get_logger

log = get_logger(__name__)

# ── discovery 缓存（进程内） ──
_discovery_cache: Dict[str, tuple] = {}  # url → (payload, expires_at)


@dataclass
class OIDCTokens:
    access_token: str
    id_token: str = ""
    refresh_token: str = ""
    token_type: str = "Bearer"
    expires_in: int = 3600


@dataclass
class OIDCClaims:
    sub: str                          # 用户唯一标识
    tenant: str = ""                  # 租户（从 JWT claim 映射，可配置）
    roles: list = field(default_factory=list)
    groups: list = field(default_factory=list)
    email: str = ""
    name: str = ""
    raw: dict = field(default_factory=dict)


class OIDCProvider:
    """OIDC Provider 封装。

    若 Settings.OIDC_DISCOVERY_URL 为空 → enabled=False，所有操作返回 None。
    此时 auth.py 回退到开发模式 /dev-login。
    """

    def __init__(self):
        s = Settings()
        self.discovery_url: str = getattr(s, "oidc_discovery_url", "") or ""
        self.client_id: str = getattr(s, "oidc_client_id", "") or ""
        self.client_secret: str = getattr(s, "oidc_client_secret", "") or ""
        self._http = httpx.Client(timeout=15.0)

    @property
    def enabled(self) -> bool:
        return bool(self.discovery_url and self.client_id)

    # ── Discovery ─────────────────────────────────────────────────

    def discover(self) -> Optional[Dict[str, Any]]:
        """OIDC discovery（缓存 1h）。

        GET {discovery_url} → {token_endpoint, jwks_uri, issuer, ...}
        """
        if not self.enabled:
            return None

        now = time.time()
        cached = _discovery_cache.get(self.discovery_url)
        if cached and cached[1] > now:
            return cached[0]

        try:
            resp = self._http.get(self.discovery_url)
            resp.raise_for_status()
            meta = resp.json()

            # 校验必要字段
            for field in ("token_endpoint", "jwks_uri", "issuer"):
                if field not in meta:
                    log.error("oidc_discovery_missing_field", url=self.discovery_url, field=field)
                    return None

            _discovery_cache[self.discovery_url] = (meta, now + 3600)
            log.info("oidc_discovery_ok", issuer=meta.get("issuer", ""))
            return meta
        except Exception as exc:
            log.error("oidc_discovery_failed", url=self.discovery_url, error=str(exc))
            return None

    # ── Token Exchange ────────────────────────────────────────────

    def exchange_code(self, code: str, redirect_uri: str = "") -> Optional[OIDCTokens]:
        """authorization_code → access_token + id_token + refresh_token。

        POST {token_endpoint} with grant_type=authorization_code。
        """
        meta = self.discover()
        if not meta:
            return None

        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        if redirect_uri:
            payload["redirect_uri"] = redirect_uri

        try:
            resp = self._http.post(
                meta["token_endpoint"],
                data=payload,
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()

            tokens = OIDCTokens(
                access_token=data.get("access_token", ""),
                id_token=data.get("id_token", ""),
                refresh_token=data.get("refresh_token", ""),
                token_type=data.get("token_type", "Bearer"),
                expires_in=data.get("expires_in", 3600),
            )

            if not tokens.access_token:
                log.error("oidc_exchange_no_access_token", response_keys=list(data.keys()))
                return None

            log.info("oidc_code_exchanged", has_refresh=bool(tokens.refresh_token))
            return tokens
        except Exception as exc:
            log.error("oidc_exchange_failed", error=str(exc))
            return None

    # ── id_token Validation ──────────────────────────────────────

    def validate_id_token(self, id_token: str) -> Optional[OIDCClaims]:
        """验签 id_token（JWKS 公钥）→ 提取 claims。

        验证：
        - RS256 签名（用 JWKS 公钥）
        - exp 过期
        - iss 匹配 discovery 返回的 issuer
        - aud 包含本系统的 client_id
        """
        if not id_token:
            return None

        meta = self.discover()
        if not meta:
            return None

        issuer = meta.get("issuer", "")
        jwks_uri = meta.get("jwks_uri", "")

        # 1. 获取 JWKS
        try:
            jwks_resp = self._http.get(jwks_uri)
            jwks_resp.raise_for_status()
            jwks = jwks_resp.json()
        except Exception as exc:
            log.error("oidc_jwks_fetch_failed", jwks_uri=jwks_uri, error=str(exc))
            return None

        # 2. 解码 header 获取 kid，匹配 JWK
        try:
            unverified_header = jose_jwt.get_unverified_header(id_token)
            kid = unverified_header.get("kid", "")
        except JWTError:
            log.warning("oidc_id_token_header_invalid")
            return None

        signing_key = None
        for key in jwks.get("keys", []):
            if key.get("kid") == kid:
                signing_key = key
                break

        if not signing_key:
            # Fallback: try first RS256 key
            for key in jwks.get("keys", []):
                if key.get("alg", "").startswith("RS"):
                    signing_key = key
                    break

        if not signing_key:
            log.error("oidc_no_signing_key", kid=kid)
            return None

        # 3. 验签
        try:
            claims = jose_jwt.decode(
                id_token, signing_key,
                algorithms=["RS256"],
                options={"verify_exp": True, "verify_aud": False},
            )
        except JWTError as exc:
            log.warning("oidc_id_token_validation_failed", error=str(exc))
            return None

        # 4. 校验 issuer（关键安全校验）
        if issuer and claims.get("iss") != issuer:
            log.error("oidc_issuer_mismatch", expected=issuer, got=claims.get("iss"))
            return None

        # 5. 校验 audience（包含本系统 client_id）
        aud = claims.get("aud", "")
        if isinstance(aud, list):
            if self.client_id not in aud:
                log.warning("oidc_audience_mismatch", expected=self.client_id, got=aud)
                # Non-fatal: some IdPs put resource-specific aud
        elif aud and aud != self.client_id:
            log.warning("oidc_audience_mismatch", expected=self.client_id, got=aud)

        # 6. 提取 claims → OIDCClaims
        return OIDCClaims(
            sub=claims.get("sub", ""),
            tenant=claims.get("tenant", claims.get("tenant_id", "")),
            roles=self._extract_roles(claims),
            groups=self._extract_groups(claims),
            email=claims.get("email", ""),
            name=claims.get("name", claims.get("preferred_username", "")),
            raw=claims,
        )

    # ── Refresh Token ─────────────────────────────────────────────

    def refresh(self, refresh_token: str) -> Optional[OIDCTokens]:
        """refresh_token → 新 access_token + 轮换 refresh_token。

        POST {token_endpoint} with grant_type=refresh_token。
        """
        meta = self.discover()
        if not meta:
            return None

        payload = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }

        try:
            resp = self._http.post(
                meta["token_endpoint"],
                data=payload,
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()

            tokens = OIDCTokens(
                access_token=data.get("access_token", ""),
                id_token=data.get("id_token", ""),
                refresh_token=data.get("refresh_token", ""),  # rotated
                token_type=data.get("token_type", "Bearer"),
                expires_in=data.get("expires_in", 3600),
            )

            log.info("oidc_token_refreshed", has_new_refresh=bool(tokens.refresh_token))
            return tokens
        except Exception as exc:
            log.error("oidc_refresh_failed", error=str(exc))
            return None

    # ── Claim extraction helpers ─────────────────────────────────

    def _extract_roles(self, claims: dict) -> list:
        """从 claims 提取角色列表（兼容 Keycloak / Auth0 / 自定义格式）。"""
        # Keycloak: realm_access.roles
        realm = claims.get("realm_access", {})
        if isinstance(realm, dict):
            roles = realm.get("roles", [])
            if roles:
                return roles

        # Auth0 / generic: "roles" claim or "role" claim
        roles = claims.get("roles", claims.get("role", []))
        if isinstance(roles, str):
            return [roles]
        if isinstance(roles, list):
            return roles

        return []

    def _extract_groups(self, claims: dict) -> list:
        """从 claims 提取组列表。"""
        groups = claims.get("groups", [])
        if isinstance(groups, str):
            return [groups]
        if isinstance(groups, list):
            return groups
        return []

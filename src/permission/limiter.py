"""RAG 限流器单例 — 认证端点防暴力破解（/dev-login /refresh /token）。"""

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["100/minute"],
)

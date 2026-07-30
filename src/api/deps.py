"""FastAPI 依赖注入。

提供：
- get_request_context: 从中间件注入的 request.state.ctx 获取 RequestContext
- get_settings: Settings 单例
- require_permission: 路由级权限门禁
"""

from functools import lru_cache

from fastapi import Request, Depends

from src.permission.context import RequestContext


def get_request_context(request: Request) -> RequestContext:
    """从 AuthMiddleware 注入的 request.state.ctx 获取 RequestContext。

    公开路由无 ctx → 返回开发期默认 ctx。
    """
    ctx = getattr(request.state, "ctx", None)
    if ctx is not None:
        return ctx
    # 公开路径 fallback
    return RequestContext(
        request_id="dev-public",
        user_id="anonymous",
        tenant_id="tenant-dev",
        credential="",
        roles=["user"],
        principals=["user:anonymous"],
    )


@lru_cache()
def get_settings():
    from src.config import Settings
    return Settings()

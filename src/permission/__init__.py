"""P-AUTHC：权限消费适配模块。

本系统访问外部权限服务（Cerbos）的唯一出口。

提供：
- build_context              JWT 校签 + ctx 构建 + credential 保管
- check                      单条权限判定 → POST /v1/check
- check_batch                批量判定（阶段一：串行调 check）
- filter_items               检索后逐条复核（阶段一：接口有，strict 默认 false）
- get_prefilter              检索前编译 → PreFilter | SUSPENDED
- compile_filter             六条件 → Haystack MetadataFilter
- mint_ctx_token             铸造异步任务 ctx_token → POST /v1/context
- register_resource / link_resource / unlink_resource / retire_resource

不做：不存任何权益数据、不写任何策略、不做任何判定。
"""

from .context import build_context, resolve_ctx_token
from .authz import (
    check,
    check_batch,
    filter_items,
    get_prefilter,
    compile_filter,
    mint_ctx_token,
    register_resource,
    link_resource,
    unlink_resource,
    retire_resource,
    get_visibility,
    require_permission,
)

__all__ = [
    "build_context",
    "resolve_ctx_token",
    "check",
    "check_batch",
    "filter_items",
    "get_prefilter",
    "compile_filter",
    "mint_ctx_token",
    "register_resource",
    "link_resource",
    "unlink_resource",
    "retire_resource",
    "get_visibility",
    "require_permission",
]

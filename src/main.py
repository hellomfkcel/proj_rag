"""RAG 系统 v14 — FastAPI 应用入口。

API 进程只做请求接收、参数校验、任务分发和流式转发。
任何 Haystack Pipeline 的计算只在 Celery Worker 内执行。

启动顺序（严格遵守）：
1. 模块级：结构化日志 → OTel SDK → 创建 app → FastAPI 自动插桩（必须在 app 启动前）
2. lifespan 内：Prometheus 指标 → Langfuse 模型观测
3. 模块级（app 创建后）：CORS 中间件 → Auth 中间件 → 路由注册

原因：Starlette 的 middleware_stack 在首次 __call__（含 lifespan scope）时构建，
之后 add_middleware 会抛出 RuntimeError。FastAPIInstrumentor.instrument_app 内部
调用 add_middleware，因此必须在 lifespan 之外、app 接收任何 scope 之前执行。
"""

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# ═══════════════════════════════════════════════════════════════════════
# 阶段 0：P-OBS 最早初始化（模块级别）
# ═══════════════════════════════════════════════════════════════════════

# 结构化日志（最早，确保后续所有 import 的日志被 structlog 处理）
from src.platform.obs.logger import setup_logging
setup_logging()

# 分布式追踪 SDK（必须在 FastAPIInstrumentor 之前初始化，因为 instrumentor 读取全局 TracerProvider）
from src.platform.obs.tracing import init_tracing
init_tracing("rag-v14")

# ═══════════════════════════════════════════════════════════════════════
# 阶段 1：FastAPI 应用创建 + OTel 自动插桩（必须在 lifespan 之外）
# ═══════════════════════════════════════════════════════════════════════


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：仅处理不修改 middleware 栈的初始化。

    OTel 插桩（instrument_fastapi）不在 lifespan 内，原因是它内部调用
    app.add_middleware()——而 lifespan 执行时 middleware_stack 已构建完毕。
    """
    # P-OBS: Prometheus 指标初始化（fail-open，OTLP 不可达时降级到内存累计）
    from src.platform.obs.metrics import init_metrics
    init_metrics()

    # P-MODEL: Langfuse 模型观测（fail-open，未配置 key 时跳过）
    from src.platform.model.registry import init_langfuse
    init_langfuse()
    yield


app = FastAPI(
    title="RAG v14",
    version="0.1.0",
    lifespan=lifespan,
)

# FastAPI OTel 自动插桩：为每个 HTTP 请求创建 span，记录 method/path/status_code。
# ★ 必须在所有其他 add_middleware 之前调用，且必须在 app 启动前（lifespan 之外）。
from src.platform.obs.tracing import instrument_fastapi
instrument_fastapi(app)

# ═══════════════════════════════════════════════════════════════════════
# 阶段 2：业务中间件 + 路由注册
# ═══════════════════════════════════════════════════════════════════════

# CORS 先加（处理 preflight），Auth 后加
# 来源白名单从环境变量 CORS_ALLOWED_ORIGINS 读取（逗号分隔）
# 开发默认值：localhost:3001,localhost:3000；生产必须配置为具体域名
from src.api.routes import router as api_router
from src.api.auth import router as auth_router, tenant_router
from src.api.kb_routes import router as kb_router
from src.api.chat_routes import router as chat_router
from src.api.dir_routes import router as dir_router
from src.api.settings_routes import router as settings_router
from src.api.dashboard_routes import router as dashboard_router
from src.permission.middleware import AuthMiddleware
from src.config import Settings as _Settings

_cors_settings = _Settings()
_cors_origins = [o.strip() for o in _cors_settings.cors_allowed_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
    allow_credentials=True,
)
app.add_middleware(AuthMiddleware)

app.include_router(api_router)
app.include_router(auth_router)
app.include_router(tenant_router)
app.include_router(kb_router)
app.include_router(chat_router)
app.include_router(dir_router)
app.include_router(settings_router)
app.include_router(dashboard_router)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}

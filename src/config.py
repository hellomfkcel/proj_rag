"""应用配置 — 唯一入口。

优先级：环境变量 (.env) > 代码默认值。
禁止在其他模块中硬编码 URL / 密码 / 模型名 / 端口。

模型切换只需改 .env 或 DB model_registry 表 — 零代码改动。
"""

import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    """应用设置，全部从环境变量读取。"""

    # ── 数据库 ──
    # 无默认值：开发由 .env / compose 注入；生产由 validate_production_config 兜底
    database_url: str = os.getenv("DATABASE_URL", "")

    # ── Redis ──
    redis_url: str = os.getenv("REDIS_URL", "")

    # ── Milvus ──
    milvus_host: str = os.getenv("MILVUS_HOST", "localhost")
    milvus_port: int = int(os.getenv("MILVUS_PORT", "19530"))

    # ── 对象存储 (SeaweedFS S3) ──
    s3_endpoint_url: str = os.getenv("S3_ENDPOINT_URL", "http://localhost:18333")
    s3_access_key: str = os.getenv("S3_ACCESS_KEY", "minioadmin")
    s3_secret_key: str = os.getenv("S3_SECRET_KEY", "minioadmin")
    s3_bucket: str = os.getenv("S3_BUCKET", "rag-files")

    # ── 权限服务 (Cerbos / 外部权限服务) ──
    authz_base_url: str = os.getenv("AUTHZ_BASE_URL", "http://localhost:13592")
    authz_timeout_ms: int = int(os.getenv("AUTHZ_TIMEOUT_MS", "2000"))

    # 权限服务模式：local（直接调 Cerbos PDP）| remote（调外部权限服务后端）
    authz_service_mode: str = os.getenv("AUTHZ_SERVICE_MODE", "local")
    # 外部权限服务后端地址（remote 模式使用）。上线必须显式配置，
    # 不提供默认值 —— 缺省即 remote 模式不可用，避免静默连到过期地址。
    authz_service_url: str = os.getenv("AUTHZ_SERVICE_URL", "")
    # VisibilityChanged 事件流 Redis URL（remote 模式使用）
    # 无默认值：生产由 validate_production_config 强制要求显式配置
    authz_event_stream_redis_url: str = os.getenv("AUTHZ_EVENT_STREAM_REDIS_URL", "")
    # RAG 系统所属的项目 ID，用于权限服务生命周期端点 (register/link/unlink/retire)
    # 默认 "rag-v14" 对应权限服务启动时自动种子项目
    authz_project_id: str = os.getenv("AUTHZ_PROJECT_ID", "rag-v14")

    # 服务间认证凭据 — 调用权限服务后端时作为 X-Api-Key 头发送
    # 生产环境必须配置，与权限服务侧 shared key 一致
    authz_client_credential: str = os.getenv("AUTHZ_CLIENT_CREDENTIAL", "")

    # ctx_token 签名密钥（HMAC-SHA256）
    # 生产环境必须显式配置。未配置时回退到 Redis URL hash（仅开发兼容）。
    ctx_token_secret: str = os.getenv("CTX_TOKEN_SECRET", "")

    # ── LLM / Embedding 模型服务（provider 自动判定） ──
    # LLM_BASE_URL 同时用于 LLM chat 和 embedding 调用。
    # 兼容：OLLAMA_BASE_URL（向下兼容旧变量名）
    llm_base_url: str = os.getenv("LLM_BASE_URL",
        os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"))

    # Embedding 专用 URL（如果和 LLM 不是同一个服务），默认 = llm_base_url
    embedding_base_url: str = os.getenv("EMBEDDING_BASE_URL",
        os.getenv("LLM_BASE_URL",
        os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")))

    # 默认 Embedding 模型名
    embedding_model: str = os.getenv("EMBEDDING_MODEL", "qwen3-embedding:0.6b")

    # 默认 LLM 模型名
    llm_model: str = os.getenv("LLM_MODEL",
        os.getenv("OLLAMA_MODEL", "qwen2.5-coder:14b"))

    # API Key（OpenAI / vLLM / DeepSeek 等需要；Ollama 不需要）
    llm_api_key: str = os.getenv("LLM_API_KEY", "ollama")

    # LLM 生成参数（P-MODEL invoke_llm 默认值，env 可覆盖；推理类模型需预留 reasoning token 预算）
    llm_max_tokens: int = int(os.getenv("LLM_MAX_TOKENS", "8192"))
    llm_temperature: float = float(os.getenv("LLM_TEMPERATURE", "0.1"))

    # 向下兼容（registry.py / pipeline_runner 仍在用）
    @property
    def ollama_base_url(self) -> str: return self.llm_base_url
    @property
    def ollama_model(self) -> str: return self.embedding_model

    # ── JWT 认证 ──
    jwt_private_key_path: str = os.getenv("JWT_PRIVATE_KEY_PATH", "./config/jwt_private.pem")
    jwt_public_key_path: str = os.getenv("JWT_PUBLIC_KEY_PATH", "./config/jwt_public.pem")
    jwt_algorithm: str = os.getenv("JWT_ALGORITHM", "RS256")
    jwt_expire_seconds: int = int(os.getenv("JWT_EXPIRE_SECONDS", "3600"))
    # Keycloak IdP 配置（所有登录均通过 Keycloak 验证用户名密码）。
    # 上线必须显式配置（KEYCLOAK_SERVER_URL），缺省为空 → 登录返回明确的 503。
    keycloak_server_url: str = os.getenv("KEYCLOAK_SERVER_URL", "")
    keycloak_realm: str = os.getenv("KEYCLOAK_REALM", "rag-v14")
    keycloak_client_id: str = os.getenv("KEYCLOAK_CLIENT_ID", "rag-frontend")

    # ── Pipeline ──
    pipeline_yaml_dir: str = os.getenv("PIPELINE_YAML_DIR", "./pipelines")

    # ── OTel ──
    otel_endpoint: str = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")

    # ── Langfuse ──
    langfuse_public_key: str = os.getenv("LANGFUSE_PUBLIC_KEY", "")
    langfuse_secret_key: str = os.getenv("LANGFUSE_SECRET_KEY", "")
    langfuse_host: str = os.getenv("LANGFUSE_HOST", "http://localhost:13000")

    # ── 前端外部链接（Dashboard / 设置页跳转） ──
    # 浏览器通过局域网 IP 访问这些服务，默认回退到后端连接地址
    grafana_url: str = os.getenv("GRAFANA_URL", "http://localhost:3000")
    # Grafana Tempo 数据源 UID（前端 trace 深链跳转用；观测栈 provision 固定为 tempo-uid）
    grafana_tempo_datasource_uid: str = os.getenv("GRAFANA_TEMPO_DATASOURCE_UID", "tempo-uid")
    langfuse_public_url: str = os.getenv("LANGFUSE_PUBLIC_URL", os.getenv("LANGFUSE_HOST", "http://localhost:13000"))
    cerbos_public_url: str = os.getenv("CERBOS_PUBLIC_URL", os.getenv("AUTHZ_BASE_URL", "http://localhost:13592"))
    admin_console_url: str = os.getenv("ADMIN_CONSOLE_URL", "")

    # ── JWT JWKS（生产环境从 IdP 自动获取公钥，取代本地 PEM 文件） ──
    jwt_jwks_url: str = os.getenv("JWT_JWKS_URL", "")

    # ── CORS 允许来源（逗号分隔，生产必须配置为具体域名） ──
    cors_allowed_origins: str = os.getenv(
        "CORS_ALLOWED_ORIGINS", "http://localhost:3001,http://localhost:3000")

    # ── OIDC 认证（通用协议层，适配 Keycloak / Auth0 / Okta / 企业 IdP） ──
    # 不配置 OIDC_DISCOVERY_URL 时自动退回 /dev-login 开发模式
    oidc_discovery_url: str = os.getenv("OIDC_DISCOVERY_URL", "")
    oidc_client_id: str = os.getenv("OIDC_CLIENT_ID", "")
    oidc_client_secret: str = os.getenv("OIDC_CLIENT_SECRET", "")

    # ── 日志 ──
    log_level: str = os.getenv("LOG_LEVEL", "INFO")

    # ── 运行模式 ──
    # APP_ENV=production 时：强制 SSO/JWKS/密钥校验、禁用 dev 认证路径（/dev-login、/refresh dev 分支）。
    # 默认 development（本地/联调）；deploy 脚本置 production。
    app_env: str = os.getenv("APP_ENV", "development")

    @property
    def production(self) -> bool:
        """生产模式判定：APP_ENV=production。"""
        return self.app_env == "production"

    # 前端可访问的本系统外部基址（OIDC redirect_uri / SSO 回调推导用）。
    # 生产必须配置为浏览器可达地址（域名或公网 IP），不配置则回退 OIDC exchange 不传 redirect_uri。
    public_base_url: str = os.getenv("PUBLIC_BASE_URL", "")

    # ── 模型提供商判定 ──
    @property
    def is_ollama(self) -> bool:
        """是否使用本地 Ollama（基于 URL 自动判定）。"""
        return "11434" in self.llm_base_url and "vllm" not in self.llm_base_url


# ══════════════════════════════════════════════════════════════════
# 生产安全启动检查（APP_ENV=production）
# ══════════════════════════════════════════════════════════════════

def validate_production_config() -> list[str]:
    """启动时校验关键安全配置，返回警告列表。

    开发环境（APP_ENV != production）：仅收集 warnings，不阻塞。
    生产环境（APP_ENV=production）：关键项缺失 raise RuntimeError，阻止以弱配置上线。

    生产要求：
    1. CTX_TOKEN_SECRET 显式配置（禁止回退 Redis URL hash）
    2. AUTHZ_CLIENT_CREDENTIAL 显式配置（RAG→权限服务 X-Api-Key）
    3. AUTHZ_SERVICE_MODE=remote（权限判定走外部权限平台）
    4. OIDC_DISCOVERY_URL + JWT_JWKS_URL 配置（SSO 登录 + IdP 远程验签）
    5. DB/Redis 连接串不含开发口令；AUTHZ_EVENT_STREAM_REDIS_URL 显式配置
    """
    s = Settings()
    warnings: list[str] = []
    errors: list[str] = []

    # 1. ctx_token 签名密钥
    if not s.ctx_token_secret:
        msg = (
            "CTX_TOKEN_SECRET is not set (falls back to REDIS_URL hash for ctx_token signing). "
            "In production, set a strong random value (min 32 chars)."
        )
        if s.production:
            errors.append(msg)
        else:
            warnings.append(msg)

    # 2. 服务间认证凭据
    if not s.authz_client_credential:
        msg = (
            "AUTHZ_CLIENT_CREDENTIAL is not set. "
            "Production requires the permission-service shared API key (X-Api-Key)."
        )
        if s.production:
            errors.append(msg)
        else:
            warnings.append(msg)

    # 3. 权限判定模式
    if s.authz_service_mode != "remote":
        msg = (
            f"AUTHZ_SERVICE_MODE={s.authz_service_mode}; "
            "production requires 'remote' (external permission service)."
        )
        if s.production:
            errors.append(msg)
        else:
            warnings.append(msg)

    # 4. SSO 登录（生产禁用 dev-login）
    if not s.oidc_discovery_url or not s.oidc_client_id:
        msg = (
            "OIDC_DISCOVERY_URL / OIDC_CLIENT_ID is not set. "
            "Production requires SSO (dev-login is disabled in production)."
        )
        if s.production:
            errors.append(msg)
        else:
            warnings.append(msg)

    if not s.jwt_jwks_url:
        msg = (
            "JWT_JWKS_URL is not set. "
            "Production should verify tokens against the IdP JWKS endpoint."
        )
        if s.production:
            errors.append(msg)
        else:
            warnings.append(msg)

    # 5. 开发口令泄漏进生产连接串
    _dev_creds = ("rag_dev_pwd_2026", "perm_pass", "perm_redis_pwd_2026")
    for _name, _url in (
        ("DATABASE_URL", s.database_url),
        ("REDIS_URL", s.redis_url),
        ("AUTHZ_EVENT_STREAM_REDIS_URL", s.authz_event_stream_redis_url),
    ):
        if any(c in _url for c in _dev_creds):
            msg = f"{_name} contains a development/default credential."
            if s.production:
                errors.append(msg)
            else:
                warnings.append(msg)

    # 5b. 事件流 Redis 必须配置（visibility-events 订阅依赖）
    if not s.authz_event_stream_redis_url:
        msg = "AUTHZ_EVENT_STREAM_REDIS_URL is not set; visibility-events cannot subscribe."
        if s.production:
            errors.append(msg)
        else:
            warnings.append(msg)

    if errors:
        raise RuntimeError(
            "Production security checks failed:\n- " + "\n- ".join(errors)
        )

    return warnings

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
    database_url: str = os.getenv("DATABASE_URL",
        "postgresql+asyncpg://rag:rag_dev_pwd_2026@localhost:25432/rag")

    # ── Redis ──
    redis_url: str = os.getenv("REDIS_URL",
        "redis://:rag_dev_pwd_2026@localhost:16379/0")

    # ── Milvus ──
    milvus_host: str = os.getenv("MILVUS_HOST", "localhost")
    milvus_port: int = int(os.getenv("MILVUS_PORT", "19530"))

    # ── 对象存储 (SeaweedFS S3) ──
    s3_endpoint_url: str = os.getenv("S3_ENDPOINT_URL", "http://localhost:18333")
    s3_access_key: str = os.getenv("S3_ACCESS_KEY", "minioadmin")
    s3_secret_key: str = os.getenv("S3_SECRET_KEY", "minioadmin")
    s3_bucket: str = os.getenv("S3_BUCKET", "rag-files")

    # ── 权限服务 (Cerbos) ──
    authz_base_url: str = os.getenv("AUTHZ_BASE_URL", "http://localhost:13592")
    authz_timeout_ms: int = int(os.getenv("AUTHZ_TIMEOUT_MS", "2000"))

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
    langfuse_public_url: str = os.getenv("LANGFUSE_PUBLIC_URL", os.getenv("LANGFUSE_HOST", "http://localhost:13000"))
    cerbos_public_url: str = os.getenv("CERBOS_PUBLIC_URL", os.getenv("AUTHZ_BASE_URL", "http://localhost:13592"))
    admin_console_url: str = os.getenv("ADMIN_CONSOLE_URL", "")

    # ── JWT JWKS（生产环境从 IdP 自动获取公钥，取代本地 PEM 文件） ──
    jwt_jwks_url: str = os.getenv("JWT_JWKS_URL", "")

    # ── 开发期本地文件目录（生产环境为空，跳过本地文件 fallback） ──
    dev_docs_dir: str = os.getenv("DEV_DOCS_DIR", "")

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

    # ── 模型提供商判定 ──
    @property
    def is_ollama(self) -> bool:
        """是否使用本地 Ollama（基于 URL 自动判定）。"""
        return "11434" in self.llm_base_url and "vllm" not in self.llm_base_url

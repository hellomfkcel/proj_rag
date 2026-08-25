#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════
# RAG 系统 — 生产部署脚本（把启动环境准备好）
#
# 四步配置模型：
#   1) 校验基础设施依赖正常运行（本机 infra + 外部权限系统/Keycloak/可观测）
#   2) 处理可自动生成的配置值（强随机口令/密钥、JWT 密钥对）
#   3) 处理需读取环境/基础设施的配置值（权限平台 service_api_key、共享 ctx_token_secret、
#      perm-redis 事件流、OTel/Langfuse/LLM 端点、PUBLIC_BASE_URL）
#   4) 处理需人工设置的值（缺失即 fail，并说明是什么值、从哪获取）
#
# 用法（仓库根目录执行）：
#   bash scripts/deploy.sh                   # 生产部署（默认 APP_ENV=production）
#   APP_ENV=development bash scripts/deploy.sh  # 联调（保留 dev 登录路径）
#   BUILD=1 bash scripts/deploy.sh           # 强制重建镜像（含 embedding-service）
#   ROTATE_KEYS=1 bash scripts/deploy.sh     # 强制轮换 JWT 密钥对（泄露后的补救）
#   NGINX_TLS=true bash scripts/deploy.sh    # nginx 443 TLS（自签或 TLS_CERT_FILE/TLS_KEY_FILE）
#   RESET=1 bash scripts/deploy.sh           # 全新部署：先清空全部数据卷（含向量库/模型缓存，数据不可恢复）
#
# 多主机部署（RAG 与权限系统/可观测在不同主机时，用 env 覆盖服务地址）：
#   PERMISSION_HOST=<权限主机IP>               # 权限服务/perm-redis（默认 host.docker.internal 单机）
#   KEYCLOAK_HOST=<Keycloak主机IP>             # Keycloak（默认 host.docker.internal）
#   OBSERVABILITY_HOST=<观测主机IP>            # OTel :4318 / Langfuse :13000（默认 host.docker.internal）
#   AUTHZ_CLIENT_CREDENTIAL / CTX_TOKEN_SECRET / PERM_REDIS_PASSWORD
#       跨系统共享值：多主机下无法读本地权限配置，必须显式 env 提供（单机自动读本地）
#
# 正式域名（#7）：
#   EXTERNAL_HOST=https 域名（如 rag.example.com） + NGINX_TLS=true + 正式证书（TLS_CERT_FILE/TLS_KEY_FILE）
#   Keycloak 侧同步 KC_HOSTNAME=域名 + https redirect URIs（keycloak_bootstrap 自动按 EXTERNAL_HOST 推导）
#
# 人工设置值（第 4 步，缺失会 fail）：
#   LLM_BASE_URL / LLM_MODEL / LLM_API_KEY  LLM 服务地址/模型名/密钥。
#                         来源：模型供应商控制台（如 https://api.deepseek.com/v1 + deepseek-chat + DeepSeek 平台 API key）。
#   EXTERNAL_HOST         浏览器访问本系统的地址（域名或公网 IP，用于 PUBLIC_BASE_URL 与前端外部链接）。
#                         来源：部署机对外地址 / 域名解析。
#   LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY  模型观测密钥（可选，缺失则 Langfuse 观测 fail-open 跳过）。
#                         来源：Langfuse 项目面板。
#
# 依赖：docker；同主机已部署 权限系统（默认 ../permission-system，可用 PERM_ROOT 覆盖）。
# ══════════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

PERM_ROOT="${PERM_ROOT:-$(cd "$REPO_ROOT/../permission-system" 2>/dev/null && pwd)}"
ENV_FILE="$REPO_ROOT/.env"
INFRA_TIMEOUT="${INFRA_TIMEOUT:-300}"
APP_TIMEOUT="${APP_TIMEOUT:-120}"
BUILD="${BUILD:-0}"
APP_ENV="${APP_ENV:-production}"
ROTATE_KEYS="${ROTATE_KEYS:-0}"
RESET="${RESET:-0}"                     # 1=全新部署：透传给 start.sh 清空全部数据卷（数据不可恢复）

info() { echo -e "\033[36m[i]\033[0m $*"; }
ok()   { echo -e "\033[32m[✓]\033[0m $*"; }
warn() { echo -e "\033[33m[!]\033[0m $*"; }
fail() { echo -e "\033[31m[x]\033[0m $*" >&2; exit 1; }

# ── 1. Pre-flight ─────────────────────────────────────────────────
command -v docker >/dev/null || fail "缺少 docker 命令"
docker info >/dev/null 2>&1 || fail "docker daemon 不可用"
command -v openssl >/dev/null || fail "缺少 openssl"
[ -f "$ENV_FILE" ] || fail "缺少 .env——请 cp .env.example .env 后运行"

# 预创建 build_cache 为当前用户：nginx 挂载 build_cache/tls 时 docker 会用 root 创建缺失目录，
# 导致 deploy 无法写入证书。任何 docker 操作前先确保该目录归当前用户。
mkdir -p "$REPO_ROOT/build_cache"
if [ ! -w "$REPO_ROOT/build_cache" ]; then
    if [ ! -d "$REPO_ROOT/build_cache/wheels" ]; then
        rm -rf "$REPO_ROOT/build_cache" && mkdir -p "$REPO_ROOT/build_cache"
        ok "build_cache 曾被 docker 以 root 创建，已重建为当前用户"
    else
        warn "build_cache 含 wheels 且不可写——请手动 chown -R $(id -u):$(id -g) $REPO_ROOT/build_cache"
    fi
fi

set -a; source "$ENV_FILE"; set +a

# ── 第 4 步：人工值（缺失即 fail，说明来源） ──
for v in LLM_BASE_URL LLM_MODEL LLM_API_KEY; do
    if [ -z "${!v:-}" ]; then
        fail "${v} 未设置。\n    值：$( [ "$v" = LLM_BASE_URL ] && echo 'LLM 服务地址（如 https://api.deepseek.com/v1）' || [ "$v" = LLM_MODEL ] && echo 'LLM 模型 id（如 deepseek-chat）' || echo '模型商 API key' )。\n    获取：模型供应商控制台（DeepSeek 开放平台 / OpenAI 等），填 .env 对应项。"
    fi
done
EXTERNAL_HOST="${EXTERNAL_HOST:-}"
if [ -z "$EXTERNAL_HOST" ]; then
    fail "EXTERNAL_HOST 未设置。\n    值：浏览器访问本系统的地址（域名或公网 IP，如 http://rag.example.com 或 http://203.0.113.5）。\n    获取：部署机对外地址 / 域名解析。写入 .env 的 EXTERNAL_HOST。"
fi
PUBLIC_BASE_URL="${PUBLIC_BASE_URL:-http://${EXTERNAL_HOST}}"

# ── 第 2 步：自动生成（仅缺失时） ──
gen_env_if_missing() { # key [hex_length]
    local key="$1" len="${2:-24}"
    if ! grep -q "^${key}=" "$ENV_FILE" 2>/dev/null; then
        echo "${key}=$(openssl rand -hex "$len")" >> "$ENV_FILE"
        ok "已生成 $key 并写入 .env"
    fi
}
update_env() { # key value（value 安全字符，避免 sed 特殊字符）
    local k="$1" v="$2"
    if grep -q "^${k}=" "$ENV_FILE"; then
        sed -i "s|^${k}=.*|${k}=${v}|" "$ENV_FILE"
    else
        echo "${k}=${v}" >> "$ENV_FILE"
    fi
}
gen_env_if_missing POSTGRES_PASSWORD 24
gen_env_if_missing REDIS_PASSWORD 24
gen_env_if_missing MINIO_ACCESS_KEY 16
gen_env_if_missing MINIO_SECRET_KEY 24

# 同步 DATABASE_URL / REDIS_URL（宿主 dev 进程连接串；容器由 compose 注入不受影响）
set -a; source "$ENV_FILE"; set +a
update_env DATABASE_URL "postgresql+asyncpg://rag:${POSTGRES_PASSWORD}@localhost:${POSTGRES_HOST_PORT:-25432}/rag"
update_env REDIS_URL "redis://:${REDIS_PASSWORD}@localhost:${REDIS_HOST_PORT:-16379}/0"

# 生产强制：开发弱口令不得进入
if [ "$APP_ENV" = "production" ]; then
    if grep -qE '^(POSTGRES_PASSWORD|REDIS_PASSWORD)=.*(rag_dev_pwd_2026|minioadmin).*$' "$ENV_FILE"; then
        fail "APP_ENV=production 但 .env 仍为开发弱口令（rag_dev_pwd_2026/minioadmin）。\n    值：强随机口令。\n    获取：删除 .env 里这两行后重跑（脚本自动生成）；若数据卷已用旧口令初始化需删除重建。"
    fi
fi

# JWT 密钥对（RAG 为源）：轮换或缺失才生成
mkdir -p config
if [ "$ROTATE_KEYS" = "1" ] || [ ! -f config/jwt_private.pem ] || [ ! -f config/jwt_public.pem ]; then
    if [ -f config/jwt_private.pem ]; then
        cp config/jwt_private.pem "config/jwt_private.pem.bak.$(date +%s)" 2>/dev/null || true
    fi
    openssl genrsa -out config/jwt_private.pem 2048 2>/dev/null
    openssl rsa -in config/jwt_private.pem -pubout -out config/jwt_public.pem 2>/dev/null
    ok "已生成/轮换 JWT 密钥对（旧私钥备份至 config/jwt_private.pem.bak.*）"
fi

# ── 第 3 步：读取环境/基础设施（跨系统同步） ──

# AUTHZ_CLIENT_CREDENTIAL：与权限平台 service_api_key 一致。
# 同主机下以权限侧 config 文件为权威（避免 .env 里的旧值/失配导致权限调用 401）
AUTHZ_CLIENT_CREDENTIAL="${AUTHZ_CLIENT_CREDENTIAL:-}"
if [ -f "$PERM_ROOT/permission-service/config/service_api_key" ]; then
    AUTHZ_CLIENT_CREDENTIAL="$(cat "$PERM_ROOT/permission-service/config/service_api_key")"
    ok "从权限系统读取 service_api_key 作为 AUTHZ_CLIENT_CREDENTIAL（权威）"
fi
[ -n "$AUTHZ_CLIENT_CREDENTIAL" ] || fail "AUTHZ_CLIENT_CREDENTIAL 未设置。\n    值：权限平台为 RAG 签发的 service API key（X-Api-Key）。\n    获取：与权限系统 deploy 输出的 service_api_key 一致（同主机自动读取；跨主机需手动填 .env）。"

# CTX_TOKEN_SECRET：必须与权限侧共享（resolve_ctx_token 生产验 HMAC 用），同样以权限侧为权威
CTX_TOKEN_SECRET="${CTX_TOKEN_SECRET:-}"
if [ -f "$PERM_ROOT/permission-service/config/ctx_token_secret" ]; then
    CTX_TOKEN_SECRET="$(cat "$PERM_ROOT/permission-service/config/ctx_token_secret")"
    ok "从权限系统读取共享 ctx_token_secret（权威）"
fi
[ -n "$CTX_TOKEN_SECRET" ] || fail "CTX_TOKEN_SECRET 未设置。\n    值：与权限平台共享的 ctx_token 签名密钥（≥32B）。\n    获取：与权限系统 permission-service/config/ctx_token_secret 一致（同主机自动读取）。"

# AUTHZ_EVENT_STREAM_REDIS_URL：perm-redis（密码来自权限系统 .env）
PERM_REDIS_PASSWORD="${PERM_REDIS_PASSWORD:-}"
if [ -z "$PERM_REDIS_PASSWORD" ] && [ -f "$PERM_ROOT/.env" ]; then
    PERM_REDIS_PASSWORD="$(grep -E '^PERM_REDIS_PASSWORD=' "$PERM_ROOT/.env" | head -1 | cut -d= -f2- || true)"
fi
[ -n "$PERM_REDIS_PASSWORD" ] || fail "PERM_REDIS_PASSWORD 未设置。\n    值：权限系统 perm-redis 的密码（用于构造 AUTHZ_EVENT_STREAM_REDIS_URL）。\n    获取：权限系统 .env 的 PERM_REDIS_PASSWORD（同主机自动读取）。"
# 容器内 host.docker.internal 才能访问宿主机 perm-redis；localhost 指向容器自身
AUTHZ_EVENT_STREAM_REDIS_URL="redis://:${PERM_REDIS_PASSWORD}@${PERMISSION_HOST:-host.docker.internal}:${PERM_REDIS_HOST_PORT:-16380}/0"

# 写回 .env（生产所需键）
update_env AUTHZ_CLIENT_CREDENTIAL "$AUTHZ_CLIENT_CREDENTIAL"
update_env CTX_TOKEN_SECRET "$CTX_TOKEN_SECRET"
update_env PERMISSION_REDIS_PASSWORD "$PERM_REDIS_PASSWORD"
update_env AUTHZ_EVENT_STREAM_REDIS_URL "$AUTHZ_EVENT_STREAM_REDIS_URL"
update_env PUBLIC_BASE_URL "$PUBLIC_BASE_URL"
update_env OIDC_CLIENT_ID "${OIDC_CLIENT_ID:-${KEYCLOAK_CLIENT_ID:-rag-frontend}}"
# CORS 收敛：生产浏览器来源 = EXTERNAL_HOST（前端经 nginx 同源，无需额外端口）
update_env CORS_ALLOWED_ORIGINS "http://${EXTERNAL_HOST},http://${EXTERNAL_HOST}:${FRONTEND_HOST_PORT:-3001}"

# ── TLS：nginx 443 终止（NGINX_TLS=true 时启用） ────────────────
NGINX_TLS="${NGINX_TLS:-false}"
if [ "$NGINX_TLS" = "true" ]; then
    CERT_DIR="$REPO_ROOT/build_cache/tls"
    mkdir -p "$CERT_DIR"
    # nginx compose 挂载该目录时 docker 可能以 root 创建 → 需 chown 才能写入证书
    chown -R "$(id -u):$(id -g)" "$CERT_DIR" 2>/dev/null || true
    TLS_CERT_FILE="${TLS_CERT_FILE:-$CERT_DIR/tls.crt}"
    TLS_KEY_FILE="${TLS_KEY_FILE:-$CERT_DIR/tls.key}"
    if [ ! -s "$TLS_CERT_FILE" ] || [ ! -s "$TLS_KEY_FILE" ]; then
        openssl req -x509 -nodes -newkey rsa:2048 -days 825 \
            -keyout "$TLS_KEY_FILE" -out "$TLS_CERT_FILE" \
            -subj "/CN=${EXTERNAL_HOST}" 2>/dev/null
        ok "已生成自签证书（$TLS_CERT_FILE）——生产请替换为正式证书（TLS_CERT_FILE/TLS_KEY_FILE）"
        # 自签证书：api 调用 IdP/nginx 需跳过 TLS 校验（正式证书则保持 OIDC_SSL_VERIFY=true）
        update_env OIDC_SSL_VERIFY false
        warn "自签证书 → OIDC_SSL_VERIFY=false（api 跳过 IdP TLS 校验）；正式证书部署请删除该行以恢复校验"
    fi
    # https 化浏览器侧地址（覆盖 .env 里可能的旧 http 值）
    update_env PUBLIC_BASE_URL "https://${EXTERNAL_HOST}"
    update_env CORS_ALLOWED_ORIGINS "https://${EXTERNAL_HOST}"
    # IdP 走权限平台独立入口（permission-nginx :18081）：Keycloak issuer 带 :18081 端口，
    # RAG 侧 discovery/JWKS 必须与之同源，否则 id_token 验签失败。
    update_env OIDC_DISCOVERY_URL "http://${EXTERNAL_HOST}:${PERMISSION_NGINX_PORT:-18081}/realms/${KEYCLOAK_REALM:-rag-v14}/.well-known/openid-configuration"
    update_env JWT_JWKS_URL "http://${EXTERNAL_HOST}:${PERMISSION_NGINX_PORT:-18081}/realms/${KEYCLOAK_REALM:-rag-v14}/protocol/openid-connect/certs"
    update_env NEXT_PUBLIC_KEYCLOAK_URL "http://${EXTERNAL_HOST}:${PERMISSION_NGINX_PORT:-18081}"
    ok "NGINX_TLS=true：启用 nginx 443；IdP 走权限平台入口 :${PERMISSION_NGINX_PORT:-18081}"
else
    # NGINX_TLS=false：重置浏览器侧 URL 为 http（避免复制/历史 .env 残留 https 导致 SSO 指向无 443 的 nginx）
    update_env PUBLIC_BASE_URL "http://${EXTERNAL_HOST}"
    update_env CORS_ALLOWED_ORIGINS "http://${EXTERNAL_HOST},http://${EXTERNAL_HOST}:${FRONTEND_HOST_PORT:-3001}"
    update_env OIDC_DISCOVERY_URL "http://${EXTERNAL_HOST}:${PERMISSION_NGINX_PORT:-18081}/realms/${KEYCLOAK_REALM:-rag-v14}/.well-known/openid-configuration"
    update_env JWT_JWKS_URL "http://${EXTERNAL_HOST}:${PERMISSION_NGINX_PORT:-18081}/realms/${KEYCLOAK_REALM:-rag-v14}/protocol/openid-connect/certs"
    update_env NEXT_PUBLIC_KEYCLOAK_URL "http://${EXTERNAL_HOST}:${PERMISSION_NGINX_PORT:-18081}"
    ok "NGINX_TLS=false：nginx 保持 HTTP；IdP 走权限平台入口 :${PERMISSION_NGINX_PORT:-18081}"
fi

# 重载 .env：上面的 update_env 把 OIDC/JWKS/URL 写入了 .env，但 shell 变量还是旧值。
# 全新 .env（首次部署）时 shell 无这些值 → 下方生产 SSO 校验会误判失败。
set -a; source "$ENV_FILE"; set +a

# 容器内 127.0.0.1/localhost 指向容器自身，非宿主机——对这些宿主 URL 值告警
# （LLM/Otel/Langfuse 等；AUTHZ_SERVICE_URL 已由 compose 从 PERMISSION_HOST 计算）。
for _u in LLM_BASE_URL OTEL_EXPORTER_OTLP_ENDPOINT LANGFUSE_HOST; do
    _v="${!_u:-}"
    if [[ -n "$_v" && "$_v" =~ ^https?://(127\.0\.0\.1|localhost)(:|/) ]]; then
        warn "$_u=$_v 指向容器自身——容器内不可达。宿主服务请用 host.docker.internal（如 http://host.docker.internal:11434/v1）；外部 API（如 api.deepseek.com）不受影响。"
    fi
done

# 渲染 nginx.conf（TLS 开关决定是否注入 443 server）
python3 - "$NGINX_TLS" <<'PYEOF'
import os, sys
tls_enabled = sys.argv[1] == "true"
kc_port = os.environ.get("KEYCLOAK_HOST_PORT", "8080")
ssl_server = '''    server {
        listen 443 ssl;
        server_name _;
        ssl_certificate /etc/nginx/certs/tls.crt;
        ssl_certificate_key /etc/nginx/certs/tls.key;
        ssl_protocols TLSv1.2 TLSv1.3;
        ssl_ciphers HIGH:!aNULL:!MD5;

        location /healthz {
            proxy_pass http://backend;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto https;
        }

        location /realms/ {
            proxy_pass http://host.docker.internal:__KEYCLOAK_PORT__;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto https;
            proxy_set_header X-Forwarded-Host $host;
            proxy_http_version 1.1;
            proxy_read_timeout 60s;
        }

        location /api/ {
            proxy_pass http://backend;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto https;
            proxy_buffering off;
            proxy_cache off;
            proxy_read_timeout 300s;
            proxy_http_version 1.1;
            proxy_set_header Connection '';
        }

        location / {
            proxy_pass http://frontend;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto https;
            proxy_http_version 1.1;
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection "upgrade";
        }
    }
'''
with open("frontend/nginx.conf.tmpl") as f:
    conf = f.read()
conf = conf.replace("__TLS_SERVER__", ssl_server if tls_enabled else "")
# 80 端口 location /：TLS 模式页面流量 301 → https（healthz 在独立 location 保留直通）；非 TLS 空
conf = conf.replace("__HTTP_REDIRECT_HTTPS__", "return 301 https://$host$request_uri;\n" if tls_enabled else "")
conf = conf.replace("__KEYCLOAK_PORT__", kc_port)
with open("frontend/nginx.conf", "w") as f:
    f.write(conf)
PYEOF
ok "nginx.conf 已渲染（NGINX_TLS=$NGINX_TLS）"
if [ "$APP_ENV" = "production" ] && ! grep -q '^APP_ENV=production$' "$ENV_FILE"; then
    sed -i 's/^APP_ENV=.*/APP_ENV=production/' "$ENV_FILE" 2>/dev/null || true
    grep -q '^APP_ENV=production$' "$ENV_FILE" || echo "APP_ENV=production" >> "$ENV_FILE"
    ok "已置 .env APP_ENV=production"
fi

# 生产前置校验（与容器内 validate_production_config 语义一致）
if [ "$APP_ENV" = "production" ]; then
    for v in CTX_TOKEN_SECRET AUTHZ_CLIENT_CREDENTIAL; do
        [ -n "${!v:-}" ] || fail "$v 缺失（生产必需）"
    done
    if [ -z "${OIDC_DISCOVERY_URL:-}" ] || [ -z "${JWT_JWKS_URL:-}" ]; then
        fail "APP_ENV=production 需要 SSO。\n    值：OIDC_DISCOVERY_URL（Keycloak/IdP 的 .well-known/openid-configuration）与 JWT_JWKS_URL（对应 /certs）。\n    获取：Keycloak realm 配置页（如 http://${EXTERNAL_HOST}:${KEYCLOAK_HOST_PORT:-8080}/realms/${KEYCLOAK_REALM:-rag-v14}/.well-known/openid-configuration）。填 .env。"
    fi
fi

# ── 校验外部依赖（第 1 步） ──
verify_url() { # name url [optional]
    if curl -fsS -m 5 "$2" >/dev/null 2>&1; then
        ok "$1 可达"
    elif [ "${3:-}" = "optional" ]; then
        warn "$1 不可达（${2}）—— fail-open，可观测缺失不阻断"
    else
        fail "$1 不可达（${2}）—— 请先部署并启动对应服务"
    fi
}
info "校验外部依赖..."
verify_url "权限系统 permission-service" "http://127.0.0.1:${PERMISSION_SERVICE_HOST_PORT:-18080}/healthz"
verify_url "Keycloak realm" "http://127.0.0.1:${KEYCLOAK_HOST_PORT:-8080}/realms/${KEYCLOAK_REALM:-rag-v14}/.well-known/openid-configuration"
verify_url "OTel Collector" "http://127.0.0.1:4318/" optional
verify_url "Langfuse" "http://127.0.0.1:13000/" optional

# ── 委托 start.sh 完成编排（infra → init-db → app） ──
info "启动编排（复用 start.sh start，BUILD=$BUILD，RESET=$RESET，APP_ENV=$APP_ENV）..."
export BUILD
export RESET
bash scripts/start.sh start

# nginx.conf 由 deploy 渲染（bind mount），compose up 不会因文件内容变化重建容器 → 强制重建让 443/TLS 生效
info "强制重建 nginx（加载 deploy 渲染的 nginx.conf）..."
docker compose -f docker-compose.infra.yml -f docker-compose.app.yml up -d --force-recreate nginx 2>&1 | tail -1
ok "nginx 已加载最新配置（NGINX_TLS=$NGINX_TLS）"

# ── 最终验证 ──
local_waited=0
while (( local_waited < APP_TIMEOUT )); do
    if curl -fsS -m 3 "http://127.0.0.1:${API_HOST_PORT:-8000}/healthz" >/dev/null 2>&1; then
        ok "API :${API_HOST_PORT:-8000}/healthz 就绪"; break
    fi
    sleep 3; local_waited=$((local_waited+3))
done
if (( local_waited >= APP_TIMEOUT )); then
    warn "API 未就绪，请查日志: scripts/start.sh logs api"
fi

# ── smoke 检查：系统性拦截"本地正常、部署失效"类问题（schema/连接/认证/超管初始化） ──
info "运行部署后 smoke 检查（scripts/smoke_check.sh）..."
if bash scripts/smoke_check.sh; then
    ok "smoke 检查全部通过"
else
    warn "smoke 检查存在失败项——请按上方 [x] 提示修复后重跑：bash scripts/smoke_check.sh"
fi

echo ""
echo "══════ RAG 系统已部署（APP_ENV=$APP_ENV）══════"
echo "  统一入口   http://${EXTERNAL_HOST}          (nginx :${NGINX_HTTP_PORT:-80})"
echo "  后端 API   http://${EXTERNAL_HOST}:${API_HOST_PORT:-8000}  (/healthz)"
echo "  权限判定   外部权限平台 remote（X-Api-Key 已同步）"
echo "  SSO        Keycloak realm=${KEYCLOAK_REALM:-rag-v14}"
echo "  ⚠ TLS 由外部 LB/Ingress 终结；本机直接部署请确保网络安全边界"
echo "  （APP_ENV=development 联调时保留 /dev-login；production 已禁用）"
echo "═══════════════════════════"

#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════
# RAG 系统 — 启动/运维脚本（Docker 部署形态）
#
# 用法（在仓库根目录执行，或任意目录执行本脚本的绝对路径）：
#   scripts/start.sh start           # 按序启动：infra → init-db → app
#   scripts/start.sh stop            # 停止全部（保留数据卷）
#   scripts/start.sh restart         # 重启全部
#   scripts/start.sh status          # 查看各服务健康状态
#   scripts/start.sh logs [服务名]    # 查看日志（-f 跟随）
#   scripts/start.sh init-db         # 手动执行建表（幂等）
#   scripts/start.sh db-seed         # 写入开发期测试数据（仅开发）
#
# 依赖：
#   - .env 已配置（cp .env.example .env 后填写必需变量）
#   - 宿主机已部署 独立权限系统（permission-service :18080、perm-redis :16380、
#     统一观测平台 OTel :4318、Langfuse :13000），见 docs/ops/RAG上线运维手册.md
# ══════════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

INFRA="docker compose -f docker-compose.infra.yml"
ALL="docker compose -f docker-compose.infra.yml -f docker-compose.app.yml"

INFRA_TIMEOUT="${INFRA_TIMEOUT:-300}"   # 基础设施健康等待上限（秒）
APP_TIMEOUT="${APP_TIMEOUT:-120}"       # app 就绪等待上限（秒）

info() { echo -e "\033[36m[i]\033[0m $*"; }
ok()   { echo -e "\033[32m[✓]\033[0m $*"; }
warn() { echo -e "\033[33m[!]\033[0m $*"; }
fail() { echo -e "\033[31m[x]\033[0m $*" >&2; exit 1; }

# ── 加载 .env 并校验必需变量 ─────────────────────────────────────
load_env() {
    if [[ ! -f .env ]]; then
        fail "缺少 .env 文件。请先: cp .env.example .env 并填写 POSTGRES_PASSWORD / REDIS_PASSWORD / LLM_* 等。"
    fi
    set -a; source .env; set +a
    for v in POSTGRES_PASSWORD REDIS_PASSWORD LLM_BASE_URL LLM_MODEL; do
        if [[ -z "${!v:-}" ]]; then
            fail ".env 缺少必需变量: $v"
        fi
    done
}

# ── Docker 部署模式连接地址覆盖 ───────────────────────────────────
# 本地 .env 的 localhost 地址是给宿主机进程用的；容器内必须用网络内服务名
# 与 host-gateway 宿主地址。shell 环境变量优先级高于 .env，这里显式导出。
apply_docker_overrides() {
    export EMBEDDING_SERVICE_URL="http://embedding-service:19500"
    export INFINITY_URL="http://infinity:7997"
    # 权限判定统一走外部权限平台（remote）；无内部 Cerbos
    export AUTHZ_SERVICE_URL="http://host.docker.internal:18080"
    export AUTHZ_SERVICE_MODE="${AUTHZ_SERVICE_MODE:-remote}"
    export OTEL_EXPORTER_OTLP_ENDPOINT="http://host.docker.internal:4318"
    export LANGFUSE_HOST="http://host.docker.internal:13000"
    # Keycloak 是服务端调用（api 容器→宿主机 IdP），容器内 localhost 指向自身，
    # 必须无条件覆盖（.env 的 localhost 值仅供宿主机 dev 进程用）
    export KEYCLOAK_SERVER_URL="http://host.docker.internal:8080"

    # 事件流 Redis 是权限系统的 perm-redis；从 .env 提取密码并换 host-gateway 宿主地址
    local perm_pwd
    perm_pwd="$(sed -nE 's#^AUTHZ_EVENT_STREAM_REDIS_URL=redis://:([^@]+)@.*#\1#p' .env)"
    if [[ -n "$perm_pwd" ]]; then
        export AUTHZ_EVENT_STREAM_REDIS_URL="redis://:${perm_pwd}@host.docker.internal:16380/0"
    else
        warn ".env 未配置 AUTHZ_EVENT_STREAM_REDIS_URL，visibility-events 将无法订阅权限事件"
    fi
}

# ── 等待全部服务 healthy ─────────────────────────────────────────
wait_healthy() {
    local compose_cmd="$1" timeout="$2" label="$3"
    local waited=0
    info "等待 $label 全部就绪（上限 ${timeout}s）..."
    while (( waited < timeout )); do
        # 输出示例行: {"Name":"x","Health":"healthy","State":"running"}
        local bad
        bad="$(eval "$compose_cmd ps --format json 2>/dev/null" | python3 -c "
import json,sys
bad=[]
for line in sys.stdin:
    line=line.strip()
    if not line: continue
    r=json.loads(line)
    h=(r.get('Health') or '').lower()
    s=(r.get('State') or '').lower()
    # 无 healthcheck 的服务：running 即视为就绪；有 healthcheck：必须 healthy
    if s=='running' and (h=='healthy' or h==''):
        continue
    bad.append(r.get('Service') or r.get('Name') or '?')
print(' '.join(bad))
" || echo "UNPARSE")"
        if [[ -z "$bad" ]]; then
            ok "$label 全部就绪"
            return 0
        fi
        if [[ "$bad" == "UNPARSE" ]]; then
            # 可能尚无容器或版本差异，重试
            sleep 3
            waited=$((waited+3))
            continue
        fi
        sleep 3
        waited=$((waited+3))
    done
    echo ""
    warn "超时。尚未就绪的服务: $bad"
    eval "$compose_cmd ps"
    fail "$label 启动超时（${timeout}s），请查看上方状态与日志。"
}

# ── 初始化数据库（幂等建表） ─────────────────────────────────────
init_db() {
    info "执行数据库建表（幂等）..."
    # 在 api 镜像内执行，避免依赖宿主机 conda 环境
    $ALL run --rm --no-deps api python -m src.scripts.init_db || fail "init_db 失败"
    ok "数据库表就绪"
}

# ══════════════════════════════════════════════════════════════════
# 子命令
# ══════════════════════════════════════════════════════════════════

cmd_start() {
    load_env
    apply_docker_overrides

    # 1. 基础设施（内部按 depends_on healthy 排序）
    info "启动基础设施..."
    $INFRA up -d

    # 2. 等待 infra 全部 healthy（Milvus 通常最慢）
    wait_healthy "$INFRA" "$INFRA_TIMEOUT" "基础设施"

    # 3. 建表（幂等，重复启动安全）
    init_db

    # 4. 计算层（api / workers / embedding-service / relay / visibility / frontend / nginx）
    info "启动计算层（app）..."
    $ALL up -d --build

    # 5. 等待 API 就绪
    local waited=0
    info "等待 API http://localhost:8000/healthz ..."
    while (( waited < APP_TIMEOUT )); do
        if curl -fsS -m 3 http://localhost:8000/healthz >/dev/null 2>&1; then
            ok "API 就绪"
            break
        fi
        sleep 3; waited=$((waited+3))
    done
    if (( waited >= APP_TIMEOUT )); then
        warn "API 未在 ${APP_TIMEOUT}s 内就绪，请检查日志: $0 logs api"
    fi

    echo ""
    echo "══════ RAG 系统已启动 ══════"
    echo "  统一入口   http://localhost          (nginx :80)"
    echo "  后端 API   http://localhost:8000     (/healthz)"
    echo "  前端       http://localhost:3001"
    echo "  日志       scripts/start.sh logs [-f] [服务名]"
    echo "  状态       scripts/start.sh status"
    echo "  观测       Grafana http://localhost:3000 · Langfuse http://localhost:13000"
    echo "═══════════════════════════════"
}

cmd_stop() {
    $ALL down
    ok "已停止（数据卷保留）。"
}

cmd_restart() {
    cmd_stop
    cmd_start
}

cmd_status() {
    echo "── 基础设施 ──"
    $INFRA ps
    echo ""
    echo "── 计算层 ──"
    $ALL ps
}

cmd_logs() {
    local svc="${1:-}"
    if [[ "$svc" == "-f" || "$svc" == "--follow" ]]; then
        $ALL logs -f --tail=200
    elif [[ -n "$svc" ]]; then
        $ALL logs -f --tail=300 "$svc"
    else
        $ALL logs --tail=200
    fi
}

cmd_help() {
    sed -n '1,20p' "$0" | sed 's/^# \{0,1\}//'
}

case "${1:-help}" in
    start)       shift; cmd_start "$@" ;;
    stop)        shift; cmd_stop "$@" ;;
    restart)     shift; cmd_restart "$@" ;;
    status)      shift; cmd_status "$@" ;;
    logs)        shift; cmd_logs "$@" ;;
    init-db)     load_env; apply_docker_overrides; init_db ;;
    db-seed)     load_env; apply_docker_overrides
                 info "写入开发期测试数据（仅开发）..."
                 $ALL run --rm --no-deps api python -m src.scripts.seed_dev ;;
    help|--help|-h) cmd_help ;;
    *)           fail "未知命令: $1（可用: start|stop|restart|status|logs|init-db|db-seed|help）" ;;
esac

#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════
# 部署后 smoke 检查 — 系统性拦截"本地正常、部署失效"这一类问题
#
# 覆盖（都曾在生产部署中真实发生过）：
#   [schema]  conversation_turns.answer 列 / document_directory_entry 表
#            （模型与 init.sql 漂移 → 全新部署 500）
#   [连接]    api 容器内 AUTHZ_SERVICE_URL 非 127.0.0.1/localhost 且可达
#            （.env 开发值泄漏 → 权限服务不可达 fail-closed）
#   [认证]    RAG 的 X-Api-Key 能被权限服务接受（非 401）
#            （service_api_key 末尾换行 → sha256 失配 401）
#   [超管]    role_bindings 有 user:admin→platform_admin_role；project_api_keys.key_hash 正确
#            （seed 只授平台角色 / principal 用 UUID → RAG 权限全 deny）
#
# 用法：bash scripts/smoke_check.sh [--strict]
#   --strict  任一检查失败即 exit 1（供 CI / 严格部署门禁）
#   默认：打印结果并 exit 1（有失败时），deploy.sh 调用后展示摘要。
# ══════════════════════════════════════════════════════════════════
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

# 任一检查失败即 exit 1（deploy.sh 据此提示）；无失败 exit 0。
STRICT=1

COMPOSE_F="docker-compose.infra.yml -f docker-compose.app.yml"
COMPOSE="docker compose -f $COMPOSE_F"

ok()   { echo -e "\033[32m[✓]\033[0m $*"; }
fail() { echo -e "\033[31m[x]\033[0m $*"; FAILED=1; }
info() { echo -e "\033[36m[i]\033[0m $*"; }
warn() { echo -e "\033[33m[!]\033[0m $*"; }

FAILED=0

# 取 RAG 容器（compose 项目前缀会变，用 ps -q 定位）
RAG_PG="$($COMPOSE ps -q postgres 2>/dev/null | head -1)"
RAG_API="$($COMPOSE ps -q api 2>/dev/null | head -1)"
# 权限系统固定容器名（compose 里 container_name 指定）
PERM_PG="$(docker ps --format '{{.Names}}' | grep -E '^perm-postgres$' | head -1)"
PERM_SVC="$(docker ps --format '{{.Names}}' | grep -E '^permission-service$' | head -1)"

[ -n "$RAG_PG" ]   || { fail "找不到 RAG postgres 容器（$COMPOSE ps -q postgres）"; }
[ -n "$RAG_API" ]  || { fail "找不到 RAG api 容器"; }
[ -n "$PERM_PG" ]  || warn "找不到权限系统 postgres（perm-postgres），跳过超管初始化检查"

echo "══════ smoke 检查开始 ══════"

# ── [1] schema ────────────────────────────────────────────────────
if [ -n "$RAG_PG" ]; then
    if docker exec "$RAG_PG" psql -U rag -d rag -tAc \
        "SELECT 1 FROM information_schema.columns WHERE table_name='conversation_turns' AND column_name='answer'" 2>/dev/null | grep -q 1; then
        ok "schema: conversation_turns.answer 列存在"
    else
        fail "schema: conversation_turns 缺 answer 列（chat 保存对话 500）。修复：scripts/init.sql 已补，需对运行中 DB 执行 ALTER TABLE conversation_turns ADD COLUMN answer TEXT NOT NULL DEFAULT ''"
    fi
    if docker exec "$RAG_PG" psql -U rag -d rag -tAc \
        "SELECT 1 FROM pg_tables WHERE schemaname='public' AND tablename='document_directory_entry'" 2>/dev/null | grep -q 1; then
        ok "schema: document_directory_entry 表存在"
    else
        fail "schema: 缺 document_directory_entry 表（目录接口 500）。修复：scripts/init.sql 已补，需执行 CREATE TABLE（见 init.sql）"
    fi
fi

# ── [2] api → 权限服务连接 ────────────────────────────────────────
if [ -n "$RAG_API" ]; then
    AUTHZ_URL="$(docker exec "$RAG_API" sh -c 'printf "%s" "$AUTHZ_SERVICE_URL"' 2>/dev/null)"
    if [ -z "$AUTHZ_URL" ]; then
        fail "连接: api 容器无 AUTHZ_SERVICE_URL"
    elif echo "$AUTHZ_URL" | grep -qE '127\.0\.0\.1|localhost'; then
        fail "连接: api 的 AUTHZ_SERVICE_URL=$AUTHZ_URL 指向容器自身（应 host.docker.internal）。修复：compose 已改为由 PERMISSION_HOST 计算，重建 api 容器即可"
    else
        code="$(docker exec "$RAG_API" sh -c "curl -s -o /dev/null -w '%{http_code}' -m 3 '$AUTHZ_URL/healthz' 2>/dev/null")"
        if [ "$code" = "200" ]; then
            ok "连接: api→权限服务 $AUTHZ_URL/healthz → 200"
        else
            fail "连接: api→权限服务 $AUTHZ_URL/healthz → ${code:-不可达}"
        fi
    fi

    # ── [3] 权限 key 认证 ──
    key_code="$(docker exec "$RAG_API" sh -c "
        KEY=\$(printf '%s' \"\$AUTHZ_CLIENT_CREDENTIAL\")
        curl -s -o /dev/null -w '%{http_code}' -m 3 -H \"X-Api-Key: \$KEY\" \"\$AUTHZ_SERVICE_URL/v1/health\" 2>/dev/null
    ")"
    case "$key_code" in
        200|403) ok "认证: X-Api-Key 被权限服务接受（/v1/health → $key_code）" ;;
        401)     fail "认证: X-Api-Key 被权限服务拒绝（401）。修复：project_api_keys.key_hash 必须 = sha256(service_api_key 不含换行)；见权限 deploy.sh 的同步步骤" ;;
        *)       fail "认证: /v1/health 返回 ${key_code:-不可达}" ;;
    esac
fi

# ── [4] 超管初始化（权限系统 DB） ─────────────────────────────────
if [ -n "$PERM_PG" ]; then
    if docker exec "$PERM_PG" psql -U perm_user -d permission_db -tAc \
        "SELECT 1 FROM role_bindings WHERE principal='user:admin' AND role='platform_admin_role' AND NOT revoked" 2>/dev/null | grep -q 1; then
        ok "超管: role_bindings 有 user:admin→platform_admin_role（RAG 超管可用）"
    else
        fail "超管: role_bindings 缺 user:admin→platform_admin_role（RAG 权限全 deny）。修复：跑权限系统迁移 d5e6f7a8b9c0（alembic upgrade head）"
    fi

    # key_hash = sha256(service_api_key 不含换行)
    KEY_SRC="$(docker exec "$RAG_API" sh -c 'printf "%s" "$AUTHZ_CLIENT_CREDENTIAL"' 2>/dev/null)"
    if [ -n "$KEY_SRC" ]; then
        EXPECT_HASH="$(printf '%s' "$KEY_SRC" | sha256sum | awk '{print $1}')"
        DB_HASH="$(docker exec "$PERM_PG" psql -U perm_user -d permission_db -tAc \
            "SELECT key_hash FROM project_api_keys WHERE project_id='rag-v14' LIMIT 1" 2>/dev/null | tr -d ' \n')"
        if [ "$DB_HASH" = "$EXPECT_HASH" ]; then
            ok "超管: project_api_keys.key_hash 与当前 service_api_key 一致"
        else
            fail "超管: project_api_keys.key_hash 失配（DB=${DB_HASH:0:12}... 期望=${EXPECT_HASH:0:12}...）。修复：权限 deploy.sh 的 key 同步步骤（tr -d 换行后 sha256sum）"
        fi
    fi
fi

echo ""
if [ "$FAILED" = "1" ]; then
    echo -e "\033[31m══════ smoke 检查：存在失败项（见上方 [x]，按提示修复后重跑 bash scripts/smoke_check.sh）══════\033[0m"
    exit 1
else
    echo -e "\033[32m══════ smoke 检查：全部通过 ══════\033[0m"
    exit 0
fi

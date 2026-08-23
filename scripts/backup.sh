#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════
# RAG 系统 — 数据备份脚本
#
# 产物：backups/YYYYMMDD-HHMMSS/ 下
#   - rag-pg.sql                 PostgreSQL 逻辑备份（一致性）
#   - redis-volume.tar.gz        Redis 卷快照（appendonly.aof，含 Celery/流缓存）
#   - seaweedfs-volume.tar.gz    SeaweedFS 卷快照（文档原文件）
#   - milvus-volume.tar.gz       Milvus 卷快照（向量 + 权限戳记；best-effort，
#                                大库建议用 milvus_backup 工具 + 停写）
#
# 用法（仓库根目录执行）：
#   bash scripts/backup.sh [保留份数=7]
#   scripts/start.sh backup        # start.sh 子命令
#
# 调度（生产建议，宿主 crontab）：
#   30 2 * * * cd /home/mfkcel/proj_rag_dev && bash scripts/backup.sh 7 >> /var/log/rag-backup.log 2>&1
# ══════════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

KEEP="${1:-7}"
BACKUP_ROOT="$REPO_ROOT/backups"
STAMP="$(date +%Y%m%d-%H%M%S)"
DEST="$BACKUP_ROOT/$STAMP"

info() { echo -e "\033[36m[i]\033[0m $*"; }
ok()   { echo -e "\033[32m[✓]\033[0m $*"; }
fail() { echo -e "\033[31m[x]\033[0m $*" >&2; exit 1; }

command -v docker >/dev/null || fail "缺少 docker"
[ -f .env ] || fail "缺少 .env"
set -a; source .env; set +a
mkdir -p "$DEST"
info "备份到 $DEST ..."

# 1. PostgreSQL 逻辑备份（一致性；容器内本地 trust 免密）
info "PostgreSQL 逻辑备份..."
if docker compose -f docker-compose.infra.yml exec -T postgres pg_dump -U rag rag > "$DEST/rag-pg.sql" 2>/dev/null; then
    ok "rag-pg.sql ($(du -h "$DEST/rag-pg.sql" | cut -f1))"
else
    fail "pg_dump 失败"
fi

# 2. Redis 卷快照（appendonly.aof）
info "Redis 卷快照..."
if docker run --rm -v proj_rag_dev_redis_data:/data -v "$DEST":/backup alpine \
    sh -c 'tar czf /backup/redis-volume.tar.gz -C /data .' 2>/dev/null; then
    ok "redis-volume.tar.gz"
else
    warn "Redis 卷快照失败（卷可能不存在？）"
fi

# 3. SeaweedFS 卷快照（文档原文件）
info "SeaweedFS 卷快照..."
if docker run --rm -v proj_rag_dev_seaweedfs_data:/data -v "$DEST":/backup alpine \
    sh -c 'tar czf /backup/seaweedfs-volume.tar.gz -C /data .' 2>/dev/null; then
    ok "seaweedfs-volume.tar.gz"
else
    warn "SeaweedFS 卷快照失败"
fi

# 4. Milvus 卷快照（向量 + 权限戳记；best-effort）
info "Milvus 卷快照（best-effort，大库建议 milvus_backup 工具 + 停写）..."
if docker run --rm -v proj_rag_dev_milvus_data:/data -v "$DEST":/backup alpine \
    sh -c 'tar czf /backup/milvus-volume.tar.gz -C /data .' 2>/dev/null; then
    ok "milvus-volume.tar.gz"
else
    warn "Milvus 卷快照失败"
fi

# 5. 保留轮转：只留最近 KEEP 份
old=$(ls -1d "$BACKUP_ROOT"/*/ 2>/dev/null | sort | head -n -"$KEEP" || true)
if [ -n "$old" ]; then
    echo "$old" | xargs -r rm -rf
    ok "已清理 $(echo "$old" | wc -l) 份旧备份（保留 $KEEP 份）"
fi

echo ""
ok "备份完成：$DEST（$(du -sh "$DEST" | cut -f1)）"
ls -lh "$DEST" | tail -n +2 | awk '{printf "  %-28s %s\n", $9, $5}'

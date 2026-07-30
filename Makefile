.PHONY: infra infra-down infra-reset db-init db-seed dev-api dev-ingest dev-retrieve dev-stamp dev-relay dev-frontend deploy deploy-restart-app deploy-frontend

# ── 基础设施 ──────────────────────────────────────────────────────────

# 启动基础设施（首次或重启后执行）
infra:
	docker-compose -f docker-compose.infra.yml up -d

# 停止基础设施（保留数据）
infra-down:
	docker-compose -f docker-compose.infra.yml down

# 完全重置（删除所有数据，慎用）
infra-reset:
	docker-compose -f docker-compose.infra.yml down -v

# ── 数据库初始化 ──────────────────────────────────────────────────────

# 初始化数据库表（首次启动后执行一次）
db-init:
	python -m src.scripts.init_db

# 写入开发期测试数据（admin / reader / writer）
db-seed:
	python -m src.scripts.seed_dev

# 预热 BGE-M3 等模型（~2GB，首次需要下载）
warmup:
	python -m src.scripts.warmup_models

# ── 本地进程（开发期直接跑，不走 Docker） ─────────────────────────────

# FastAPI（8000 端口，--reload 代码改动自动重载）
dev-api:
	uvicorn src.main:app --host 0.0.0.0 --port 8000 --reload

# 摄入 worker（消费 ingestion_queue）
dev-ingest:
	celery -A src.platform.task.celery_app worker \
		-Q ingestion_queue --concurrency=2 \
		-n ingestion-worker@%h --loglevel=info

# 检索 worker（消费 retrieval_queue）
dev-retrieve:
	celery -A src.platform.task.celery_app worker \
		-Q retrieval_queue --concurrency=2 \
		-n retrieval-worker@%h --loglevel=info

# 盖戳 worker（消费 stamping_queue）
dev-stamp:
	celery -A src.platform.task.celery_app worker \
		-Q stamping_queue --concurrency=2 \
		-n stamping-worker@%h --loglevel=info

# outbox relay
dev-relay:
	python -m src.platform.task.outbox_relay

# Celery beat（定时任务调度：对账等）
dev-beat:
	celery -A src.platform.task.celery_app beat --loglevel=info

# 前端（开发期直接跑，不走 Docker）
dev-frontend:
	cd frontend && npm run dev

# ── 上线部署 ─────────────────────────────────────────────────────────

# 构建前端 Docker 镜像
deploy-frontend:
	docker build -t rag-v14-frontend ./frontend

# 启动全栈（infra + app，上线使用）
deploy:
	docker-compose -f docker-compose.infra.yml -f docker-compose.app.yml up -d

# 只重启计算层（上线后更新代码用）
deploy-restart-app:
	docker-compose -f docker-compose.app.yml restart

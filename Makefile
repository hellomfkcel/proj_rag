.PHONY: infra infra-down infra-reset db-init db-seed dev-api dev-ingest dev-retrieve dev-stamp dev-relay dev-frontend deploy deploy-stop deploy-restart deploy-status deploy-frontend

# ── 基础设施 ──────────────────────────────────────────────────────────
# 注：docker-compose 命令为 v1 写法，本机已使用 docker compose（v2），
#     统一改用 `docker compose`。生产/全栈部署请使用 scripts/start.sh。

# 启动基础设施（首次或重启后执行）
infra:
	docker compose -f docker-compose.infra.yml up -d

# 停止基础设施（保留数据）
infra-down:
	docker compose -f docker-compose.infra.yml down

# 完全重置（删除所有数据，慎用）
infra-reset:
	docker compose -f docker-compose.infra.yml down -v

# ── 数据库初始化 ──────────────────────────────────────────────────────

# 初始化数据库表（首次启动后执行一次）
db-init:
	python -m src.scripts.init_db

# 写入开发期测试数据（admin / reader / writer，仅开发）
db-seed:
	python -m src.scripts.seed_dev

# 预热 BGE-M3 等模型（~2GB，首次需要下载）
warmup:
	python -m src.scripts.warmup_models

# ── 本地进程（开发期直接跑，不走 Docker） ─────────────────────────────

# FastAPI（8000 端口，--reload 代码改动自动重载）
# OTEL_SERVICE_NAME=api 让 Grafana/Tempo 中按服务名区分 trace 来源
dev-api:
	OTEL_SERVICE_NAME=api conda run -n rag_dev_v14 uvicorn src.main:app --host 0.0.0.0 --port 8000 --reload

# 摄入 worker（消费 ingestion_queue）
# OTEL_SERVICE_NAME 与 docker-compose.app.yml 保持一致
dev-ingest:
	OTEL_SERVICE_NAME=ingestion-worker \
	celery -A src.platform.task.celery_app worker \
		-Q ingestion_queue --concurrency=2 \
		-n ingestion-worker@%h --loglevel=info

# 检索 worker（消费 retrieval_queue）
dev-retrieve:
	OTEL_SERVICE_NAME=retrieval-worker \
	celery -A src.platform.task.celery_app worker \
		-Q retrieval_queue --concurrency=2 \
		-n retrieval-worker@%h --loglevel=info

# 盖戳 worker（消费 stamping_queue）
dev-stamp:
	OTEL_SERVICE_NAME=stamping-worker \
	celery -A src.platform.task.celery_app worker \
		-Q stamping_queue --concurrency=2 \
		-n stamping-worker@%h --loglevel=info

# BGE-M3 嵌入服务（Layer 3，单进程 GPU 模型共享）
dev-embedding:
	OTEL_SERVICE_NAME=embedding-service \
	conda run -n rag_dev_v14 uvicorn src.services.embedding_service:app --host 0.0.0.0 --port 19500 --reload

# outbox relay
dev-relay:
	OTEL_SERVICE_NAME=outbox-relay python -m src.platform.task.outbox_relay

# VisibilityChanged 事件订阅器（Redis Pub/Sub + 轮询兜底）
dev-visibility-events:
	OTEL_SERVICE_NAME=visibility-events python -m src.permission.visibility_events

# Celery beat（定时任务调度：对账等）
dev-beat:
	OTEL_SERVICE_NAME=celery-beat celery -A src.platform.task.celery_app beat --loglevel=info

# 前端（开发期直接跑，不走 Docker）
dev-frontend:
	cd frontend && npm run dev

# ── 上线部署 ─────────────────────────────────────────────────────────
# Docker 全栈部署统一走 scripts/start.sh（负责启动顺序、健康等待、init-db、日志）。

# 启动全栈（infra → init-db → app，上线使用）
deploy:
	bash scripts/start.sh start

# 停止全栈（保留数据卷）
deploy-stop:
	bash scripts/start.sh stop

# 重启全栈
deploy-restart:
	bash scripts/start.sh restart

# 查看状态/日志
deploy-status:
	bash scripts/start.sh status

# 构建前端 Docker 镜像
deploy-frontend:
	docker build -t rag-v14-frontend ./frontend

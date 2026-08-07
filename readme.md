  权限系统 与 RAG 系统 — 开发模式 vs 生产模式启动方式分析

  一、系统总览

  整个体系包含 两个独立项目仓库，加上外部身份源：

  ┌─────────────────────────────────────────────────────────────────┐
  │ 项目 A: proj_rag_dev (RAG v14 知识库平台)                        │
  │   Python FastAPI + Celery Workers + Next.js 前端                  │
  │   conda env: rag_dev_v14                                         │
  ├─────────────────────────────────────────────────────────────────┤
  │ 项目 B: permission-system (外部权限管理系统)                      │
  │   FastAPI 权限服务后端 + Next.js 管理台前端                       │
  │   conda env: perm_service                                        │
  ├─────────────────────────────────────────────────────────────────┤
  │ 外部: Keycloak (IdP)                                             │
  │   用户/组/角色管理 + JWT 签发 + SSO                              │
  │   开发期可选: Keycloak 24 容器 (docker-compose.keycloak.yml)     │
  └─────────────────────────────────────────────────────────────────┘

  ---
  二、RAG 项目 (proj_rag_dev) 启动方式

  2.1 基础设施（开发/生产通用）

  基础服务由 docker-compose.infra.yml 编排，已在运行中：

  ┌─────────────────┬───────┬──────────────────────────────┐
  │      服务       │ 端口  │             用途             │
  ├─────────────────┼───────┼──────────────────────────────┤
  │ PostgreSQL 16   │ 25432 │ 业务库/审计/outbox           │
  ├─────────────────┼───────┼──────────────────────────────┤
  │ Redis 7         │ 16379 │ Celery broker + 流式 Pub/Sub │
  ├─────────────────┼───────┼──────────────────────────────┤
  │ Milvus 2.4      │ 19530 │ 向量库 (chunk + 权限戳记)    │
  ├─────────────────┼───────┼──────────────────────────────┤
  │ MinIO           │ 内网  │ Milvus 内部对象存储          │
  ├─────────────────┼───────┼──────────────────────────────┤
  │ etcd            │ 内网  │ Milvus 元数据                │
  ├─────────────────┼───────┼──────────────────────────────┤
  │ SeaweedFS       │ 18333 │ 文档原文件 S3 网关           │
  ├─────────────────┼───────┼──────────────────────────────┤
  │ Cerbos PDP 0.39 │ 13592 │ 权限策略决策引擎             │
  └─────────────────┴───────┴──────────────────────────────┘

  # 启动/管理基础设施
  make infra           # docker-compose -f docker-compose.infra.yml up -d
  make infra-down      # 停止（保留数据）
  make infra-reset     # 完全删除数据重建

  # 数据库初始化（首次或重建后）
  make db-init         # 建表
  make db-seed         # 写入开发测试数据 (admin/reader/writer)
  make warmup          # 预热 BGE-M3 模型 (~2GB)

  2.2 开发模式 — 本地进程（不使用 Docker 运行应用）

  conda 环境: conda activate rag_dev_v14

  通过在宿主机直接运行 Python 进程/Celery worker/Next.js dev server，支持热更新 (--reload)：

  # RAG 后端 API (FastAPI, 端口 8000, --reload 热更新)
  make dev-api
  # 等价: uvicorn src.main:app --host 0.0.0.0 --port 8000 --reload

  # 摄入 Worker
  make dev-ingest
  # 等价: celery -A src.platform.task.celery_app worker -Q ingestion_queue --concurrency=2

  # 检索 Worker
  make dev-retrieve
  # 等价: celery -A src.platform.task.celery_app worker -Q retrieval_queue --concurrency=2

  # 盖戳 Worker
  make dev-stamp
  # 等价: celery -A src.platform.task.celery_app worker -Q stamping_queue --concurrency=2

  # Outbox Relay
  make dev-relay

  # VisibilityChanged 事件订阅器
  make dev-visibility-events

  # 定时任务调度
  make dev-beat

  # RAG 前端 (Next.js dev server, 端口 3001, HMR 热更新)
  make dev-frontend
  # 等价: cd frontend && npm run dev

  特点：
  - 所有进程通过 localhost 直连基础设施（PG:25432 / Redis:16379 / Milvus:19530 / Cerbos:13592）
  - 配置来源：proj_rag_dev/.env
  - AUTHZ_SERVICE_MODE=remote，RAG 调用外部权限服务 http://127.0.0.1:18080
  - 开发期 JWT 由 RAG 系统自签 RS256，POST /api/v1/auth/dev-login 签发
  - 开发期前端访问：http://localhost:3001

  2.3 生产模式 — Docker 容器化

  # 全栈启动（infra + app）
  make deploy
  # 等价: docker-compose -f docker-compose.infra.yml -f docker-compose.app.yml up -d

  # 仅重启计算层（更新代码后）
  make deploy-restart-app

  Docker 容器启动的服务（docker-compose.app.yml）：

  ┌───────────────────┬──────┬────────────────────────────────────┐
  │       容器        │ 端口 │                说明                │
  ├───────────────────┼──────┼────────────────────────────────────┤
  │ api               │ 8000 │ FastAPI HTTP 入口，不执行 Pipeline │
  ├───────────────────┼──────┼────────────────────────────────────┤
  │ ingestion-worker  │ —    │ 摄入队列，concurrency=2            │
  ├───────────────────┼──────┼────────────────────────────────────┤
  │ retrieval-worker  │ —    │ 检索队列，concurrency=4            │
  ├───────────────────┼──────┼────────────────────────────────────┤
  │ stamping-worker   │ —    │ 盖戳队列（独立），concurrency=4    │
  ├───────────────────┼──────┼────────────────────────────────────┤
  │ outbox-relay      │ —    │ Outbox 事件投递                    │
  ├───────────────────┼──────┼────────────────────────────────────┤
  │ visibility-events │ —    │ 权限事件订阅                       │
  ├───────────────────┼──────┼────────────────────────────────────┤
  │ frontend          │ 3001 │ Next.js standalone 模式            │
  ├───────────────────┼──────┼────────────────────────────────────┤
  │ nginx             │ 80   │ 统一入口（/ → 前端，/api → 后端）  │
  └───────────────────┴──────┴────────────────────────────────────┘

  特点：
  - 网络：容器间通过 rag-network (bridge) 互访
  - 权限服务地址：Docker 内部 http://cerbos:3592（local 模式）或 http://permission-service:8080（remote 模式）
  - JWT 校签改用 JWKS URL（从 Keycloak 获取公钥），不再用本地 PEM
  - 生产前端访问：http://<host>（nginx 端口 80 统一入口）

  ---
  三、权限系统项目 (permission-system) 启动方式

  3.1 基础设施

  由 docker-compose.yml 编排，独立于 RAG 基础设施：

  ┌───────────────┬───────┬─────────────────────────────────────────────────┐
  │     服务      │ 端口  │                      用途                       │
  ├───────────────┼───────┼─────────────────────────────────────────────────┤
  │ perm-postgres │ 25433 │ 权限服务独立数据库 (ACL/角色绑定/限制/资源镜像) │
  ├───────────────┼───────┼─────────────────────────────────────────────────┤
  │ perm-redis    │ 16380 │ 权限事件 Pub/Sub (VisibilityChanged)            │
  └───────────────┴───────┴─────────────────────────────────────────────────┘

  # 启动基础设施
  docker compose up -d perm-postgres perm-redis

  Cerbos PDP 和 Keycloak 与 RAG 系统共用，不在此编排中。

  Keycloak 独立编排：
  docker compose -f docker-compose.keycloak.yml up -d  # 端口 8080

  3.2 开发模式 — 本地进程

  conda 环境: conda activate perm_service

  # 权限服务后端 (FastAPI, 端口 18080, --reload 热更新)
  cd ~/permission-system/permission-service
  conda activate perm_service
  uvicorn app.main:app --host 0.0.0.0 --port 18080 --reload

  # 管理台前端 (Next.js dev server, 端口 3002, HMR 热更新)
  cd ~/permission-system/admin-console
  npm run dev
  # 等价: next dev -p 3002

  特点：
  - 权限服务后端直连 perm-postgres:25433 / perm-redis:16380 / Cerbos:13592
  - 管理台前端配置 admin-console/.env.local（浏览器端环境变量 NEXT_PUBLIC_*）
  - 支持 --reload 代码热更新
  - PRODUCTION=false（.env 中配置）

  3.3 生产模式 — Docker 容器化

  # 启动应用服务
  docker compose up -d permission-service admin-console

  # 全部启动
  docker compose up -d

  ┌────────────────────┬───────┬──────────────────────┐
  │        容器        │ 端口  │         说明         │
  ├────────────────────┼───────┼──────────────────────┤
  │ permission-service │ 18080 │ FastAPI 权限服务后端 │
  ├────────────────────┼───────┼──────────────────────┤
  │ admin-console      │ 3002  │ 管理台前端           │
  └────────────────────┴───────┴──────────────────────┘

  生产模式差异（与开发模式对比）：

  ┌────────────────┬────────────────────────────┬──────────────────────────────────────────────┐
  │     配置项     │          开发模式          │                   生产模式                   │
  ├────────────────┼────────────────────────────┼──────────────────────────────────────────────┤
  │ PRODUCTION     │ false                      │ "true" (启用密钥强制校验)                    │
  ├────────────────┼────────────────────────────┼──────────────────────────────────────────────┤
  │ JWT 公钥       │ 本地 PEM 文件              │ /run/secrets/jwt_public_key (Docker secret)  │
  ├────────────────┼────────────────────────────┼──────────────────────────────────────────────┤
  │ ctx_token 密钥 │ .env 明文 CTX_TOKEN_SECRET │ /run/secrets/ctx_token_secret                │
  ├────────────────┼────────────────────────────┼──────────────────────────────────────────────┤
  │ Keycloak 密钥  │ .env 明文                  │ /run/secrets/keycloak_client_secret          │
  ├────────────────┼────────────────────────────┼──────────────────────────────────────────────┤
  │ 服务间 API Key │ .env 明文                  │ /run/secrets/service_api_key                 │
  ├────────────────┼────────────────────────────┼──────────────────────────────────────────────┤
  │ TLS            │ 关闭                       │ /run/secrets/tls_cert + /run/secrets/tls_key │
  ├────────────────┼────────────────────────────┼──────────────────────────────────────────────┤
  │ DB 连接        │ localhost:25433            │ 容器内 perm-postgres:5432                    │
  ├────────────────┼────────────────────────────┼──────────────────────────────────────────────┤
  │ Redis 连接     │ localhost:16380            │ 容器内 perm-redis:6379                       │
  └────────────────┴────────────────────────────┴──────────────────────────────────────────────┘

  ---
  四、两个系统的协作关系

                        RAG 系统                        权限系统
                    ┌──────────────┐              ┌──────────────────┐
    浏览器 ──:80──►  │ Nginx        │              │                  │
                    │  ├─ / → 前端  │──跳转链接──►│ 管理台 :3002     │
                    │  └─ /api→后端 │              │                  │
                    │      :8000   │──HTTP 调用──►│ 权限服务 :18080   │
                    │              │              │   ├─ /v1/check   │
                    │ Cerbos :13592│◄─────────────│   ├─ /v1/filter  │
                    └──────────────┘              │   ├─ /v1/prefilter│
                                                  │   ├─ /v1/visibility│
                                                  │   └─ /v1/context │
                                                  └──────────────────┘

  RAG 系统通过 .env 中的 AUTHZ_SERVICE_MODE 切换权限调用模式：

  ┌────────┬───────────────────────────┬───────────────────────────────────────────────────────────────────────────────────┬──────────────────────────────────┐
  │  模式  │            值             │                                   权限判定调用                                    │             适用场景             │
  ├────────┼───────────────────────────┼───────────────────────────────────────────────────────────────────────────────────┼──────────────────────────────────┤
  │ local  │ AUTHZ_SERVICE_MODE=local  │ RAG 直接调 Cerbos PDP localhost:13592，用本地 resource_registry/mount_registry 表 │ 纯 RAG 开发，不需要外部权限服务  │
  ├────────┼───────────────────────────┼───────────────────────────────────────────────────────────────────────────────────┼──────────────────────────────────┤
  │ remote │ AUTHZ_SERVICE_MODE=remote │ RAG 通过 P-AUTHC 调权限服务后端 http://127.0.0.1:18080                            │ 需要完整的 ACL/角色/限制管理能力 │
  └────────┴───────────────────────────┴───────────────────────────────────────────────────────────────────────────────────┴──────────────────────────────────┘

  当前 RAG .env 配置为 AUTHZ_SERVICE_MODE=remote。

  ---
  五、完整开发环境启动顺序

  第 1 步: 启动 RAG 基础设施（如未运行）

  Docker 容器启动的服务（docker-compose.app.yml）：

  ┌───────────────────┬──────┬────────────────────────────────────┐
  │       容器        │ 端口 │                说明                │
  ├───────────────────┼──────┼────────────────────────────────────┤
  │ api               │ 8000 │ FastAPI HTTP 入口，不执行 Pipeline │
  ├───────────────────┼──────┼────────────────────────────────────┤
  │ ingestion-worker  │ —    │ 摄入队列，concurrency=2            │
  ├───────────────────┼──────┼────────────────────────────────────┤
  │ retrieval-worker  │ —    │ 检索队列，concurrency=4            │
  ├───────────────────┼──────┼────────────────────────────────────┤
  │ stamping-worker   │ —    │ 盖戳队列（独立），concurrency=4    │
  ├───────────────────┼──────┼────────────────────────────────────┤
  │ outbox-relay      │ —    │ Outbox 事件投递                    │
  ├───────────────────┼──────┼────────────────────────────────────┤
  │ visibility-events │ —    │ 权限事件订阅                       │
  ├───────────────────┼──────┼────────────────────────────────────┤
  │ frontend          │ 3001 │ Next.js standalone 模式            │
  ├───────────────────┼──────┼────────────────────────────────────┤
  │ nginx             │ 80   │ 统一入口（/ → 前端，/api → 后端）  │
  └───────────────────┴──────┴────────────────────────────────────┘

  特点：
  - 网络：容器间通过 rag-network (bridge) 互访
  - 权限服务地址：Docker 内部 http://cerbos:3592（local 模式）或 http://permission-service:8080（remote 模式）
  - JWT 校签改用 JWKS URL（从 Keycloak 获取公钥），不再用本地 PEM
  - 生产前端访问：http://<host>（nginx 端口 80 统一入口）

  ---
  三、权限系统项目 (permission-system) 启动方式

  3.1 基础设施

  由 docker-compose.yml 编排，独立于 RAG 基础设施：

  ┌───────────────┬───────┬─────────────────────────────────────────────────┐
  │     服务      │ 端口  │                      用途                       │
  ├───────────────┼───────┼─────────────────────────────────────────────────┤
  │ perm-postgres │ 25433 │ 权限服务独立数据库 (ACL/角色绑定/限制/资源镜像) │
  ├───────────────┼───────┼─────────────────────────────────────────────────┤
  │ perm-redis    │ 16380 │ 权限事件 Pub/Sub (VisibilityChanged)            │
  └───────────────┴───────┴─────────────────────────────────────────────────┘

  # 启动基础设施
  docker compose up -d perm-postgres perm-redis

  Cerbos PDP 和 Keycloak 与 RAG 系统共用，不在此编排中。

  Keycloak 独立编排：
  docker compose -f docker-compose.keycloak.yml up -d  # 端口 8080

  3.2 开发模式 — 本地进程

  conda 环境: conda activate perm_service

  # 权限服务后端 (FastAPI, 端口 18080, --reload 热更新)
  cd ~/permission-system/permission-service
  conda activate perm_service
  uvicorn app.main:app --host 0.0.0.0 --port 18080 --reload

  # 管理台前端 (Next.js dev server, 端口 3002, HMR 热更新)
  cd ~/permission-system/admin-console
  npm run dev
  # 等价: next dev -p 3002

  特点：
  - 权限服务后端直连 perm-postgres:25433 / perm-redis:16380 / Cerbos:13592
  - 管理台前端配置 admin-console/.env.local（浏览器端环境变量 NEXT_PUBLIC_*）
  - 支持 --reload 代码热更新
  - PRODUCTION=false（.env 中配置）

  3.3 生产模式 — Docker 容器化

  # 启动应用服务
  docker compose up -d permission-service admin-console

  # 全部启动
  docker compose up -d

  ┌────────────────────┬───────┬──────────────────────┐
  │        容器        │ 端口  │         说明         │
  ├────────────────────┼───────┼──────────────────────┤
  │ permission-service │ 18080 │ FastAPI 权限服务后端 │
  ├────────────────────┼───────┼──────────────────────┤
  │ admin-console      │ 3002  │ 管理台前端           │
  └────────────────────┴───────┴──────────────────────┘

  生产模式差异（与开发模式对比）：

  ┌────────────────┬────────────────────────────┬──────────────────────────────────────────────┐
  │     配置项     │          开发模式          │                   生产模式                   │
  ├────────────────┼────────────────────────────┼──────────────────────────────────────────────┤
  │ PRODUCTION     │ false                      │ "true" (启用密钥强制校验)                    │
  ├────────────────┼────────────────────────────┼──────────────────────────────────────────────┤
  │ JWT 公钥       │ 本地 PEM 文件              │ /run/secrets/jwt_public_key (Docker secret)  │
  ├────────────────┼────────────────────────────┼──────────────────────────────────────────────┤
  │ ctx_token 密钥 │ .env 明文 CTX_TOKEN_SECRET │ /run/secrets/ctx_token_secret                │
  ├────────────────┼────────────────────────────┼──────────────────────────────────────────────┤
  │ Keycloak 密钥  │ .env 明文                  │ /run/secrets/keycloak_client_secret          │
  ├────────────────┼────────────────────────────┼──────────────────────────────────────────────┤
  │ 服务间 API Key │ .env 明文                  │ /run/secrets/service_api_key                 │
  ├────────────────┼────────────────────────────┼──────────────────────────────────────────────┤
  │ TLS            │ 关闭                       │ /run/secrets/tls_cert + /run/secrets/tls_key │
  ├────────────────┼────────────────────────────┼──────────────────────────────────────────────┤
  │ DB 连接        │ localhost:25433            │ 容器内 perm-postgres:5432                    │
  ├────────────────┼────────────────────────────┼──────────────────────────────────────────────┤
  │ Redis 连接     │ localhost:16380            │ 容器内 perm-redis:6379                       │
  └────────────────┴────────────────────────────┴──────────────────────────────────────────────┘

  ---
  四、两个系统的协作关系

                        RAG 系统                        权限系统
                    ┌──────────────┐              ┌──────────────────┐
    浏览器 ──:80──►  │ Nginx        │              │                  │
                    │  ├─ / → 前端  │──跳转链接──►│ 管理台 :3002     │
                    │  └─ /api→后端 │              │                  │
                    │      :8000   │──HTTP 调用──►│ 权限服务 :18080   │
                    │              │              │   ├─ /v1/check   │
                    │ Cerbos :13592│◄─────────────│   ├─ /v1/filter  │
                    └──────────────┘              │   ├─ /v1/prefilter│
                                                  │   ├─ /v1/visibility│
                                                  │   └─ /v1/context │
                                                  └──────────────────┘

  RAG 系统通过 .env 中的 AUTHZ_SERVICE_MODE 切换权限调用模式：

  ┌────────┬───────────────────────────┬───────────────────────────────────────────────────────────────────────────────────┬──────────────────────────────────┐
  │  模式  │            值             │                                   权限判定调用                                    │             适用场景             │
  ├────────┼───────────────────────────┼───────────────────────────────────────────────────────────────────────────────────┼──────────────────────────────────┤
  │ local  │ AUTHZ_SERVICE_MODE=local  │ RAG 直接调 Cerbos PDP localhost:13592，用本地 resource_registry/mount_registry 表 │ 纯 RAG 开发，不需要外部权限服务  │
  ├────────┼───────────────────────────┼───────────────────────────────────────────────────────────────────────────────────┼──────────────────────────────────┤
  │ remote │ AUTHZ_SERVICE_MODE=remote │ RAG 通过 P-AUTHC 调权限服务后端 http://127.0.0.1:18080                            │ 需要完整的 ACL/角色/限制管理能力 │
  └────────┴───────────────────────────┴───────────────────────────────────────────────────────────────────────────────────┴──────────────────────────────────┘

  当前 RAG .env 配置为 AUTHZ_SERVICE_MODE=remote。

  ---
  五、完整开发环境启动顺序

  第 1 步: 启动 RAG 基础设施（如未运行）
    make infra          # PG/Redis/Milvus/MinIO/etcd/SeaweedFS/Cerbos

  第 2 步: 启动权限系统基础设施
    docker compose up -d perm-postgres perm-redis

  第 3 步: 启动 Keycloak（如需 SSO）
    docker compose -f docker-compose.keycloak.yml up -d

  第 4 步: 启动权限服务后端（开发模式）
    conda activate perm_service
    cd permission-service && uvicorn app.main:app --host 0.0.0.0 --port 18080 --reload

  第 5 步: 启动管理台前端（开发模式）
    cd admin-console && npm run dev     # :3002

  第 6 步: 启动 RAG 各开发进程（每个一个终端）
    conda activate rag_dev_v14
    终端2: make dev-api                 # :8000
    终端3: make dev-ingest              # ingestion worker
    终端4: make dev-retrieve            # retrieval worker
    终端5: make dev-stamp               # stamping worker
    终端6: make dev-relay               # outbox relay
    终端7: make dev-visibility-events   # 事件订阅
    终端8: make dev-frontend            # :3001

  第 7 步: 访问
    RAG 前端:     http://localhost:3001
    管理台:       http://localhost:3002
    权限服务 API: http://localhost:18080/docs
    Keycloak:     http://192.168.1.127:8080

  ---
  六、完整生产环境启动顺序

  # 第 1 步: RAG 基础设施
  docker-compose -f docker-compose.infra.yml up -d

  # 第 2 步: Keycloak
  docker compose -f docker-compose.keycloak.yml up -d

  # 第 3 步: 权限系统基础设施
  docker compose up -d perm-postgres perm-redis

  # 第 4 步: 数据库初始化（首次）
  cd proj_rag_dev && make db-init && make db-seed

  # 第 5 步: 权限服务 + 管理台
  docker compose up -d permission-service admin-console

  # 第 6 步: RAG 全栈
  docker-compose -f docker-compose.infra.yml -f docker-compose.app.yml up -d

  # 访问: http://<host> (nginx :80 统一入口)

  ---
  七、关键差异对照表

  ┌──────────────┬───────────────────────────┬────────────────────────────────┬────────────────────────┬────────────────────────────┐
  │     维度     │         RAG 开发          │            RAG 生产            │     权限系统 开发      │       权限系统 生产        │
  ├──────────────┼───────────────────────────┼────────────────────────────────┼────────────────────────┼────────────────────────────┤
  │ 运行方式     │ 宿主机进程 + --reload     │ Docker 容器                    │ 宿主机进程 + --reload  │ Docker 容器                │
  ├──────────────┼───────────────────────────┼────────────────────────────────┼────────────────────────┼────────────────────────────┤
  │ API 端口     │ :8000 (uvicorn)           │ 容器内 :8000 → nginx :80       │ :18080 (uvicorn)       │ 容器内 :8080 → 映射 :18080 │
  ├──────────────┼───────────────────────────┼────────────────────────────────┼────────────────────────┼────────────────────────────┤
  │ 前端端口     │ :3001 (next dev)          │ 容器内 :3001 → nginx :80       │ :3002 (next dev)       │ 容器内 :3000 → 映射 :3002  │
  ├──────────────┼───────────────────────────┼────────────────────────────────┼────────────────────────┼────────────────────────────┤
  │ DB 连接      │ localhost:25432           │ 容器内 postgres:5432           │ localhost:25433        │ 容器内 perm-postgres:5432  │
  ├──────────────┼───────────────────────────┼────────────────────────────────┼────────────────────────┼────────────────────────────┤
  │ Redis 连接   │ localhost:16379           │ 容器内 redis:6379              │ localhost:16380        │ 容器内 perm-redis:6379     │
  ├──────────────┼───────────────────────────┼────────────────────────────────┼────────────────────────┼────────────────────────────┤
  │ Cerbos       │ localhost:13592 (docker)  │ cerbos:3592 (容器)             │ localhost:13592 (共用) │ host.docker.internal:13592 │
  ├──────────────┼───────────────────────────┼────────────────────────────────┼────────────────────────┼────────────────────────────┤
  │ JWT 校签     │ 本地 PEM 文件             │ JWKS URL (Keycloak)            │ 本地 PEM 文件          │ Docker secret              │
  ├──────────────┼───────────────────────────┼────────────────────────────────┼────────────────────────┼────────────────────────────┤
  │ 密钥管理     │ .env 明文                 │ Docker secrets                 │ .env 明文              │ /run/secrets/*             │
  ├──────────────┼───────────────────────────┼────────────────────────────────┼────────────────────────┼────────────────────────────┤
  │ TLS          │ 关闭                      │ 关闭（开发）                   │ 关闭                   │ 开启                       │
  ├──────────────┼───────────────────────────┼────────────────────────────────┼────────────────────────┼────────────────────────────┤
  │ 模型下载     │ 自动缓存到宿主机          │ 共享 Docker volume model_cache │ 不涉及                 │ 不涉及                     │
  ├──────────────┼───────────────────────────┼────────────────────────────────┼────────────────────────┼────────────────────────────┤
  │ 代码更新     │ 保存即生效 (--reload)     │ 重新构建镜像 + 重启            │ 保存即生效             │ 重新构建镜像               │
  ├──────────────┼───────────────────────────┼────────────────────────────────┼────────────────────────┼────────────────────────────┤
  │ RAG 权限模式 │ AUTHZ_SERVICE_MODE=remote │ AUTHZ_SERVICE_MODE=remote      │ —                      │ —                          │
  └──────────────┴───────────────────────────┴────────────────────────────────┴────────────────────────┴────────────────────────────┘

  ---
  八、运维操作手册

  8.1 安全关机与重启

  **必须先 stop 容器再关机**，Milvus Standalone 扛不住硬关机。
  直接断电/关机可能导致 etcd 或 MinIO 元数据损坏，collection 全部丢失。

  # 安全关机流程
  docker stop $(docker ps -q)          # 先停所有容器
  # 或按项目分别停止:
  docker compose -f ~/proj_rag_dev/docker-compose.infra.yml stop
  docker compose -f ~/proj_rag_dev/docker-compose.app.yml stop
  docker compose -f ~/permission-system/docker-compose.yml stop

  # 重启后容器会自动拉起 (restart: unless-stopped)，但建议手动确认:
  docker compose -f ~/proj_rag_dev/docker-compose.infra.yml up -d
  docker compose -f ~/proj_rag_dev/docker-compose.app.yml up -d
  docker compose -f ~/permission-system/docker-compose.yml up -d

  # 验证关键服务
  curl localhost:8000/healthz          # RAG API
  curl localhost:18080/healthz         # 权限服务
  docker ps --filter "health=healthy"  # 全部 healthy

  8.2 Milvus 数据恢复

  **症状**：检索返回空、日志报 `collection not found[collection=rag_documents]`。

  **诊断**：
  conda activate rag_dev_v14
  python3 -c "
  from pymilvus import MilvusClient
  client = MilvusClient(uri='http://localhost:19530')
  print('Collections:', client.list_collections())
  "
  # 如果输出 Collections: [] → collection 丢失，需要重建

  **恢复步骤**：
  # 1. 停止相关容器
  docker stop proj_rag_dev-milvus-1 proj_rag_dev-etcd-1 proj_rag_dev-minio-1

  # 2. 删除损坏的卷（数据不可恢复，确认后执行）
  docker rm proj_rag_dev-milvus-1 proj_rag_dev-etcd-1 proj_rag_dev-minio-1
  docker volume rm proj_rag_dev_milvus_data proj_rag_dev_etcd_data proj_rag_dev_minio_data

  # 3. 重建容器 + 卷
  docker compose -f ~/proj_rag_dev/docker-compose.infra.yml up -d etcd minio milvus

  # 4. 等待 healthy 后，重新摄入文档（collection 会在首次写入时自动创建）
  #    不需要手动建 collection——MilvusDocumentStoreWriter._ensure_collection() 自动处理

  8.3 权限资源回填（文档无法删除/无权限时）

  **症状**：前端删除文档报 "权限不足"，日志中出现 `unlink_resource_not_registered`。

  **原因**：文档在 PostgreSQL 中有记录，但权限服务（resource_registry / mount_registry）中没有对应条目。
  常见于：早期上传的文档、权限服务数据被重建后、reset_rag_data.py 执行后。

  **回填脚本**：
  conda activate rag_dev_v14
  cd ~/proj_rag_dev
  python -m src.scripts.backfill_resource_registry

  **输出示例**：
  回填完成:
    文档: 注册 15, 跳过(已存在) 120, 失败 0
    挂载: 链接 15, 跳过(已存在) 120, 失败 0

  脚本是幂等的——已注册的文档会自动跳过，可以安全地多次运行。

  **验证回填效果**：
  # 在前端 KB 页面尝试删除之前报 403 的文档
  # 或查询权限服务数据库:
  PGPASSWORD=perm_pass psql -h localhost -p 25433 -U perm_user -d permission_db \
    -c "SELECT resource_type, count(*) FROM resource_registry WHERE retired=false GROUP BY resource_type;"

  8.4 批量删除文档

  前端 KB 管理页支持两种删除方式：

  | 方式 | 触发 | 权限 | 说明 |
  |------|------|------|------|
  | 单条删除 | 文档行右侧 🗑 按钮 | `doc:unmount` | 调用 `DELETE /documents/{doc_id}/kb/{kb_id}` |
  | 批量删除 | 勾选多文档 → [🗑 批量删除] | `doc:unmount`（逐资源独立校验） | 调用 `POST /documents/batch/delete`，某文档权限不足时该条单独失败，不影响其余 |

  批量删除后，前端会弹窗汇总：成功数 + 失败数 + 每条失败的 doc_id 和原因。

  如果批量删除全部失败，检查：
  1. 字段名一致性：前端发送 `document_id`，后端接收 `document_id`（已于 2026-08-07 修复）
  2. 权限：当前用户需要在 KB 上有 `kb_writer` / `kb_admin` / `admin` 角色
  3. 资源注册：如果文档未在权限服务注册，运行 8.3 回填脚本

  8.5 权限诊断速查

  # 查看 RAG 当前权限模式
  grep AUTHZ_SERVICE_MODE ~/proj_rag_dev/.env

  # 查看权限服务是否正常
  curl -s http://localhost:18080/healthz

  # 查看 Cerbos PDP 是否正常
  curl -s http://localhost:13592/_ah/health

  # 查看权限服务日志（开发模式）
  # 终端中 uvicorn 输出

  # 查看权限服务日志（Docker 模式）
  docker logs permission-service --tail 50

  # 查看 RAG 侧权限调用日志
  grep "authz\|perm_service" ~/proj_rag_dev/logs/*.log 2>/dev/null | tail -20

  8.6 日志与可观测

  所有权限相关的异常处理都输出了结构化日志，可在 Grafana Loki 中查询：

  # 关键日志标记
  unlink_resource_not_registered    # 删除时发现文档未在权限服务注册（自动降级处理）
  retire_resource_not_registered    # 退役时发现文档未注册（自动降级处理）
  authz_check_failed                # 权限判定调用失败
  perm_service_check_failed         # 权限服务 /v1/check 请求失败
  backfill_document_failed          # 回填脚本注册文档失败
  backfill_link_failed              # 回填脚本链接挂载失败

  对应的 Grafana 仪表盘路径：Explore → Loki → 选择 label `service=rag-api` / `service=permission-service`。


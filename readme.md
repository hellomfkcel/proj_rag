# RAG v14 — 企业级多知识库检索增强生成系统

多知识库 RAG 平台：检索算法基于 Haystack 2.x，**授权判定全部外置**到外部权限服务（Cerbos PDP），本系统零权限判定，仅负责消费决策、执行过滤、传递上下文。

## 系统架构

### 进程组成

```
浏览器 ──► nginx :80
              ├─ /api → api :8000（FastAPI，仅鉴权/分发/SSE，不执行 Pipeline）
              └─ / → frontend :3001（Next.js）
                         │
   Celery workers（计算，不分发）
     ingestion-worker    解析 → 切分 → 嵌入 → 写 Milvus → 盖戳
     retrieval-worker    查询 Pipeline（三层权限检索）
     stamping-worker     盖戳管道（可见性物化到向量库）
     outbox-relay / visibility-events   事件可靠投递与订阅
   embedding-service :19500   BGE-M3 HTTP 服务（共享 GPU，可选）
```

### 基础设施（docker-compose.infra.yml）

| 组件 | 版本 | 用途 |
|------|------|------|
| PostgreSQL | 16 | 业务库 |
| Redis | 7 | Celery broker + Pub/Sub |
| Milvus (+etcd/MinIO) | 2.4 | 向量库 |
| SeaweedFS | 3.68 | 对象存储 |
| Infinity | latest | 推理引擎（可选） |

### 核心机制

- **授权外置**：所有授权决策由外部权限服务做出，系统只经 P-AUTHC 调用，不做任何本地判定。
- **三层检索权限链路**：L1 prefilter 注入向量库查询条件 → L2 过采样补检索 → L3 逐条复核。禁止事后过滤、禁止无过滤回退。
- **盖戳管道**：把可见性（allow/deny stamps）物化进 Milvus chunk payload，检索命中即已过滤。

## 部署与运维

### 前置要求

- Docker + Docker Compose v2
- 外部权限平台（权限服务 + Cerbos + Keycloak）另行部署，并配置 `.env` 中 `AUTHZ_*` / `KEYCLOAK_*` 相关项
- 复制 `.env.example` 为 `.env` 并填写（密钥、模型 key 等）

### 全栈部署

```bash
# 一键启动：基础设施 → 初始化数据库 → 应用
bash scripts/start.sh start

# 停止 / 重启 / 状态 / 日志
bash scripts/start.sh stop
bash scripts/start.sh restart
bash scripts/start.sh status
bash scripts/start.sh logs [服务名]
```

### 常用运维命令（Makefile）

```bash
make infra              # 启动基础设施
make infra-down         # 停止基础设施（保留数据）
make infra-reset        # 完全重置（删除所有数据，慎用）
make db-init            # 初始化数据库表（首次部署后执行一次）
make db-seed            # 写入开发测试数据（仅开发）
```

本地开发可分别启动进程：`make dev-api`、`make dev-ingest`、`make dev-retrieve`、`make dev-stamp`、`make dev-embedding`、`make dev-frontend` 等。

### 服务端口

| 服务 | 端口 |
|------|------|
| nginx（统一入口） | 80 |
| API | 8000 |
| 前端 | 3001 |
| embedding-service | 19500 |
| PostgreSQL | 25432 |
| Redis | 16379 |
| Milvus | 19530 |
| SeaweedFS | 18333 |

## 常用接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/ping` | 健康检查 |
| POST | `/api/v1/auth/dev-login` | 开发登录（未配置 OIDC 时） |
| POST | `/api/v1/auth/token` | 登录获取 Token |
| GET/POST | `/api/v1/knowledge-bases` | 知识库列表 / 创建 |
| POST | `/api/v1/documents/upload` | 上传文档（触发解析 → 摄入 → 盖戳） |
| GET | `/api/v1/knowledge-bases/{kb_id}/documents` | 库内文档列表 |
| POST | `/api/v1/conversations/query` | 问答（检索 + 生成） |
| GET | `/api/v1/conversations/{id}/stream` | 问答流式结果（SSE） |
| GET/POST | `/api/v1/models` | 模型配置 |

完整接口见源码 `src/api/`。

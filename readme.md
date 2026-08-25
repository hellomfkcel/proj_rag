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
- 外部权限平台（权限系统 + Cerbos + Keycloak）已部署（默认同主机 `../permission-system`）
- 复制 `.env.example` 为 `.env` 并填写必需变量

### 部署（scripts/deploy.sh）— 首次上线 / 配置变更后

`deploy.sh` 负责：校验外部依赖 → 自动生成强随机密钥/口令（含 JWT 密钥对）→ 读取环境与基础设施配置（权限平台 key、perm-redis、OTel/Langfuse/LLM 等）→ 人工补值（缺失即报错并说明来源）→ 编排启动 → 最终验证与 smoke 检查。

```bash
bash scripts/deploy.sh                       # 生产部署（默认 APP_ENV=production）
APP_ENV=development bash scripts/deploy.sh   # 联调模式（保留 dev 登录）
BUILD=1 bash scripts/deploy.sh               # 代码变更后强制重建镜像
ROTATE_KEYS=1 bash scripts/deploy.sh         # 密钥泄露后强制轮换 JWT
NGINX_TLS=true bash scripts/deploy.sh        # nginx 443 TLS
RESET=1 bash scripts/deploy.sh               # 全新部署：清空全部数据卷（数据不可恢复）
```

### 运维（scripts/start.sh）— 日常起停与排障

```bash
bash scripts/start.sh start                  # 按序启动：infra → init-db → app
RESET=1 bash scripts/start.sh start          # 全新部署：清空全部数据卷（数据不可恢复）
bash scripts/start.sh stop                   # 停止全部（保留数据卷）
bash scripts/start.sh restart                # 重启全部
bash scripts/start.sh status                 # 查看各服务健康状态
bash scripts/start.sh logs [服务名]           # 查看日志（-f 跟随）
bash scripts/start.sh backup                 # 数据备份（PG + 卷快照）
```

> **全新部署（RESET=1）**：数据卷（postgres/redis/etcd/minio/milvus/seaweedfs/infinity_hf/model_cache）
> 首次初始化时固化口令，`POSTGRES_PASSWORD` 对既有卷不生效。重部署时 `.env` 口令与旧卷不一致会导致
> 数据库认证失败——脚本会在建表前做口令预检并提示。`RESET=1` 清空全部数据卷后按 `.env` 全新初始化
> （含向量库与模型缓存，**数据不可恢复**）。

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

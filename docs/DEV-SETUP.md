# 开发环境配置指南

本指南帮助新人在本地搭建 RAG 系统 v14 开发环境。

## 前置条件

- Docker + docker-compose
- Python 3.11
- Ollama（可选，LLM 也可以在容器中运行）

## 快速开始

### 1. 配置环境变量

```bash
cp .env.example .env
```

必须填写的变量：
- `POSTGRES_PASSWORD` — 数据库密码
- `REDIS_PASSWORD` — Redis 密码
- `OLLAMA_BASE_URL` — LLM 服务地址：
  - **Mac / Windows**：`http://host.docker.internal:11434`
  - **Linux**：`http://172.17.0.1:11434`（或宿主机 Docker 网关 IP）
- `JWT_PUBLIC_KEY_URL` — IdP 公钥地址
- `OTEL_COLLECTOR_ENDPOINT` — 可观测平台地址

以下变量有默认值，无需修改：
- `POSTGRES_PASSWORD` / `REDIS_PASSWORD` — 按需修改
- `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` — 默认 `minioadmin`
- `OLLAMA_MODEL` — 默认 `qwen2.5:7b`

### 2. 启动基础设施

```bash
make infra
```

等待所有服务就绪（约 30-60 秒，主要等 Milvus）：

```bash
docker-compose -f docker-compose.infra.yml ps
```

所有服务状态应为 `healthy`。

### 3. 初始化数据库

```bash
make db-init    # 首次启动执行一次
make db-seed    # 写入开发期测试数据
```

### 4. 验证 Cerbos 策略加载

```bash
curl http://localhost:3592/_cerbos/health
```

### 5. 预热嵌入模型

BGE-M3 首次下载约 2GB，仅首次需要：

```bash
python -m src.scripts.warmup_models
```

### 6. 启动开发进程

日常开发只需要启动对应模块的进程，不需要全部跑起来：

| 开发场景 | 需要启动的进程 |
|---------|-------------|
| 文档上传流程 | `dev-api` + `dev-ingest` |
| 查询流程 | `dev-api` + `dev-retrieve` |
| 盖戳调试 | `dev-stamp`（也需要启动 `dev-api` 触发摄入） |
| 事件投递 | `dev-relay` |

```bash
make dev-api        # 终端 1：FastAPI（端口 8000）
make dev-ingest     # 终端 2：摄入 worker
make dev-retrieve   # 终端 3：检索 worker
make dev-stamp      # 终端 4：盖戳 worker
make dev-relay      # 终端 5：outbox relay
```

## 服务端口速查

| 服务 | 端口 | 用途 |
|------|------|------|
| API | 8000 | 对外 HTTP 入口 |
| PostgreSQL | 5432 | 业务数据库 |
| Redis | 6379 | 任务队列 + 缓存 |
| Milvus | 19530 | 向量库 gRPC |
| SeaweedFS | 8333 | 对象存储 S3 网关 |
| Cerbos | 3592 | 权限服务 HTTP |

## 常用命令

```bash
# 查看基础设施状态
docker-compose -f docker-compose.infra.yml ps

# 查看具体服务日志
docker-compose -f docker-compose.infra.yml logs -f cerbos
docker-compose -f docker-compose.infra.yml logs -f milvus

# 手动测试 Cerbos 权限判定
curl -s http://localhost:3592/api/check/resources \
  -H "Content-Type: application/json" \
  -d '{
    "requestId": "test-001",
    "principal": {
      "id": "user:usr-reader-001",
      "roles": ["user"],
      "attr": {"tenant_id": "tenant-dev", "granted_actions": {"kb-001": ["read"]}}
    },
    "resources": [{
      "actions": ["doc:view"],
      "resource": {
        "kind": "document", "id": "doc-001",
        "attr": {"is_enabled": true, "retired": false, "allow_download": false}
      }
    }]
  }' | python3 -m json.tool

# 运行契约测试
pytest tests/contract/ -v

# 完全重置开发环境（删除所有数据，慎用）
make infra-reset
```

## 目录结构

```
├── docker-compose.infra.yml    # 基础设施（开发期常驻）
├── docker-compose.app.yml      # 计算层（上线部署使用）
├── .env                        # 环境变量（不进 Git）
├── Dockerfile                  # 容器镜像
├── requirements.txt
├── Makefile                    # 快捷命令
├── pipelines/                  # Haystack Pipeline YAML
├── cerbos/                     # Cerbos 权限策略
├── scripts/
│   └── init.sql                # 建表 SQL
├── src/                        # 源代码
└── tests/                      # 测试
```

## 上线部署

```bash
# 启动全栈
make deploy

# 更新代码后只重启计算层
make deploy-restart-app
```

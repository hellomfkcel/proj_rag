# 前端部署 SOP（Standard Operating Procedure）

> RAG v14 前端部署标准操作流程
> 更新日期：2026-07-29
> 适用范围：开发环境、测试环境、生产环境

---

## 部署模式总览

| 模式 | 适用环境 | 命令 | 说明 |
|------|---------|------|------|
| **模式 A：本地开发** | 开发 | `cd frontend && npm run dev` | 热更新，端口 3001，Next.js dev server 自带代理 |
| **模式 B：Docker 独立** | 测试/生产 | `docker-compose up -d frontend` | 仅构建前端容器，端口 3001 |
| **模式 C：Docker 全栈** | 测试/生产 | `docker-compose -f docker-compose.infra.yml -f docker-compose.app.yml up -d` | 全栈部署（nginx 统一入口 :80） |
| **模式 D：静态导出** | CDN 部署 | `cd frontend && npm run build && npm run export` | 纯静态文件，需要外部 nginx 反代 API |

---

## 模式 A：本地开发（推荐开发期使用）

```bash
# 前提：后端 API 在 localhost:8000 运行
#      基础设施在 docker-compose.infra.yml 运行中

cd frontend
npm install
npm run dev
# → 访问 http://localhost:3001
# → API 请求自动代理到 http://localhost:8000（通过 next.config.js rewrites）
```

**特点**：
- 代码修改即时热更新（HMR）
- 不需要构建 Docker 镜像
- 需要 Node.js 20+ 环境

---

## 模式 B：Docker 独立部署（仅前端容器化）

```bash
# 1. 构建前端镜像
cd frontend
docker build -t rag-v14-frontend .

# 2. 运行前端容器（后端 API 必须在同网络可达）
#    假设后端在宿主机 localhost:8000，使用 host 网络模式
docker run -d \
  --name rag-frontend \
  --network host \
  -e NODE_ENV=production \
  -e PORT=3001 \
  rag-v14-frontend

# 3. 验证
curl http://localhost:3001
```

**注意**：独立部署时，前端 API 请求通过 Next.js rewrites 代理。若后端地址不是 `localhost:8000`，需要在构建前修改 `next.config.js` 中的 `destination`。

---

## 模式 C：Docker 全栈部署（推荐生产环境）

```bash
# 1. 确保基础设施已启动
make infra
# 或：docker-compose -f docker-compose.infra.yml up -d

# 2. 初始化数据库（首次部署）
make db-init
make db-seed

# 3. 启动全栈应用
make deploy
# 或：docker-compose -f docker-compose.infra.yml -f docker-compose.app.yml up -d

# 4. 验证
curl http://localhost/healthz          # 后端健康检查
curl http://localhost                   # 前端页面
curl http://localhost:3001              # 前端直连（兼容）
```

**架构图**：
```
浏览器 → :80 (nginx)
           ├── /          → frontend:3001 (Next.js standalone)
           ├── /api/*     → api:8000      (FastAPI)
           └── /healthz   → api:8000
```

**服务清单**（全栈模式下共 11 个容器）：

| 服务 | 端口 | 来源 |
|------|------|------|
| nginx | 80 | `app.yml` |
| frontend | 3001 (内部) | `app.yml` |
| api | 8000 (内部) | `app.yml` |
| ingestion-worker | — | `app.yml` |
| retrieval-worker | — | `app.yml` |
| stamping-worker | — | `app.yml` |
| outbox-relay | — | `app.yml` |
| postgres | 25432 | `infra.yml` |
| redis | 16379 | `infra.yml` |
| milvus | 19530 | `infra.yml` |
| cerbos | 13592 | `infra.yml` |

---

## 模式 D：静态导出（用于 CDN / 对象存储）

```bash
# 1. 修改 next.config.js 添加 output: "export"
#    （注意：静态导出不支持 rewrites/SSR/ISR）

# 2. 构建导出
cd frontend
npm run build
# 输出到 out/ 目录

# 3. 上传到 CDN / S3 / Nginx 静态目录
# 需要额外配置 nginx 反代 API 请求到后端
```

**适用场景**：前端托管在 CDN，后端独立部署在不同域名。

**限制**：
- 不支持 Next.js rewrites（需要用 nginx 处理 API 代理）
- 不支持 Image Optimization
- 动态路由需要在 `next.config.js` 中配置 `generateStaticParams`

---

## 环境变量清单

### 前端容器环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `NODE_ENV` | `production` | 运行模式 |
| `PORT` | `3001` | 前端服务端口 |

### 后端传递给前端的配置（通过 `/api/v1/config`）

| 变量 | 说明 | 配置位置 |
|------|------|---------|
| `GRAFANA_URL` | Grafana 跳转链接 | `.env` |
| `LANGFUSE_PUBLIC_URL` | Langfuse 跳转链接 | `.env` |
| `CERBOS_PUBLIC_URL` | Cerbos 跳转链接 | `.env` |
| `ADMIN_CONSOLE_URL` | 管理台跳转链接 | `.env` |

---

## 常见问题

### Q1: 前端容器无法连接后端 API

**症状**：页面加载正常，但 API 请求失败（Network Error）

**原因**：Next.js standalone 模式的 rewrites 目标地址硬编码为构建时的值

**解决方案**：
1. **使用 nginx 模式**（推荐）：nginx 统一代理 `/api/*` 和前端，前端使用相对路径
2. **使用 host 网络**：`docker run --network host` 让容器直接访问宿主机 localhost
3. **构建时指定后端地址**：修改 `next.config.js` 中的 `destination` 为目标地址后重新构建

### Q2: CORS 错误

**症状**：浏览器控制台报 `Cross-Origin Request Blocked`

**解决方案**：
- 使用 nginx 统一入口（模式 C），所有请求同源，无需 CORS
- 或配置 `CORS_ALLOWED_ORIGINS` 环境变量包含前端域名

### Q3: SSE 流式输出断开

**症状**：对话页面流式输出中途断开

**解决方案**：
- 检查 nginx `proxy_read_timeout`（已设为 300s）
- 检查 `proxy_buffering off`（已关闭缓冲）
- 检查后端 API 的流式超时配置

### Q4: 构建镜像时内存不足

**症状**：`docker build` 失败，OOM

**解决方案**：
- Next.js 构建需要 ~2GB 内存
- 增加 Docker 内存限制
- 或在宿主机先 `npm run build`，然后只复制 `.next/standalone` 到镜像

---

## 部署检查清单

部署前逐项检查：

- [ ] 基础设施全部健康：`docker-compose -f docker-compose.infra.yml ps`（所有服务 healthy）
- [ ] 数据库已初始化：表结构存在（`\dt` 看到 16 张表）
- [ ] 种子数据已写入：`knowledge_bases` 表有数据
- [ ] Cerbos 策略已加载：`curl http://localhost:13592/api/policies` 返回策略列表
- [ ] Milvus collection 已创建：`rag_documents` 存在
- [ ] 前端已构建：`frontend/.next/standalone` 存在（或 Docker 镜像构建成功）
- [ ] nginx 配置已更新：`frontend/nginx.conf` 中 upstream 地址正确
- [ ] 环境变量已配置：`.env` 文件中所有必填项已填写
- [ ] 端口不冲突：80 / 8000 / 3001 未被其他进程占用
- [ ] 日志收集已配置：OTel Collector 地址已填写（如适用）

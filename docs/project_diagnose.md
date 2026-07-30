# RAG v14 项目诊断报告

> 诊断日期：2026-07-29
> 对照基准：`docs/RAG系统设计v14.md` + `docs/frontend-design.md`
> 诊断范围：前端 `frontend/` + 后端 `src/`

---

## 总体评估

| 维度 | 完成度 | 评级 |
|------|--------|------|
| **前端页面/组件** | ~90% | 🟢 功能基本完整 |
| **前端技术栈合规** | ~60% | 🟡 核心库未安装 |
| **后端 REST 端点** | ~95% (50/53) | 🟢 端点齐全 |
| **后端核心架构** | ~30% | 🔴 关键设计被绕过 |

---

## 一、前端诊断

### 1.1 已完整实现（13 个功能域）

| # | 功能域 | 涉及文件 |
|---|--------|---------|
| 1 | 登录/登出流程（开发模式） | `login/page.tsx`, `stores/useAuthStore.ts`, `lib/auth.ts` |
| 2 | 租户选择页 | `select-tenant/page.tsx` |
| 3 | 全局 Header（KB 选择器 + 租户切换 + 用户信息 + 退出） | `components/Header.tsx`, `components/KBList.tsx` |
| 4 | KB 管理页 — 两栏布局 + 文档表格 | `kb/page.tsx`, `components/DocTable.tsx` |
| 5 | 目录树（kb_bound + manual 两种类型） | `components/DirTree.tsx` |
| 6 | 文档上传区（拖拽 + 格式/大小校验） | `components/UploadZone.tsx` |
| 7 | 文档预览面板（右侧滑出，chunks + 原文） | `components/DocPreviewSheet.tsx` |
| 8 | 对话页（会话列表 + SSE 流式 + 来源卡片 + 输入栏） | `chat/page.tsx`, `components/MessageList.tsx`, `components/InputBar.tsx`, `components/SourcesCard.tsx`, `components/ConvList.tsx` |
| 9 | 设置页（模型管理 + 检索参数 + 切分配置 + Prompt 管理） | `settings/page.tsx` |
| 10 | Dashboard（使用统计 + Top KB + 文档处理状态 + 检索质量） | `dashboard/page.tsx` |
| 11 | 路由保护（AuthGuard + 会话计时器 + 无活动超时） | `components/AuthGuard.tsx` |
| 12 | 全局错误处理（Axios 拦截器 + ErrorBoundary + Toast 通知） | `lib/api.ts`, `components/ErrorBoundary.tsx`, `components/Toast.tsx` |
| 13 | 管理台跳转入口（设置页 / KB 页 / 403 页，三处全部实现） | `settings/page.tsx`, `kb/page.tsx`, `not-authorized/page.tsx` |

### 1.2 部分实现（4 项）

| # | 项 | 现状 | 设计预期 |
|---|----|------|---------|
| 1 | 上传进度条 | 仅显示"待处理/上传中/成功/错误"状态 Badge | 应使用 `axios.onUploadProgress` 展示实时百分比进度条 |
| 2 | 上传失败重试 | 失败文件无重试按钮，需重新拖拽 | 失败文件上应有"重试"按钮，保留在队列中供重试 |
| 3 | 文档轮询机制 | `setInterval` 每 5 秒全量刷新 | TanStack Query 每 3 秒自动 refetch + 缓存 |
| 4 | 检索模式选择器 | `RConfig` 接口定义了 `retrieval_mode` 字段，但设置页无对应 UI 控件 | 应有 hybrid / sparse / dense 三种模式的下拉选择 |

### 1.3 缺失（3 项）

| # | 项 | 影响 | 说明 |
|---|----|------|------|
| 1 | `/auth/callback` SSO 回调页 | 🔴 生产模式 SSO 登录链路断裂 | IdP 回调后会 404，`exchangeCode()` 函数已写好但无处调用 |
| 2 | `lib/permissions.ts` | 🟡 Cerbos check 辅助函数缺失 | `frontend_implement.md` 规划了此文件，用于前端权限判断 |
| 3 | Recharts 图表库 | 🟡 Dashboard 使用手写 CSS 柱状图 | 设计文档和实施方案均指定 Recharts（LineChart / BarChart / PieChart） |

### 1.4 技术栈偏差（5 个库未安装）

| 设计指定的库 | 实际方案 | 功能等效？ | 说明 |
|-------------|---------|-----------|------|
| `@tanstack/react-query` | 原生 `useEffect` + `setInterval` | ⚠️ 等效但缺缓存 | 无自动缓存、无 dedup、无 window focus refetch |
| `recharts` | 手写 CSS 柱状图/条形图 | ❌ 不等效 | 无 tooltip、无 legend 交互、无响应式 |
| `ai`（Vercel AI SDK `useChat`） | 原生 `EventSource` | ✅ 等效 | SSE 消费逻辑手工实现但功能完整 |
| `@dnd-kit/core` | 原生 HTML5 Drag & Drop API | ✅ 等效 | 目录树拖放功能正常 |
| `shadcn/ui` 组件 | 手写 Tailwind 组件 | ⚠️ 样式匹配 | 复用性差，无无障碍保证 |

### 1.5 当前路由表

| 路由 | 页面 | 需登录 | 状态 |
|------|------|--------|------|
| `/` | 重定向到 `/kb` 或 `/login` | — | ✅ |
| `/login` | 登录页（开发模式 + SSO 入口） | 否 | ✅ |
| `/select-tenant` | 多租户选择页 | 是 | ✅ |
| `/kb` | 知识库管理页 | 是 | ✅ |
| `/chat` | 对话页 | 是 | ✅ |
| `/settings` | 设置页 | 是 | ✅ |
| `/dashboard` | Dashboard 统计页 | 是 | ✅ |
| `/not-authorized` | 403 权限不足页 | 是 | ✅ |
| `/auth/callback` | SSO 回调处理 | — | ❌ 缺失 |

---

## 二、后端诊断

### 2.1 REST 端点：50/53 已实现

全部 8 个路由文件正常注册，端点清单如下：

#### 认证与租户（4/5 实现，1 个 501 桩）

| # | 端点 | 方法 | 状态 |
|---|------|------|------|
| 1 | `/api/v1/auth/dev-login` | POST | ✅ 开发模式 JWT 签发 |
| 2 | `/api/v1/auth/token` | POST | ⚠️ 501 — 生产 OAuth2 code 换 token |
| 3 | `/api/v1/auth/refresh` | POST | ⚠️ 501 — 生产 refresh_token 换新 JWT |
| 4 | `/api/v1/tenants` | GET | ✅ |
| 5 | `/api/v1/tenants/{id}/stats` | GET | ✅ |

#### KB 管理（5/5）

| # | 端点 | 方法 | 状态 |
|---|------|------|------|
| 6 | `/api/v1/knowledge-bases` | GET | ✅ |
| 7 | `/api/v1/knowledge-bases` | POST | ✅ |
| 8 | `/api/v1/knowledge-bases/{id}` | PATCH | ✅ |
| 9 | `/api/v1/knowledge-bases/{id}` | DELETE | ✅ |
| 10 | `/api/v1/knowledge-bases/{id}/chunking-config` | GET + PATCH | ✅ |

#### 目录管理（6/6）

| # | 端点 | 方法 | 状态 |
|---|------|------|------|
| 11 | `/api/v1/knowledge-bases/{kb_id}/directories` | GET | ✅ |
| 12 | `/api/v1/directories` | POST | ✅ |
| 13 | `/api/v1/directories/{id}` | PATCH | ✅ |
| 14 | `/api/v1/directories/{id}` | DELETE | ✅ |
| 15 | `/api/v1/directories/{dir_id}/documents` | POST | ✅ |
| 16 | `/api/v1/directories/{dir_id}/documents/{doc_id}` | DELETE | ✅ |

#### 文档管理（12/12）

| # | 端点 | 方法 | 状态 |
|---|------|------|------|
| 17 | `/api/v1/knowledge-bases/{kb_id}/documents` | GET | ✅ 支持 search/status/sort/order |
| 18 | `/api/v1/documents/{doc_id}` | GET | ✅ |
| 19 | `/api/v1/documents/{doc_id}` | PATCH | ✅ 文件重命名 |
| 20 | `/api/v1/documents/{doc_id}/chunks` | GET | ✅ Milvus 查询 |
| 21 | `/api/v1/documents/{doc_id}/content` | GET | ✅ P-STORE 读取 |
| 22 | `/api/v1/documents/{doc_id}/download` | GET | ✅ 本地文件 / S3 签名 URL |
| 23 | `/api/v1/documents/upload` | POST | ✅ |
| 24 | `/api/v1/documents/{doc_id}/kb/{kb_id}` | DELETE | ✅ |
| 25 | `/api/v1/documents/{doc_id}/kb/{kb_id}` | PATCH | ✅ 启用/停用 |
| 26 | `/api/v1/documents/{doc_id}/trigger-parse` | POST | ✅ |
| 27 | `/api/v1/documents/batch/delete` | POST | ✅ ThreadPoolExecutor 并发 |
| 28 | `/api/v1/documents/batch/parse` | POST | ✅ |

#### 会话（5/5）

| # | 端点 | 方法 | 状态 |
|---|------|------|------|
| 29 | `/api/v1/conversations` | GET | ✅ |
| 30 | `/api/v1/conversations` | POST | ✅ |
| 31 | `/api/v1/conversations/{id}` | DELETE | ✅ |
| — | `/api/v1/conversations/query` | POST | ✅ |
| — | `/api/v1/conversations/{id}/stream` | GET | ✅ SSE |
| — | `/api/v1/conversations/{id}/turns` | GET | ✅ |

#### 设置与统计（8/8，1 个半桩）

| # | 端点 | 方法 | 状态 |
|---|------|------|------|
| 32 | `/api/v1/models` | GET | ✅ |
| — | `/api/v1/models/{id}/set-default` | PATCH | ✅ |
| 33 | `/api/v1/configs/retrieval` | GET + PATCH | ✅ |
| 34 | `/api/v1/prompts` | GET | ✅ |
| — | `/api/v1/prompts/{id}/activate` | PATCH | ✅ |
| — | `/api/v1/config` | GET | ✅ 外部服务 URL |
| — | `/api/v1/stats/usage` | GET | ✅ |
| — | `/api/v1/stats/top-kbs` | GET | ✅ |
| — | `/api/v1/stats/documents` | GET | ✅ |
| — | `/api/v1/stats/quality` | GET | ⚠️ 半桩 — `metrics/baseline.json` 缺失时返回硬编码值 |

#### 生产模式（1/4，3 个 501 桩）

| # | 端点 | 方法 | 状态 |
|---|------|------|------|
| — | `/api/v1/auth/token` | POST | ⚠️ 501 |
| — | `/api/v1/auth/refresh` | POST | ⚠️ 501 |
| — | `/api/v1/auth/callback` | GET | ⚠️ 501 |
| — | `/api/v1/auth/dev-login` | POST | ✅ 开发模式可用 |

### 2.2 平台模块完成度

| 模块 | 文件 | 完成度 | 关键问题 |
|------|------|--------|---------|
| **P-AUTHC** | `src/permission/` | ⚠️ 40% | 权限判定查本地 DB 模拟，未真正调用外部 Cerbos PDP |
| **P-AUDIT** | `src/platform/audit/` | 🟢 85% | 审计日志落库正常，缺部分指标的 OTLP 导出 |
| **P-OBS** | `src/platform/obs/` | 🟢 75% | OTel 初始化正常，metrics 仅内存存储无 OTLP 导出 |
| **P-TASK** | `src/platform/task/` | 🟢 80% | Celery 三队列正常，`pipeline_runner.py` 存在但未被使用 |
| **P-STORE** | `src/platform/store/` | 🟡 70% | S3 后端正常，`service.py` 文件缺失（`kb_routes.py` 引用失败） |
| **P-MODEL** | `src/platform/model/` | 🟡 50% | 功能正常但绕过 Haystack，直调 OpenAI SDK / requests |
| **P-CONFIG** | `src/platform/config/` | 🟢 85% | 4 层级联解析正常，feature flags 硬编码 |

### 2.3 业务模块完成度

| 模块 | 完成度 | 关键问题 |
|------|--------|---------|
| **B-DOC** | 🟡 60% | 功能正常，但生命周期端口调用只记日志未实际调用权限服务 |
| **B-INGEST** | 🟢 75% | 最强模块，盖戳 6 条纪律全部到位，但绕过 Haystack Pipeline 直写 Milvus |
| **B-RETRIEVE** | 🔴 30% | L1 缺 2/6 条件，L2 过采样正常，L3 strict 未实现，无混合检索，无 Haystack Pipeline |
| **B-CHAT** | 🟡 55% | SSE 流式正常，但绕过 Haystack 的 PromptBuilder+LiteLLMGenerator，直接调 `invoke_llm()` |

---

## 三、严重架构偏离（5 项，必须修复）

### 🔴 #1：权限服务是本地模拟的，不是外置的

**文件**：`src/permission/cerbos_client.py`

`CerbosClient` 的所有权限判定（check/batch/filter/prefilter/visibility）直查本地 PostgreSQL 的 `resource_registry` 和 `mount_registry` 表来模拟。没有调用外部 Cerbos PDP 的 HTTP API。

**影响**：整个 "权限外置" 架构的核心前提不成立。系统内部存储并评估自己的 ACL 数据，违反 CLAUDE.md 第 0 条红线。

**修复方向**：将 `CerbosClient` 改为真正调用 Cerbos PDP 的 `POST /v1/check`、`POST /v1/filter`、`POST /v1/prefilter`、`POST /v1/visibility` 等端点。`resource_registry` 和 `mount_registry` 表应只存在于 Cerbos 侧，本系统侧通过生命周期端口同步。

### 🔴 #2：Haystack 2.x Pipeline 框架完全被绕过

**涉及文件**：`src/retrieve/service.py`、`src/chat/service.py`、`src/api/routes.py`（摄入）、`src/api/kb_routes.py`（`_run_ingestion_pipeline`）

- 检索：直接调 `pymilvus.Collection.search()`，没用 Haystack `MilvusEmbeddingRetriever`
- 生成：直接调 `openai.OpenAI.chat.completions.create()`，没用 `PromptBuilder` + `LiteLLMGenerator`
- 摄入：直接操作 Milvus，没用 Haystack `DocumentSplitter` + `SentenceTransformersDocumentEmbedder` + `MilvusDocumentStore`

**影响**：设计文档 v14 的标题就是 "Haystack 框架版"，Haystack 要扛 Pipeline 编排、组件协议、OTel 埋点、向量库适配器 —— 现在全没用上。

**修复方向**：
1. 将 B-INGEST 重构为 Haystack 摄入 Pipeline（DocumentSplitter → Embedder → MilvusDocumentStore）
2. 将 B-RETRIEVE 重构为 Haystack 查询 Pipeline（Embedder → MilvusRetriever + BM25Retriever → DocumentJoiner(RRF) → Ranker）
3. 将 B-CHAT 生成步骤重构为 Haystack 生成 Pipeline（PromptBuilder → LiteLLMGenerator）

### 🔴 #3：L1 prefilter 缺少 2/6 过滤条件

**文件**：`src/permission/authz.py` 的 `compile_filter()` 函数（约 line 270-281）

当前只编译出 4 个条件：`tenant_id`、`kb_id`、`retrievable`、`vis_version > 0`。

缺少：
- **条件 3**：`allow_stamps MATCH_ANY`（允许该用户所属的任一 stamp 值）
- **条件 4**：`deny_stamps MUST_NOT`（排除被拒绝的 stamp 值）

**影响**：盖戳管道写入的 `allow_stamps`/`deny_stamps` 字段在检索时完全没被使用，等于白写。权限过滤仅依赖 `tenant_id` + `kb_id` + `retrievable`，粒度太粗。

### 🔴 #4：L3 strict 逐条复核未实现

**文件**：`src/retrieve/service.py` 的 `retrieve()` 函数

函数签名接受 `strict: bool` 参数但不做任何事。设计要求的"对 strict KB 调用 `/v1/filter` 逐条复核，批次 ≤200，失败整批拒绝"完全没有实现。

**影响**：`strict=true` 的敏感 KB 和普通 KB 行为完全一样，无法对高密级文档做实时权限复核。

### 🔴 #5：生命周期端口调用只记日志

**文件**：`src/doc/service.py`（`submit_ingest_task`、`delete_document_from_kb` 等函数）

设计要求的同步调用 `register_resource` / `link_resource` / `unlink_resource` / `retire_resource` 在文档操作路径中被替换为 `log.info()`。只有 KB 创建/删除路径（`kb_routes.py`）正确调用了这些端口。

**影响**：权限服务的结构镜像（resource mirror）与实际资源不同步。文档挂载/卸载后权限服务不知道资源变化。

---

## 四、功能缺口（6 项，建议修复）

| # | 问题 | 影响模块 | 严重性 |
|---|------|---------|--------|
| 6 | 摄入在 API 进程 daemon thread 里跑，没走 Celery Worker | B-INGEST | 🟡 违反"API 进程禁止调 pipeline.run()"规则 |
| 7 | 没有混合检索（dense + sparse BM25 + RRF 融合） | B-RETRIEVE | 🟡 只有 dense 向量检索，设计有 BM25 稀疏路径 |
| 8 | `src/platform/store/service.py` 文件缺失 | P-STORE | 🟡 `kb_routes.py:370` 引用 `read_file` 会 ImportError |
| 9 | 没有 `HierarchicalMerger` 父子 chunk 合并组件 | B-RETRIEVE | 🔵 小 chunk 检索后需合并回父 chunk 提供上下文 |
| 10 | 检索 Pipeline YAML 文件不存在 | P-TASK | 🔵 `pipeline_runner.py` 有加载函数但 `./pipelines/` 目录无 YAML |
| 11 | 三个 synthesis 模式（compact/refine/tree_summarize）未实现 | B-CHAT | 🔵 当前只硬编码了一种模板 |

---

## 五、修复优先级建议

### P0 — 架构红线（必须修，否则偏离设计意图）

| 序号 | 问题 | 工作量估计 |
|------|------|-----------|
| #1 | 权限服务改为真正调用外部 Cerbos PDP | 大（3-5 天） |
| #2 | B-RETRIEVE 重构为 Haystack 查询 Pipeline | 大（3-5 天） |
| #3 | L1 compile_filter 补齐 6 条件 | 中（1-2 天） |
| #4 | L3 strict 逐条复核 | 中（1-2 天） |
| #5 | 生命周期端口从 log 改为实际调用 | 小（0.5 天） |

### P1 — 功能缺口（影响完整体验）

| 序号 | 问题 | 工作量估计 |
|------|------|-----------|
| #7 | 混合检索 dense + sparse + RRF | 中（2-3 天） |
| #8 | 修复 `store/service.py` 缺失 | 小（0.5 天） |
| #6 | 摄入改为走 Celery Worker | 小（0.5 天） |
| 前端缺失 1 | `/auth/callback` SSO 回调页 | 小（0.5 天） |
| 前端缺失 2 | `lib/permissions.ts` | 小（0.5 天） |
| 前端缺失 3 | Recharts 替换手写图表 | 小（0.5 天） |

### P2 — 技术栈合规（渐进改进）

| 序号 | 问题 | 工作量估计 |
|------|------|-----------|
| — | 安装 TanStack Query 替代 setInterval | 中（1 天） |
| — | 安装 @dnd-kit 替代原生 DnD | 小（0.5 天） |
| — | 安装 shadcn/ui 组件库 | 小（0.5 天） |
| — | 上传进度条 + 重试按钮 | 小（0.5 天） |
| #11 | 三个 synthesis 模式 | 中（1-2 天） |
| #9 | HierarchicalMerger | 中（1 天） |

---

## 六、文件对照索引

| 设计章节 | 实现文件 | 完成度 |
|---------|---------|--------|
| §0 认证与租户 | `frontend/app/login/`, `select-tenant/`, `stores/useAuthStore.ts`, `src/api/auth.py` | 🟢 |
| §1 KB 选择器 | `frontend/app/components/Header.tsx`, `KBList.tsx` | 🟢 |
| §3.1 知识库管理页 | `frontend/app/kb/`, `components/DocTable.tsx`, `UploadZone.tsx`, `DirTree.tsx` | 🟢 |
| §3.2 组件树 | (同上) | 🟢 |
| §3.3 对话页 | `frontend/app/chat/`, `components/MessageList.tsx`, `InputBar.tsx`, `SourcesCard.tsx`, `ConvList.tsx` | 🟢 |
| §3.4 设置页 | `frontend/app/settings/`, `src/api/settings_routes.py` | 🟢 |
| §3.5 文档搜索/筛选/排序 | `frontend/app/components/DocTable.tsx`, `src/api/kb_routes.py:260-301` | 🟢 |
| §3.6 降级与错误处理 | `frontend/app/components/ErrorBoundary.tsx`, `Toast.tsx`, `lib/api.ts` | 🟢 |
| §3.7 管理台跳转 | `settings/page.tsx`, `kb/page.tsx`, `not-authorized/page.tsx` | 🟢 |
| §3.8 文件上传 | `frontend/app/components/UploadZone.tsx` | 🟢 |
| §3.9 Prompt 预览 | `frontend/app/settings/page.tsx` (lines 282-312) | 🟢 |
| §3.10 Dashboard | `frontend/app/dashboard/`, `src/api/dashboard_routes.py` | 🟡 (缺 Recharts) |
| §4 REST 端点汇总 | `src/api/*.py` (8 个路由文件, 50/53 端点) | 🟢 |
| §6 P-AUTHC | `src/permission/` | 🔴 (模拟实现) |
| §7 P-AUDIT | `src/platform/audit/` | 🟢 |
| §8 P-OBS | `src/platform/obs/` | 🟢 |
| §9 P-TASK | `src/platform/task/` | 🟢 |
| §10 P-STORE | `src/platform/store/` | 🟡 (`service.py` 缺失) |
| §11 P-MODEL | `src/platform/model/` | 🟡 (绕过 Haystack) |
| §12 P-CONFIG | `src/platform/config/` | 🟢 |
| §13 B-DOC | `src/doc/` | 🟡 (生命周期端口记日志) |
| §14 B-INGEST | `src/ingest/` | 🟢 (盖戳纪律完整) |
| §15 B-RETRIEVE | `src/retrieve/` | 🔴 (L1 缺条件, L3 未实现) |
| §16 B-CHAT | `src/chat/` | 🟡 (绕过 Haystack 生成) |

---

## 七、环境与运行状态（2026-07-29 验证通过）

| 层 | 服务 | 端口 | 状态 |
|----|------|------|------|
| 基础设施 | PostgreSQL 16 | 25432 | ✅ healthy |
| | Redis 7 | 16379 | ✅ healthy |
| | Milvus 2.4.13 | 19530 | ✅ healthy |
| | SeaweedFS 3.68 | 18333 | ✅ healthy |
| | Cerbos 0.39.0 | 13592 | ✅ healthy |
| 后端 | FastAPI (uvicorn --reload) | 8000 | ✅ 运行中 |
| | Ingestion Worker (Celery) | — | ✅ ready |
| | Retrieval Worker (Celery) | — | ✅ ready |
| | Stamping Worker (Celery) | — | ✅ ready |
| | Outbox Relay | — | ✅ 轮询中 |
| 前端 | Next.js 14 | 3001 | ✅ 运行中 |
| 观测 | Grafana 11 | 3000 | ✅ |
| | OTel Collector | 4317/4318 | ✅ |
| | Langfuse 3 | 13000 | ✅ |

### 启动命令速查

```bash
# 基础设施
docker compose -f docker-compose.infra.yml up -d

# 后端（4 个终端窗口）
conda activate rag_dev_v14
make dev-api        # FastAPI :8000
make dev-ingest     # 摄入 Worker
make dev-retrieve   # 检索 Worker
make dev-stamp      # 盖戳 Worker
make dev-relay      # Outbox Relay

# 前端
cd frontend && npx next dev -p 3001
```

### 已知代码修复

- `src/platform/task/celery_app.py`：`task_queues` 从 `dict` 改为 `kombu.Queue` 对象，兼容 Celery 5.4+

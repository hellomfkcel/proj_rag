# 前端实施诊断报告

> 对照 `docs/frontend-design.md` 逐项审计当前前端实现，标注完成状态与缺口。
> 审计日期：2026-07-28

---

## 总览

| 模块 | 状态 | 完整度 |
|------|------|--------|
| 〇 认证与租户 | ⚠️ 部分实现 | 40% |
| 一 全局 KB 选择器 | ⚠️ 基本可用 | 70% |
| 二 页面路由结构 | ✅ 完成 | 90% |
| 三.0 KB 生命周期管理 | ⚠️ 骨架完成 | 65% |
| 三.1~3.2 知识库管理页 | ⚠️ 骨架完整 / 交互缺失 | 50% |
| 三.3 对话页 | ⚠️ 可跑 / 缺细节 | 55% |
| 三.4 设置页 | ⚠️ 只读 / 缺交互 | 45% |
| 三.5 文档搜索/筛选/排序 | ❌ 未实现 | 0% |
| 三.6 Dashboard | ⚠️ 基础完成 | 60% |
| 三.7 降级与错误处理 | ❌ 未实现 | 15% |
| 三.8 管理台跳转入口 | ❌ 未实现 | 10% |

---

## 〇、认证与租户 — 8 项缺失

| # | 设计要求 | 实现状态 | 严重程度 |
|---|---------|---------|---------|
| 1 | **生产模式 SSO** — 设计 §0 要求 `/auth/callback` 回调 + `POST /api/v1/auth/token` 换取 JWT | ❌ 登录页只有开发模式，"通过 Keycloak 登录"仅一行静态文字 | 🔴 |
| 2 | **自动 token 刷新** — 设计要求 "过期前 5 分钟 `POST /api/v1/auth/refresh`" | ❌ 开发模式不刷新可以理解，但完全没有 refresh 机制 | 🟡 |
| 3 | **会话超时检测** — "Zustand store 记录 `lastActivity`，每 30 秒检查，超 1 小时清 token" | ❌ 完全没有实现；`useAuthStore` 只解析 JWT exp 字段 | 🟡 |
| 4 | **`/select-tenant` 多租户选择页** — 设计有完整页面：列表展示 KB 数+文档数 | ❌ 完全没有；登录后直接跳到 `/kb` | 🔴 |
| 5 | **`POST /api/v1/auth/dev-login` 动态租户列表** — 设计要求下拉选择租户 | ⚠️ 租户下拉硬编码 `["tenant-dev"]`，角色也只有 2 个 | 🟡 |
| 6 | **Header 租户切换** — 设计要求 "租户名右侧下拉箭头 → 切换租户" | ❌ Header 中 `tenant_id` 只是静态 Badge 展示，无切换功能 | 🟡 |
| 7 | **AuthGuard 未全局启用** — 组件已存在但 `layout.tsx` 没有包裹 | ⚠️ `AuthGuard.tsx` 写了但没在 `layout.tsx` 中使用；每个页面自己重复写守卫逻辑 | 🟡 |
| 8 | **403 处理** — `api.ts` interceptor 只处理 401，不处理 403 | ❌ 设计要求 `403 → Toast "权限不足"` | 🟡 |

### 影响文件

| 文件 | 当前问题 |
|------|---------|
| `frontend/app/login/page.tsx` | 硬编码租户列表 `["tenant-dev"]`，无 SSO 回调逻辑 |
| `frontend/stores/useAuthStore.ts` | 无 `lastActivity`、无自动登出定时器 |
| `frontend/lib/api.ts` | interceptor 缺 403 处理、缺全局 Toast |
| `frontend/app/components/Header.tsx` | 租户仅静态 Badge，无可切换下拉 |
| `frontend/app/layout.tsx` | 未包裹 `AuthGuard` |
| `frontend/app/components/AuthGuard.tsx` | 已写但未被使用 |

### 缺失页面

- `/select-tenant` — 多租户选择页，完全未创建

---

## 一、全局 KB 选择器 — 3 项缺失

| # | 设计要求 | 实现状态 | 严重程度 |
|---|---------|---------|---------|
| 9 | **KB 选择器在全局 Header 中**（设计 "不是某个页面内部组件"） | ⚠️ 实际 `KBList` 是 `/kb` 页面内的下拉组件，不在 Header；对话页用的是 `useKBStore` 里的选中值但没有 KB 下拉 | 🔴 |
| 10 | **多选能力** — "对话场景选多个 KB 做交叉检索" | ❌ `useKBStore` 只有 `selectedKB: string \| null`，对话页也只发一个 KB ID 的数组（`const kbIds = [selectedKB!]`） | 🔴 |
| 11 | **持久化到 URL query params** — "`?kb_ids=xxx,yyy`" | ❌ 完全没有；仅内存 Zustand store | 🟡 |

### 影响文件

| 文件 | 需改动 |
|------|--------|
| `frontend/stores/useKBStore.ts` | `selectedKB: string \| null` → `selectedKBs: string[]` |
| `frontend/app/components/Header.tsx` | 移入 KB 选择器下拉（多选） |
| `frontend/app/kb/page.tsx` | 移除页面内 KBList，改用 Header 中的 |

---

## 三.0 KB 生命周期管理 — 3 项缺失

| # | 设计要求 | 实现状态 | 严重程度 |
|---|---------|---------|---------|
| 12 | **创建 KB 时选择切分策略** — 设计：Dialog 含 `chunking_strategy` 选择 (sentence/word/passage) | ❌ 当前 `createKB` 只传 `{name, description}`，无切分配置 | 🟡 |
| 13 | **更新 KB 切分配置** — `PATCH /kb/{id}/chunking-config` | ❌ 后端无此端点，前端也无 UI | 🟡 |
| 14 | **删除 KB 前检查活跃文档** — 设计：409 提示 | ⚠️ 前端无 409 处理，直接 `confirm` 后调 API | 🟢 |

### 影响文件

| 文件 | 需改动 |
|------|--------|
| `frontend/app/kb/page.tsx` | 创建 Dialog 加 `chunking_strategy` 字段；handleDeleteKB 加 409 处理 |
| `frontend/lib/kb.ts` | `createKB` 加 `chunking_strategy` 参数 |

---

## 三.1~3.2 知识库管理页 — 8 项缺失

| # | 设计要求 | 实现状态 | 严重程度 |
|---|---------|---------|---------|
| 15 | **文档搜索** — "搜索栏 debounce 300ms → `?search=xxx`" | ❌ `DocTable` 没有搜索输入框，没有 debounce | 🔴 |
| 16 | **状态筛选** — "Dropdown: 全部/已完成/处理中/队列中/失败/未解析/已停用" | ❌ 没有状态筛选器 | 🔴 |
| 17 | **排序** — "按上传时间/文件名/文件大小 升序/降序" | ❌ 表格无排序功能 | 🟡 |
| 18 | **文件下载** — "📥 图标 → `GET /documents/{id}/download`" | ❌ DocTable 操作列无下载按钮 | 🟡 |
| 19 | **文档预览 Sheet** — 右侧滑出显示 Chunk 内容 | ❌ 完全没实现（设计文档 §3.1 底部 "文档预览 Sheet"） | 🟡 |
| 20 | **文件移动/拖拽到目录** — "拖拽文件到左侧目录树" | ❌ 没实现拖拽；有 `moveDocToDir` API 函数但 UI 没调 | 🟡 |
| 21 | **批量移动** — "选中多个文件 → 批量移动到目录" | ❌ 工具栏缺 [批量移动] 按钮 | 🟡 |
| 22 | **TanStack Query 轮询** — "每 3 秒自动 refresh" | ⚠️ 当前用 `setInterval(fetchDocs, 5000)` 手动轮询，没用 TanStack Query | 🟢 |

### 影响文件

| 文件 | 需改动 |
|------|--------|
| `frontend/app/components/DocTable.tsx` | 加搜索栏、状态筛选、排序列、下载按钮、[批量移动] 按钮 |
| `frontend/app/kb/page.tsx` | 引入 TanStack Query 替代 setInterval |
| `frontend/lib/kb.ts` | `listDocuments` 加 `search`/`status`/`sort_by`/`order` 参数 |
| 新建 `DocPreviewSheet.tsx` | 右侧滑出 Chunk 预览面板 |
| `frontend/app/components/DirTree.tsx` | 加拖拽 drop target |

---

## 三.3 对话页 — 5 项缺失

| # | 设计要求 | 实现状态 | 严重程度 |
|---|---------|---------|---------|
| 23 | **流式引用原文** — "`retrieved` 事件多传 `chunks: [{id, content: 前200字}]`" | ❌ SourceCard 用 `document_id` 而非原文内容展示；`handleSend` fallback 写死 `content: "[来源]"` | 🔴 |
| 24 | **SSE error 事件处理** — 设计 §3.6 要求处理 `chat:stream_timeout` | ⚠️ `evtSource.onerror` 只 close 不解析错误码，无重试 UI | 🟡 |
| 25 | **对话重命名** — ConvList 只显示 "新会话" 或 title | ⚠️ 无内联重命名功能 | 🟢 |
| 26 | **InputBar textarea 而非 input** — 设计用的是 Textarea（支持多行） | ⚠️ 当前是 `<input type="text">` | 🟢 |
| 27 | **引用卡片 HoverCard** — 设计 "HoverCard 显示 chunk 原文" | ⚠️ SourcesCard 用了 `onMouseEnter` 弹出 div，功能有但非 shadcn HoverCard | 🟢 |

### 影响文件

| 文件 | 需改动 |
|------|--------|
| `frontend/app/chat/page.tsx` | `retrieved` 事件解析 `chunks[].content`；加 SSE error 事件处理 + 重试按钮 |
| `src/retrieve/` (后端) | `retrieved` 流事件加 `chunks: [{id, content}]` |
| `frontend/app/components/InputBar.tsx` | `<input>` → `<textarea>` |
| `frontend/app/components/ConvList.tsx` | 加内联重命名 |
| `frontend/app/components/SourcesCard.tsx` | 改用 shadcn HoverCard |

---

## 三.4 设置页 — 4 项缺失

| # | 设计要求 | 实现状态 | 严重程度 |
|---|---------|---------|---------|
| 28 | **Prompt 预览面板** — "选中模板后显示渲染预览（Jinja2 近似渲染）" | ❌ 没有；只显示模板 ID + version | 🟡 |
| 29 | **Prompt 切换** — 设计: 下拉选择 prompt_id + version | ⚠️ 当前只是列表展示 + "激活" 按钮，没有下拉选择 | 🟡 |
| 30 | **模型切换** — "从 DB model_registry 读，Select 切换" | ⚠️ 只做了列表展示，没有 Select 切换默认模型的交互 | 🟡 |
| 31 | **动态外部链接** — Langfuse / Cerbos / Grafana 从 API 获取 | ✅ 已修复（2026-07-28） | ✅ |

### 影响文件

| 文件 | 需改动 |
|------|--------|
| `frontend/app/settings/page.tsx` | 加 Prompt 预览面板、模型切换 Select、Prompt 版本下拉 |
| `frontend/lib/settings.ts` | 加 `setDefaultModel`、`switchPromptVersion` API |

---

## 三.5 文档搜索 / 筛选 / 排序 — 1 项缺失（后端）

| # | 设计要求 | 实现状态 | 严重程度 |
|---|---------|---------|---------|
| 43 | 后端 `GET /kb/{kb_id}/documents?search=&status=&sort_by=&order=` 加 query params | ❌ `listDocuments(kbId)` 没有传这些参数，后端也未实现 | 🟡 |

---

## 三.6 Dashboard — 无严重缺口

当前实现已覆盖使用统计柱状图、活跃 KB 排行、文档状态、检索质量指标、外部工具链接。外部链接已改为动态配置 ✅。

---

## 三.7 降级与错误处理 — 6 项缺失

| # | 设计要求 | 实现状态 | 严重程度 |
|---|---------|---------|---------|
| 32 | **`retrieve:insufficient_evidence`** — "未找到足够信息"（普通文本） | ❌ 无区分处理，所有错误统一显示 `err.message` | 🟡 |
| 33 | **`auth:authz_unavailable`** — "⚠️ 警告样式 + 重试按钮" | ❌ 无区分处理 | 🟡 |
| 34 | **`retrieve:vector_store_unavailable`** — "⚠️ 警告 + 自动重试倒计时" | ❌ 无区分处理 | 🟡 |
| 35 | **网络错误/超时 Toast** — "请求失败，点击重试" | ❌ 无全局 Toast 组件 | 🟡 |
| 36 | **React Error Boundary** — "包裹每个页面" | ❌ 没有 Error Boundary | 🔴 |
| 37 | **DataTable Loading/Empty/Error 三态** — Skeleton / 空状态 / 错误重试 | ⚠️ DocTable 只有简单 "加载中..." / "暂无文档"，无 Skeleton、无错误态 | 🟡 |

### 影响文件

| 文件 | 需改动 |
|------|--------|
| 新建 `frontend/app/components/ErrorBoundary.tsx` | React Error Boundary |
| 新建 `frontend/app/components/Toast.tsx` | 全局 Toast（含重试按钮） |
| `frontend/lib/api.ts` | 加 403 interceptor + 全局错误码映射 + Toast 触发 |
| `frontend/app/chat/page.tsx` | `handleSend` catch 按 `error_code` 区分展示 |
| `frontend/app/components/DocTable.tsx` | 加 Skeleton 行 + Error 态 + 重试按钮 |

---

## 三.8 管理台跳转入口 — 3 项缺失

| # | 设计要求 | 实现状态 | 严重程度 |
|---|---------|---------|---------|
| 38 | **设置页 "🔗 权限管理" 卡片** → 跳转管理台 (`ADMIN_CONSOLE_URL`) | ❌ 没有 | 🟡 |
| 39 | **KB 管理页 Header "👥" 图标** → 跳转该 KB 授权页面 | ❌ 没有 | 🟡 |
| 40 | **403 错误页 "前往管理台申请权限"** | ❌ 没有 403 页面 | 🟡 |

### 影响文件

| 文件 | 需改动 |
|------|--------|
| `frontend/app/settings/page.tsx` | 加 "🔗 权限管理" 卡片 |
| `frontend/app/kb/page.tsx` | KB 名称旁加 "👥" 授权跳转按钮 |
| 新建 `frontend/app/error/403/page.tsx` | 403 页面 |

---

## 缺失后端端点（需配合实现）

| # | 端点 | 方法 | 优先级 | 备注 |
|---|------|------|--------|------|
| 1 | `/api/v1/auth/refresh` | POST | 🟡 P1 | token 自动刷新 |
| 2 | `/api/v1/tenants` | GET | 🔴 P0 | 多租户选择 |
| 3 | `/api/v1/tenants/{id}/stats` | GET | 🟡 P1 | 租户选择页统计 |
| — | `/api/v1/knowledge-bases/{kb_id}/documents?search=&status=&sort_by=&order=` | GET | 🟡 P1 | DocTable 搜索/筛选/排序 |
| — | `retrieved` SSE 事件加 `chunks[].content` | — | 🔴 P0 | 引用卡片原文展示 |

---

## 汇总：按严重程度

### 🔴 阻塞性问题（9 项）

| # | 问题 |
|---|------|
| 1 | 无 SSO 生产模式登录 |
| 4 | 无 `/select-tenant` 多租户页面 |
| 9 | KB 选择器不在全局 Header（对话页无法选 KB） |
| 10 | KB 选择器只支持单选（设计要求对话页多选） |
| 15 | 文档列表无搜索功能 |
| 16 | 文档列表无状态筛选 |
| 23 | 流式响应中引用来源无原文（需后端配合 `retrieved` 事件加 content） |
| 36 | 无 Error Boundary（页面崩溃白屏） |
| — | 缺后端 `GET /tenants` 端点 |

### 🟡 完整性问题（25 项）

Token 刷新、会话超时、租户切换、批量移动、文档下载、文档预览 Sheet、文件拖拽到目录、Prompt 预览面板、模型切换 Select、全局 Toast、SSE error 事件、错误码区分展示、DocTable Skeleton/Error 态、管理台跳转入口 (3 处)、搜索/筛选/排序后端 query params、创建 KB 切分策略选择、AuthGuard 全局启用、403 interceptor。

### 🟢 轻微偏差（6 项）

- InputBar 用 `<input>` 而非 `<textarea>`
- KB 删除无 409 处理
- 手动 `setInterval` 而非 TanStack Query
- SourcesCard 用原生 hover 而非 shadcn HoverCard
- 租户列表硬编码而非动态获取
- ConvList 无内联重命名

---

## 建议修复顺序

按依赖关系排列（与设计文档 §六 实施顺序对齐）：

1. **Error Boundary + 全局 Toast** — 错误处理基础设施，所有页面受益
2. **AuthGuard 全局启用** — 消除每个页面重复的守卫代码
3. **KB 选择器移入 Header + 多选** — 对话页和 KB 页的共同依赖
4. **`retrieved` SSE 事件加 chunk 原文**（后端 + 前端） — 引用卡片核心体验
5. **文档搜索 + 状态筛选 + 排序** — KB 管理页高频操作
6. **多租户选择页 + 后端 `/tenants` 端点** — 租户基础设施
7. **文档预览 Sheet + 下载** — 文档详情体验
8. **管理台跳转入口** — 独立模块，可并行
9. **Prompt 预览 + 模型切换** — 设置页交互完善
10. **SSO 生产模式 + token 刷新** — 生产部署前完成

# RAG v14 前端落地方案

> 基于 `docs/frontend-design.md` 完整设计
> 原则：每个阶段交付一个**可独立运行、可演示**的子系统，前一阶段的产物是后一阶段的基石

---

## 总体策略：6 个阶段，6 个里程碑

```
Phase 1 (认证骨架)  →  Phase 2 (KB+文档管理)  →  Phase 3 (对话核心)
      ↓                       ↓                        ↓
  能登录/登出            能创建KB、上传文件        能提问、看流式回答
  JWT 签发+校验          文档列表+状态轮询         SSE+引用卡片
      ↓                       ↓                        ↓
  后端: 2 个端点          后端: 10 个端点            后端: 3 个端点
  前端: 3 个页面          前端: 知识库管理页           前端: 对话页

Phase 4 (目录+批量)  →  Phase 5 (设置+降级)  →  Phase 6 (Dashboard)
      ↓                       ↓                        ↓
  目录树、批量操作          模型切换、错误处理           使用统计、质量趋势
  文件移动、重命名          Strict/top_k调节            Langfuse跳转
      ↓                       ↓                        ↓
  后端: 8 个端点          后端: 6 个端点            后端: 5 个端点
  前端: 知识库页升级       前端: 设置页+全局错误        前端: Dashboard页
```

每个阶段的"完成"定义为：**在该阶段末尾，一个真实的用户可以完成一个完整的闭环操作**，而不是"代码写好了但没连起来"。

---

## Phase 1：认证骨架 — 能登录、能登出、能区分用户

**目标**：用户能通过浏览器登录系统，系统知道"当前用户是谁、属于哪个租户"。

**闭环**：打开浏览器 → `/login` → 输入用户名+租户 → 点击登录 → 进入 `/kb`（空白） → Header 显示用户名和租户 → 1 小时后自动退出 → 回到 `/login`。

### 后端：2 个端点

| # | 端点 | 优先级 | 说明 |
|---|------|--------|------|
| 1 | `POST /api/v1/auth/dev-login` | 🔴 | 接收 `{username, tenant, role}`，自签 JWT (RS256)，返回 `{access_token, expires_at, user: {id, tenant_id, roles}}` |
| 2 | `GET /api/v1/tenants` | 🔴 | 返回用户所属的租户列表（开发模式从 `knowledge_bases` 表反查 `DISTINCT tenant_id`） |

**实现要点**：
- JWT payload: `{sub, tenant, roles, iat, exp}`。开发模式用 `python-jose` 自签，不依赖外部 IdP
- `require_permission` 中间件在 Phase 1 只做 JWT 校验，不做 Cerbos 判定（Cerbos 已在后端就绪，Phase 1 开启）
- Axios interceptor: 自动在请求头加 `Authorization: Bearer <token>`

### 前端：3 个页面 + 1 个全局组件

| 页面/组件 | 路由 | 说明 |
|----------|------|------|
| `/login` | 公开 | 开发模式表单（用户名 + 租户下拉 + 角色下拉） |
| `/select-tenant` | 需登录 | 仅多租户用户可见，单租户自动跳过 |
| `/kb` | 需登录 | 空白页 + "这是知识库管理页，Phase 2 实现" |
| `AuthGuard` | 全局 | 路由保护组件，包裹 `{children}` |

**前端关键技术点**：
- `AuthGuard`：读取 Zustand `useAuthStore` → 无 token → `redirect /login`；有 token 但过期 → 清 store → `redirect /login`
- `useAuthStore`：`{token, user, tenant, expiresAt, login(), logout()}`
- Axios interceptor：响应 401 → `logout()`
- Session timer：`setInterval(30s)` 检查 `expiresAt - now < 0` → `logout()`

### 验证方式
1. 打开 `http://localhost:3000` → 自动跳到 `/login`
2. 输入 "admin" / "tenant-dev" / "system_admin" → 进入 `/kb`，Header 显示 "🏢 tenant-dev 👤 admin"
3. 修改 JWT `exp` 为过去时间 → 1 分钟内自动跳到 `/login`
4. 刷新页面 → 保持在登录状态（localStorage token 仍有效）

---

## Phase 2：KB + 文档管理 — 能建库、能上传、能看到文档

**目标**：用户能创建知识库、上传文件、看到文件列表和状态。

**闭环**：进入系统 → 看到 KB 列表（空） → 创建第一个 KB → 进入该 KB → 上传 test_docs/下的文档 → 看到文件出现在列表中，状态从 `not_parsed` → `queued` → `processing` → `completed`。

### 后端：10 个端点

| # | 端点 | 方法 | 说明 |
|---|------|------|------|
| 1 | `/api/v1/knowledge-bases` | GET | 当前租户下的 KB 列表（带 Cerbos kb:read 过滤） |
| 2 | `/api/v1/knowledge-bases` | POST | 创建 KB + register_resource + seed configs + 创建 kb_bound 目录 |
| 3 | `/api/v1/knowledge-bases/{id}` | PATCH | 重命名 KB |
| 4 | `/api/v1/knowledge-bases/{id}` | DELETE | 删除 KB（前置：无活跃挂载） |
| 5 | `/api/v1/knowledge-bases/{kb_id}/documents` | GET | KB 下的文档列表（JOIN document_kb_mounts + documents） |
| 6 | `/api/v1/documents/upload` | POST | 上传文件（已有，`auto_parse` 默认 false） |
| 7 | `/api/v1/documents/{doc_id}/trigger-parse` | POST | 手动触发解析 |
| 8 | `/api/v1/documents/{doc_id}` | GET | 文档元信息（含 parse_status） |
| 9 | `/api/v1/documents/{doc_id}/kb/{kb_id}` | DELETE | 从 KB 移除文档 |
| 10 | `/api/v1/documents/{doc_id}/kb/{kb_id}` | PATCH | 启用/停用文档 |

**注意**：Phase 2 不上端点 7 (trigger-parse) — 因为 B-INGEST Celery Worker 还没跑。先做到"上传后状态 = `not_parsed`"，解析在 Phase 3 再补。

### 前端：知识库管理页 v1（基础版）

```
┌──────────────────────────────────────────────────────────────┐
│  🏠 RAG v14  [📚 知识库: 开发测试KB ▼]     🏢 tenant-dev 👤 admin  🚪  │
├──────────────────────────────────────────────────────────────┤
│                                                              │
│   [上传文件]                                                  │
│                                                              │
│   ┌──────────────────────────────────────────────────────────┐│
│   │ 文件名             大小   状态      上传时间   操作       ││
│   │ agent-framework.md  10KB  ✅完成   2h前      👁 🗑        ││
│   │ design-v14.md       131KB not_parsed 刚刚      🔄 🗑     ││
│   └──────────────────────────────────────────────────────────┘│
│                                                              │
└──────────────────────────────────────────────────────────────┘
```

- DataTable 列：文件名、大小、状态 Badge、上传时间、操作（预览/删除/停用）
- 状态 Badge 颜色：`not_parsed`(gray)、`completed`(green)、`failed`(red)
- 操作按钮受 Cerbos 权限控制（`kb:write` 才能删/停用）
- 上传区：shadcn/ui Dropzone，支持 .txt .md

### 验证方式
1. 创建 KB "测试知识库" → 列表中看到它
2. 上传 `test_docs/主流agent开发框架.md` → 列表中新增 1 行，状态 `not_parsed`
3. 重命名 KB → 名称更新
4. 删除一个文档 → 确认 Dialog → 列表中消失
5. 停用文档 → 状态栏变灰色 "已停用"
6. 删除 KB → 确认 → KB 列表刷新

---

## Phase 3：对话核心 — 能问、能答、能看到引用

**目标**：用户能在选定 KB 中提问，系统流式返回答案并附带引用来源。

**闭环**：创建会话 → 选择 KB → 输入问题 → "发送" → 看到打字效果 → 回答完整 → 底部显示引用卡片 → hover 看到原文 → 追问"那第二条呢" → 系统改写为完整查询 → 返回正确答案。

这是**整个产品的核心价值交付**。

### 后端：3 个端点 + 1 个修改

| # | 端点 | 方法 | 说明 |
|---|------|------|------|
| 1 | `/api/v1/conversations` | GET | 当前用户的历史会话列表 |
| 2 | `/api/v1/conversations` | POST | 新建会话（无 KB 绑定） |
| 3 | `/api/v1/conversations/{id}/query` | POST | 发起查询（已有） |
| — | `GET /api/v1/conversations/{id}/stream` | *改* | 已有，**修改 `retrieved` 事件 payload 加入 chunk 原文前 200 字** |

### 也补上解析触发
Phase 3 同时补上 Phase 2 跳过的 `POST /api/v1/documents/{doc_id}/trigger-parse` — 启动 Celery Worker (`make dev-ingest`)，让上传的文件能从 `not_parsed` 自动走到 `completed`。

### 前端：对话页 + 知识库管理页升级

```
┌──────────────────────────────────────────────────────────────────┐
│  🏠 RAG v14  [📚 KB: 开发测试KB ▼]        🏢 tenant-dev 👤 admin 🚪 │
├────────────────────┬─────────────────────────────────────────────┤
│  💬 会话列表        │                                         │
│  ┌──────────────┐  │  🤖 根据文档，LangGraph 是当前 Agent...     │
│  │ + 新会话      │  │                                         │
│  ├──────────────┤  │  📎 引用来源:                              │
│  │ LangGraph特点 │  │  ┌──────────────────────────────┐       │
│  │ 什么是RAG     │  │  │ [来源1] LangGraph 的核心特点...│       │
│  │ 权限系统问题  │  │  │ [来源2] LangGraph 由 LangCh... │       │
│  └──────────────┘  │  └──────────────────────────────┘       │
│                    │                                         │
│                    │  ┌──────────────────────────────────┐   │
│                    │  │ 输入你的问题...               [发送]│   │
│                    │  └──────────────────────────────────┘   │
├────────────────────┴─────────────────────────────────────────────┤
```

**对话页组件**：
- `ConversationSidebar`：会话列表 + 新建按钮，点击切换会话
- `MessageList`：用户消息（右对齐蓝底）+ 助手消息（左对齐灰底）
- `StreamingText`：Vercel AI SDK `useChat` 消费 SSE `/stream`，逐 token 渲染
- `SourcesBar`：引用卡片列表，每个卡片用 HoverCard 显示前 200 字原文
- `InputBar`：Textarea + 发送按钮，支持 Enter 发送

**知识库管理页升级**：
- 每行增加 🔄 "解析" 按钮（status = not_parsed 或 failed 时显示）
- 解析触发 → 状态变为 `queued` → TanStack Query 每 3 秒轮询 → 显示处理 animation → 完成后变绿
- 增加进度统计：KB 级别显示 "15/20 已解析, 3 处理中, 2 失败"

### 验证方式
1. 创建会话 → 输入 "什么是 RAG？" → 看到逐字打字效果 → 完整答案
2. 答案下方出现 2-3 个引用卡片 → hover 看原文
3. 追问 "那第二个来源说了什么？" → resolved_query 被改写 → 返回新答案
4. 在知识库页面对 `not_parsed` 文件点 "解析" → 3 秒后状态变绿 → 此时在对话页提问能看到该文档的内容
5. 权限服务停掉 → 提问返回 503 "服务暂时不可用，请稍后重试"（⚠️ 样式）

---

## Phase 4：目录 + 批量 + 文档移动

**目标**：KB 内文档多到需要组织时，用户能建目录、移动文档、批量操作。

**闭环**：左侧目录树 → 新建手动目录 → 拖拽文档到目录 → 批量选中 10 个文档 → 一键删除 → 移入移出目录。

### 后端：8 个端点

| # | 端点 | 方法 | 说明 |
|---|------|------|------|
| 1 | `/api/v1/knowledge-bases/{kb_id}/directories` | GET | 目录树列表 |
| 2 | `/api/v1/directories` | POST | 创建手动目录 |
| 3 | `/api/v1/directories/{id}` | PATCH | 重命名目录 |
| 4 | `/api/v1/directories/{id}` | DELETE | 删除空目录 |
| 5 | `/api/v1/directories/{dir_id}/documents` | POST | 文档加入目录 |
| 6 | `/api/v1/directories/{dir_id}/documents/{doc_id}` | DELETE | 从目录移除文档 |
| 7 | `/api/v1/documents/batch/delete` | POST | 批量删除（逐资源独立 check_batch） |
| 8 | `/api/v1/documents/batch/parse` | POST | 批量解析 |
| 9 | `/api/v1/documents/{doc_id}` | PATCH | 文件重命名 |

### 前端：知识库管理页升级为两栏布局

左侧目录树 + 右侧文档表格。目录树节点支持拖放（`@dnd-kit`）。DataTable 第一列 checkbox 支持多选，顶部工具栏动态出现 [批量删除] [批量解析] [移动到目录]。

### 验证方式
1. 创建文件夹 "项目A" → 目录树中出现
2. 拖拽文档到 "项目A" → 文档出现在该目录下
3. 改文档名 → 表格中名称更新
4. 勾选 5 个文档 → [批量删除] → 确认 → 5 个文档消失

---

## Phase 5：设置 + 降级 + 错误处理

**目标**：用户能调整检索参数、切换模型、在系统异常时获得清晰的反馈。

**闭环**：进入设置页 → 看到当前检索参数 → 修改 top_k 从 10 到 20 → 保存 → 回到对话页提问 → 检索结果变多。权限服务挂了 → 看到 503 警告而不会误以为是"没搜到结果"。

### 后端：6 个端点

| # | 端点 | 方法 | 说明 |
|---|------|------|------|
| 1 | `/api/v1/models` | GET | 可用模型列表 |
| 2 | `/api/v1/configs/retrieval` | GET | 当前 KB 的检索配置 |
| 3 | `/api/v1/configs/retrieval` | PATCH | 更新检索配置 |
| 4 | `/api/v1/prompts` | GET | Prompt 模板列表 |
| 5 | `/api/v1/prompts/{id}/activate` | PATCH | 切换默认 Prompt 版本 |
| 6 | `/api/v1/knowledge-bases/{id}/chunking-config` | PATCH | 更新切分配置 |

### 前端：设置页 + 全局错误处理

**设置页**：
- 模型选择（RadioGroup: qwen3-embed / deepseek-v4-flash / Qwen2.5-72B-vllm）
- 检索参数（Slider: top_k 3-50, oversample_factor 1.0-3.0）
- Strict 模式开关（Switch + 说明 "敏感 KB 开启实时权限复核"）
- Prompt 模板选择 + Preview 面板

**全局错误处理**：
- React Error Boundary（每个页面层）
- 4 种 fail 场景的差异化 UI（§3.6）
- Toast 通知系统（上传成功/失败、删除确认、配置保存）

### 验证方式
1. 修改 top_k from 10 → 5 → 保存 → 回到对话页 → 回答引用的来源变少
2. 停掉 Cerbos → 提问 → 看到 "服务暂时不可用 ⚠️ [重试]"（不是"未找到足够信息"）
3. 切换 Prompt 模板 v1→v2 → 回答风格变化（更严格反幻觉）

---

## Phase 6：Dashboard + 管理台跳转

**目标**：管理员能看到系统使用情况，用户能找到权限管理入口。

**闭环**：Dashboard 页面 → 看到最近 30 天查询量趋势 → Top 活跃 KB → 文档处理统计。点击管理台链接 → 跳转到外部权限管理系统。

### 后端：5 个端点

| # | 端点 | 方法 | 说明 |
|---|------|------|------|
| 1 | `/api/v1/stats/usage` | GET | 日均查询量（按日期聚合 audit_logs） |
| 2 | `/api/v1/stats/top-kbs` | GET | Top 10 活跃 KB |
| 3 | `/api/v1/stats/documents` | GET | 文档解析状态分布 |
| 4 | `/api/v1/stats/quality` | GET | RAGAS recall/faithfulness 趋势 |
| 5 | `/api/v1/tenants/{id}/stats` | GET | 租户级统计 |

### 前端：Dashboard 页

- Recharts 折线图（30 天查询量）
- Recharts 柱状图（Top 10 KB）
- Recharts 饼图（文档状态分布）
- 管理台跳转卡片（`ADMIN_CONSOLE_URL` 配置）

### 验证方式
1. Dashboard 页面显示近 30 天查询统计 → 数字与 `audit_logs` 表一致
2. 点击 "🔗 权限管理" → 跳转外部管理台

---

## 端点交付节奏汇总

| Phase | 新增端点 | 累计端点 | 前端页面 |
|-------|---------|---------|---------|
| 1 | 2 | 2 | `/login` `/select-tenant` `/kb`(空壳) |
| 2 | 10 | 12 | 知识库管理页 v1（基础版） |
| 3 | 3 | 15 | 对话页 + 知识库页升级（解析触发） |
| 4 | 9 | 24 | 知识库页 v2（目录树+批量+拖拽） |
| 5 | 6 | 30 | 设置页 + 全局错误处理 |
| 6 | 5 | 35 | Dashboard + 管理台跳转 |

**总计 35 个端点，6 次增量交付。** 每次交付都是前一次的加法，不需要回头改 Phase 1 已经稳定的代码。

---

## 技术栈落地细节

| 层 | 选型 | 具体用法 |
|----|------|---------|
| 框架 | Next.js 14 App Router | `/app/login/page.tsx`, `/app/kb/page.tsx` 等 |
| 全局状态 | Zustand `useAuthStore` + `useKBStore` | auth token + 当前选中 KB |
| 服务端数据 | TanStack Query | `useQuery({queryKey: ['documents', kbId], queryFn: fetchDocuments})` 自动缓存+轮询 |
| SSE | Vercel AI SDK `useChat` | `useChat({api: '/api/v1/conversations/{id}/stream'})` 原生支持 text/event-stream |
| 组件库 | shadcn/ui | Button, Input, Dialog, Sheet, Dropdown, Badge, Tooltip, HoverCard, Accordion, ScrollArea, Sidebar, DataTable, Slider, Switch, Progress, Toast |
| 拖拽 | `@dnd-kit/core` + `@dnd-kit/sortable` | 目录树节点拖放（Phase 4） |
| 图表 | Recharts | LineChart, BarChart, PieChart（Phase 6） |
| 部署 | `next build && next export` → Nginx | 静态导出 + 反代到 FastAPI:8000 |

```tsx
// 项目结构 (Next.js 14 App Router)
app/
├── layout.tsx              // 根布局: AuthGuard + Header + Toaster
├── page.tsx                // 重定向到 /kb
├── login/
│   └── page.tsx            // 登录页
├── select-tenant/
│   └── page.tsx            // 租户选择页
├── kb/
│   ├── page.tsx            // 知识库管理页
│   └── layout.tsx          // 左侧目录树 + 右侧内容区
├── chat/
│   ├── page.tsx            // 对话页
│   └── layout.tsx          // 左侧会话列表 + 右侧消息区
├── settings/
│   └── page.tsx            // 设置页
├── dashboard/
│   └── page.tsx            // Dashboard 页
├── components/
│   ├── ui/                 // shadcn/ui 组件
│   ├── Header.tsx          // 全局 Header（KB选择器+租户+用户+退出）
│   ├── AuthGuard.tsx       // 路由保护
│   └── ErrorBoundary.tsx   // 错误边界
├── lib/
│   ├── api.ts              // Axios 实例 + interceptor
│   ├── auth.ts             // JWT 解析 + 过期检查
│   └── permissions.ts      // Cerbos check 辅助函数
└── stores/
    ├── useAuthStore.ts     // 认证状态
    └── useKBStore.ts       // 当前 KB 选择状态
```

---

## 6 步可以跑起来的顺序

```bash
# Phase 1: 认证骨架
$ npm run dev
→ http://localhost:3000 → /login → 登录 → 看到空白/kb 但 Header 正确

# Phase 2: KB + 文档
$ make dev-api     # 启动 FastAPI
→ 创建 KB → 上传文档 → 看到列表

# Phase 3: 对话核心
$ make dev-ingest  # 启动 Celery worker
→ 文档解析完成 → 提问 → 流式回答 → 引用卡片

# Phase 4: 目录 + 批量
→ 建文件夹 → 拖文档 → 批量删

# Phase 5: 设置 + 降级
→ 调参 → 切模型 → 停 Cerbos → 看 503 降级文案

# Phase 6: Dashboard
→ 看统计 → 跳转管理台
```

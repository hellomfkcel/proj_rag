# RAG v14 项目生产级投放诊断报告 v1

> **诊断日期**: 2026-07-29
> **诊断范围**: 全部后端模块（11 个）+ 前端 + 基础设施，对照 `docs/RAG系统设计v14.md` 和 `docs/frontend-design.md`
> **诊断方法**: 逐文件代码审查 + 跨模块交叉验证 + 静态分析
> **代码规模**: ~5000 行 Python（src/）+ ~2500 行 TypeScript（frontend/）

---

## 总览

| 维度 | 状态 | 说明 |
|------|------|------|
| 模块完整性 | ⚠️ 基本完整 | 11 个模块均有代码，但部分存在关键缺口 |
| 设计一致性 | ⚠️ 存在偏差 | 核心架构路径存在多处偏离设计文档 |
| Mock/Stub | ⚠️ 有残留 | 约 8 处开发期 stub，部分影响生产可用性 |
| 死代码/未使用 | ⚠️ 偏多 | 约 8 处组件/函数已实现但未接入 |
| 重复造轮子 | ⚠️ 少量 | 主要在合成层和权限镜像维护 |
| 前端完整度 | ⚠️ 基本完整 | 页面齐全但 SSE 流式有 bug |

---

# 第一部分：设计一致性缺口

## 1. P-AUTHC 权限消费模块 — 最严重的系统性缺口

### 1.1 防腐层被绕过（红线违反）

设计文档 §0.2.1 规定："任何模块不得越过 P-AUTHC 直接调用权限服务 HTTP 端点"、§6 规定 P-AUTHC 是"本系统访问权限服务的**唯一出口**"。

**实际：6 处绕过 P-AUTHC 直接调用 `get_client()`：**

| 文件 | 行号 | 绕过方式 |
|------|------|---------|
| `src/ingest/service.py` | 246 | `from src.permission.cerbos_client import get_client` |
| `src/ingest/components/visibility_stamper.py` | 29 | 同上，调 `get_visibility()` |
| `src/api/kb_routes.py` | 121, 172 | 同上，调 `register_resource()` / `retire_resource()` |
| `src/platform/task/reconciliation.py` | 47, 140 | 同上，调 `check()` / `link_resource()` |

**更严重的是：`doc/service.py` 完全不经过 P-AUTHC，直接写 `resource_registry` 和 `mount_registry` 表：**

| 位置 | 行号 | 操作 |
|------|------|------|
| `submit_ingest_task` | 98-103 | 直接 `INSERT INTO resource_registry`（应调 `register_resource`） |
| `submit_ingest_task` | 120-125 | 直接 `INSERT INTO mount_registry`（应调 `link_resource`） |
| `delete_document_from_kb` | 228-232 | 直接 `UPDATE mount_registry SET unlinked=true`（应调 `unlink_resource`） |
| `_purge_document` | 312-315 | 直接 `UPDATE resource_registry SET retired=true`（应调 `retire_resource`） |

**影响**: P-AUTHC 中的 `register_resource`/`link_resource`/`unlink_resource`/`retire_resource` 四个函数**从未被主要消费者调用**，仅被 KB 路由和对账任务使用。电路断路器、Metrics、三态映射、fail-closed 逻辑全部被绕过。

### 1.2 `get_visibility()` 无门面封装

`cerbos_client.py:318-386` 的 `get_visibility()` 方法没有对应的 `authz.py` 门面函数。调用方（`ingest/service.py`、`visibility_stamper.py`）被迫绕过 P-AUTHC。这是 `stamping_queue` worker 的核心调用路径。

### 1.3 三态映射不完整

设计文档 §6A.6 要求 allow/deny/indeterminate 三态分别处理。**实际只实现了 allow/deny 两态**：所有 Cerbos 响应在 `cerbos_client.py` 中被压平为 `"allow"` 或 `"deny"`，`EFFECT_INDETERMINATE` 从未被识别或产生。

```python
# cerbos_client.py:146 — 所有非 ALLOW 均被映射为 deny
"decision": "allow" if verdict == "EFFECT_ALLOW" else "deny",
```

`indeterminate` 仅在 `obs/metrics.py:4` 的注释中出现，从未作为实际决策值。

### 1.4 `client_id` 参数形同虚设

`_headers()` 方法（`cerbos_client.py:541-542`）接收 `client_id` 参数但**完全忽略**——只设置 `Content-Type: application/json`。Cerbos PDP 无法通过 HTTP 头区分调用方身份。

### 1.5 `VisibilityChanged` 事件订阅缺失

设计文档 §6A.8 要求 P-AUTHC 订阅权限服务事件流转交 B-INGEST。**全代码库零匹配**——grep `VisibilityChanged` / `visibility_changed` 返回零结果。这意味着权限服务推送的可见性变更不会触发实时盖戳更新，完全依赖对账兜底（§14.5c）。

### 1.6 电路断路器不一致

全局 `_authz_circuit_breaker` 声明显式（`authz.py:33-39`）但从未使用——`@_with_circuit_breaker` 装饰器为每个函数创建独立断路器实例。`get_prefilter`、`mint_ctx_token`、全部生命周期函数均未包裹断路器。

### 1.7 两个 JWT 库混用

- `middleware.py` 使用 `python-jose`（`from jose import jwt`）
- `context.py` 使用 `PyJWT`（`import jwt`）

两处的 claims 提取逻辑不同（middleware 不处理 `groups`），可能导致不同路径构建出不同的 `RequestContext`。

---

## 2. B-INGEST 摄入管线模块

### 2.1 execution_epoch 栅栏断裂（严重 Bug）

设计文档 §14.2-14.3 定义 `execution_epoch` 为乐观栅栏令牌。

**Bug**: `kb_routes.py:511` 调用 `ingest_document_task.delay(mount_id=mount_id, ...)` 时**不传 `execution_epoch`**，任务默认取 `execution_epoch=1`（`service.py:118`）。但 DB 中 `ON CONFLICT DO UPDATE SET execution_epoch = execution_epoch + 1` 已递增为 ≥2。首个 `should_abort` 检查发现 DB epoch(≥2) ≠ 任务 epoch(1)，**立即中止**。

**影响**: 所有重新解析（用户点击"解析"按钮触发）均立即失败——永远无法重新摄入。

### 2.2 `vis_version` 用 0 而非 null

设计文档 §14.5.1 要求新 chunk 的 `vis_version` 为 null。`PermissionMetadataEnricher`（`perm_enricher.py:34`）设置为 `0`。注释写的是 `null`，代码是 `0`。虽然功能上等价，但层 1 编译过滤器（`authz.py:283`）检查 `vis_version > 0` 的设计意图是排除 null——用 0 做初始值使该条件语义从"是否有戳记"变为"戳记版本是否为正"，轻微偏离。

### 2.3 两个 parse_status 状态从未使用

`not_parsed` 和 `queued` 在 `init.sql:50-51` 定义但在所有代码路径中从未被显式赋值：
- `not_parsed` 仅作为 DB DEFAULT，但 `kb_routes.py:489` INSERT 直接设置 `'processing'`
- `queued` 定义的注释说 `trigger_parse` 会设置，但实际代码设置的是 `'queued'` 字符串在 `_trigger_parse_internal` 的返回值中，不写入 DB

### 2.4 盖戳批量 upsert 无真实游标

`_upsert_stamps`（`service.py:343`）一次性查询最多 10000 行，分批 upsert 但无持久化游标。中断后重试从头开始，仅靠覆盖写幂等保证正确性。设计文档要求的"断点续跑"未完全实现。

### 2.5 对账漂移检测不自动修复

`reconciliation.py:157-164` 检测到 `stamp_drift`（chunk 戳记版本落后）仅记录日志和 metric，**不自动提交 `stamp_channel_task` 修复**。只有孤儿戳记（`vis_version == 0`）才触发自动修复。

---

## 3. B-RETRIEVE 检索模块

### 3.1 层次化合并器（HierarchicalMerger）未接入

`hierarchical_merger.py` 完整实现了窗口合并逻辑（138 行代码），但**未被任何 Pipeline YAML 引用，也不在 `retrieve()` 调用路径中**。所有层级检索合并能力处于未激活状态。

### 3.2 PrefilterInjector 未使用

`prefilter_injector.py` 封装了六条件过滤器编译，但实际 `retrieve()` 直接调用 `authz.compile_filter()`。该组件是死代码。

### 3.3 Pipeline 版本参数未传递

`chat/service.py:36-37` 接受 `pipeline_name` 和 `yaml_version` 参数但 `retrieve()` 始终硬编码 `"query_v4"`，忽略调用方传入的版本。Pipeline 灰度切换依赖此参数。

### 3.4 docstring 自相矛盾

`retrieve/__init__.py:7` 和 `service.py:7` 声称"不做事后过滤"，但 `service.py:119-126` 在 `strict=true` 时执行完整的检索后逐项复核（层 3）。

---

## 4. B-CHAT 对话编排模块

### 4.1 合成完全绕过 Haystack（架构偏离）

设计文档 §16.3 明确要求通过 Haystack `PromptBuilder` + `LiteLLMGenerator` 进行生成合成。**实际三个合成函数（compact/refine/tree_summarize）全部使用手工 f-string 拼提示 + 直接调 `invoke_llm()`**：

```python
# chat/service.py:359 — 手工拼提示
prompt = f"基于以下文档片段回答问题...\n\n文档:\n{docs_text}\n\n问题: {query}"
answer = invoke_llm(prompt)
```

Haystack `PromptBuilder` 出现在 `query_v1~v3.yaml` 中，但 `query_v4.yaml`（当前使用的版本）不含任何生成节点。

### 4.2 `no_synthesis` 模式缺失

设计文档 §16.3 定义了四种合成模式，`no_synthesis` 未实现。

### 4.3 Llama Guard 内容安全审查未实现

设计文档 §16.4 要求的内容安全审查（Llama Guard 或 OpenAI Moderation）全代码库零实现。

### 4.4 SSE token 流格式错误（Bug）

`routes.py:211` SSE 发布格式为：
```
data: {"event": "token", "content": "..."}

```

缺少 `event: token\n` 前缀。前端 `addEventListener("token", handler)` 只能匹配带 `event:` 前缀的事件。**SSE 实时逐 token 输出实际不工作**，用户只能看到最终完整答案。

### 4.5 引用校验未强制移除

`validate_citations()` 检测幻觉引用后仅"记录警告"（`chat/service.py:238` 注释），不实际移除或标记。设计文档要求"确定性 chunk_id 校验"。

---

## 5. B-DOC 文档管理模块

### 5.1 缺少事务原子性

`submit_ingest_task` 和 `delete_document_from_kb` 中的生命周期调用使用裸 `conn.execute()`，每条 SQL 自动提交。设计文档 §13.7 要求"必须携带 idempotency-key + 调用失败即回滚本地业务事务"，当前实现无法保证 `register_resource` 成功但本地 INSERT 失败时的回滚。

### 5.2 `MountEnabledChanged` 事件未发布

`events.py:85-99` 定义了事件类，但全代码库零次调用 `mount_enabled_changed_event()`。文档启停开关（`is_enabled`）的变更事件不会发出。

### 5.3 目录模型缺失 ORM 定义

`models.py` 中没有 `Directory` 或 `DocumentDirectoryEntry` 模型类。目录操作全部通过 `dir_routes.py` 中的原始 SQL 实现。

### 5.4 idempotency-key 未显式构造

设计文档 §13.7 要求的确定性幂等键（`{facade}-{tenant}-{resource_id}[-{kb_id}]-{schema_version}`）在代码中没有显式构造——依赖 DB 唯一约束（`ON CONFLICT`）做去重。

---

## 6. 平台模块

### 6.1 P-MODEL：未封装 Haystack 组件

设计文档 §11.1 要求封装 Haystack `LiteLLMGenerator`、`SentenceTransformersDocumentEmbedder`、`SentenceTransformersRanker`。实际实现全部使用 OpenAI SDK、raw HTTP、FlagEmbedding——**Haystack 模型组件从未被使用**。`invoke_llm`/`invoke_embedding`/`invoke_rerank` 函数本身功能正确，但偏离了框架防腐层的设计意图。

### 6.2 P-OBS：Metrics 仅在内存

`metrics.py` 的计数器和 Gauge 全部存储在 Python 字典中（`_metrics_store: Dict[str, int]`），**未通过 OTLP 导出到 Prometheus**。当前的 metrics 只在进程内可读，不具备生产监控能力。

### 6.3 P-CONFIG：feature_flag 是硬编码字典

`feature_flag()` 函数返回硬编码的 `{"authz.decision_cache_enabled": False, "authz.strict_default": False}`，无 DB 支持、无动态切换、未知 flag 一律返回 False。

### 6.4 P-TASK：outbox-relay 不是 Celery 队列

设计文档 §9.2 列出四个队列，但 `outbox_relay` 是独立的 async 进程——Celery 实际只有三个队列。这不影响功能，但需在部署文档中明确区分。

---

# 第二部分：Mock / Stub / 开发期残留

| # | 位置 | 内容 | 严重度 | 建议 |
|---|------|------|--------|------|
| 1 | `ingest/service.py:154-156` | S3 读取失败时回退到硬编码中文测试文本 | **高** | 生产环境会导致文档内容被替换 |
| 2 | `ingest/service.py:469-470` | `time.sleep(5)` 等待协作式取消 | **中** | 用事件/条件变量替代 |
| 3 | `permission/context.py:45-56` | `enforce_jwt=False` 时返回硬编码 dev 上下文 | **高** | 生产必须强制 `enforce_jwt=True` |
| 4 | `retrieve/service.py:41` + `chat/service.py:61` | `build_context(ctx_token or "dev-credential", enforce_jwt=False)` | **高** | 同上，多处调用绕过 JWT 验证 |
| 5 | `api/auth.py:132-139` | OAuth2 token exchange 返回 501 "not implemented" | **中** | 生产模式登录不可用 |
| 6 | `api/auth.py:194-221` | SSO callback 返回 501 "not implemented" | **中** | 同上 |
| 7 | `api/dashboard_routes.py:62` | 质量基线返回硬编码假数据 | **低** | 需运行 eval_ragas.py 生成真实基线 |
| 8 | `permission/authz.py:316-318` | `require_permission` 在 ctx=None 时创建假 `RequestContext` | **高** | 绕过所有权限检查 |

**总计**: 8 处开发期残留，其中 5 处标记为"高"严重度——直接影响生产安全性。

---

# 第三部分：死代码 / 已实现但未接入

| # | 组件/函数 | 文件 | 行数 | 说明 |
|---|----------|------|------|------|
| 1 | `VisibilityStampComponent` | `ingest/components/visibility_stamper.py` | 53 | Haystack Component，未接入任何 Pipeline YAML |
| 2 | `PrefilterInjector` | `retrieve/components/prefilter_injector.py` | 39 | 封装六条件过滤，`retrieve()` 直接调 `authz.compile_filter()` |
| 3 | `HierarchicalMerger` | `retrieve/components/hierarchical_merger.py` | 138 | 完整的窗口合并逻辑，未接入任何 Pipeline |
| 4 | `_increment_epoch` | `ingest/service.py:76-95` | 20 | 递增执行 epoch 的函数，全代码库零调用 |
| 5 | `run_pipeline_task` | `platform/task/pipeline_runner.py:56-102` | 47 | Celery 任务包装器，但 retrieve 走 `run_pipeline_sync` 直接调 |
| 6 | `ingest_v3.yaml` | `pipelines/ingest_v3.yaml` | 37 | 与 `ingest_v1.yaml` 逐字节相同 |
| 7 | 全局 `_authz_circuit_breaker` | `permission/authz.py:33-39` | 7 | 声明显式但从未使用（装饰器创建独立实例） |
| 8 | TanStack React Query | `frontend/lib/query-provider.tsx` | — | `QueryClient` 已配置但所有页面用 `useState`+`useEffect` 手动获取 |

**总计**: 约 370 行已实现但未使用的代码。

---

# 第四部分：重复造轮子 / 未使用框架能力

| # | 问题 | 涉及模块 | 设计期望 | 实际实现 |
|---|------|---------|---------|---------|
| 1 | 合成不使用 Haystack | B-CHAT | `PromptBuilder` + `LiteLLMGenerator` | 手工 f-string + `invoke_llm()` |
| 2 | 模型调用不使用 Haystack Integration | P-MODEL | `SentenceTransformersDocumentEmbedder` 等 | OpenAI SDK / raw HTTP / FlagEmbedding |
| 3 | 权限镜像自维护 | P-AUTHC | 调用 Cerbos 管理面 API | 本地 `resource_registry`/`mount_registry` 表 + 直接 SQL |
| 4 | 自建电路断路器 | P-AUTHC | 使用库（如 `pybreaker`） | 手写 `CircuitBreaker` 类 |
| 5 | Metrics 自建存储 | P-OBS | OTLP 导出到 Prometheus | Python dict 内存存储 |
| 6 | DB 连接管理 | 多模块 | 连接池（如 `asyncpg.create_pool`） | 每请求 `asyncpg.connect()` + `close()` |
| 7 | 两个 JWT 库 | P-AUTHC | 统一 JWT 库 | `python-jose` + `PyJWT` 混用 |

其中 #1、#2、#3 偏离了设计文档的框架选型决策——Haystack 被选为算法框架但核心路径（生成合成、模型调用）完全绕过了它。

---

# 第五部分：前端诊断

### 5.1 前端完整度

对照 `docs/frontend-design.md` 的 34 个端点需求：

| 类别 | 需求数 | 已实现 | 缺口 |
|------|--------|--------|------|
| 认证与租户 | 5 | 4 | SSO/OAuth2 生产模式 |
| KB 管理 | 5 | 5 | 无 |
| 目录管理 | 6 | 6 | 无 |
| 文档管理 | 10 | 10 | 无 |
| 批量操作 | 2 | 2 | 无 |
| 会话 | 3 | 3 | 无 |
| 设置 | 3 | 3 | 无 |

**已实现的页面**: `/login`、`/select-tenant`、`/kb`（知识库管理）、`/chat`（对话）、`/settings`（设置）、`/dashboard`（仪表盘）、`/auth/callback`、`/not-authorized`

### 5.2 关键 Bug

**SSE token 流不工作**: 后端 `data:` 行无 `event: token` 前缀（`routes.py:211`），前端 `addEventListener("token", ...)` 无法匹配。实时逐 token 输出不可用，用户只在最终看到完整答案。

### 5.3 技术栈符合度

| 设计要求 | 实际 | 状态 |
|---------|------|------|
| Next.js 14 App Router | ✅ Next.js 14 | 一致 |
| shadcn/ui | ✅ shadcn/ui | 一致 |
| Zustand | ✅ Zustand (useAuthStore, useKBStore) | 一致 |
| TanStack Query | ⚠️ 已配置但所有页面手动 fetch | 未充分利用 |
| Vercel AI SDK useChat | ❌ 未使用，手动 EventSource | 偏离 |
| Recharts | ✅ Dashboard 页面使用 | 一致 |

---

# 第六部分：基础设施与测试

### 6.1 基础设施

- Docker Compose 基础设施（`docker-compose.infra.yml`）完整：PostgreSQL、Redis、Milvus、SeaweedFS、Cerbos 均在运行
- `resource_registry` 和 `mount_registry` 表**不在 `init.sql` 中**——它们由 `cerbos_client.py` 运行时创建（`INSERT ... ON CONFLICT` 依赖表已存在）
- 观测栈独立 `docker-compose` 部署

### 6.2 测试覆盖

| 测试类型 | 文件 | 数量 |
|---------|------|------|
| 契约测试 | `tests/contract/test_joint_12_20.py` | 仅 1 个文件，部分测试 `@pytest.mark.skip` |
| 集成测试 | `tests/integration/` | **目录为空** |
| 单元测试 | — | **不存在** |

设计文档 §27.1 要求的 CI 强制契约测试（权限出口唯一性、零判定断言、六条件完整性、两路 filter 一致性、事后过滤禁令等）**全部未实现**。设计文档 §27.2 的 20 项联合契约测试仅 1 个文件。

---

# 第七部分：风险矩阵

| 风险 | 严重度 | 影响面 | 修复复杂度 |
|------|--------|--------|-----------|
| execution_epoch 栅栏断裂 | **严重** | 所有重新解析失败 | 低（传参即可） |
| P-AUTHC 防腐层被绕过 | **严重** | 权限决策安全边界模糊 | 中（重构调用链） |
| `enforce_jwt=False` 多处残留 | **严重** | 生产环境权限全绕过 | 低（改配置+删 fallback） |
| VisibilityChanged 事件订阅缺失 | **高** | 权限变更不实时生效 | 高（需事件流对接） |
| SSE token 流不工作 | **高** | 用户体验降级 | 低（加 `event:` 前缀） |
| 合成绕过 Haystack | **中** | Prompt 版本管理缺失 | 中（重构合成层） |
| 三态映射不完整 | **中** | indeterminate 误判为 deny | 低（加映射分支） |
| S3 失败回退测试文本 | **高** | 生产文档内容被替换 | 低（删 fallback） |
| 事务原子性缺失 | **中** | 镜像不一致风险 | 中（加事务包装） |
| Metrics 仅在内存 | **中** | 生产无监控 | 中（接 OTLP Metrics） |
| 零契约测试 | **高** | 回归风险 | 高（需大量编写） |

---

# 第八部分：修复优先级建议

### P0 — 不修复无法投产

1. **修复 execution_epoch 传递**（`kb_routes.py:511` → 读取当前 epoch 传入 `delay()`）
2. **修复 S3 fallback 测试文本**（`ingest/service.py:154-156` → 抛异常而非静默替换）
3. **关闭 enforce_jwt=False 旁路**（全局搜索 → 生产强制 JWT 验证）
4. **修复 SSE token 事件格式**（`routes.py:211` → `event: token\ndata: {...}`）
5. **将生命周期调用收敛到 P-AUTHC 门面**（`doc/service.py` → 调 `authz.register_resource()` 等而非直写 SQL）

### P1 — 安全边界加固

6. **实现 VisibilityChanged 事件订阅**（P-AUTHC 订阅权限服务事件流 → 转交 B-INGEST 盖戳）
7. **实现三态映射完整版**（cerbos_client.py → EFFECT_INDETERMINATE 独立处理）
8. **所有 CerbosClient 调用收敛到 P-AUTHC**（消除 6 处 `get_client()` 旁路）
9. **修复 `_headers()` 传递 client_id**（Cerbos 能区分调用方身份）

### P2 — 架构对齐

10. **B-CHAT 合成接入 Haystack PromptBuilder + LiteLLMGenerator**
11. **P-MODEL 封装 Haystack Embedder/Ranker（或更新设计文档承认偏离）**
12. **接入死代码组件**（HierarchicalMerger、PrefilterInjector）或清理
13. **P-OBS Metrics 接 OTLP 导出**
14. **编写契约测试**（至少覆盖 §27.1 权限相关项）

---

## 附录 A: 模块代码量统计

| 模块 | 文件数 | 行数 | 占总量 |
|------|--------|------|--------|
| P-AUTHC (permission/) | 5 | 1,137 | 22.7% |
| B-INGEST (ingest/) | 6 | 653 | 13.0% |
| B-RETRIEVE (retrieve/) | 8 | 575 | 11.5% |
| B-CHAT (chat/) | 3 | 564 | 11.2% |
| B-DOC (doc/) | 4 | 557 | 11.1% |
| P-MODEL (platform/model/) | 2 | 388 | 7.7% |
| P-TASK (platform/task/) | 5 | 452 | 9.0% |
| P-CONFIG (platform/config/) | 2 | 168 | 3.3% |
| P-AUDIT (platform/audit/) | 2 | 133 | 2.7% |
| P-OBS (platform/obs/) | 4 | 217 | 4.3% |
| P-STORE (platform/store/) | 3 | 136 | 2.7% |
| API (api/) | 8 | ~1,400 | — |
| **总计** | **56** | **~5,000** | **100%** |

## 附录 B: 诊断方法说明

- **代码审查**: 逐文件阅读全部 56 个 Python 源文件
- **交叉验证**: 对关键概念（如 `get_client` 调用位置、`enforce_jwt` 使用点）进行全库 grep 交叉验证
- **设计对照**: 将设计文档 §6-§16 的每个接口/组件规格与实际代码逐一比对
- **静态分析**: 通过 import 图分析模块间依赖是否违反依赖方向规则

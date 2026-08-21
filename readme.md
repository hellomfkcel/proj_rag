# RAG v14 — 企业级多知识库检索增强生成系统

多知识库 RAG 平台。检索算法委托 Haystack 2.x 实现，授权判定委托外部权限服务（Cerbos PDP）完成，系统内部不做任何权限判定，仅负责消费决策、执行过滤、传递上下文。

| 维度 | 内容 |
|------|------|
| 算法框架 | Haystack 2.x，Pipeline YAML 编排 + `@component` 防腐层 |
| 检索链路 | BGE-M3 稠密 + 稀疏双路召回 → RRF / 加权融合 → BGE-Reranker 重排 → 父子分块层级合并 |
| 权限链路 | 三层 PEP（prefilter 注入 / 过采样补检索 / 逐条复核）+ 盖戳管道（可见性投影至向量库 payload） |
| 运行形态 | FastAPI（分发，不计算）+ 4 类 Celery worker（计算，不分发）+ Next.js 前端 |
| 交付形态 | `docker-compose.infra.yml`（基础设施）+ `docker-compose.app.yml`（计算层）+ Makefile |

---

## 目录

1. [设计目标与约束](#1-设计目标与约束)
2. [系统架构](#2-系统架构)
3. [核心机制](#3-核心机制)
4. [组件选型](#4-组件选型)
5. [快速开始](#5-快速开始)
6. [端到端验证](#6-端到端验证)
7. [API 速查](#7-api-速查)
8. [配置说明](#8-配置说明)
9. [测试](#9-测试)
10. [目录结构](#10-目录结构)
11. [开发约束](#11-开发约束)
12. [文档索引](#12-文档索引)

---

## 1. 设计目标与约束

系统的复杂度集中在三个约束上，其余设计均由此派生。

### 1.1 授权必须前置到向量库查询条件

向量检索按相似度排序，不感知访问控制。若采用事后过滤（query-then-filter，即召回 top-k 后在应用层剔除无权限项），会产生三个后果：

- **召回塌陷**：top-10 中 9 条无权限时用户仅得 1 条，而库中仍有其他可见内容未被召回；
- **数量泄漏**：向用户暴露「共 47 条，可见 3 条」时，47 本身即为泄漏；
- **上下文泄漏**：被剔除的片段已经过 rerank，可能进入 prompt。

系统的做法是将权限条件下沉为向量库的标量过滤表达式（层 1 prefilter 注入），并通过盖戳管道预先将可见性物化进 chunk payload。过滤发生在 ANN 搜索之前。

### 1.2 授权判定外置，调用收敛到单一出口

授权是跨系统关注点，同一套 ACL 需同时服务 RAG、报表、审批等多个应用。判定内建会导致权益数据锁死在单一应用内，且合规审计时无法追溯决策依据。

系统的做法：

- 所有授权决策由外部权限服务（Cerbos PDP）做出，系统不存 ACL、不写策略、不做判定；
- 所有对权限服务的调用只经过 P-AUTHC（`src/permission/`）一个模块；
- 该约束由契约测试强制。`tests/contract/test_permission_discipline.py` 扫描全仓库源码，业务模块中出现本地判定或直接 import `cerbos_client` 即测试失败。

### 1.3 算法框架经防腐层引入

切分策略、融合算法、rerank 模型的迭代周期短于业务代码。若框架类型出现在模块接口签名中，框架升级或替换将波及业务代码。

系统沉淀的长期资产是四个能力契约，而非框架 API：

```
切分契约:     split(document)           → list[Document]
融合契约:     fuse(dense, sparse)       → list[Document]
层级合并契约: merge_levels(leaf_chunks) → list[Document]
合成契约:     synthesize(query, chunks) → Answer
```

Haystack 在系统中的定位等同于 VectorStore 抽象下的 Milvus：当前实现，可替换。权限服务的定位不同，它是有双向契约的外部系统，P-AUTHC 的作用是将契约变更的影响面收敛到一个模块。

---

## 2. 系统架构

### 2.1 四方协作

RAG 系统在权限体系中的角色是消费方与执行方，不是管理方。

```
┌───────────────┐   ┌──────────────────┐   ┌──────────────────┐
│  IdP          │   │  权限服务 PDP     │   │  管理台           │
│  (Keycloak)   │   │  (Cerbos 0.39)   │   │  (Admin Console) │
│               │   │                  │   │                  │
│ • 用户/组/角色 │   │ • 策略评估        │   │ • 角色绑定        │
│ • JWT 签发    │   │ • 五端点决策面    │   │ • 权限授予/回收   │
│ • SSO         │   │ • 可见性投影面    │   │ • 限制/审计管理   │
└───────┬───────┘   └────────┬─────────┘   └────────┬─────────┘
        │ JWT                │ 决策 / 戳记            │ 管理操作
        ▼                    ▼                       │（本系统仅提供跳转链接）
┌────────────────────────────────────────────────────┼──────────┐
│  RAG v14 本系统                                     │          │
│                                                    │          │
│  ┌──────────────────────────────────────────┐      │          │
│  │ P-AUTHC — 访问权限服务的唯一出口            │      │          │
│  │  build_context / check / check_batch       │      │          │
│  │  filter_items / get_prefilter / compile_   │      │          │
│  │  filter / get_visibility / mint_ctx_token  │      │          │
│  │  register / link / unlink / retire         │      │          │
│  │  三态映射 · 四类 fail-closed · 熔断器       │      │          │
│  └──────────────────────────────────────────┘      │          │
│                                                    │          │
│  前端（权限感知 UI）：403 页 → 管理台链接 ──────────┘          │
└───────────────────────────────────────────────────────────────┘
```

| 参与方 | 角色 | 交互方式 |
|--------|------|---------|
| IdP（Keycloak） | 身份源 | 前端跳转登录；后端本地校签 JWT（开发期 PEM，生产期 JWKS） |
| 权限服务（Cerbos PDP） | 决策权威 | 仅通过 P-AUTHC 调用五端点 |
| 管理台（Admin Console） | 授权管理 | 前端提供跳转入口，不内嵌、不代理 |
| RAG 本系统 | 权限消费与执行 | 路由拦截、检索注入、盖戳投影、UI 条件渲染 |

### 2.2 模块划分

依赖方向严格单向：

```
B-* 业务模块 ──▶ P-* 平台模块 ──▶ 外部权限服务
业务模块之间：仅允许内部接口调用或事件，禁止直读直写对方独占表
平台模块之间：互不依赖对方业务态
不存在依赖环
```

平台能力模块（横切，被依赖）：

| 模块 | 路径 | 职责 | 不做 |
|------|------|------|------|
| P-AUTHC | `src/permission/` | JWT 校签、ctx 构建、封装权限服务五端点、三态映射、四类 fail-closed、ctx_token 铸造、生命周期端口、熔断器 | 不存权益数据、不写策略、不做判定（含 `if owner then pass` 短路） |
| P-TASK | `src/platform/task/` | Celery 应用与 4 类队列、`run_pipeline_sync/async`、Redis Pub/Sub 流式回传、outbox relay、对账定时任务、OTel trace 跨进程传播 | 不持有业务逻辑 |
| P-MODEL | `src/platform/model/` | `invoke_llm` / `invoke_embedding` / `invoke_rerank` 门面、模型注册表、Prompt 版本池、Langfuse 模型观测 | 不决定哪个 KB 用哪个模型 |
| P-CONFIG | `src/platform/config/` | 检索参数四层级联、切分配置版本化、特性开关 | 不执行检索与切分 |
| P-AUDIT | `src/platform/audit/` | 审计事件落库、风险分级路由、`decision_id` 串联 | 不理解业务语义 |
| P-OBS | `src/platform/obs/` | OTel tracing、structlog、Metrics 门面 | 不评判检索质量、不做模型观测 |
| P-STORE | `src/platform/store/` | SeaweedFS S3 读写、签名 URL、删除 | 不理解文件语义、不去重 |

业务模块（纵向，依赖平台）：

| 模块 | 路径 | 职责 | 不做 |
|------|------|------|------|
| B-DOC | `src/doc/` | 文档登记与去重、挂载关系、目录树、写路径同步调用生命周期端口 | 不解析、不写执行状态、不碰 chunk、不碰权益数据 |
| B-INGEST | `src/ingest/` | 解析、切分、嵌入、写 Milvus、执行状态机、盖戳管道 | 不拥有挂载关系、不计算戳记（仅搬运） |
| B-RETRIEVE | `src/retrieve/` | 查询 Pipeline、三层权限链路、混合检索融合、rerank、层级合并 | 不做生成编排、不做权限判定、不做事后过滤 |
| B-CHAT | `src/chat/` | 对话与轮次存储、任务分发、SSE 流式、四种合成模式、引用校验 | 不做底层检索计算、不做权限判定 |

### 2.3 进程划分

```
                        ┌──────────────────────┐
   浏览器 ──► Nginx :80 │  / → frontend :3001  │
                        │  /api → api :8000    │
                        └──────────┬───────────┘
                                   │
                  ┌────────────────▼─────────────────┐
                  │ api (FastAPI)                    │  校验 / 分发 / SSE 转发
                  │ 约束：禁止调用 pipeline.run()     │
                  └───────┬───────────────┬──────────┘
                    Celery│               │Redis Pub/Sub
        ┌─────────────────┼───────────────┼────────────────┐
        ▼                 ▼               ▼                ▼
 ingestion-worker  retrieval-worker  stamping-worker  outbox-relay
 摄入 Pipeline      查询 Pipeline      盖戳管道         事件可靠投递
 长耗时，可长退避   在线，不长退避     低优先，可续跑
                                                     visibility-events
                                                     订阅 VisibilityChanged
                                   ▼
                  ┌────────────────────────────────┐
                  │ embedding-service :19500       │  BGE-M3 单进程持模型
                  │ worker 经 HTTP 共享 GPU         │  未配置则各自本地加载
                  └────────────────────────────────┘

基础设施：PostgreSQL · Redis · Milvus(+etcd/MinIO) · SeaweedFS · Cerbos
```

API 进程禁止执行 Pipeline 的原因：Haystack Pipeline 为同步阻塞执行，BGE-M3 嵌入与 rerank 单次耗时可达秒级，置于 FastAPI 事件循环内会拖垮 API 进程并发。API 仅负责 `.delay()` 分发，计算在 worker 内完成，结果经 Redis Pub/Sub 回流为 SSE。

### 2.4 单一写者

每张表有且仅有一个写者模块。

| 数据 | 唯一写者 | 其他模块获取方式 |
|------|---------|----------------|
| `documents` / `document_kb_mounts` / `directories` / `outbox` | B-DOC | 经 B-DOC 接口或其事件 |
| `ingest_executions` / chunks | B-INGEST | 经 B-INGEST 接口或事件 |
| Milvus chunk payload 的 `allow_stamps` / `deny_stamps` / `vis_version` | B-INGEST 盖戳管道 | 内容唯一来源是权限服务 `/v1/visibility` |
| `audit_logs` | P-AUDIT | 只写不读，查询走 P-AUDIT 接口 |
| `retrieval_configs` / `chunking_configs` | P-CONFIG | 经 `resolve_*` 门面 |
| `conversations` / `conversation_turns` | B-CHAT | 经 B-CHAT 接口 |
| `model_registry` / `prompt_*` | P-MODEL | 经 `resolve_*` / `invoke_*` 门面 |
| ACL / 角色绑定 / 限制 | 权限服务（不在本系统） | 仅经五端点，禁止直查 |
| 结构镜像（resource_registry / mount_registry） | 权限服务（不在本系统） | 由 B-DOC 写路径同步调 register / link / unlink / retire 维护 |

---

## 3. 核心机制

### 3.1 文档摄入链路

```
POST /api/v1/documents/upload
  │
  ├─ check(ctx, "kb:write", "kb", kb_id)         路由级 PEP，拒绝即 403
  │
  ├─ B-DOC.submit_ingest_task()
  │    ├─ SHA-256 内容去重（同一文件多 KB 挂载只存一份物理文件）
  │    ├─ P-STORE.put() → SeaweedFS S3
  │    ├─ 写 documents + document_kb_mounts
  │    ├─ P-AUTHC.register_resource(doc)         同步维护权限服务结构镜像
  │    ├─ P-AUTHC.link_resource(doc, kb)         挂载关系入镜像
  │    └─ 写 outbox（DocumentMounted 事件）
  │
  ├─ ingestion_queue ──► ingest_document_task
  │    │  Haystack 摄入 Pipeline（pipelines/ingest_v*.yaml）
  │    ├─ HierarchicalDocumentSplitter   父块 + 子块两级切分
  │    ├─ BGE_M3DocumentEmbedder         稠密 + 稀疏双路向量
  │    ├─ PermissionMetadataEnricher     注入 tenant_id / kb_id / retrievable 等元数据
  │    └─ MilvusDocumentStoreWriter      写入 rag_documents collection
  │
  └─ stamping_queue ──► stamp_channel_task       chunk 转为可检索的唯一路径
```

摄入完成不等于可被检索。chunk 写入 Milvus 时 `vis_version = 0`，而层 1 过滤器要求 `vis_version > 0`，因此只有盖戳完成后 chunk 才会进入检索结果。这是 fail-closed 的结构性保证，不依赖运行时判断。

### 3.2 盖戳管道

盖戳管道将权限服务的可见性决策物化进 Milvus chunk payload，使权限条件可在 ANN 阶段生效。`src/ingest/service.py:stamp_channel_task` 实现六条纪律：

| # | 纪律 | 理由 |
|---|------|------|
| 1 | 失败时不写入、不 ack | 空戳记等价于全员可见或全员不可见，两者均为事故；不 ack 使 Celery 重投 |
| 2 | `unmounted == true` 时清空该通道戳记 | 解除挂载须立即从检索面消失 |
| 3 | 版本单调：`response.version < 当前 vis_version` 时丢弃 | 乱序事件不得回滚较新的决策 |
| 4 | 分批 upsert（500/批），批间让渡 CPU | 大文档不阻塞 worker |
| 5 | 游标记录，支持断点续跑 | 崩溃重投经覆盖写达成幂等 |
| 6 | 审计 `STAMP_APPLIED` 采用 fail-open | 盖戳量大，审计不阻塞主流程 |

戳记内容只搬运不加工。`allow_stamps` 存储主体的原始形态（`user:u1`、`group:eng`、`role:admin`），禁止展开成员：`group:eng` 到此为止，不展开为该组当前的 user 列表。组成员变化由权限服务发出 `VisibilityChanged` 事件触发重新盖戳，不由本系统推导。

### 3.3 三层检索链路

实现位于 `src/retrieve/service.py:retrieve()`。

```
                     用户提问 + kb_ids
                            │
┌───────────────────────────▼──────────────────────────────────┐
│ 层 1 · prefilter 编译注入（检索前）                            │
│                                                              │
│  get_prefilter(ctx) ──► 权限服务 /v1/prefilter                │
│      ├─ 返回 SUSPENDED  → 整体拒答（非空结果）                 │
│      └─ 返回 PreFilter                                        │
│                                                              │
│  候选 KB = prefilter.kbs ∩ 业务传入的 kb_ids                   │
│  交集为空时直接返回，不发起向量查询                             │
│                                                              │
│  compile_filter() 逐 KB 编译六条件 MetadataFilter：            │
│    ① tenant_id == ctx.tenant_id             租户隔离          │
│    ② kb_id == <当前 KB>                     知识库隔离         │
│    ③ OR(json_contains(allow_stamps, p))     主体命中允许戳     │
│    ④ retrievable == true                    挂载启用          │
│    ⑤ AND(NOT json_contains(deny_stamps, p)) 未命中拒绝戳       │
│    ⑥ vis_version > 0                        已完成盖戳         │
│                                                              │
│  dense 与 sparse 两路 Retriever 必须接收同一 filter 对象        │
└───────────────────────────┬──────────────────────────────────┘
                            ▼
┌──────────────────────────────────────────────────────────────┐
│ 层 2 · 过采样与补检索                                          │
│   k' = top_k × oversample_factor（默认 1.5）                  │
│   结果 < min_results（默认 3）时补检索，上限 refetch_max_rounds │
│   补检索期间过滤条件保持完全一致，不得放宽                       │
└───────────────────────────┬──────────────────────────────────┘
                            ▼
┌──────────────────────────────────────────────────────────────┐
│ 层 3 · strict 库逐条复核（仅 strict=true 的 KB）                │
│   filter_items(ctx, [(doc_id, kb_id), ...]) → /v1/filter      │
│   批次 ≤ 200；失败或超时按整批拒绝；永久禁止缓存                 │
└───────────────────────────┬──────────────────────────────────┘
                            ▼
              rerank 得分过滤 → 截断 top_k → 返回
```

三层的分工：

- 层 1 保证召回正确性。过滤在 ANN 之前完成，不产生召回塌陷与数量泄漏。
- 层 2 补偿过滤造成的结果不足。用过采样换回结果数量，不以放宽条件为代价。
- 层 3 处理时效性。戳记为异步投影，权限变更到盖戳完成之间存在秒级窗口。高敏感 KB 开启 `strict`，以一次实时 `/v1/filter` 消除该窗口，代价是每次查询增加一跳。非 strict 库由 `VisibilityChanged` 事件驱动的重新盖戳自愈。

### 3.4 混合检索与融合

查询 Pipeline（`pipelines/query_v4.yaml` 为 RRF，`query_v5.yaml` 为加权求和）：

```
BGE_M3TextEmbedder ─┬─ embedding ──────► MilvusDenseRetriever  (top_k 20, +filter)
                    └─ sparse_embedding ► MilvusSparseRetriever (top_k 20, +filter)
                                                    │
                            WeightedFusionJoiner ◄──┘   RRF / weighted_sum, top_k 30
                                     │
                            BGEReranker（BGE-Reranker-v2）top_k 10
                                     │
                            HierarchicalMerger  子块命中回捞父块上下文，window=3
```

三种检索模式，按请求参数或 KB 配置切换：

| `retrieval_mode` | Pipeline | 适用场景 |
|-----------------|----------|---------|
| `hybrid`（默认） | `query_v4` / `query_v5` | 通用场景，语义与关键词互补 |
| `vector_only` | `retrieval_v1` | 纯语义问答，query 与文档措辞差异大 |
| `keyword_only` | `query_v2` | 精确术语、编号、专有名词检索 |

层级合并处理小块召回精度与大块回答完整性的矛盾：摄入时切分为父块与子块两级，检索命中子块，送入 LLM 前将相邻子块与父块上下文合并。

### 3.5 对话与生成

```
POST /api/v1/conversations/query
  ├─ 确保 conversation 存在，分配 turn_index
  ├─ mint_ctx_token(ctx, audience="retrieval-worker", ttl_s=600)   不传 JWT 原文
  ├─ resolve_retrieval_config(kb_id, tenant_id)                     四层级联
  ├─ retrieve_and_generate_task.delay(...) → retrieval_queue
  └─ 立即返回 {conversation_id, turn_index, trace_id, trace_ui_url}

GET /api/v1/conversations/{id}/stream?turn_index=N   (SSE)
  ├─ 先查 DB：worker 已完成时直接回放 retrieved / token / done
  └─ 否则订阅 Redis Pub/Sub `query-stream:{conv}:{turn}`，60s 空闲超时
```

四种合成模式（`src/chat/service.py`），可显式指定或由 `auto` 按 chunk 数选择：

| 模式 | 实现 | 适用 |
|------|------|------|
| `compact` | 全部 chunk 拼入单个 prompt，一次生成 | chunk 数少，上下文窗口充足 |
| `refine` | 分批迭代，每批在上一轮答案基础上精修 | chunk 数多且需全部纳入 |
| `tree_summarize` | 分批总结后再总结 | chunk 数很多，答案偏综述 |
| `no_synthesis` | 只返回原文片段，不生成 | 只需证据不需结论 |

生成侧有两道校验：

- `validate_citations()`：答案引用的 chunk_id 必须属于本次候选集合，防止凭空引用；
- `check_verbatim_ratio()`：最长公共子串占比超过阈值（默认 0.60）时判定为逐字复制。

拒答纪律：无权限导致的空结果与知识库中确实不存在，使用完全相同的措辞，用户无法据措辞差异推断资源存在性。

### 3.6 配置级联与模型注册

检索参数四层级联（`resolve_retrieval_config`，后者覆盖前者）：

```
tenant 默认 ──► kb 级 ──► conversation 级 ──► 本次请求（turn）级
```

请求级参数写入 `conversation_turns` 的参数快照，供审计追溯该答案所用的参数。

模型切换通过 `.env` 或 `model_registry` 表完成，无需改动代码：

```bash
# 本地 Ollama
LLM_BASE_URL=http://localhost:11434
LLM_MODEL=qwen2.5-coder:14b

# 切换到企业 vLLM
LLM_BASE_URL=http://vllm.internal:8000/v1
LLM_MODEL=Qwen2.5-72B
LLM_API_KEY=your-key
```

### 3.7 可观测、审计与失败语义

| 信号 | 归属 | 出口 |
|------|------|------|
| Trace（Haystack 每个 Component 一个 span） | P-OBS | OTel Collector → Tempo |
| 结构化日志 | P-OBS | structlog → OTel → Loki |
| Metrics | P-OBS | OTel → Prometheus → Grafana |
| 模型调用（prompt / completion / token / 成本） | P-MODEL | Langfuse |
| 审计（`AUTH_ALLOW` / `AUTH_DENY` / `STAMP_APPLIED` / `KB_QUERY` / `CHUNK_FILTERED`） | P-AUDIT | `audit_logs` 表 |

trace context 在 API → Redis → Worker → Haystack Pipeline 全链路贯通。`celery_app.py` 的 `before_task_publish` 与 `task_prerun` 信号负责注入与还原，覆盖全部任务调度点。查询响应头携带 `X-Trace-Id`，前端可深链至 Grafana Tempo。

失败语义不得交叉污染：

| 组件不可达 | 行为 | 理由 |
|-----------|------|------|
| 可观测（P-OBS / Langfuse） | fail-open，业务继续 | 遥测丢失可接受 |
| 审计高风险事件（P-AUDIT） | fail-closed，事务回滚 | 合规义务 |
| 权限服务（外部） | fail-closed，全部拒绝 | 授权绕过不可逆 |

权限服务不纳入 `/readyz`。权限服务抖动时的预期行为是请求被拒绝，而非整个 RAG 服务被摘出负载均衡，两种语义须并存。该约束由 `test_permission_discipline.py:test_readyz_excludes_permission_service` 保证。

P-AUTHC 内置熔断器（默认 failure_threshold=10，recovery_timeout=60s）。权限服务持续失败时熔断打开，直接返回类型正确的 deny 值，不再向下游施压。

---

## 4. 组件选型

### 4.1 算法框架：Haystack 2.x

| 项 | 说明 |
|----|------|
| 关键理由 | `Pipeline` 提供 DAG 编排与 YAML 序列化，检索流程成为可版本化的配置而非代码；`@component` 以类型注解约束 I/O，构成防腐层；节点即 OTel span，无需手工埋点；官方 Integration 覆盖 Milvus、BGE、LiteLLM |
| 替代方案 | LlamaIndex（v13 采用）、LangChain、手写编排 |
| 未选 LlamaIndex 的原因 | `QueryEngine` 抽象层级偏高，注入自定义 MetadataFilter 与替换单个环节均需绕行；Haystack 的 Component 边界更清晰，替换单个 Component 不影响 Pipeline 其余节点 |
| 替换代价 | 低。四个能力契约构成接口，Haystack 类型不出现在任何模块签名中 |
| 未覆盖需自建的部分 | P-AUTHC 权限适配、盖戳管道、写路径结构镜像、Celery 异步包装（Haystack Pipeline 为同步执行） |

### 4.2 授权判定：外部权限服务（Cerbos PDP）

| 项 | 说明 |
|----|------|
| 外置理由 | 授权是跨系统关注点，同一套 ACL 需服务多个应用；判定内建将权益数据锁死在单一应用内，合规审计无法追溯决策依据 |
| 选择 Cerbos 的理由 | 策略即代码（YAML），可评审、可 diff、可版本化；Derived Roles 可直接表达「用户属性 + 资源属性 → 有效角色」；独立 PDP 进程，判定与业务进程解耦；决策返回 `decision_id`，支持跨系统审计串联 |
| 替代方案 | OPA / Rego（表达力更强，可读性与学习成本劣于 Cerbos）、Casbin（模型偏静态，难表达资源属性条件）、自建 ACL 表（回到 §1.2 的问题） |
| 隔离方式 | 五端点封装于 P-AUTHC，base URL、超时、重试、三态映射、Envelope 构造仅在此处存在。`AUTHZ_SERVICE_MODE=local` 直连 Cerbos PDP，`remote` 走外部权限服务后端，业务代码无感知 |
| 替换代价 | 中。防腐层的目标不是替换权限服务，而是将双向契约变更的影响面收敛到一个模块 |

### 4.3 向量库：Milvus 2.4

| 项 | 说明 |
|----|------|
| 关键理由 | 单集合同时支持稠密与稀疏向量及混合检索，可容纳 BGE-M3 的双路输出；标量过滤支持 `json_contains`；提供官方 Haystack Integration；支持 upsert，盖戳管道据此实现幂等覆盖写 |
| 决定性因素 | `json_contains` 过滤能力。缺少该能力时 `allow_stamps` / `deny_stamps` 无法表达为查询条件，只能退化为事后过滤（§1.1） |
| 替代方案 | Qdrant（过滤能力强，稀疏向量支持较晚）、pgvector（少一个组件，十万级以上召回质量与性能吃紧）、Elasticsearch（关键词强，向量弱） |
| 替换代价 | 中。需重写 `MilvusDenseRetriever`、`MilvusSparseRetriever`、`MilvusDocumentStoreWriter` 三个 Component 与 `_compile_filter_expr` 的表达式编译 |

### 4.4 嵌入与重排：BGE-M3 + BGE-Reranker-v2

| 项 | 说明 |
|----|------|
| 选择 BGE-M3 的理由 | 单模型同时输出稠密向量与稀疏权重，混合检索两路共用一次前向；中文语料表现良好；支持长文本 |
| 引入 Reranker 的理由 | 向量召回为粗筛，Cross-Encoder 重排为精排。融合后 top-30 经 rerank 取 top-10 |
| 服务化设计 | 每个 worker 独立加载 BGE-M3 会重复占用数 GB 显存。配置 `EMBEDDING_SERVICE_URL` 指向 `embedding-service:19500` 后，worker 经 HTTP 共享同一份 GPU 模型；未配置时降级为本地加载 |
| 替代方案 | OpenAI text-embedding-3（需出网，涉及数据出境）、m3e / bce（稀疏能力弱） |

### 4.5 任务队列：Celery + Redis

| 项 | 说明 |
|----|------|
| 关键理由 | Haystack Pipeline 同步阻塞，须移出 API 进程；三类工作负载特征差异大，需独立队列独立扩缩容 |
| 四类队列 | `ingestion_queue`（长耗时，可长退避重试）、`retrieval_queue`（用户在线等待，不做长退避）、`stamping_queue`（低优先级，可分批、可断点续跑）、`outbox-relay`（事件可靠投递） |
| 盖戳独立成队列的原因 | 盖戳为高吞吐低优先级任务，与摄入队列混用会阻塞用户可见的解析任务 |
| Redis 的三重角色 | Celery broker、result backend、SSE 流式回传的 Pub/Sub 通道 |
| 替代方案 | RQ（无 beat 与路由能力）、Dramatiq、Arq、Kafka（偏重，且系统不需要日志保留语义） |

### 4.6 其余组件

| 组件 | 选择 | 关键理由 | 替代方案 |
|------|------|---------|---------|
| 关系库 | PostgreSQL 16 + asyncpg | 事务保证 outbox 与业务写同库同事务，这是至少一次投递的前提；JSONB 存参数快照 | MySQL（JSON 能力与约束支持偏弱） |
| 对象存储 | SeaweedFS（S3 网关） | 轻量，单容器可运行，S3 兼容便于生产切换 MinIO 或云 OSS | MinIO（更重）、本地磁盘（不可横向扩展） |
| LLM 网关 | LiteLLM | 单一接口对接 Ollama、vLLM、OpenAI、DeepSeek，换模型仅改 `.env` | 直连各家 SDK（每换一家改一次代码） |
| 可观测 | OTel Collector + Tempo / Loki / Prometheus + Grafana | 厂商中立；单一入口单一出口；Haystack 内置 tracing 可直接接入 | 各家 APM（存在锁定） |
| 模型观测 | Langfuse | 通用 APM 不采集 prompt、completion、token 与成本，该信号线需单独走 | Phoenix、自建 |
| RAG 评估 | RAGAS | `POST /api/v1/query/eval` 与线上 `/query` 复用同一套检索生成核心，仅额外输出中间结果，保证评测与线上一致 | 人工评测（不可回归） |
| 认证 | Keycloak + 本地校签 | 开发期 RS256 自签（`config/*.pem`），生产期从 JWKS 拉公钥，代码路径一致 | 自建用户体系（回到 §1.2 的问题） |
| 前端 | Next.js 14 + shadcn/ui + TanStack Query + Zustand | App Router 支持 SSE 流式；`doc:view` 要求服务端渲染而非签名 URL | 纯 SPA（不满足服务端渲染要求） |

---

## 5. 快速开始

### 5.1 前置条件

- Docker 与 docker-compose
- Python 3.11（Makefile 默认 conda 环境名 `rag_dev_v14`）
- Node.js 18+（前端）
- GPU 可选。无 GPU 时 BGE-M3 走 CPU，速度显著下降但可运行
- LLM 服务：本地 Ollama 或任意 OpenAI 兼容端点
- Keycloak：登录接口经 Keycloak password grant 验证，需先启动并配置 realm

### 5.2 启动步骤

```bash
# 1. 配置环境变量
cp .env.example .env
#    至少填写 POSTGRES_PASSWORD / REDIS_PASSWORD / LLM_BASE_URL / LLM_MODEL

# 2. 启动基础设施（约 30–60 秒，主要等待 Milvus）
make infra
docker-compose -f docker-compose.infra.yml ps      # 全部 healthy 后继续

# 3. 初始化数据库
make db-init      # 建表，首次执行一次
make db-seed      # 写入开发期测试数据

# 4. 预热模型（BGE-M3 约 2GB，仅首次）
make warmup

# 5. 分终端启动进程（按需，无需全部启动）
make dev-api        # 终端 1：FastAPI :8000
make dev-ingest     # 终端 2：摄入 worker
make dev-retrieve   # 终端 3：检索 worker
make dev-stamp      # 终端 4：盖戳 worker
make dev-relay      # 终端 5：outbox relay
make dev-frontend   # 终端 6：Next.js :3001
```

### 5.3 按开发场景选择进程

| 开发内容 | 需启动 |
|---------|-------|
| 文档上传与解析 | `dev-api` + `dev-ingest` |
| 检索与问答 | `dev-api` + `dev-retrieve` |
| 权限盖戳 | `dev-api` + `dev-ingest` + `dev-stamp` |
| 事件投递与挂载变更 | 追加 `dev-relay` + `dev-visibility-events` |
| 对账定时任务 | 追加 `dev-beat` |
| 多 worker 共享 GPU | 先启 `dev-embedding`（:19500），并设置 `EMBEDDING_SERVICE_URL` |
| 前端 | `dev-frontend`（:3001，HMR） |

### 5.4 端口

基础设施端口做了偏移，避免与本机已有的 PostgreSQL / Redis 冲突。

| 服务 | 宿主机端口 | 容器内端口 | 用途 |
|------|-----------|-----------|------|
| API (FastAPI) | 8000 | 8000 | 对外 HTTP 入口 |
| 前端 (Next.js) | 3001 | 3001 | Web UI |
| Nginx（生产） | 80 | 80 | 统一入口，`/` → 前端，`/api` → 后端 |
| PostgreSQL | 25432 | 5432 | 业务库 / 审计 / outbox |
| Redis | 16379 | 6379 | Celery broker + 流式 Pub/Sub |
| Milvus | 19530 / 9091 | 19530 / 9091 | 向量库 gRPC / health |
| SeaweedFS | 18333 | 8333 | 对象存储 S3 网关 |
| Cerbos PDP | 13592 / 13593 | 3592 / 3593 | 权限判定 HTTP / gRPC |
| embedding-service | 19500 | 19500 | BGE-M3 共享推理 |
| etcd / MinIO | 内网 | 2379 / 9000 | Milvus 元数据与持久化，不对外暴露 |

`.env.example` 中的连接地址使用宿主机偏移端口（25432 / 16379 / 19530 / 18333 / 13592），容器内端口见上方表格。

### 5.5 生产部署

```bash
make deploy             # 全栈部署（= scripts/start.sh start，按序 infra→init-db→app）
make deploy-stop        # 停止全部（保留数据卷）
make deploy-restart     # 重启全部
make deploy-status      # 查看健康状态
make deploy-frontend    # 单独构建前端镜像
```

> Docker 化部署统一使用 `scripts/start.sh`（负责启动顺序、健康等待、init-db、日志），
> 详见 `docs/ops/RAG上线运维手册.md`。

生产与开发的差异：

| 配置项 | 开发 | 生产 |
|--------|------|------|
| 登录 | `POST /api/v1/auth/dev-login`（Keycloak password grant + 本系统签发 JWT） | SSO 授权码流（`/api/v1/auth/callback`） |
| JWT 校签 | 本地 PEM（`config/jwt_public.pem`） | JWKS URL（`JWT_JWKS_URL`，PyJWKClient 自动拉取） |
| 权限服务地址 | `http://localhost:13592`（local） | `remote`：`AUTHZ_SERVICE_URL=http://host.docker.internal:18080`（同机）或权限平台真实地址 |
| `AUTHZ_SERVICE_MODE` | `local`（直连 Cerbos PDP） | `remote`（走外部权限服务后端） |
| `CTX_TOKEN_SECRET` | 未配置时回退为 Redis URL hash | 必须显式配置独立 secret |
| `CORS_ALLOWED_ORIGINS` | `localhost:3001,localhost:3000` | 必须收敛至具体域名 |
| `ADMIN_CONSOLE_URL` | 可空，跳转按钮 disabled | 必填 |
| 入口 | 直连 :8000 / :3001 | Nginx :80 统一入口 |

---

## 6. 端到端验证

```bash
# 1. 登录获取 token
TOKEN=$(curl -s -X POST http://localhost:8000/api/v1/auth/dev-login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"<your-password>","tenant":"tenant-dev"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

# 2. 创建知识库
KB=$(curl -s -X POST http://localhost:8000/api/v1/knowledge-bases \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"name":"测试库"}' | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")

# 3. 上传文档，自动触发解析 → 摄入 → 盖戳
curl -s -X POST http://localhost:8000/api/v1/documents/upload \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@<本机任一文本文件>" -F "kb_id=$KB" -F "auto_parse=true"

# 4. 确认已可检索：chunk 的 vis_version 须大于 0
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/v1/knowledge-bases/$KB/documents"

# 5. 提问，接口立即返回，答案经 SSE 推送
RESP=$(curl -s -X POST http://localhost:8000/api/v1/conversations/query \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"question\":\"这份文档讲了什么？\",\"kb_ids\":[\"$KB\"]}")
echo $RESP     # {conversation_id, turn_index, trace_id, trace_ui_url}

# 6. 订阅流式结果
CONV=$(echo $RESP | python3 -c "import sys,json; print(json.load(sys.stdin)['conversation_id'])")
curl -N "http://localhost:8000/api/v1/conversations/$CONV/stream?turn_index=1"
```

答案为空时的排查顺序：

1. `dev-retrieve` worker 是否在运行；
2. chunk 的 `vis_version` 是否仍为 0（`dev-stamp` 未运行或 Cerbos 不可达）；
3. `get_prefilter` 是否返回 SUSPENDED（权限服务判定该主体被挂起）；
4. 候选 KB 交集是否为空（`prefilter.kbs ∩ kb_ids`）；
5. 以响应头 `X-Trace-Id` 在 Grafana Tempo 查看完整链路。

---

## 7. API 速查

所有端点前缀为 `/api/v1`，除健康检查外均需 `Authorization: Bearer <JWT>`。

| 分组 | 端点 | 说明 |
|------|------|------|
| 认证 | `POST /auth/dev-login` | 开发登录（Keycloak 验证 + 本系统签发 JWT） |
| | `POST /auth/token` · `POST /auth/refresh` | 令牌签发与刷新 |
| | `GET /auth/callback` | OIDC 授权码回调（生产 SSO） |
| | `POST /auth/check-permission` | 前端条件渲染所需的权限查询 |
| | `POST /auth/switch-tenant` | 切换租户 |
| 知识库 | `GET/POST /knowledge-bases` · `PATCH/DELETE /knowledge-bases/{id}` | KB CRUD |
| | `GET/PATCH /knowledge-bases/{id}/chunking-config` | 切分配置（版本化） |
| | `GET /knowledge-bases/{id}/documents` | KB 下文档列表 |
| 文档 | `POST /documents/upload` | 上传（登记 + 挂载 + 触发解析） |
| | `GET /documents/{id}` · `/chunks` · `/content` · `/download` | 详情 / 分块 / 只读渲染 / 下载 |
| | `POST /documents/{id}/trigger-parse` | 手动触发解析 |
| | `POST /documents/batch/parse` · `/batch/delete` | 批量操作 |
| | `DELETE /documents/{id}/kb/{kb_id}` | 从 KB 移除（unmount，非 purge） |
| 目录 | `GET /knowledge-bases/{id}/directories` · `POST/PATCH/DELETE /directories/...` | 目录树管理 |
| 对话 | `POST /conversations/query` | 提问，分发至 worker 后立即返回 |
| | `GET /conversations/{id}/stream` | SSE 流式结果（`retrieved` / `token` / `done` / `error`） |
| | `GET/POST /conversations` · `PATCH/DELETE /conversations/{id}` | 会话管理 |
| | `GET /conversations/{id}/turns` | 历史轮次 |
| 设置 | `GET /models` · `PATCH /models/{id}/set-default` | 模型注册表 |
| | `GET/PATCH /configs/retrieval` | 检索参数 |
| | `GET /prompts` · `PATCH /prompts/{id}/activate` | Prompt 版本池 |
| | `GET /config` · `GET /system/enums` | 前端配置与枚举 |
| 统计 | `GET /stats/usage` · `/top-kbs` · `/documents` · `/quality` | Dashboard 数据 |
| 评测 | `POST /query/eval` | RAGAS 评测入口，输出 contexts / retrieved_chunks / is_answerable |
| 健康 | `GET /healthz` · `GET /api/v1/readyz` · `GET /api/v1/ping` | `/readyz` 不含权限服务 |

---

## 8. 配置说明

配置唯一入口为 `src/config.py` 的 `Settings`，优先级为环境变量（`.env`）高于代码默认值。其他模块不得硬编码 URL、密码、模型名与端口。

```bash
# 基础设施
DATABASE_URL=postgresql+asyncpg://rag:${POSTGRES_PASSWORD}@localhost:25432/rag
REDIS_URL=redis://:${REDIS_PASSWORD}@localhost:16379/0
MILVUS_HOST=localhost
MILVUS_PORT=19530
S3_ENDPOINT_URL=http://localhost:18333

# 模型
LLM_BASE_URL=http://localhost:11434
LLM_MODEL=qwen2.5-coder:14b
EMBEDDING_MODEL=qwen3-embedding:0.6b
LLM_API_KEY=ollama
LLM_MAX_TOKENS=8192                            # 推理类模型需预留 reasoning token 预算
EMBEDDING_SERVICE_URL=http://localhost:19500   # 配置后 worker 共享 GPU 模型

# 权限服务
AUTHZ_SERVICE_MODE=local                       # local=直连 Cerbos PDP，remote=外部权限服务后端
AUTHZ_BASE_URL=http://localhost:13592
AUTHZ_SERVICE_URL=http://<perm-service>:18080  # remote 模式
AUTHZ_TIMEOUT_MS=2000
AUTHZ_PROJECT_ID=rag-v14
AUTHZ_CLIENT_CREDENTIAL=                       # 服务间 X-Api-Key，生产必填
CTX_TOKEN_SECRET=                              # ctx_token HMAC 密钥，生产必填

# 认证
KEYCLOAK_SERVER_URL=http://<keycloak>:8080
KEYCLOAK_REALM=rag-v14
KEYCLOAK_CLIENT_ID=rag-frontend
JWT_PUBLIC_KEY_PATH=./config/jwt_public.pem    # 开发
# JWT_JWKS_URL=https://.../certs               # 生产

# 可观测
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
LANGFUSE_PUBLIC_KEY= / LANGFUSE_SECRET_KEY= / LANGFUSE_HOST=

# 前端跳转链接
GRAFANA_URL= / LANGFUSE_PUBLIC_URL= / CERBOS_PUBLIC_URL= / ADMIN_CONSOLE_URL=

# 其他
CORS_ALLOWED_ORIGINS=http://localhost:3001,http://localhost:3000   # 生产收敛至具体域名
PIPELINE_YAML_DIR=./pipelines
```

依赖版本以 `requirements.txt` 为准，当前主要版本：`haystack-ai>=2.19`、`milvus-haystack==0.0.18`、`pymilvus>=2.6.15,<3`、`celery==5.4.0`、`fastapi==0.115.0`、`litellm==1.42.0`、`sentence-transformers==3.1.1`、`FlagEmbedding==1.4.0`、`opentelemetry-sdk==1.33.1`、`langfuse==4.14.0`、`ragas==0.1.21`。torch GPU 版本需按文件内注释单独安装。

---

## 9. 测试

```bash
pytest tests/contract/ -v       # 契约测试，CI 强制
pytest tests/integration/ -v    # 集成测试，需基础设施运行中
pytest tests/unit/ -v           # 单元测试
```

契约测试通过扫描源码将设计约束表达为可执行断言，覆盖范围：

| 测试文件 | 断言内容 |
|---------|---------|
| `test_permission_discipline.py` | `cerbos_client` 仅被 P-AUTHC import；无本地权限判定；无废弃动词；`doc:retrieve` 不走 `/v1/check`；六条件过滤器完整；无 query-then-filter 模式；dense 与 sparse 接收同一 filter 对象；日志中无 credential；`/readyz` 不含权限服务 |
| `test_joint_1_11.py` | 分享可检索性、跨 KB 隔离、通道封禁、退役四合一、戳记不展开、KB 粒度事件、strict 实时回收、非 strict 自愈 |
| `test_joint_12_20.py` | 动作与端点绑定、准入矩阵、`filter` 批次上限、`decision_id` 可追溯、超时 fail-closed、禁用文档不进 strict 范围 |

改动权限、检索或盖戳相关代码后，需先通过 `pytest tests/contract/ -v` 再提交。

---

## 10. 目录结构

```
├── src/
│   ├── main.py                  FastAPI 入口，启动顺序及其约束见文件内注释
│   ├── config.py                配置唯一入口
│   ├── api/                     REST 路由，仅做校验、分发、SSE 转发
│   ├── permission/              P-AUTHC，访问权限服务的唯一出口
│   │   ├── authz.py               五端点门面、compile_filter 六条件、熔断器
│   │   ├── cerbos_client.py       Cerbos PDP 客户端与结构镜像维护
│   │   ├── permission_service_client.py   remote 模式客户端
│   │   ├── context.py             RequestContext 构建与 ctx_token 解析
│   │   ├── middleware.py          AuthMiddleware，JWT 校验与 ctx 注入
│   │   └── visibility_events.py   VisibilityChanged 订阅与对账兜底
│   ├── platform/
│   │   ├── task/                P-TASK：celery_app / pipeline_runner / outbox_relay / reconciliation
│   │   ├── model/               P-MODEL：invoke_llm / embedding / rerank、Prompt 池、Langfuse
│   │   ├── config/              P-CONFIG：四层级联、切分配置版本化、feature flag
│   │   ├── audit/               P-AUDIT
│   │   ├── obs/                 P-OBS：tracing / logger / metrics
│   │   └── store/               P-STORE：SeaweedFS S3
│   ├── doc/                     B-DOC：登记去重、挂载、目录、生命周期端口
│   ├── ingest/                  B-INGEST：摄入任务与盖戳管道
│   │   └── components/            Haystack 摄入 Component（切分 / 嵌入 / 权限元数据 / 写入）
│   ├── retrieve/                B-RETRIEVE：三层检索链路
│   │   └── components/            Haystack 查询 Component（嵌入 / 双路召回 / 融合 / rerank / 层级合并）
│   ├── chat/                    B-CHAT：对话编排、四种合成模式、引用校验
│   ├── services/                embedding_service（GPU 模型共享）与客户端
│   └── scripts/                 init_db / seed_dev / warmup_models
├── pipelines/                   Haystack Pipeline YAML（ingest_v1–v5 / query_v1–v7 / retrieval_v2）
├── cerbos/                      Cerbos 策略：derived_roles、resource_policies、schemas
├── scripts/init.sql             建表 SQL
├── scripts/eval_ragas.py        RAGAS 离线评测（本地/手动）
├── scripts/start.sh             启动/停止/状态/日志 运维脚本
├── frontend/                    Next.js 14 前端（login / kb / chat / dashboard / settings / 403）
├── tests/                       contract / integration / unit / eval_sets
├── docs/archive/                历史诊断/阶段/参考拓扑文档归档
├── docker-compose.infra.yml     基础设施，开发期常驻
├── docker-compose.app.yml       计算层，部署使用
└── Makefile                     常用命令
```

---

## 11. 开发约束

以下约束来自设计文档，多数由契约测试强制执行。

权限：

1. 不得绕过 P-AUTHC 直接调用权限服务 HTTP 端点。
2. 不得在 Haystack Component 的 `run()` 内实现权限逻辑，`MetadataFilter` 由外部注入。
3. 不得在业务模块内构造权限服务 Envelope、地址或解释 reasons。
4. 不得实现本地权限判定，包括 `if uploaded_by == user_id` 形式的短路。
5. 不得缓存决策结果。`/v1/filter` 永久禁止缓存，`/v1/check` 默认关闭缓存。
6. JWT 原文不得进入日志、trace、审计 payload 与任务参数，异步链路使用 `ctx_token`（TTL ≤ 600s）。
7. 权限服务不得纳入 `/readyz`。

检索：

8. 不得使用事后过滤（query-then-filter）。
9. 补检索时不得放宽过滤条件，各轮 MetadataFilter 须完全一致。
10. prefilter 失败时不得回退为无过滤查询，应返回空或拒答。
11. 用户可见输出中不得暴露被过滤的数量，拒答措辞与「信息不足」完全一致。

盖戳：

12. 盖戳失败时不得写入空戳记，不 ack 并重试。
13. 不得展开 `allow_stamps` / `deny_stamps` 的成员，保持主体原始形态。
14. 不得在 B-INGEST 内计算或推导戳记值，仅搬运 `/v1/visibility` 的返回。

架构：

15. 业务模块不得 import 可观测 SDK，一律经 P-OBS 门面。
16. 不得绕过 P-MODEL 调用模型，Generator / Embedder / Ranker 实例封装于 P-MODEL 内。
17. 业务模块之间不得直读直写对方独占表。
18. API 进程不得调用 `pipeline.run()`，计算仅在 worker 内执行。
19. Haystack 类型不得出现在模块接口签名中。
20. 不得使用框架默认 prompt，模板须来自 `resolve_prompt`。
21. `doc:view` 不得使用签名 URL，须服务端渲染。

动词命名：权威源为上游契约的 16 个动词，不得新建、改名或使用废弃动词。已废弃的映射关系：`doc:write` → `kb:write`；`doc:delete` → `doc:purge` / `doc:unmount`；`acl:update` → `doc:share` / `kb:grant`。

新增代码可用的脚手架位于 `.claude/skills/`：`new-module`、`new-interface`、`new-table`、`new-event`、`new-haystack-component`、`new-pipeline-yaml`、`new-contract-test`。

---

## 12. 文档索引

| 文档 | 内容 |
|------|------|
| [`docs/ops/RAG上线运维手册.md`](docs/ops/RAG上线运维手册.md) | 上线运维：日常参数设置、启停/升级、日志观测、备份、排障、权限联调、安全清单 |
| [`docs/RAG系统设计v14.md`](docs/RAG系统设计v14.md) | 主设计规格：模块注册表、共享契约、七个平台模块与四个业务模块章节、三层检索、盖戳管道 |
| [`docs/RAG系统设计v14落地方案.md`](docs/RAG系统设计v14落地方案.md) | 四阶段落地路径，compose、Dockerfile、requirements、Cerbos 配置全文 |
| [`docs/权限管理系统架构设计.md`](docs/权限管理系统架构设计.md) | 权限四方协作、16 动词、准入矩阵、五条权限数据流、已实现清单、文件索引 |
| [`docs/外部系统设计.md`](docs/外部系统设计.md) | 外部权限服务后端与管理台的设计规格 |
| [`docs/外部系统实施方案.md`](docs/外部系统实施方案.md) | 外部系统实施细则 |
| [`docs/frontend-design.md`](docs/frontend-design.md) | 前端架构与实现 |
| [`docs/统一观测使用指南_跨域trace链路_20260817.md`](docs/统一观测使用指南_跨域trace链路_20260817.md) | 跨域 trace 链路使用指南（Tempo/Loki/Langfuse） |
| [`docs/deploy/security-checklist.md`](docs/deploy/security-checklist.md) | 上线安全检查清单 |
| [`docs/archive/`](docs/archive/) | 历史诊断/阶段/测试报告文档归档（git 历史同步可查） |
| [`CLAUDE.md`](CLAUDE.md) | 仓库开发约定 |

# RAG v14 — 企业级多知识库检索增强生成系统

> **一句话定位**：一个把「知识库检索」和「企业权限」彻底解耦的 RAG 平台。
> 检索算法委托 Haystack 2.x，授权判定委托外部权限服务（Cerbos PDP），
> **本系统内部零权限判定**——它只负责正确消费决策、正确执行过滤、正确传递上下文。

| | |
|---|---|
| **算法框架** | Haystack 2.x（Pipeline YAML + `@component` 防腐层） |
| **检索形态** | BGE-M3 稠密 + 稀疏双路混合检索 → RRF / 加权融合 → BGE-Reranker 重排 → 父子分块层级合并 |
| **权限形态** | 三层 PEP（prefilter 注入 / 过采样补检索 / 逐条复核）+ 盖戳管道（可见性投影进向量库 payload） |
| **运行形态** | FastAPI（只分发不计算）+ 4 类 Celery worker（只计算不分发）+ Next.js 前端 |
| **交付形态** | `docker-compose.infra.yml`（基础设施）+ `docker-compose.app.yml`（计算层）+ Makefile |

---

## 目录

1. [这个系统解决什么问题](#1-这个系统解决什么问题)
2. [系统架构](#2-系统架构)
3. [核心功能详解](#3-核心功能详解)
4. [组件选型与选择理由](#4-组件选型与选择理由)
5. [快速开始](#5-快速开始)
6. [端到端走一遍](#6-端到端走一遍)
7. [API 速查](#7-api-速查)
8. [配置说明](#8-配置说明)
9. [测试](#9-测试)
10. [目录结构](#10-目录结构)
11. [开发红线](#11-开发红线扩展代码前必读)
12. [文档索引](#12-文档索引)

---

## 1. 这个系统解决什么问题

普通 RAG demo 三行代码就能跑通：切分 → 嵌入 → 检索 → 塞给 LLM。
但它一进企业就会撞上三堵墙，本系统的全部复杂度都来自于翻越这三堵墙：

### 墙一：向量检索天生不认权限

向量库只认相似度，不认「谁能看这一段」。
常见的错误做法是**先查后过滤**（query-then-filter）：查回 top-10，再在应用层剔掉没权限的。
这有三个致命问题：

- **召回塌陷**——top-10 里 9 条没权限，用户只拿到 1 条，明明库里还有能看的；
- **数量泄漏**——"共找到 47 条，您可见 3 条"，47 这个数字本身就是泄漏；
- **上下文泄漏**——被过滤掉的片段可能已经进过 rerank 或 prompt。

**本系统的答案**：权限必须**下沉到向量库的查询条件里**（层 1 prefilter 注入），
并且提前把"谁能看"物化进 chunk 的 payload（盖戳管道）。
过滤发生在 ANN 搜索之前，而不是之后。

### 墙二：权限逻辑一旦散落，就再也收不回来

`if doc.owner == user.id: pass` 这种"顺手的短路"，写第一次很爽，
第二年做合规审计时，没人能回答"这个文档到底为什么可见"。

**本系统的答案**：**权限外置 + 单一出口**。

- 所有授权决策由外部权限服务（Cerbos PDP）做出，本系统**不存 ACL、不写策略、不做判定**；
- 所有对权限服务的调用只能经过 `P-AUTHC`（`src/permission/`）这一个模块；
- 这条纪律由**契约测试强制**（`tests/contract/test_permission_discipline.py`），
  比如 `test_no_local_permission_decisions` 会扫描全仓库源码，
  发现业务模块里出现本地判定就直接失败。

### 墙三：算法组件迭代快，业务代码不该跟着抖

切分策略、融合算法、rerank 模型每季度都在换。
如果 `LlamaIndex.QueryEngine` / `Haystack.Pipeline` 的类型出现在业务接口签名里，
换框架就等于重写系统。

**本系统的答案**：**框架经防腐层引入，类型不出签名**。
沉淀下来的长期资产是四个能力契约，而不是某个框架的 API：

```
切分契约:     split(document)              → list[Document]
融合契约:     fuse(dense, sparse)          → list[Document]
层级合并契约: merge_levels(leaf_chunks)    → list[Document]
合成契约:     synthesize(query, chunks)    → Answer
```

Haystack 在本系统中的地位，**等同于 VectorStore 抽象下的 Milvus——当前实现，可随时替换**。

---

## 2. 系统架构

### 2.1 四方协作全景（谁负责什么）

RAG 系统只是权限体系里的**消费方**，不是管理方：

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
        ▼                    ▼                       │（本系统只提供跳转链接）
┌────────────────────────────────────────────────────┼──────────┐
│  RAG v14 本系统（权限消费方 + 执行方）               │          │
│                                                    │          │
│  ┌──────────────────────────────────────────┐      │          │
│  │ P-AUTHC —— 访问权限服务的唯一出口          │      │          │
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

| 参与方 | 角色 | 本系统如何交互 |
|--------|------|---------------|
| IdP（Keycloak） | 身份源 | 前端跳转登录；后端本地校签 JWT（开发期 PEM，生产期 JWKS） |
| 权限服务（Cerbos PDP） | 决策权威 | **仅通过 P-AUTHC** 调用五端点 |
| 管理台（Admin Console） | 授权管理 | 前端提供跳转入口，**不内嵌、不代理** |
| **RAG 本系统** | 权限消费 + 执行 | 路由拦截、检索注入、盖戳投影、UI 条件渲染 |

### 2.2 模块视图（依赖方向严格单向）

```
B-* 业务模块 ──单向──▶ P-* 平台模块 ──单向──▶ 外部权限服务
业务模块之间：只允许「内部接口调用」或「事件」，禁止直读直写对方独占表
平台模块之间：互不依赖对方业务态；禁止任何依赖环
```

**平台能力模块**（横切、被依赖）：

| 模块 | 路径 | 职责 | 明确不做 |
|------|------|------|---------|
| **P-AUTHC** | `src/permission/` | JWT 校签、ctx 构建、封装权限服务五端点、三态映射、四类 fail-closed、ctx_token 铸造、生命周期端口、熔断器 | 不存权益数据、不写策略、**不做任何判定**（含 `if owner then pass`） |
| **P-TASK** | `src/platform/task/` | Celery 应用与 4 类队列、`run_pipeline_sync/async`、Redis Pub/Sub 流式回传、outbox relay、对账定时任务、OTel trace 跨进程传播 | 不持有业务逻辑 |
| **P-MODEL** | `src/platform/model/` | `invoke_llm/embedding/rerank` 门面、模型注册表、Prompt 版本池、Langfuse 模型观测 | 不决定哪个 KB 用哪个模型 |
| **P-CONFIG** | `src/platform/config/` | 检索参数四层级联、切分配置版本化、特性开关 | 不执行检索/切分 |
| **P-AUDIT** | `src/platform/audit/` | 审计事件落库、风险分级路由、`decision_id` 串联 | 不理解业务语义 |
| **P-OBS** | `src/platform/obs/` | OTel tracing / structlog / Metrics 门面 | 不评判检索质量、不做模型观测 |
| **P-STORE** | `src/platform/store/` | SeaweedFS S3 读写、签名 URL、删除 | 不理解文件语义、不去重 |

**业务模块**（纵向、依赖平台）：

| 模块 | 路径 | 职责 | 明确不做 |
|------|------|------|---------|
| **B-DOC** | `src/doc/` | 文档登记与去重、挂载关系、目录树、**写路径同步调生命周期端口** | 不解析、不写执行状态、不碰 chunk、不碰权益数据 |
| **B-INGEST** | `src/ingest/` | 解析 → 切分 → 嵌入 → 写 Milvus、执行状态机、**盖戳管道** | 不拥有挂载关系、**只搬运戳记不计算戳记** |
| **B-RETRIEVE** | `src/retrieve/` | 查询 Pipeline、三层权限链路、混合检索融合、rerank、层级合并 | 不做生成编排、**不做权限判定、不做事后过滤** |
| **B-CHAT** | `src/chat/` | 对话/轮次存储、任务分发、SSE 流式、四种合成模式、引用校验 | 不做底层检索计算、不做权限判定 |

### 2.3 进程视图（谁在跑，跑什么）

```
                        ┌──────────────────────┐
   浏览器 ──► Nginx :80 │  / → frontend :3001  │
                        │  /api → api :8000    │
                        └──────────┬───────────┘
                                   │
                  ┌────────────────▼─────────────────┐
                  │ api (FastAPI)                    │  ← 只做校验 / 分发 / SSE 转发
                  │ 红线：禁止调 pipeline.run()       │
                  └───────┬───────────────┬──────────┘
                    Celery│               │Redis Pub/Sub
        ┌─────────────────┼───────────────┼────────────────┐
        ▼                 ▼               ▼                ▼
 ingestion-worker  retrieval-worker  stamping-worker  outbox-relay
 摄入 Pipeline      查询 Pipeline      盖戳管道         事件可靠投递
 （长耗时长退避）   （在线不长退避）   （低优先可续跑）
                                                     visibility-events
                                                     订阅 VisibilityChanged
                                   ▼
                  ┌────────────────────────────────┐
                  │ embedding-service :19500       │  ← BGE-M3 单进程持模型
                  │ 所有 worker 经 HTTP 共享 GPU    │     （不设则各自本地加载）
                  └────────────────────────────────┘

基础设施：PostgreSQL · Redis · Milvus(+etcd/MinIO) · SeaweedFS · Cerbos
```

**为什么 API 进程禁止跑 Pipeline**：Haystack Pipeline 是同步阻塞的，
BGE-M3 嵌入 + rerank 单次可达秒级。放进 FastAPI 事件循环会拖垮整个 API 进程的并发。
所以 API 只负责 `.delay()` 分发，计算一律在 worker 内，结果经 Redis Pub/Sub 回流成 SSE。
这条红线由契约测试和代码审查共同保证。

### 2.4 单一写者原则（每张表只有一个写者）

数据不一致的根源往往是"两个模块都在写同一张表"。本系统的所有权划分：

| 数据 | 唯一写者 | 其他模块如何获取 |
|------|---------|----------------|
| `documents` / `document_kb_mounts` / `directories` / `outbox` | **B-DOC** | 经 B-DOC 接口或其事件 |
| `ingest_executions` / chunks | **B-INGEST** | 经 B-INGEST 接口或事件 |
| Milvus chunk payload 的 `allow_stamps`/`deny_stamps`/`vis_version` | **B-INGEST 盖戳管道（唯一写者）** | 内容唯一来源是权限服务 `/v1/visibility` |
| `audit_logs` | **P-AUDIT** | 只写不读，查询走 P-AUDIT 接口 |
| `retrieval_configs` / `chunking_configs` | **P-CONFIG** | 经 `resolve_*` 门面 |
| `conversations` / `conversation_turns` | **B-CHAT** | 经 B-CHAT 接口 |
| `model_registry` / `prompt_*` | **P-MODEL** | 经 `resolve_*` / `invoke_*` 门面 |
| ACL / 角色绑定 / 限制 | **★ 权限服务独占，不在本系统** | 只能经五端点，禁止直查 |
| 结构镜像（resource_registry / mount_registry） | **★ 权限服务独占** | 由 B-DOC 写路径同步调 register/link/unlink/retire 维护 |

---

## 3. 核心功能详解

### 3.1 文档摄入链路

```
POST /api/v1/documents/upload
  │
  ├─ check(ctx, "kb:write", "kb", kb_id)         ← 路由级 PEP（拒绝即 403）
  │
  ├─ B-DOC.submit_ingest_task()
  │    ├─ SHA-256 内容去重（同一文件多 KB 挂载只存一份物理文件）
  │    ├─ P-STORE.put() → SeaweedFS S3
  │    ├─ 写 documents + document_kb_mounts
  │    ├─ P-AUTHC.register_resource(doc)          ← 同步维护权限服务结构镜像
  │    ├─ P-AUTHC.link_resource(doc, kb)          ← 挂载关系入镜像
  │    └─ 写 outbox（DocumentMounted 事件）
  │
  ├─ ingestion_queue ──► ingest_document_task
  │    │  Haystack 摄入 Pipeline（pipelines/ingest_v*.yaml）
  │    ├─ HierarchicalDocumentSplitter   父块 + 子块两级切分
  │    ├─ BGE_M3DocumentEmbedder         稠密 + 稀疏双路向量
  │    ├─ PermissionMetadataEnricher     注入 tenant_id/kb_id/retrievable 等元数据
  │    └─ MilvusDocumentStoreWriter      写入 rag_documents collection
  │
  └─ stamping_queue ──► stamp_channel_task        ← chunk 变为「可检索」的唯一路径
```

**关键点**：摄入完成 ≠ 可被检索。
chunk 写进 Milvus 时 `vis_version = 0`，而层 1 过滤器要求 `vis_version > 0`。
**只有盖戳管道跑完，chunk 才会进入任何人的检索结果**——这是 fail-closed 的物理保证。

### 3.2 盖戳管道（可见性投影）

盖戳管道把权限服务的决策**物化**进 Milvus chunk payload，让向量检索能在 ANN 阶段就应用权限。
`src/ingest/service.py:stamp_channel_task` 实现了六条纪律，缺一即缺陷：

| # | 纪律 | 为什么 |
|---|------|-------|
| 1 | **失败不落盘、不 ack** | 写空戳记 = 全员可见或全员不可见，两种都是事故；不 ack 让 Celery 重投 |
| 2 | `unmounted == true` → 清空该通道戳记 | 解除挂载必须立即从检索面消失 |
| 3 | **版本单调性**：`response.version < 当前 vis_version` → 丢弃 | 乱序事件不得回滚新决策 |
| 4 | 分批 upsert（500/批），批间让渡 CPU | 大文档不阻塞 worker |
| 5 | 游标断点续跑 | 崩溃重投天然幂等（覆盖写） |
| 6 | 审计 `STAMP_APPLIED` **fail-open** | 盖戳量大，审计不可阻塞主流程 |

**戳记只搬运、不加工**：`allow_stamps` 里存的是**主体原始形态**（`user:u1`、`group:eng`、`role:admin`），
**禁止展开成员**——`group:eng` 就停在这里，不展开成该组当前的 user 列表。
组成员变化由权限服务重新发 `VisibilityChanged` 事件驱动重新盖戳，而不是本系统去推导。

### 3.3 三层检索链路（本系统的核心）

`src/retrieve/service.py:retrieve()` 完整实现：

```
                     用户提问 + kb_ids
                            │
┌───────────────────────────▼──────────────────────────────────┐
│ 层 1 · prefilter 编译注入（检索前）                            │
│                                                              │
│  get_prefilter(ctx) ──► 权限服务 /v1/prefilter                │
│      │                                                       │
│      ├─ 返回 SUSPENDED  → 整体拒答（不是空结果，是拒答）        │
│      └─ 返回 PreFilter                                        │
│                                                              │
│  候选 KB = prefilter.kbs ∩ 业务传入的 kb_ids                   │
│  交集为空 → 直接返回空，不发起任何向量查询                      │
│                                                              │
│  compile_filter() 生成六条件 MetadataFilter，逐 KB 编译：       │
│    ① tenant_id == ctx.tenant_id        租户隔离               │
│    ② kb_id == <当前 KB>                知识库隔离              │
│    ③ json_contains(allow_stamps, p) 的 OR   主体命中允许戳     │
│    ④ retrievable == true               挂载启用               │
│    ⑤ NOT json_contains(deny_stamps, p) 的 AND  未命中拒绝戳    │
│    ⑥ vis_version > 0                   已完成盖戳             │
│                                                              │
│  ★ dense 与 sparse 两路 Retriever 必须收到同一个 filter 对象    │
└───────────────────────────┬──────────────────────────────────┘
                            ▼
┌──────────────────────────────────────────────────────────────┐
│ 层 2 · 过采样与补检索                                          │
│   k' = top_k × oversample_factor（默认 1.5）                  │
│   结果 < min_results（默认 3）→ 补检索，最多 refetch_max_rounds │
│   ★ 补检索期间过滤条件必须完全相同——绝不放宽                    │
└───────────────────────────┬──────────────────────────────────┘
                            ▼
┌──────────────────────────────────────────────────────────────┐
│ 层 3 · strict 库逐条复核（仅 strict=true 的 KB）                │
│   filter_items(ctx, [(doc_id, kb_id), ...]) → /v1/filter      │
│   批次 ≤ 200；★ 失败/超时 = 整批拒绝；★ 永久禁止缓存            │
└───────────────────────────┬──────────────────────────────────┘
                            ▼
              rerank 得分过滤 → 截断 top_k → 返回
```

**三层各自解决什么**：

- **层 1** 解决召回正确性——过滤在 ANN 之前，不塌陷、不泄漏数量；
- **层 2** 解决过滤导致的召回不足——用过采样换回结果数，但绝不用放宽条件来换；
- **层 3** 解决**时效性**——戳记是异步投影，存在秒级窗口期。高敏感 KB 打开 `strict`，
  用一次实时 `/v1/filter` 换掉这个窗口，代价是每次查询多一跳。
  非 strict 库靠 `VisibilityChanged` 事件驱动的重新盖戳自愈。

### 3.4 混合检索与融合

查询 Pipeline（`pipelines/query_v4.yaml` = RRF，`query_v5.yaml` = 加权求和）：

```
BGE_M3TextEmbedder ─┬─ embedding ──────► MilvusDenseRetriever  (top_k 20, +filter)
                    └─ sparse_embedding ► MilvusSparseRetriever (top_k 20, +filter)
                                                    │
                            WeightedFusionJoiner ◄──┘   RRF / weighted_sum，top_k 30
                                     │
                            BGEReranker（BGE-Reranker-v2）top_k 10
                                     │
                            HierarchicalMerger  子块命中 → 回捞父块上下文，window=3
```

三种检索模式，按请求参数或 KB 配置切换：

| `retrieval_mode` | Pipeline | 适用场景 |
|-----------------|----------|---------|
| `hybrid`（默认） | `query_v4` / `query_v5` | 通用；语义 + 关键词互补 |
| `vector_only` | `retrieval_v1` | 纯语义问答，query 与文档措辞差异大 |
| `keyword_only` | `query_v2` | 精确术语、编号、专有名词检索 |

**层级合并（HierarchicalMerger）**解决的是「小块召回准、大块回答全」的矛盾：
摄入时切成父块（粗）+ 子块（细），检索命中子块，喂给 LLM 前把相邻子块和父块上下文合并回来。

### 3.5 对话与生成

```
POST /api/v1/conversations/query
  ├─ 确保 conversation 存在 → 分配 turn_index
  ├─ mint_ctx_token(ctx, audience="retrieval-worker", ttl_s=600)   ← 不传 JWT 原文
  ├─ resolve_retrieval_config(kb_id, tenant_id)                     ← 四层级联
  ├─ retrieve_and_generate_task.delay(...)  → retrieval_queue
  └─ 立即返回 {conversation_id, turn_index, trace_id, trace_ui_url}

GET /api/v1/conversations/{id}/stream?turn_index=N   (SSE)
  ├─ 先查 DB：worker 已完成 → 直接回放 retrieved/token/done
  └─ 否则订阅 Redis Pub/Sub `query-stream:{conv}:{turn}`，60s 空闲超时
```

四种合成模式（`src/chat/service.py`），可显式指定或 `auto` 按 chunk 数自动选：

| 模式 | 做法 | 何时用 |
|------|------|-------|
| `compact` | 所有 chunk 拼进一个 prompt，一次生成 | chunk 少、上下文窗口够 |
| `refine` | 分批迭代，每批在上一轮答案上精修 | chunk 多、需要全部纳入 |
| `tree_summarize` | 分批总结再总结 | chunk 很多、答案偏综述 |
| `no_synthesis` | 只返回原文片段，不生成 | 只要证据不要结论 |

**生成侧的两道质量闸门**：

- `validate_citations()` — 答案里引用的 chunk_id 必须在本次候选集合内，杜绝凭空引用；
- `check_verbatim_ratio()` — 最长公共子串占比超阈值（默认 0.60）判定为逐字复制并处理。

**拒答纪律**：无权限导致的空结果与「知识库里确实没有」使用**完全相同的措辞**。
用户不能通过措辞差异推断"存在但我看不到"。

### 3.6 配置级联与模型注册

**检索参数四层级联**（`resolve_retrieval_config`，后者覆盖前者）：

```
tenant 默认 ──► kb 级 ──► conversation 级 ──► 本次请求（turn）级
```

请求级参数直接写进 `conversation_turns` 的参数快照，用于审计追溯"这个答案当时用的什么参数"。

**模型切换零代码改动**——改 `.env` 或 `model_registry` 表即可：

```bash
# 本地 Ollama
LLM_BASE_URL=http://localhost:11434
LLM_MODEL=qwen2.5-coder:14b

# 换成企业 vLLM，只改这三行
LLM_BASE_URL=http://vllm.internal:8000/v1
LLM_MODEL=Qwen2.5-72B
LLM_API_KEY=your-key
```

### 3.7 可观测、审计与失败语义

| 信号 | 归属 | 出口 |
|------|------|------|
| Trace（含 Haystack 每个 Component 一个 span） | P-OBS | OTel Collector → Tempo |
| 结构化日志 | P-OBS | structlog → OTel → Loki |
| Metrics | P-OBS | OTel → Prometheus → Grafana |
| 模型调用（prompt/completion/token/成本） | P-MODEL | Langfuse |
| 审计（`AUTH_ALLOW`/`AUTH_DENY`/`STAMP_APPLIED`/`KB_QUERY`/`CHUNK_FILTERED`） | P-AUDIT | `audit_logs` 表 |

trace context 跨 API → Redis → Worker → Haystack Pipeline 全链路贯通
（`celery_app.py` 的 `before_task_publish` / `task_prerun` 信号自动注入与还原，
覆盖全部任务调度点，无需逐个改）。查询响应头带 `X-Trace-Id`，前端可直接深链到 Grafana Tempo。

**失败语义严禁交叉污染**——这张表是设计核心之一：

| 组件不可达 | 行为 | 理由 |
|-----------|------|------|
| 可观测（P-OBS / Langfuse） | **fail-open**，业务继续 | 遥测丢失可接受 |
| 审计高风险事件（P-AUDIT） | **fail-closed**，事务回滚 | 合规义务 |
| **权限服务（外部）** | **fail-closed**，全部拒绝 | **授权绕过不可逆** |

**权限服务永远不进 `/readyz`**。原因：权限服务抖动时，应该是"请求被拒绝"，
而不是"整个 RAG 服务被摘出负载均衡"。这两种语义必须并存，
由 `tests/contract/test_permission_discipline.py:test_readyz_excludes_permission_service` 守住。

P-AUTHC 还带**熔断器**（默认 failure_threshold=10 / recovery_timeout=60s）：
权限服务持续失败时熔断打开，直接返回类型正确的 deny 值，不再打爆下游。

---

## 4. 组件选型与选择理由

每一项都回答三个问题：**为什么是它**、**替代方案是什么**、**换掉要付多大代价**。

### 4.1 算法框架：Haystack 2.x

| | |
|---|---|
| **为什么** | ① `Pipeline` 提供 DAG 编排 + **YAML 序列化**，检索流程变成可版本化的配置文件而非代码；② `@component` 装饰器用类型注解约束 I/O，天然是防腐层；③ **节点即 OTel span**，无需手工埋点；④ 官方 Integration 覆盖 Milvus / BGE / LiteLLM |
| **替代方案** | LlamaIndex（v13 用的就是它）、LangChain、纯手写 |
| **为什么不用 LlamaIndex** | `QueryEngine` 抽象层级过高，注入自定义 MetadataFilter 与替换单个环节都要绕；Haystack 的 Component 边界更清晰，**替换单个 Component 不影响 Pipeline 其余节点** |
| **替换代价** | 低。四个能力契约（切分/融合/层级合并/合成）是接口，Haystack 类型不出现在任何模块签名里 |
| **Haystack 没覆盖、本系统自建** | P-AUTHC 权限适配、盖戳管道、写路径结构镜像、Celery 异步包装（Haystack Pipeline 是同步的） |

### 4.2 授权判定：外部权限服务（Cerbos PDP）

| | |
|---|---|
| **为什么外置** | 授权是**跨系统关注点**——同一套 ACL 要同时服务 RAG、报表、审批。内建等于把权限锁死在一个应用里，且合规审计时无法回答"决策依据是什么" |
| **为什么 Cerbos** | ① 策略即代码（YAML），可评审、可 diff、可版本化；② **Derived Roles** 天然表达"用户 + 资源属性 → 有效角色"；③ 独立 PDP 进程，判定与业务进程解耦；④ 决策返回 `decision_id`，可跨系统串联审计 |
| **替代方案** | OPA/Rego（表达力更强但可读性差、学习曲线陡）、Casbin（模型偏静态，难表达资源属性条件）、自建 ACL 表（回到墙二） |
| **本系统怎么隔离它** | 五端点全部封装在 P-AUTHC，**base URL / 超时 / 重试 / 三态映射 / Envelope 构造只在这一处存在**。`AUTHZ_SERVICE_MODE=local` 直连 Cerbos PDP，`remote` 走外部权限服务后端，业务代码零感知 |
| **替换代价** | 中。防腐层的目的不是"将来好换掉权限服务"，而是**把双向契约变更的影响面收敛到一个模块** |

### 4.3 向量库：Milvus 2.4

| | |
|---|---|
| **为什么** | ① 单集合同时支持**稠密 + 稀疏**向量与混合检索，BGE-M3 的双路输出可以放在一起；② 标量过滤表达式支持 `json_contains`，这是六条件过滤器**能在 ANN 之前生效**的前提；③ 有官方 Haystack Integration；④ 支持 upsert，盖戳管道靠它做幂等覆盖写 |
| **替代方案** | Qdrant（过滤能力强但稀疏向量支持较晚）、pgvector（省一个组件但十万级以上召回质量与性能吃紧）、Elasticsearch（关键词强、向量弱） |
| **关键约束** | 选型的**决定性因素是 `json_contains` 过滤**——没有它，`allow_stamps`/`deny_stamps` 就无法在查询条件里表达，只能退化成事后过滤（墙一） |
| **替换代价** | 中。需要重写 `MilvusDenseRetriever` / `MilvusSparseRetriever` / `MilvusDocumentStoreWriter` 三个 Component 和 `_compile_filter_expr` 的表达式编译 |

### 4.4 嵌入与重排：BGE-M3 + BGE-Reranker-v2

| | |
|---|---|
| **为什么 BGE-M3** | 一个模型同时产出**稠密向量 + 稀疏权重**，混合检索两路共用一次前向，省一半推理成本；中文语料表现强；支持长文本 |
| **为什么要 Reranker** | 向量召回是"粗筛"，Cross-Encoder 重排是"精排"。融合后 top-30 经 rerank 挑出 top-10，是性价比最高的一档质量提升 |
| **服务化设计** | 每个 worker 各自加载 BGE-M3 会重复占用数 GB 显存。`EMBEDDING_SERVICE_URL` 指向 `embedding-service:19500` 后，**所有 worker 经 HTTP 共享同一份 GPU 模型**；不配置则降级为本地加载（向后兼容） |
| **替代方案** | OpenAI text-embedding-3（走网络、有数据出境问题）、m3e / bce（稀疏能力弱） |

### 4.5 任务队列：Celery + Redis

| | |
|---|---|
| **为什么** | ① Haystack Pipeline 同步阻塞，必须搬出 API 进程；② 三类工作负载特征差异极大，需要**独立队列独立扩缩容** |
| **四类队列** | `ingestion_queue`（长耗时，可长退避重试）、`retrieval_queue`（用户在线等待，**不做长退避**）、`stamping_queue`（低优先级、可分批、可断点续跑）、`outbox-relay`（事件可靠投递） |
| **为什么盖戳单独一个队列** | 盖戳是高吞吐低优先级任务，混进摄入队列会饿死用户可见的解析任务 |
| **Redis 的三重身份** | Celery broker + result backend + **SSE 流式回传的 Pub/Sub 通道** |
| **替代方案** | RQ（功能太薄，无 beat 无路由）、Dramatiq、Arq、Kafka（重，且本系统不需要日志保留语义） |

### 4.6 其余组件

| 组件 | 选择 | 理由 | 替代方案 |
|------|------|------|---------|
| **关系库** | PostgreSQL 16 + asyncpg | 事务保证 outbox 与业务写同库同事务（这是"至少一次投递"的前提）；JSONB 存参数快照 | MySQL（JSON 能力与事务外键约束偏弱） |
| **对象存储** | SeaweedFS（S3 网关） | 轻量、单容器可跑、S3 兼容 → 生产可无缝换 MinIO / 云 OSS | MinIO（更重）、本地磁盘（不可横向扩展） |
| **LLM 网关** | LiteLLM | 一套接口打通 Ollama / vLLM / OpenAI / DeepSeek，**换模型只改 `.env`** | 直连各家 SDK（每换一家改一次代码） |
| **可观测** | OTel Collector + Tempo/Loki/Prometheus + Grafana | 厂商中立；**一个入口一个出口**；Haystack 内置 tracing 直接接入 | 各家 APM（锁定） |
| **模型观测** | Langfuse | 通用 APM 看不到 prompt/completion/token/成本，这条线必须单独走 | Phoenix、自建 |
| **RAG 评估** | RAGAS | `POST /api/v1/query/eval` 与线上 `/query` **复用同一套检索生成核心**，仅多吐中间结果，保证评测与线上一致 | 人工评测（不可回归） |
| **认证** | Keycloak + 本地校签 | 开发期 RS256 自签（`config/*.pem`），生产期 JWKS 拉公钥，**代码路径完全一致** | 自建用户体系（回到墙二） |
| **前端** | Next.js 14 + shadcn/ui + TanStack Query + Zustand | App Router 支持 SSE 流式；**`doc:view` 红线要求服务端渲染而非签名 URL**，Next.js 天然满足 | 纯 SPA（无法满足服务端渲染要求） |

---

## 5. 快速开始

### 5.1 前置条件

- Docker + docker-compose
- Python 3.11（推荐 conda 环境，Makefile 中默认环境名 `rag_dev_v14`）
- Node.js 18+（前端）
- GPU 可选：无 GPU 时 BGE-M3 走 CPU，明显变慢但可用
- LLM 服务：本地 Ollama，或任意 OpenAI 兼容端点
- **Keycloak**：登录接口走 Keycloak password grant 验证，需先启动并配置 realm

### 5.2 五步跑通

```bash
# ① 配置环境变量
cp .env.example .env
#   至少填写：POSTGRES_PASSWORD / REDIS_PASSWORD / LLM_BASE_URL / LLM_MODEL

# ② 启动基础设施（约 30–60 秒，主要等 Milvus）
make infra
docker-compose -f docker-compose.infra.yml ps      # 全部 healthy 再继续

# ③ 初始化数据库
make db-init      # 建表（首次执行一次）
make db-seed      # 写入开发期测试数据

# ④ 预热模型（BGE-M3 约 2GB，仅首次）
make warmup

# ⑤ 分终端启动进程（按需，不必全开）
make dev-api        # 终端 1：FastAPI :8000
make dev-ingest     # 终端 2：摄入 worker
make dev-retrieve   # 终端 3：检索 worker
make dev-stamp      # 终端 4：盖戳 worker
make dev-relay      # 终端 5：outbox relay
make dev-frontend   # 终端 6：Next.js :3001
```

### 5.3 按开发场景决定启动哪些进程

| 你在开发什么 | 需要启动 |
|-------------|---------|
| 文档上传 / 解析 | `dev-api` + `dev-ingest` |
| 检索 / 问答 | `dev-api` + `dev-retrieve` |
| 权限盖戳 | `dev-api` + `dev-ingest` + `dev-stamp` |
| 事件投递 / 挂载变更 | 再加 `dev-relay` + `dev-visibility-events` |
| 对账定时任务 | 再加 `dev-beat` |
| 多 worker 共享 GPU | 先起 `dev-embedding`（:19500），并设置 `EMBEDDING_SERVICE_URL` |
| 前端 | `dev-frontend`（:3001，HMR） |

### 5.4 端口速查（宿主机实际映射）

基础设施端口做了偏移，避免与机器上已有的 PostgreSQL/Redis 冲突：

| 服务 | 宿主机端口 | 容器内端口 | 用途 |
|------|-----------|-----------|------|
| API (FastAPI) | **8000** | 8000 | 对外 HTTP 入口 |
| 前端 (Next.js) | **3001** | 3001 | Web UI |
| Nginx（生产） | **80** | 80 | 统一入口：`/` → 前端，`/api` → 后端 |
| PostgreSQL | **25432** | 5432 | 业务库 / 审计 / outbox |
| Redis | **16379** | 6379 | Celery broker + 流式 Pub/Sub |
| Milvus | **19530** / 9091 | 19530 / 9091 | 向量库 gRPC / health |
| SeaweedFS | **18333** | 8333 | 对象存储 S3 网关 |
| Cerbos PDP | **13592** / 13593 | 3592 / 3593 | 权限判定 HTTP / gRPC |
| embedding-service | **19500** | 19500 | BGE-M3 共享推理 |
| etcd / MinIO | 内网 | 2379 / 9000 | Milvus 元数据与持久化（不对外） |

> 注：`docs/DEV-SETUP.md` 里的端口表写的是容器内端口（5432/6379/3592），
> 本地连接请以上表的宿主机端口为准（`.env.example` 中也是宿主机端口）。

### 5.5 生产部署

```bash
make deploy               # infra + app 全栈起
make deploy-restart-app   # 只重启计算层（更新代码后）
make deploy-frontend      # 单独构建前端镜像
```

生产与开发的关键差异：

| 配置项 | 开发 | 生产 |
|--------|------|------|
| 登录 | `POST /api/v1/auth/dev-login`（Keycloak password grant + 本系统自签 JWT） | SSO 授权码流（`/api/v1/auth/callback`） |
| JWT 校签 | 本地 PEM（`config/jwt_public.pem`） | JWKS URL（`JWT_JWKS_URL`，PyJWKClient 自动拉取） |
| 权限服务地址 | `http://localhost:13592` | 容器内 `http://cerbos:3592` 或 `http://permission-service:8080` |
| `AUTHZ_SERVICE_MODE` | `local`（直连 Cerbos PDP） | `remote`（走外部权限服务后端） |
| `CTX_TOKEN_SECRET` | 未配置时回退 Redis URL hash | **必须显式配置独立 secret** |
| `CORS_ALLOWED_ORIGINS` | `localhost:3001,localhost:3000` | **必须收敛到具体域名** |
| `ADMIN_CONSOLE_URL` | 可空（跳转按钮 disabled） | 必填 |
| 入口 | 直连 :8000 / :3001 | Nginx :80 统一入口 |

---

## 6. 端到端走一遍

```bash
# ① 登录拿 token
TOKEN=$(curl -s -X POST http://localhost:8000/api/v1/auth/dev-login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"<your-password>","tenant":"tenant-dev"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

# ② 建知识库
KB=$(curl -s -X POST http://localhost:8000/api/v1/knowledge-bases \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"name":"测试库"}' | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")

# ③ 上传文档（自动触发解析 → 摄入 → 盖戳）
curl -s -X POST http://localhost:8000/api/v1/documents/upload \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@test_docs/<你的文件>.md" -F "kb_id=$KB" -F "auto_parse=true"

# ④ 确认已可检索：chunks 的 vis_version 必须 > 0（盖戳完成）
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/v1/knowledge-bases/$KB/documents"

# ⑤ 提问（立即返回，答案走 SSE）
RESP=$(curl -s -X POST http://localhost:8000/api/v1/conversations/query \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"question\":\"这份文档讲了什么？\",\"kb_ids\":[\"$KB\"]}")
echo $RESP     # → {conversation_id, turn_index, trace_id, trace_ui_url}

# ⑥ 订阅流式结果
CONV=$(echo $RESP | python3 -c "import sys,json; print(json.load(sys.stdin)['conversation_id'])")
curl -N "http://localhost:8000/api/v1/conversations/$CONV/stream?turn_index=1"
```

**排查提示**：如果 ⑤ 返回空答案，按这个顺序查——

1. `dev-retrieve` worker 有没有在跑；
2. chunk 的 `vis_version` 是不是还是 0（`dev-stamp` 没跑或 Cerbos 不可达）；
3. `get_prefilter` 是否返回 SUSPENDED（权限服务判定该用户被挂起）；
4. 候选 KB 交集是否为空（`prefilter.kbs ∩ kb_ids`）；
5. 用响应头的 `X-Trace-Id` 去 Grafana Tempo 看完整链路。

---

## 7. API 速查

所有端点前缀 `/api/v1`，除健康检查外均需 `Authorization: Bearer <JWT>`。

| 分组 | 端点 | 说明 |
|------|------|------|
| **认证** | `POST /auth/dev-login` | 开发登录（Keycloak 验证 + 本系统签发 JWT） |
| | `POST /auth/token` · `POST /auth/refresh` | 令牌签发 / 刷新 |
| | `GET /auth/callback` | OIDC 授权码回调（生产 SSO） |
| | `POST /auth/check-permission` | 前端条件渲染用的权限查询 |
| | `POST /auth/switch-tenant` | 切换租户 |
| **知识库** | `GET/POST /knowledge-bases` · `PATCH/DELETE /knowledge-bases/{id}` | KB CRUD |
| | `GET/PATCH /knowledge-bases/{id}/chunking-config` | 切分配置（版本化） |
| | `GET /knowledge-bases/{id}/documents` | KB 下文档列表 |
| **文档** | `POST /documents/upload` | 上传（登记 + 挂载 + 触发解析） |
| | `GET /documents/{id}` · `/chunks` · `/content` · `/download` | 详情 / 分块 / 只读渲染 / 下载 |
| | `POST /documents/{id}/trigger-parse` | 手动触发解析 |
| | `POST /documents/batch/parse` · `/batch/delete` | 批量操作 |
| | `DELETE /documents/{id}/kb/{kb_id}` | 从 KB 移除（unmount，非 purge） |
| **目录** | `GET /knowledge-bases/{id}/directories` · `POST/PATCH/DELETE /directories/...` | 目录树管理 |
| **对话** | `POST /conversations/query` | 提问（分发到 worker，立即返回） |
| | `GET /conversations/{id}/stream` | **SSE 流式结果**（`retrieved` / `token` / `done` / `error`） |
| | `GET/POST /conversations` · `PATCH/DELETE /conversations/{id}` | 会话管理 |
| | `GET /conversations/{id}/turns` | 历史轮次 |
| **设置** | `GET /models` · `PATCH /models/{id}/set-default` | 模型注册表 |
| | `GET/PATCH /configs/retrieval` | 检索参数 |
| | `GET /prompts` · `PATCH /prompts/{id}/activate` | Prompt 版本池 |
| | `GET /config` · `GET /system/enums` | 前端配置 / 枚举 |
| **统计** | `GET /stats/usage` · `/top-kbs` · `/documents` · `/quality` | Dashboard 数据 |
| **评测** | `POST /query/eval` | RAGAS 评测入口（透出 contexts / retrieved_chunks / is_answerable） |
| **健康** | `GET /healthz` · `GET /api/v1/readyz` · `GET /api/v1/ping` | **`/readyz` 不含权限服务** |

---

## 8. 配置说明

配置唯一入口是 `src/config.py`（`Settings`），优先级：环境变量（`.env`）> 代码默认值。
**禁止在其他模块硬编码 URL / 密码 / 模型名 / 端口。**

关键变量：

```bash
# ── 基础设施 ──
DATABASE_URL=postgresql+asyncpg://rag:${POSTGRES_PASSWORD}@localhost:25432/rag
REDIS_URL=redis://:${REDIS_PASSWORD}@localhost:16379/0
MILVUS_HOST=localhost
MILVUS_PORT=19530
S3_ENDPOINT_URL=http://localhost:18333

# ── 模型（改模型只改这里，零代码改动）──
LLM_BASE_URL=http://localhost:11434
LLM_MODEL=qwen2.5-coder:14b
EMBEDDING_MODEL=qwen3-embedding:0.6b
LLM_API_KEY=ollama
LLM_MAX_TOKENS=8192          # 推理类模型需预留 reasoning token 预算
EMBEDDING_SERVICE_URL=http://localhost:19500   # 设置后 worker 共享 GPU 模型

# ── 权限服务 ──
AUTHZ_SERVICE_MODE=local     # local=直连 Cerbos PDP | remote=外部权限服务后端
AUTHZ_BASE_URL=http://localhost:13592
AUTHZ_SERVICE_URL=http://<perm-service>:18080   # remote 模式
AUTHZ_TIMEOUT_MS=2000
AUTHZ_PROJECT_ID=rag-v14
AUTHZ_CLIENT_CREDENTIAL=     # 服务间 X-Api-Key，生产必填
CTX_TOKEN_SECRET=            # ctx_token HMAC 密钥，生产必填

# ── 认证 ──
KEYCLOAK_SERVER_URL=http://<keycloak>:8080
KEYCLOAK_REALM=rag-v14
KEYCLOAK_CLIENT_ID=rag-frontend
JWT_PUBLIC_KEY_PATH=./config/jwt_public.pem     # 开发
# JWT_JWKS_URL=https://.../certs                # 生产

# ── 可观测 ──
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
LANGFUSE_PUBLIC_KEY= / LANGFUSE_SECRET_KEY= / LANGFUSE_HOST=

# ── 前端跳转链接 ──
GRAFANA_URL= / LANGFUSE_PUBLIC_URL= / CERBOS_PUBLIC_URL= / ADMIN_CONSOLE_URL=

# ── 其他 ──
CORS_ALLOWED_ORIGINS=http://localhost:3001,http://localhost:3000   # 生产收敛到具体域名
PIPELINE_YAML_DIR=./pipelines
```

**依赖版本以 `requirements.txt` 为准**（当前主要版本）：
`haystack-ai>=2.19` · `milvus-haystack==0.0.18` · `pymilvus>=2.6.15,<3` ·
`celery==5.4.0` · `fastapi==0.115.0` · `litellm==1.42.0` ·
`sentence-transformers==3.1.1` · `FlagEmbedding==1.4.0` ·
`opentelemetry-sdk==1.33.1` · `langfuse==4.14.0` · `ragas==0.1.21`。
torch GPU 版本需按注释单独安装。

---

## 9. 测试

```bash
pytest tests/contract/ -v       # 契约测试（CI 强制，必须全绿）
pytest tests/integration/ -v    # 集成测试（需基础设施在跑）
pytest tests/unit/ -v           # 单元测试
```

**契约测试是本项目的架构护栏**，不是普通的功能测试。它们扫描源码，把设计红线变成可执行断言：

| 测试文件 | 守住什么 |
|---------|---------|
| `test_permission_discipline.py` | `cerbos_client` 只能被 P-AUTHC import、无本地权限判定、无废弃动词、`doc:retrieve` 不走 `/v1/check`、六条件过滤器完整、无 query-then-filter 模式、dense/sparse 收到同一 filter 对象、日志中无 credential、`/readyz` 不含权限服务 |
| `test_joint_1_11.py` | 分享可检索性、跨 KB 隔离、通道封禁、退役四合一、戳记不展开、KB 粒度事件、strict 实时回收、非 strict 自愈 |
| `test_joint_12_20.py` | 动作与端点绑定、准入矩阵、`filter` 批次上限、`decision_id` 可追溯、超时 fail-closed、禁用文档不进 strict 范围 |

改动权限、检索或盖戳相关代码后，`pytest tests/contract/ -v` 必须先跑通再提交。

---

## 10. 目录结构

```
├── src/
│   ├── main.py                  FastAPI 入口（严格的启动顺序，注释里说明了为什么）
│   ├── config.py                配置唯一入口
│   ├── api/                     REST 路由：只做校验 / 分发 / SSE 转发
│   ├── permission/              ★ P-AUTHC — 访问权限服务的唯一出口
│   │   ├── authz.py               五端点门面 + compile_filter 六条件 + 熔断器
│   │   ├── cerbos_client.py       Cerbos PDP 客户端 + 结构镜像维护
│   │   ├── permission_service_client.py   remote 模式客户端
│   │   ├── context.py             RequestContext 构建 + ctx_token 解析
│   │   ├── middleware.py          AuthMiddleware：JWT 校验 + ctx 注入
│   │   └── visibility_events.py   VisibilityChanged 订阅 + 对账兜底
│   ├── platform/
│   │   ├── task/                P-TASK：celery_app / pipeline_runner / outbox_relay / reconciliation
│   │   ├── model/               P-MODEL：invoke_llm/embedding/rerank + Prompt 池 + Langfuse
│   │   ├── config/              P-CONFIG：四层级联 + 切分配置版本化 + feature flag
│   │   ├── audit/               P-AUDIT
│   │   ├── obs/                 P-OBS：tracing / logger / metrics
│   │   └── store/               P-STORE：SeaweedFS S3
│   ├── doc/                     B-DOC：登记去重、挂载、目录、生命周期端口
│   ├── ingest/                  B-INGEST：摄入任务 + ★ 盖戳管道
│   │   └── components/            Haystack 摄入 Component（切分/嵌入/权限元数据/写入）
│   ├── retrieve/                B-RETRIEVE：★ 三层检索链路
│   │   └── components/            Haystack 查询 Component（嵌入/双路召回/融合/rerank/层级合并）
│   ├── chat/                    B-CHAT：对话编排 + 四种合成模式 + 引用校验
│   ├── services/                embedding_service（GPU 模型共享）+ client
│   └── scripts/                 init_db / seed_dev / warmup_models / reset / backfill
├── pipelines/                   Haystack Pipeline YAML（ingest_v1–v5 / query_v1–v5 / retrieval_v1）
├── cerbos/                      Cerbos 策略：derived_roles + resource_policies + schemas
├── scripts/init.sql             建表 SQL
├── scripts/eval_ragas.py        RAGAS 离线评测
├── frontend/                    Next.js 14 前端（login / kb / chat / dashboard / settings / 403）
├── tests/                       contract（架构护栏）/ integration / unit / eval_sets
├── metrics/                     Prometheus 配置 + 告警规则
├── docker-compose.infra.yml     基础设施（开发期常驻）
├── docker-compose.app.yml       计算层（部署使用）
├── docker-compose.prod.yml      生产编排
└── Makefile                     所有常用命令
```

---

## 11. 开发红线（扩展代码前必读）

以下每一条都来自设计文档，多数由契约测试强制执行。违反会被 CI 拦下：

**权限相关**

1. 禁止绕过 P-AUTHC 直接调权限服务 HTTP 端点
2. 禁止在 Haystack Component 的 `run()` 内写任何权限逻辑——`MetadataFilter` 从外部注入
3. 禁止业务模块自行构造权限服务 Envelope / 地址 / reasons 解释
4. 禁止任何本地权限判定，包括 `if uploaded_by == user_id` 这种"顺手短路"
5. 禁止缓存决策结果——`/v1/filter` 永久禁止缓存，`/v1/check` 默认关闭缓存
6. 禁止 JWT 原文进入日志、trace、审计 payload、任务参数——异步链路一律用 `ctx_token`（TTL ≤ 600s）
7. 权限服务永远不进 `/readyz`

**检索相关**

8. 禁止事后过滤（query-then-filter）——召回塌陷 + 数量泄漏 + 上下文泄漏
9. 禁止补检索时放宽过滤条件——各轮 MetadataFilter 必须完全一致
10. 禁止 prefilter 失败时回退到无过滤查询——返回空或拒答
11. 禁止在用户可见输出中暴露"被过滤了多少条"——拒答措辞与"信息不足"完全一致

**盖戳相关**

12. 盖戳失败禁止写空戳记——不 ack，重试
13. 禁止展开 `allow_stamps`/`deny_stamps` 的成员——保持主体原始形态
14. 禁止在 B-INGEST 内计算或推导戳记值——只搬运 `/v1/visibility` 的返回

**架构相关**

15. 禁止业务模块 import 可观测 SDK——一律经 P-OBS 门面
16. 禁止绕过 P-MODEL 直接调模型——Generator/Embedder/Ranker 实例封装在 P-MODEL 内
17. 禁止业务模块之间直读直写对方独占表
18. 禁止在 API 进程调 `pipeline.run()`——计算只在 worker
19. 禁止 Haystack 类型出现在模块接口签名中
20. 禁止使用框架默认 prompt——模板必须来自 `resolve_prompt`
21. `doc:view` 禁止用签名 URL——必须服务端渲染

**动词命名**：权威源是上游契约的 16 个动词，禁止新建、改名或使用废弃动词。
已废弃：`doc:write` → `kb:write`；`doc:delete` → `doc:purge` / `doc:unmount`；
`acl:update` → `doc:share` / `kb:grant`。

**新增代码时可用的脚手架**（`.claude/skills/`）：
`new-module`、`new-interface`、`new-table`、`new-event`、`new-haystack-component`、
`new-pipeline-yaml`、`new-contract-test`。

---

## 12. 文档索引

| 文档 | 内容 |
|------|------|
| [`docs/RAG系统设计v14.md`](docs/RAG系统设计v14.md) | **主设计规格**：模块注册表、共享契约、七个平台模块 + 四个业务模块全部章节、三层检索、盖戳管道 |
| [`docs/RAG系统设计v14落地方案.md`](docs/RAG系统设计v14落地方案.md) | 四阶段落地路径、compose / Dockerfile / requirements / Cerbos 配置全文 |
| [`docs/权限管理系统架构设计.md`](docs/权限管理系统架构设计.md) | 权限四方协作、16 动词、准入矩阵、五条权限数据流、已实现清单、文件索引 |
| [`docs/外部系统设计.md`](docs/外部系统设计.md) | 外部权限服务后端 + 管理台的设计规格（供对接方阅读） |
| [`docs/外部系统实施方案.md`](docs/外部系统实施方案.md) | 外部系统实施细则 |
| [`docs/DEV-SETUP.md`](docs/DEV-SETUP.md) | 开发环境搭建（注意端口以本 README §5.4 为准） |
| [`docs/frontend-design.md`](docs/frontend-design.md) · [`docs/frontend_implement.md`](docs/frontend_implement.md) | 前端架构与实现 |
| [`docs/test_cases.md`](docs/test_cases.md) · [`docs/execute_test_cases_report.md`](docs/execute_test_cases_report.md) | 测试用例与执行报告 |
| [`docs/AUDIT-REPORT.md`](docs/AUDIT-REPORT.md) · `docs/project_diagnose_v*.md` | 架构审计与历次诊断记录 |
| [`docs/deploy/security-checklist.md`](docs/deploy/security-checklist.md) | 上线安全检查清单 |
| [`CLAUDE.md`](CLAUDE.md) | 给 AI 助手的仓库约定（红线的机器可读版本） |

---

> **一句话总结**：本系统是权限的**消费方**和**执行方**，不是**管理方**。
> 授权决策由外部 Cerbos PDP 完成，授权管理由外部管理台完成；
> 本系统负责的是——正确消费决策（P-AUTHC 五端点）、正确执行过滤（三层检索 + 盖戳管道）、
> 正确传递上下文（JWT → ctx_token → prefilter → MetadataFilter）。

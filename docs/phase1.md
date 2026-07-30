# 阶段一：单条链路跑通 — 实施分析

> 来源：`docs/RAG系统设计v14落地方案.md` 第四章
> 目标：一次上传 + 一次查询能端到端跑通，权限是真实三道校验，不是 mock

---

## 1. 覆盖的 RAG 环节

阶段一覆盖 5 个关键环节，形成完整的上传→检索→生成链路：

```
上传文档 → 解析切分 → BGE-M3 嵌入 → 写入 Milvus（空戳记）→ 盖戳（取 visibility）
                                                                    ↓
查询提问 → 嵌入 → 混合检索（稠密+稀疏，六条件过滤）→ RRF 融合 → LLM 生成答案 → SSE 流式返回
              ↑                    ↑
         P-AUTHC prefilter     P-AUTHC check(入口门禁)
```

| 环节 | RAG 阶段 | 对应模块 | 阶段一实现程度 |
|------|---------|---------|-------------|
| 文档上传 + 登记 + 权限注册 | 摄入前端 | B-DOC + P-AUTHC | 简化：只做登记、同步调 register/link |
| 解析 + 切分 + 嵌入 + 写库 | 摄入管线 | B-INGEST | **完整**：Haystack Pipeline |
| 从 Cerbos 取 visibility 落戳记 | 盖戳 | B-INGEST 盖戳管道 | **完整**：六条纪律全部实现 |
| 入口门禁 + prefilter + 六条件编译 | 检索权限 | P-AUTHC | **完整** |
| 混合检索 + RRF 融合 | 检索 | B-RETRIEVE | **完整**（不含 rerank） |
| LLM 生成 + 流式返回 | 生成 | B-CHAT | 简化：单轮、hardcode Prompt、resolved_query=原始问题 |

### 阶段一明确不做（接口已占位）

| 占位内容 | 阶段一行为 | 升级阶段 |
|---------|----------|---------|
| `invoke_rerank` | no-op，直接返回输入列表 | 阶段二 |
| `filter_items`（层 3 strict） | 接口有，`strict` 默认 false，不调 `/v1/filter` | 阶段二 |
| 多轮对话改写 | `resolved_query = user_question` | 阶段二 |
| 目录管理 | 接口签名有，返回 `501 Not Implemented` | 阶段二 |
| `check_batch` | 串行调 N 次 `check`（正确但慢） | 阶段二 |
| P-AUDIT 落库 | `emit_audit_event` 记 structlog 日志 | 阶段二 |
| `purge=true` 删除 | 返回 501 | 阶段三 |

---

## 2. 需要新建的文件

`src/` 目录当前完全为空，所有文件都要新建。总计约 40+ 个文件。

### 平台模块（P-*）

| 目录 | 新建文件 | 用途 |
|------|---------|------|
| `src/platform/store/` | `__init__.py`, `backend.py` | P-STORE：SeaweedFS S3 读写（`put`/`get`/`generate_presigned_url`/`delete`） |
| `src/platform/config/` | `__init__.py`, `service.py` | P-CONFIG：`resolve_retrieval_config`（kb→tenant 两级）、`resolve_chunking_config` |
| `src/platform/obs/` | `__init__.py`, `logger.py` | P-OBS：structlog 配置，Haystack 内置 OTel 自动生效 |
| `src/platform/model/` | `__init__.py`, `registry.py` | P-MODEL：`invoke_embedding`（BGE-M3）、`invoke_llm`（Ollama/Qwen2.5）、`resolve_prompt`（hardcode 模板）、`invoke_rerank`（no-op） |
| `src/platform/task/` | `__init__.py`, `celery_app.py`, `pipeline_runner.py`, `outbox_relay.py` | P-TASK：Celery app（三类队列）、`run_pipeline_async`、Redis Pub/Sub 流式回传、Outbox relay |
| `src/platform/audit/` | `__init__.py`, `service.py` | P-AUDIT：`emit_audit_event`（structlog 版，不落库）、`emit_audit_event_txn`（同实现） |
| `src/permission/` | `__init__.py`, `context.py`, `cerbos_client.py`, `authz.py` | P-AUTHC：`build_context`（JWT 校签+credential 保管）、Cerbos HTTP 封装（五端点）、三态映射+四类 fail-closed、`compile_filter`（六条件→MetadataFilter）、生命周期端口封装 |

### 业务模块（B-*）

| 目录 | 新建文件 | 用途 |
|------|---------|------|
| `src/doc/` | `__init__.py`, `service.py`, `models.py`, `events.py` | B-DOC：`submit_ingest_task`（登记+register+link+auto_parse）、`trigger_parse`（发 DocumentMounted 事件）、`delete_document_from_kb`（purge=false） |
| `src/ingest/` | `__init__.py`, `service.py` | B-INGEST：`ingest_document_task`、`stamp_channel_task`（六条纪律）、`has_execution`、`get_parse_status` |
| `src/ingest/components/` | `__init__.py`, `sparse_embedder.py`, `perm_enricher.py`, `visibility_stamper.py` | 自定义 Haystack @component：BGE-M3SparseEmbedder、PermissionMetadataEnricher、VisibilityStampComponent |
| `src/retrieve/` | `__init__.py`, `service.py` | B-RETRIEVE：`retrieve`（层1 MetadataFilter 注入 + 层2 过采样补检索）、Hybrid Pipeline 管理 |
| `src/retrieve/components/` | `__init__.py`, `sparse_text_embedder.py` | BGE-M3SparseTextEmbedder（查询侧） |
| `src/chat/` | `__init__.py`, `service.py`, `models.py` | B-CHAT：对话/轮次存储、`retrieve_and_generate_task`（检索分发+流式生成）、SSE 流式回传 |

### 入口 + API

| 文件 | 用途 |
|------|------|
| `src/main.py` | FastAPI 应用入口，挂载路由 |
| `src/api/__init__.py` | — |
| `src/api/routes.py` | REST 端点：上传、查询、管理 |
| `src/api/deps.py` | FastAPI 依赖注入（ctx 构建、`require_permission`） |

### Pipeline YAML + 脚本

| 文件 | 用途 |
|------|------|
| `pipelines/ingest_v1.yaml` | 摄入 Pipeline：DocumentSplitter→DenseEmbedder→SparseEmbedder→PermEnricher→MilvusWriter |
| `pipelines/query_v1.yaml` | 查询 Pipeline：DenseTextEmbedder+SparseTextEmbedder→DenseRetriever+SparseRetriever→Joiner(RRF)→PromptBuilder→Generator |
| `src/scripts/__init__.py` | — |
| `src/scripts/init_db.py` | Python 版数据库初始化脚本 |
| `src/scripts/seed_dev.py` | 开发期测试数据写入 |
| `src/scripts/warmup_models.py` | BGE-M3 预热 |

---

## 3. 实现顺序

原则：**被依赖的先实现，能独立测试的先实现。** 信息流是 `B-* → P-* → 外部`，平台模块优先。

| 步骤 | 模块 | 做什么 | 独立验证方式 |
|------|------|------|------------|
| **0** | `src/main.py` + `src/api/` | FastAPI 骨架（空路由，先跑起来） | `curl localhost:8000/docs` 看到 Swagger |
| **1** | P-OBS | structlog 配置 | import 后打日志看到 JSON 格式输出 |
| **2** | P-STORE | S3 后端封装 | 对已运行的 SeaweedFS（localhost:18333）做 put/get 测试 |
| **3** | P-CONFIG | 数据库初始化 + `resolve_*` 门面 | `psql -p 25432` 验证 11 张表已建好，Python 读配置返回默认值 |
| **4** | P-MODEL | `invoke_embedding` + `invoke_llm` + `resolve_prompt` + `invoke_rerank`（no-op） | BGE-M3 编码一段文本返回向量；Ollama 生成一句话 |
| **5** | P-TASK | Celery app + `run_pipeline_async` + Redis Pub/Sub | Celery worker 启动（`make dev-ingest`），发空任务收到结果 |
| **6** | P-AUTHC | `build_context` + `check` + `get_prefilter` + `compile_filter` + 生命周期端口 | 对 Cerbos（localhost:13592）调 `/v1/check` 验证策略生效 |
| **7** | B-DOC | 登记/挂载/触发解析 + `register`/`link` 同步调用 | `curl` 上传 API → PostgreSQL 有记录、Cerbos 有镜像 |
| **8** | B-INGEST（摄入） | 摄入 Pipeline：切分→嵌入→写 Milvus | 上传文件 → Milvus 有 chunk 数据（`vis_version=null`） |
| **9** | B-INGEST（盖戳） | `stamp_channel_task` 六条纪律 | 摄入完成 → chunk 的 `vis_version` 不为 null / zero |
| **10** | B-RETRIEVE | 查询 Pipeline：embed→检索→RRF→六条件过滤 | 硬编码 query 能返回 chunk 列表 + chunk_id |
| **11** | B-CHAT | 对话+生成+SSE 流式 | `curl` 查询 API → SSE 流式返回答案 + 来源引用 |
| **12** | 端到端集成 | API 全链路：上传→摄入→盖戳→查询→生成 | 运行 7 条验收 checklist |

### 调试策略

- **步骤 0-6**（平台模块）：每个模块写独立 Python 脚本测试，不依赖 HTTP 链路
- **步骤 7 之后**：需要走 API → Celery worker 全链路，但在每个步骤可以先单独验证该模块的输出（查 DB、查 Milvus）
- **日常开发**：只需要启动对应模块的 worker 进程，不需要全部跑起来（如只开发上传流程，启动 `dev-api` + `dev-ingest` 即可）

---

## 4. 特别注意的约束

### 4.1 接口骨架定死（最重要）

> "每个接口签名、每张表的字段、每个事件的 payload 从第一天就按 v14 最终形态定义"

| 约束 | 阶段一具体操作 |
|------|-------------|
| 表字段现在就要建全 | `execution_epoch` 固定为 1、`pipeline_yaml_version` 固定为 `"v1"`、`resolved_query` 等于 `user_question`、`authz_decision_ref` 空字符串、`content_fingerprint` 计算写入 |
| 接口现在就要有签名 | `filter_items` 接口有但 strict 默认 false；`invoke_rerank` 接口有但 no-op；目录管理返回 501 |
| 后续只填实现不动骨架 | 阶段二开 strict 时不加新接口，只改内部逻辑；`filter_items` 不新增参数 |

### 4.2 P-AUTHC 必须完整实现

> "权限是系统的安全边界，简化版的权限等于没有权限"

- JWT 本地校签 + `build_context`（含 credential 原样保管）
- `check` 单条判定 → `POST /v1/check`
- `get_prefilter` → `compile_filter` → Haystack `MetadataFilter`（六条件）
- `filter_items` 接口有（strict 默认 false）
- `mint_ctx_token` → `POST /v1/context`
- `register_resource` / `link_resource` / `unlink_resource` / `retire_resource`（完整实现）
- 三态映射（allow/deny/indeterminate）+ 四类 fail-closed
- 阶段一之后不用再动 P-AUTHC

### 4.3 盖戳管道六条纪律必须第一版就到位

> "空戳记的 chunk 对任何人不可见，方向是对的；但如果第一版盖戳写错（失败时落了空戳记），修起来要全量重盖，成本极高"

| # | 纪律 | 阶段一必须验证 |
|---|------|-------------|
| 1 | 失败不落盘 | `/v1/visibility` 超时 → chunk 保持 `vis_version=null`，不 ack |
| 2 | unmounted 清空 | 文档卸载后 chunk 戳记 `allow_stamps=[], deny_stamps=[], vis_version=null` |
| 3 | 版本单调性 | `response.version < current vis_version` → 丢弃，不覆盖 |
| 4 | 分批让渡 | 批大小 500，批间让出 CPU |
| 5 | 断点续跑 | 游标记录，崩溃重投靠覆盖写天然幂等 |
| 6 | 审计 fail-open | `STAMP_APPLIED` 写失败不阻塞管线 |

### 4.4 Haystack 防腐层红线

- `@component` 的 `run()` 方法内**零权限逻辑**（不出现 check/filter/prefilter/user_id/roles/principals）
- 六条件 `MetadataFilter` 在 Component **外部**编译注入
- hybrid 模式下稠密路和稀疏路**必须传入同一个 `MetadataFilter` 对象引用**
- 业务模块不得直接 import Haystack 模型类（必须经 P-MODEL 的 `invoke_*` 门面）
- **API 进程禁止调 `pipeline.run()`**——计算只在 Worker 进程内

### 4.5 credential（JWT 原文）保护

- 不出现在日志、Trace span attribute、审计 payload、任务参数中
- 异步链路用 `ctx_token`（`mint_ctx_token`，TTL ≤ 600s），绝对不序列化 JWT

### 4.6 表结构一旦有数据就不能改

阶段一建表时就要加入所有最终字段。以下字段虽然是后续阶段才真正使用，但现在就必须建好并填默认值：

| 表 | 字段 | 阶段一的值 |
|---|-----|----------|
| `ingest_execution` | `execution_epoch` | 固定为 1 |
| `ingest_execution` | `pipeline_yaml_version` | 固定为 `"v1"` |
| `conversation_turn` | `resolved_query` | 等于 `user_question` |
| `conversation_turn` | `authz_decision_ref` | 空字符串 |
| `documents` | `content_fingerprint` | SHA-256 计算写入 |

### 4.7 权限服务不可达 → fail-closed

不是返回 500，不是返回空结果——必须返回 **503 `auth:authz_unavailable`**。与"未找到足够信息"（200）通过 HTTP status 和 error_code 可区分。

### 4.8 存在性三通道纪律

- deny/indeterminate 的 chunk **静默丢弃**，不进 LLM 上下文、不进流式事件
- **绝不告知用户"另有 N 条无权查看"**——计数本身即泄露
- 检索 deny 时对外文案与"未找到足够信息"完全相同，不能说"您没有权限"

### 4.9 依赖方向

```
B-DOC/B-INGEST/B-RETRIEVE/B-CHAT → P-AUTHC/P-TASK/P-MODEL/P-CONFIG/P-STORE/P-OBS/P-AUDIT → Cerbos
```

- 禁止 B-* 之间直接读写对方表
- 禁止任何模块越过 P-AUTHC 直连 Cerbos
- 禁止依赖环

---

## 5. 完成标准（7 条验收 checklist）

```
[ ] 上传 txt/md 文件 → 写入 Milvus，vis_version 有值（盖戳已完成）
[ ] 发查询 → 返回有意义的答案 + 来源引用（chunk_id 列表）
[ ] clearance=1 的用户查询 classification=3 的文档，返回"未找到足够信息"
[ ] KB-A 的文档不出现在 KB-B 的查询结果里
[ ] 端到端 P50 < 10s（CPU 模式，含 LLM 生成）
[ ] 权限服务不可达时，查询返回 503（而不是 500 或空结果）
[ ] 盖戳失败时，chunk 的 vis_version 保持 null（不落空戳记）
```

## 6. 运行环境

| 组件 | 版本 | 连接方式 |
|------|------|---------|
| Python | 3.11 | 本地进程 |
| PostgreSQL | 16-alpine | `localhost:25432` |
| Redis | 7-alpine | `localhost:16379` |
| Milvus | v2.4.13 | `localhost:19530` |
| SeaweedFS | 3.68 | `localhost:18333` |
| Cerbos | 0.39.0 | `localhost:13592` |
| Ollama (Qwen2.5:7b) | 宿主机 | `http://172.17.0.1:11434` |

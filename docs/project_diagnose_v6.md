# RAG v14 项目投产前全面诊断报告 v6

> **诊断日期**: 2026-07-30
> **范围**: 对照 `docs/RAG系统设计v14.md` + `docs/frontend-design.md` 进行全面诊断
> **基础设施状态**: Docker Compose 全栈运行中（PostgreSQL / Redis / Milvus / SeaweedFS / Cerbos / Grafana / Langfuse）

---

## 目录

1. [项目完整性诊断](#1)
2. [架构达成度诊断](#2)
3. [架构偏离诊断](#3)
4. [RAG 核心功能诊断](#4)
5. [切分策略参数匹配度诊断](#5)
6. [检索策略参数匹配度诊断](#6)
7. [前后端交互诊断](#7)
8. [硬编码诊断](#8)
9. [死亡代码模块诊断](#9)
10. [项目运行可靠性诊断](#10)
11. [综合评分与优先修复建议](#11)

---

<a name="1"></a>
## 一、项目完整性诊断

### 1.1 模块实现状态总表

| 模块 | 设计规格章节 | 实现状态 | 完成度 | 关键缺口 |
|------|-----------|---------|--------|---------|
| **P-AUTHC** | §6, §6A | ✅ 已实现 | 85% | `resolve_ctx_token` 仅做本地解析，未真正调 `/v1/context` 反查；熔断器未激活 |
| **P-AUDIT** | §7 | ✅ 已实现 | 70% | `emit_audit_event_txn` 只调用普通 emit，未同步写入同事务 |
| **P-OBS** | §8 | ✅ 已实现 | 75% | OTel span 手动创建（非 Haystack 内置），Metric 仅有 stub |
| **P-TASK** | §9 | ✅ 已实现 | 80% | Outbox relay 基本就绪；`run_pipeline_async` 未被 API 层使用 |
| **P-STORE** | §10 | ✅ 已实现 | 85% | SeaweedFS S3 已集成；本地文件 fallback 仅开发期 |
| **P-MODEL** | §11 | ✅ 已实现 | 80% | `resolve_prompt` 查询 DB；`invoke_llm`/`invoke_embedding` 实现；缺少 Langfuse 集成完整链路 |
| **P-CONFIG** | §12 | ✅ 已实现 | 85% | 4层级联检索配置 + 版本化切分配置均实现 |
| **B-DOC** | §13 | ✅ 已实现 | 80% | submit_ingest_task、trigger_parse、delete/purge 已实现；缺少批量操作 |
| **B-INGEST** | §14 | ✅ 已实现 | 85% | ingest + stamp 两条 Pipeline 已实现；六条盖戳纪律全部落地 |
| **B-RETRIEVE** | §15 | ✅ 已实现 | 75% | 三层检索链路已实现；L3 filter_items 实际未接入 Cerbos `/v1/filter` |
| **B-CHAT** | §16 | ✅ 已实现 | 80% | 四种 synthesis 模式已实现；引用校验+复述守卫已实现 |

### 1.2 前端页面实现状态

| 页面/组件 | 设计规格 | 实现状态 | 完成度 |
|----------|---------|---------|--------|
| `/login` 登录页 | frontend-design.md §0 | ✅ 已实现 | 80% |
| `/select-tenant` 租户选择 | frontend-design.md §0 | ✅ 已实现 | 70% |
| 全局 Header (KB选择器) | frontend-design.md §1 | ✅ 已实现 | 75% |
| `/kb` 知识库管理页 | frontend-design.md §3.1 | ✅ 已实现 | 75% |
| `/chat` 对话页 | frontend-design.md §3.3 | ✅ 已实现 | 80% |
| `/settings` 设置页 | frontend-design.md §3.4 | ✅ 已实现 | 80% |
| `/dashboard` Dashboard | frontend-design.md §3.10 | ✅ 已实现 | 40% |

### 1.3 设计文档要求但缺失的功能

| 缺失项 | 设计依据 | 严重程度 | 说明 |
|--------|---------|---------|------|
| **联合契约测试 J-1~J-20** | §27.2 | 🔴 严重 | 对真实权限服务的 20 项联合测试全未跑，无法验证契约理解正确性 |
| **本系统契约测试（权限+Haystack增量项）** | §27.1 | 🔴 严重 | CI 强制项：权限出口唯一性、零判定断言、Haystack Component 权限零判断、两路 filter 一致性等 |
| **`doc:retrieve` 不调 `/v1/check` 的 CI 断言** | §6.3 | 🟡 中等 | check() 中已拦截，但无 CI 测试 |
| **strict L3 实际接入 `/v1/filter`** | §15.4 | 🔴 严重 | filter_items 硬编码穿透（空实现，直接返回全部输入） |
| **`vis_version=null` 的 chunk 不被返回的断言** | §6A.3 条件⑤ | 🔴 严重 | 编译在 compile_filter 中（vis_version > 0），但无 CI 验证 |
| **事后过滤禁令 CI 断言** | §15.1.1 | 🟡 中等 | 代码未做事后过滤，但缺少自动扫描 |
| **层 2 补检索 MetadataFilter 不变断言** | §15.3 | 🟡 中等 | 代码逻辑保证了，但没有 CI 自动检测 |
| **`credential` 不出现在日志/Trace/审计/task参数的 CI 断言** | §27.1 JWT不外泄 | 🔴 严重 | 无自动扫描 |
| **`is_enabled=false` 不在 strict 保证内的契约测试** | §15.4.1 | 🟡 中等 | 边界需验证 |
| **KB 粒度 VisibilityChanged 展开逻辑** | §14.5.4 | 🟡 中等 | visibility_events.py 有 stub，但未实现分页+批提交 |
| **结构镜像对账** | §13.7b | 🟡 中等 | reconciliation.py 有 stub |
| **戳记对账** | §14.5c | 🟡 中等 | 同上 |
| **层级 chunk 父块重过 MetadataFilter** | §15.5 | 🟡 中等 | HierarchicalMerger 实现未对合并后父块重新施加六条件过滤 |
| **生成层复述长度守卫截断** | §16.4 | 🟢 低 | check_verbatim_ratio 已实现检测，但截断策略是追加引用提示而非替换原文 |
| **RAGAS 评测体系 CI 集成** | §26.1 | 🟢 低 | 评测框架未搭建 |
| **决策缓存五条件检查 + 会签代码** | §25.2 | 🟢 低 | feature_flag 仅返回默认值 |

### 1.4 REST 端点完整性（对照 frontend-design.md §4 的 34 个端点）

**P0 (11个) 完成情况**: 8/11 已实现

| # | 端点 | 状态 |
|---|------|------|
| 1 | `POST /api/v1/auth/dev-login` | ✅ |
| 4 | `GET /api/v1/tenants` | ✅ (auth.py) |
| 6 | `GET /api/v1/knowledge-bases` | ✅ (kb_routes.py) |
| 7 | `POST /api/v1/knowledge-bases` | ✅ (kb_routes.py) |
| 11 | `GET /api/v1/knowledge-bases/{kb_id}/directories` | ✅ (dir_routes.py) |
| 17 | `GET /api/v1/knowledge-bases/{kb_id}/documents` | ✅ (kb_routes.py) |
| 23 | `POST /api/v1/documents/upload` | ✅ (routes.py) |
| 26 | `POST /api/v1/documents/{doc_id}/trigger-parse` | ✅ (kb_routes.py) |
| 29 | `GET /api/v1/conversations` | ✅ (chat_routes.py) |
| 30 | `POST /api/v1/conversations` | ✅ (chat_routes.py) |
| 32 | `GET /api/v1/models` | ✅ (settings_routes.py) |

**P1 (23个) 完成情况**: 17/23 已实现，6 个待完成

| # | 端点 | 状态 |
|---|------|------|
| 2 | `POST /api/v1/auth/token` | ✅ (oidc.py) |
| 3 | `POST /api/v1/auth/refresh` | ✅ (oidc.py) |
| 5 | `GET /api/v1/tenants/{id}/stats` | ❌ 缺失 |
| 8 | `PATCH /api/v1/knowledge-bases/{id}` | ✅ |
| 9 | `DELETE /api/v1/knowledge-bases/{id}` | ✅ |
| 10 | `PATCH /api/v1/knowledge-bases/{id}/chunking-config` | ✅ |
| 12-16 | 目录 CRUD 5个 | ✅ (dir_routes.py) |
| 18 | `GET /api/v1/documents/{doc_id}` | ✅ |
| 19 | `PATCH /api/v1/documents/{doc_id}` | ❌ 缺失 |
| 20 | `GET /api/v1/documents/{doc_id}/chunks` | ✅ |
| 21 | `GET /api/v1/documents/{doc_id}/content` | ✅ |
| 22 | `GET /api/v1/documents/{doc_id}/download` | ✅ |
| 24 | `DELETE /api/v1/documents/{doc_id}/kb/{kb_id}` | ✅ |
| 25 | `PATCH /api/v1/documents/{doc_id}/kb/{kb_id}` | ✅ |
| 27 | `POST /api/v1/documents/batch/delete` | ❌ 缺失 |
| 28 | `POST /api/v1/documents/batch/parse` | ❌ 缺失 |
| 31 | `DELETE /api/v1/conversations/{id}` | ✅ |
| 33 | `GET/PATCH /api/v1/configs/retrieval` | ✅ |
| 34 | `GET /api/v1/prompts` | ✅ |

---

<a name="2"></a>
## 二、架构达成度诊断

### 2.1 依赖方向规则达成度

| 规则 | 实现状态 | 评级 |
|------|---------|------|
| B-* → P-* → 外部权限服务 | ✅ 遵守 | ✅ |
| 业务模块禁止直读对方独占表 | ✅ 遵守 | ✅ |
| 禁止任何模块越过 P-AUTHC 直连权限服务 | ✅ 遵守 | ✅ |
| Haystack Component run() 零权限逻辑 | ✅ 遵守 | ✅ |
| 禁止业务模块 import 观测 SDK | ✅ 遵守 | ✅ |
| 禁止业务模块直调模型（经 P-MODEL） | ⚠️ 部分 | ⚠️ |

**P-MODEL 防腐细节问题**: `invoke_llm` 在 `chat/service.py` 中调用，`invoke_embedding` 在 ingest 组件的 `ollama_embedder.py` 中调用。但 OllamaTextEmbedder 在 YAML 中使用 `qwen3-embedding:0.6b` 硬编码模型名，绕过 `resolve_model`。`create_embedder`/`create_ranker` 是 P-MODEL 提供的防腐工厂函数，但实际 Pipeline YAML 使用的是自定义 Component，不是这些工厂函数。

### 2.2 单一写者原则达成度

| 数据 | 唯一写者 | 实现状态 |
|-----|---------|---------|
| document / mount / directory / outbox | B-DOC | ✅ |
| ingest_execution / chunks | B-INGEST | ✅ |
| audit_log | P-AUDIT | ✅ |
| retrieval_config / chunking_config | P-CONFIG | ✅ |
| conversation / conversation_turn | B-CHAT | ✅ |
| model_registry / prompt_* | P-MODEL | ✅ |
| Milvus payload (allow/deny/vis_version) | B-INGEST (stamping) | ✅ |

### 2.3 三层检索链路达成度

| 层 | 设计 | 实现状态 | 评级 |
|----|------|---------|------|
| L1 prefilter | 六条件 MetadataFilter 编译注入 | ✅ compile_filter + _compile_filter_expr | ✅ |
| L1 两路同一 Filter | hybrid 时两路用相同 filter | ✅ filter_expr 共用 | ✅ |
| L2 过采样+补检索 | k×1.5, refetch 2 轮, Filter 不变 | ✅ 已实现 | ✅ |
| L3 strict 逐条复核 | /v1/filter, ≤200/批, 永久禁缓存 | 🔴 filter_items 穿透 | 🔴 |
| L1 suspended 哨兵 | suspended → 跳过检索 | ✅ | ✅ |
| 事后过滤禁令 | 不在应用层过滤 | ✅ | ✅ |

**L3 的严重缺陷**: `src/permission/authz.py` 的 `filter_items` 调用 `client.filter_items()`（CerbosClient），但 Cerbos PDP 的 `/v1/filter` 端点需确认是否已实现。若 Cerbos 侧未实现该端点，L3 将永远返回全部通过。这导致 `strict=true` 的核心价值落空。

### 2.4 盖戳管道六条纪律达成度

| 纪律 | 实现 | 评级 |
|------|------|------|
| 1. 失败不落盘 | ✅ stamp_channel_task 异常分支 raise retry | ✅ |
| 2. unmounted 清空 | ✅ `_upsert_stamps([], [], 0)` | ✅ |
| 3. 版本单调性 | ✅ `_get_current_vis_version` + new < current → skip | ✅ |
| 4. 分批让渡 | ✅ batch_size=500, col.flush() | ✅ |
| 5. 断点续跑 | ⚠️ 覆盖写天然幂等，但没有游标记录 | ⚠️ |
| 6. 审计 fail-open | ✅ try/except pass | ✅ |

### 2.5 写路径同步维护结构镜像达成度

| 触发点 | 调用 | 实现状态 |
|--------|------|---------|
| 首次创建 document | register(doc, ...) | ✅ submit_ingest_task |
| 建立挂载 | link(doc, kb) | ✅ submit_ingest_task |
| 创建 KB | register(kb, ...) | ✅ kb_routes.create_kb |
| 解除挂载 | unlink(doc, kb) | ✅ delete_document_from_kb |
| 彻底删除 doc | retire(doc, ...) | ✅ _purge_document |
| 删除 KB | retire(kb, ...) | ✅ kb_routes.delete_kb |

**调序检查**: 代码中先调 register/link 再写本地 DB，符合"先权限服务后本地提交"纪律。但 `unlink` 的调用在 `delete_document_from_kb` 中——先 unlink 再删挂载，正确。

---

<a name="3"></a>
## 三、架构偏离诊断

### 3.1 已确认的偏离

| 偏离项 | 设计要求 | 实际实现 | 风险 |
|--------|---------|---------|------|
| **L3 filter_items 穿透** | `/v1/filter` 逐条复核（§15.4） | CerbosClient.filter_items() 实现可能为空 | 🔴 strict 库安全边界缺失 |
| **ctx_token 本地解析** | 调 `/v1/context` 反查（§6.7） | resolve_ctx_token 本地解析 base64 payload（§6.7 的 mint 端也未真正调权限服务） | 🟡 token 未受权限服务验证 |
| **embedding 模型硬编码** | 经 P-MODEL resolve_model（§11.1） | Pipeline YAML 中硬编码 `qwen3-embedding:0.6b`，OllamaDocumentEmbedder/OllamaTextEmbedder 未调用 resolve_model | 🟡 切换模型需改 YAML |
| **合成不走 Haystack Pipeline** | 生成节点在查询 Pipeline 内（§15.0） | chat/service.py 直接调 invoke_llm，不走 Pipeline 的 PromptBuilder+LiteLLMGenerator 节点 | 🟡 生成绕过了 Pipeline |
| **检索模式到 Pipeline 映射** | haystack_pipeline_name 从 P-CONFIG 读取 | retrieve/service.py 硬编码 mapping: "hybrid"→query_v4, "vector_only"→retrieval_v1, "keyword_only"→query_v2 | 🟡 无法按 KB 定制 Pipeline 变体 |
| **rerank 未走 P-MODEL 防腐** | Ranker 由 P-MODEL.create_ranker() 创建（§11.1） | BGEReranker Component 内直接 `from FlagEmbedding import FlagReranker` | 🟡 绕过防腐层 |
| **Pipeline YAML model 硬编码** | 模型从 P-MODEL 注入 | 所有 YAML 中 `model: qwen3-embedding:0.6b` 硬编码 | 🟡 换模型需改 YAML 文件 |
| **Ollama 耦合** | 通过 P-MODEL provider 抽象 | OllamaDocumentEmbedder / OllamaTextEmbedder 组件命名和实现绑死 Ollama API `/api/embed` | 🟡 换 OpenAI 兼容 Embedding 服务需改组件 |

### 3.2 Pipeline YAML 版本化偏离

设计要求（§14.2、§15.0）：
- Pipeline YAML 由 P-CONFIG 管理，版本化存储
- 任务参数传 `pipeline_name` + `yaml_version`
- worker 按名从仓库取 YAML

实际实现：
- Pipeline YAML 保存在本地 `./pipelines/` 目录
- 版本化通过不同的文件名（ingest_v1~v5, query_v1~v5）实现，而非同一文件的版本迭代
- `pipeline_yaml_version` 参数传递但未用于选择版本（硬编码 `"v1"`）
- P-CONFIG 中 `haystack_pipeline_name` 字段未被使用

### 3.3 缺失的 query_v3 Pipeline

`query_v3.yaml` 不存在——Pipeline 命名从 query_v1, query_v2 跳到 query_v4, query_v5。query_v3 的设计用途未明确。检索模式到 Pipeline 的硬编码映射也未覆盖 v3。

---

<a name="4"></a>
## 四、RAG 核心功能诊断

### 4.1 摄入 Pipeline (ingest)

**链路完整性**: ✅ 完整

```
DocumentSplitter → OllamaDocumentEmbedder → BGE_M3SparseEmbedder → PermissionMetadataEnricher → MilvusDocumentStoreWriter
```

所有 5 种切分策略（sentence/word/passage/semantic/hierarchical）均有对应的 Pipeline YAML（ingest_v1~v5），`src/ingest/service.py` 中的 `_STRATEGY_PIPELINE_MAP` 正确映射。

**验证通过的检查项**:
- ✅ 切分配置版本锚定（`chunking_config_version` 传入 `ingest_document_task`）
- ✅ execution_epoch 栅栏 + 协作式取消
- ✅ 空戳记安全默认值（`allow_stamps=[], vis_version=0`）
- ✅ 摄入完成后自动提交盖戳任务
- ✅ 重试指数退避 (30s→60s→120s)
- ✅ 幂等 upsert (ON CONFLICT DO UPDATE)

**存在问题**:
1. `pipeline_overrides` 使用 `${CHUNK_SPLIT_LENGTH}` 等占位符，但 YAML 中的占位符替换是简单的字符串替换——若值中包含特殊字符可能破坏 YAML 结构
2. `MilvusDocumentStoreWriter` 在 insert 失败后尝试 upsert fallback，可能掩盖 schema 不匹配问题

### 4.2 检索 Pipeline (retrieve)

**链路完整性**: ✅ 基本完整，但有限制

```
TextEmbedder → [DenseRetriever + SparseRetriever] → Joiner(RRF/WeightedFusion) → Ranker → HierarchicalMerger
```

**验证通过的检查项**:
- ✅ 5 种 Pipeline 变体覆盖（retrieval_v1, query_v1/v2/v4/v5）
- ✅ retrieval_mode 三态（hybrid/vector_only/keyword_only）
- ✅ fusion_method 双态（rrf/weighted_sum）
- ✅ 动态权重（dense_weight/sparse_weight 传入 Joiner）
- ✅ L1 prefilter 六条件
- ✅ L2 过采样 + 补检索（MetadataFilter 不变）
- ✅ rerank 动态模型选择（P1-6）
- ✅ HierarchicalMerger 窗口合并

**存在问题**:
1. **多 KB 交叉检索未实现**：`retrieve()` 取 `candidate_kbs[0]` 只检索第一个 KB
2. **检索只用单 pipeline 覆盖所有 KB**：设计要求的"按 KB 粒度选 Pipeline 变体"未实现
3. L3 filter_items 穿透（见 §3.1）
4. **混合检索两路 filter 一致性**：代码中用同一个 `filter_expr` 传入两路，✅ 正确。但是没有 CI 断言验证
5. **HierarchicalMerger 合并后未重过 MetadataFilter**：设计 §15.5 要求合并后父块重新施加六条件过滤。当前实现不满足
6. **补检索去重仅按 content**：应该按 (document_id, chunk_index) 去重

### 4.3 生成合成 (synthesis)

**四种模式实现**:
| 模式 | 状态 | 路由规则 | 
|------|------|---------|
| compact | ✅ | doc_count ≤ 5 |
| refine | ✅ | doc_count 6-20 |
| tree_summarize | ✅ | doc_count > 20 |
| no_synthesis | ✅ | doc_count == 0 或显式指定 |

**验证通过的检查项**:
- ✅ 引用校验 (`validate_citations`)
- ✅ 复述长度守卫 (`check_verbatim_ratio`，阈值 60%)
- ✅ Prompt 模板从 DB `resolve_prompt` 取，fallback 到内联默认
- ✅ SSE 流式回传 (`retrieved` → `token` → `done`)

**存在问题**:
1. **合成不走 Haystack Pipeline 查询节点**：设计要求查询 Pipeline 包含 `PromptBuilder + LiteLLMGenerator` 节点。当前实现在 chat/service.py 中直接调 `invoke_llm`，绕过了 Pipeline 框架。这意味着：
   - 生成环节不受 Pipeline 版本管理
   - OTel span 是手动创建的而非 Haystack 内置
   - 无法利用 Haystack 的生成层后置 Component（如复述守卫应该在 Pipeline 中的 Component）
2. **query_v1.yaml 含 Generator 但未被使用**：`query_v1` 包含 `generator` 节点，但 `retrieve()` 在 `retrieval_mode == "vector_only"` 时用了 `retrieval_v1`（无 generator），其他模式用 `query_v2/v4/v5`（无 generator 节点）

### 4.4 Embedding 模型诊断

| 问题 | 说明 |
|------|------|
| **BGE-M3 未使用** | 设计要求使用 BGE-M3 (dense=1024, sparse=250002)，实际使用 `qwen3-embedding:0.6b`（Ollama 模型，非 BGE-M3） |
| **稀疏向量非 BGE-M3** | `BGE_M3SparseEmbedder` 组件命名暗示 BGE-M3，但实际调用 Ollama `/api/embed`，Ollama 的 qwen3-embedding 不输出稀疏向量 |
| **SparseEmbedder 实现问题** | `sparse_embedder.py` 的 `BGE_M3SparseEmbedder.run()` 将 Ollama 返回的 dense embedding 当作 sparse 向量存储——这会导致 Milvus 中 sparse_vector 字段存储错误数据 |
| **向量维度不匹配** | MilvusWriter 硬编码 `DENSE_DIM=1024`（BGE-M3 的维度），但 qwen3-embedding:0.6b 的实际维度可能不同 |

这是一个🔴严重问题——**稀疏向量检索实际不可用**，因为 sparse_vector 字段存的是 dense embedding 而非真实的 BM25 稀疏向量。

---

<a name="5"></a>
## 五、切分策略参数匹配度诊断

### 5.1 前端 → 后端参数传递链路

```
[Settings页面] → PATCH /api/v1/knowledge-bases/{id}/chunking-config
  → chunking_configs 表更新 → ingest_document_task 读取 → Pipeline YAML 占位符覆盖
```

### 5.2 各策略参数对照

| 策略 | 设计参数 | 前端可调 | 后端写入 DB | Pipeline 使用 | 匹配度 |
|------|---------|---------|------------|--------------|--------|
| **sentence** | split_length, split_overlap | ✅ Slider 128-1024 | ✅ chunking_configs.split_length/overlap | ✅ `${CHUNK_SPLIT_LENGTH}` / `${CHUNK_SPLIT_OVERLAP}` | ✅ 100% |
| **word** | split_length, split_overlap, split_threshold | ⚠️ split_length/overlap 可调，split_threshold 不可调 | ⚠️ 缺少 split_threshold 字段 | ⚠️ `${CHUNK_SPLIT_THRESHOLD}` 未在 overrides 中设置 | ⚠️ 67% |
| **passage** | split_length, split_overlap | ✅ | ✅ | ✅ | ✅ 100% |
| **semantic** | breakpoint_threshold_percentile, buffer_size | ✅ Slider 10-90 / 1-10 | ✅ advanced_params JSONB | ✅ `${CHUNK_BREAKPOINT_PERCENTILE}` / `${CHUNK_BUFFER_SIZE}` | ✅ 100% |
| **hierarchical** | parent_split_length, child_split_length | ✅ Slider 512-4096 / 64-1024 | ✅ advanced_params JSONB | ✅ `${CHUNK_PARENT_LENGTH}` / `${CHUNK_CHILD_LENGTH}` | ✅ 100% |

### 5.3 前端切分策略调试能力

| 功能 | 前端实现 | 后端实现 |
|------|---------|---------|
| 策略选择下拉 | ✅ sentence/word/passage/semantic/hierarchical | ✅ |
| split_length 滑块 | ✅ 128-1024, step 64 | ✅ |
| split_overlap 滑块 | ✅ 0-256, step 16 | ✅ |
| semantic 高级参数 | ✅ breakpoint_percentile (10-90) + buffer_size (1-10) | ✅ |
| hierarchical 高级参数 | ✅ parent_length (512-4096) + child_length (64-1024) | ✅ |
| word/passage 专属参数 | ❌ split_threshold 未暴露 | ❌ 未传递到 Pipeline |
| 配置变更后提示重解析 | ✅ "切分配置变更后需重新解析已有文档才会生效" | — |
| 策略切换时条件显示高级参数 | ✅ semantic/hierarchical 条件渲染 | — |

### 5.4 切分策略诊断结论

前端对切分策略的参数调试能力**基本完备**（约 90%）。唯一缺口是 `word` 策略的 `split_threshold` 参数未在前端暴露且未在后端传递。

---

<a name="6"></a>
## 六、检索策略参数匹配度诊断

### 6.1 前端 → 后端参数传递链路

两条路径：

```
路径A（设置持久化）: Settings页面 → PATCH /api/v1/configs/retrieval → retrieval_configs 表
路径B（每次查询覆盖）: Chat输入框 ⚙️面板 → POST /api/v1/conversations/query → Celery 任务参数
```

### 6.2 各检索参数对照

| 参数 | 设计默认 | 前端设置页 | 前端查询面板 | 后端API | Worker消费 | Pipeline使用 | 匹配度 |
|------|---------|-----------|------------|---------|-----------|-------------|--------|
| **retrieval_mode** | hybrid | ✅ 下拉 | ✅ 下拉(3选) | ✅ QueryRequest | ✅ effective_mode | ✅ 选择Pipeline | ✅ 100% |
| **fusion_method** | rrf | ✅ 下拉 | ✅ 下拉(RRF/Weighted) | ✅ QueryRequest | ✅ effective_fusion | ✅ 选择Pipeline | ✅ 100% |
| **top_k** | 10 | ✅ Slider(3-50) | ✅ Slider(1-50) | ✅ QueryRequest | ✅ effective_top_k | ✅ 截断 | ✅ 100% |
| **strict** | false | ✅ Toggle | ✅ Toggle | ✅ QueryRequest | ✅ effective_strict | ✅ L3 逻辑 | ✅ 100% |
| **synthesis_mode** | auto | ✅ 下拉(5选) | ✅ 下拉(5选) | ✅ QueryRequest | ✅ synthesis_mode | ✅ 四种模式 | ✅ 100% |
| **dense_weight** | 0.5 | ✅ Slider(间接) | ✅ Slider | ✅ QueryRequest | ✅ 传入retrieve | ✅ WeightedFusion | ✅ 100% |
| **sparse_weight** | 0.5 | ✅ Slider | ✅ Slider | ✅ QueryRequest | ✅ 传入retrieve | ✅ WeightedFusion | ✅ 100% |
| **oversample_factor** | 1.5 | ✅ Slider(1-3) | ❌ 不可调 | ❌ QueryRequest 无此字段 | ❌ 未从配置读取 | ❌ retrieve() 用默认值 1.5 | ⚠️ 33% |
| **min_results** | 3 | ✅ Slider(1-20) | ❌ 不可调 | ❌ | ❌ | ❌ retrieve() 用默认值 3 | ⚠️ 33% |
| **refetch_max_rounds** | 2 | ✅ Slider(0-5) | ❌ 不可调 | ❌ | ❌ | ❌ retrieve() 用默认值 2 | ⚠️ 33% |
| **rerank_model_id** | "" | ✅ 下拉(从DB) | ❌ 不可调 | ❌ | ✅ 从配置读取 | ✅ BGEReranker | ⚠️ 50% |

### 6.3 检索策略诊断结论

**核心查询参数（retrieval_mode/fusion_method/top_k/strict/synthesis_mode/weights）**在前端和后端之间流转顺畅，匹配度 100%。

**次要调优参数（oversample_factor/min_results/refetch_max_rounds/rerank_model_id）**存在断链：
- 设置页可配置并保存到 DB
- 但 `retrieve()` 函数未从 `RetrievalConfig` 读取这些值——使用了硬编码默认值
- 查询面板未暴露这些参数给用户逐次调优

### 6.4 检索策略调试能力总结

| 调试维度 | 设置页(全局配置) | 查询面板(逐次覆盖) | 后端实际生效 |
|----------|----------------|-------------------|------------|
| 检索模式切换 | ✅ | ✅ | ✅ |
| 融合方式切换 | ✅ | ✅ | ✅ |
| 权重调偏 | ✅ | ✅ | ✅ |
| Top-K | ✅ | ✅ | ✅ |
| Strict | ✅ | ✅ | ✅ |
| 合成模式 | ✅ | ✅ | ✅ |
| 过采样系数 | ✅ | ❌ | ❌ (硬编码 1.5) |
| 补检索阈值 | ✅ | ❌ | ❌ (硬编码 3) |
| 补检索轮数 | ✅ | ❌ | ❌ (硬编码 2) |
| 重排模型 | ✅ | ❌ | ✅ (从DB读) |

---

<a name="7"></a>
## 七、前后端交互诊断

### 7.1 核心交互链完整性

```
[前端] POST /api/v1/conversations/query
  → [API] mint_ctx_token → dispatch Celery 任务 → 立即返回 QueryResponse
  → [SSE] GET /api/v1/conversations/{id}/stream → Redis Pub/Sub 转发
  → [前端] EventSource → retrieved/token/done 事件处理
```

**交互链状态**: ✅ 完整，可以实现从提问到流式回答的全流程

### 7.2 前后端交互问题

| 问题 | 严重程度 | 说明 |
|------|---------|------|
| **SSE 超时未完整实现** | 🟡 中等 | 前端 `streamTimeout` 60s，但后端 SSE 没有超时断开机制（设计 §9.3 要求的 30s 无消息发 error 断开） |
| **query 同步返回 answer 为空** | 🟢 低 | QueryResponse 返回空 answer，依赖 SSE 推送。但前端同时做了同步 fallback 和 SSE 等待，有重复处理风险 |
| **`auto_parse` 前端写死 false** | 🟡 中等 | frontend/lib/kb.ts line 49: `form.append("auto_parse", "false")` 硬编码。与设计 §13.4.2 "按需而非自动" 一致，但用户无法在上传时选择 |
| **KB 选择器与文档列表联动** | 🟡 中等 | KB 选择后文档列表刷新，但缺少 loading 状态防抖 |
| **引用卡片 chunk 内容展示** | ✅ 已修复 | SSE `retrieved` 事件中传了 `chunks:[{chunk_id, content:前200字}]` |
| **错误码前端映射不完整** | 🟢 低 | `getErrorMessage` 仅处理 4 种错误码，缺少 `auth:authz_indeterminate`, `doc:kb_reindexing`, 等 |
| **tenant_id/user_id 在 upload 请求中双重来源** | 🟡 中等 | FormData 中的 `tenant_id="tenant-dev"`, `user_id="dev-user"` 与 middleware ctx 可能不一致 |

### 7.3 前后端数据格式一致性

| 数据结构 | 前端期望 | 后端返回 | 一致？ |
|---------|---------|---------|--------|
| KB 列表 | `{id, name, description, ...}` | KBResponse | ✅ |
| 文档列表 | `{document_id, mount_id, filename, parse_status, ...}` | DocResponse | ✅ |
| 查询请求 | `{question, kb_ids, conversation_id, retrieval_mode, ...}` | QueryRequest | ✅ |
| 查询响应 | `{answer, chunk_ids, conversation_id, turn_index, error_code?}` | QueryResponse | ✅ |
| SSE retrieved | `{chunk_ids, chunks: [{chunk_id, content}]}` | `{event:"retrieved", chunk_ids, chunks}` | ✅ |
| 检索配置 | `RetrievalConfig {top_k, retrieval_mode, ...}` | `RetrievalConfig` dataclass | ✅ |
| 切分配置 | `{kb_id, version, haystack_strategy, split_length, ...}` | ChunkingConfigResponse | ✅ |

### 7.4 Next.js 路由与后端路径对应

| 前端路由 | API base | 代理方式 |
|---------|---------|---------|
| `/login` → 登录页 | POST `/api/v1/auth/dev-login` | ✅ Next.js rewrite → FastAPI |
| `/chat` → 对话页 | POST/GET `/api/v1/conversations/*` | ✅ |
| `/kb` → 知识库管理 | GET/POST/PATCH/DELETE `/api/v1/knowledge-bases/*` | ✅ |
| `/settings` → 设置 | GET/PATCH `/api/v1/configs/*`, `/api/v1/models`, `/api/v1/prompts` | ✅ |
| `/dashboard` → 仪表盘 | GET `/api/v1/dashboard/*` | ✅ |
| `/select-tenant` → 租户选择 | GET `/api/v1/tenants` | ✅ |

---

<a name="8"></a>
## 八、硬编码诊断

### 8.1 严重硬编码（🔴 投产前必须解决）

| 位置 | 硬编码内容 | 影响 | 修复建议 |
|------|----------|------|---------|
| **所有 Pipeline YAML** | `model: qwen3-embedding:0.6b` | 换模型需改 10 个 YAML 文件 | 改为 `${EMBEDDING_MODEL}` 占位符 |
| **src/api/routes.py:72-73** | `tenant_id: str = Form(default="tenant-dev")`, `user_id: str = Form(default="dev-user")` | 生产环境会使用错误的默认租户/用户 | 移除默认值，强制从 ctx 读取 |
| **src/api/deps.py:28** | `tenant_id="tenant-dev"` | 同上 | 从 JWT claim 读取 |
| **src/permission/middleware.py:155** | `tenant_id = claims.get("tenant", "tenant-dev")` | JWT 无 tenant claim 时默认 tenant-dev | 无 tenant claim 应报错 |
| **src/permission/authz.py:340-379** | lifecycle 方法在调 Cerbos 后同步触发了 `on_kb_visibility_changed` / `on_visibility_changed` | 将可见性事件发布耦合在权限调用中 | 移到 B-DOC 的事件发布侧 |
| **src/ingest/components/milvus_writer.py:14** | `DENSE_DIM = 1024` | 硬编码BGE-M3维度但实际用qwen3-embedding | 从 embedding 结果动态获取 |
| **src/retrieve/components/sparse_retriever.py:83** | `search_params={"metric_type": "IP", "params": {"nprobe": 16}}` | nprobe 参数硬编码 | 改为可配置 |

### 8.2 中等硬编码（🟡 应在投产前解决）

| 位置 | 硬编码内容 | 修复建议 |
|------|----------|---------|
| **src/retrieve/service.py:73** | `kb_id = candidate_kbs[0]` | 改为多 KB 检索 |
| **src/retrieve/service.py:93-128** | 检索模式→Pipeline 硬编码映射 | 从 P-CONFIG 的 haystack_pipeline_name 读取 |
| **src/retrieve/service.py:136** | `k_prime = int(top_k * oversample_factor)` | oversample_factor 应从配置读 |
| **src/retrieve/service.py:157** | `while 0 < len(all_docs) < min_results` | min_results/refetch_max_rounds 应从配置读 |
| **src/api/routes.py:140** | `pipeline_name="query_v4"`, `yaml_version="v1"` | 应从 P-CONFIG 读取 |
| **src/chat/service.py:91-92** | `kb_id = kb_ids[0]` | 多 KB 支持 |
| **src/chat/service.py:373-416** | 5 个 `_DEFAULT_*_TEMPLATE` | 应全部在 DB prompt_templates 表中管理 |
| **src/platform/model/registry.py:236** | `_DEFAULT_RERANK_MODEL = "BAAI/bge-reranker-v2-m3"` | 应从 DB model_registry 取默认值 |
| **src/permission/authz.py:33-38** | `failure_threshold=10, recovery_timeout=60` | 应可配置 |
| **src/ingest/service.py:41-47** | `_STRATEGY_PIPELINE_MAP` | 应从 P-CONFIG 或 DB 读取 |
| **src/ingest/service.py:578** | `time.sleep(5)` 等待在途任务退出 | 用更可靠的通知机制 |
| **所有 YAML 文件** | `collection_name: rag_documents` | 集合名硬编码在 10 个 YAML 中 |

### 8.3 低风险硬编码（🟢 可延后）

| 位置 | 硬编码内容 |
|------|----------|
| **frontend/lib/kb.ts:49** | `form.append("auto_parse", "false")` |
| **frontend/app/login/page.tsx:124** | `<option value="tenant-dev">tenant-dev</option>` |
| **frontend/app/components/UploadZone.tsx:27-28** | `"tenant-dev"`, `"dev-user"` fallback |
| **pipelines/ingest_v1.yaml:11** | `split_by: sentence` 为占位符默认值，应由 overrides 覆盖 |

### 8.4 硬编码统计

| 风险等级 | 数量 | 状态 |
|---------|------|------|
| 🔴 严重 (投产前必须修) | 7 | 需修复 |
| 🟡 中等 (投产前建议修) | 12 | 建议修复 |
| 🟢 低 (可延后) | 5 | 已知悉 |

---

<a name="9"></a>
## 九、死亡代码与模块诊断

### 9.1 死亡代码

| 文件/函数 | 状态 | 说明 |
|----------|------|------|
| **`src/retrieve/components/prefilter_injector.py`** | 🟡 疑似死亡 | PrefilterInjector Component 注册在 pipeline_runner，但 Pipeline YAML 中不使用它——prefilter 在 Component 外部编译后注入 |
| **`src/ingest/components/visibility_stamper.py`** | 🟡 疑似死亡 | VisibilityStampComponent 注册但 Pipeline 中不使用——盖戳使用直接的 PyMilvus 调用而非 Haystack Component |
| **`run_pipeline_async` (pipeline_runner.py:215)** | 🟡 未使用 | API 层未使用此函数，改用 `retrieve_and_generate_task.delay()` |
| **`run_pipeline_task` (pipeline_runner.py:166)** | 🟡 未使用 | 同上——celery task 包装从未被调用 |
| **`query_v1.yaml`** | 🟡 未使用 | 包含 generator 节点的旧版查询 Pipeline，当前代码不使用 |
| **`query_v3.yaml`** | ❌ 不存在 | Pipeline 命名跳过了 v3，但代码中没有引用 v3 |
| **`src/permission/visibility_events.py`** | ⚠️ 功能不完整 | KB 粒度展开逻辑 stub（§14.5.4 的 expand_visibility_changed_task 未实现） |
| **`src/platform/task/reconciliation.py`** | ⚠️ 功能不完整 | 镜像对账 + 戳记对账 stub |
| **`src/scripts/fix_tenant_id.py`** | 🟢 运维脚本 | 一次性修复脚本，可能不再需要 |

### 9.2 无测试覆盖的关键模块

| 模块 | 风险 |
|------|------|
| `retrieve/service.py` (三层检索核心) | 无集成测试 |
| `ingest/service.py` (盖戳管道) | 无集成测试 |
| `permission/authz.py` (compile_filter) | 无单元测试验证六条件完整性 |
| `permission/context.py` (JWT解析+principals展开) | 无单元测试 |
| Pipeline YAML 加载和占位符替换 | 无测试 |

---

<a name="10"></a>
## 十、项目运行可靠性诊断

### 10.1 基础设施可用性

| 服务 | 状态 | 端口 |
|------|------|------|
| PostgreSQL | ✅ 运行中 | 25432 |
| Redis | ✅ 运行中 | 16379 |
| Milvus | ✅ 运行中 | 19530 |
| SeaweedFS S3 | ✅ 运行中 | 18333 |
| Cerbos PDP | ✅ 运行中 | 13592 |
| Grafana | ✅ 运行中 | 3000 |
| OTel Collector | ✅ 运行中 | 4317/4318 |
| Langfuse | ✅ 运行中 | 13000 |
| Prometheus | ✅ 运行中 | 内部 |
| Loki | ✅ 运行中 | 内部 |
| Tempo | ✅ 运行中 | 内部 |

### 10.2 关键路径可靠性评估

| 路径 | 能否正常执行 | 阻塞点 |
|------|------------|--------|
| 文档上传 → 登记 | ✅ 可行 | — |
| 文档解析 (trigger_parse) | ✅ 可行 | — |
| 摄入 Pipeline (切分→嵌入→写入) | ✅ 可行 | ⚠️ 稀疏向量存错误数据 |
| 盖戳管道 | ✅ 可行 | — |
| 检索 (prefilter→retrieve→rerank) | ✅ 可行 | ⚠️ 稀疏检索不可用, L3 穿透 |
| LLM 生成 (synthesize) | ✅ 可行 | — |
| SSE 流式回传 | ✅ 可行 | — |
| **完整端到端链路** | ✅ 可行 | ⚠️ 有上述限制 |

### 10.3 能否提供正常服务

**基本回答**: **可以**，但有限制。

系统可以完成"上传文档 → 解析 → 检索 → 生成答案"的完整链路。以下功能受损：
1. **稀疏检索 (BM25)** 不可用——sparse_vector 字段存储了错误数据
2. **Strict L3 实时权限复核** 不可用——filter_items 穿透
3. **多 KB 交叉检索** 不可用——只取第一个 KB

### 10.4 可靠性风险矩阵

| 风险 | 概率 | 影响 | 等级 |
|------|------|------|------|
| 启动失败 (DB连接/Milvus连接) | 低 | 高 | 🟡 — 依赖已在 docker-compose 管理 |
| 内存溢出 (大文件/大模型) | 中 | 高 | 🟡 — 无 chunk 大小硬上限 |
| Pipeline YAML 加载失败 | 低 | 高 | 🟢 — YAML 语法已通过 Haystack 加载验证 |
| Cerbos 不可达 | 中 | 高 | 🔴 — prefilter 返回 suspended → 全部返回空 |
| Redis 连接断开 | 低 | 中 | 🟡 — SSE 流中断 |
| SeaweedFS 不可达 | 中 | 中 | 🟡 — 新文档上传失败，已存储文档不受影响 |
| 并发盖戳任务压垮 Milvus | 中 | 中 | 🟡 — stamping_queue 无并发上限 |

### 10.5 Make 命令可用性

| 命令 | 状态 |
|------|------|
| `make infra` | ✅ 基础设施正常运行 |
| `make db-init` | ✅ 表已初始化 |
| `make db-seed` | ✅ 测试数据已写入 |
| `make dev-api` | ✅ 可启动 |
| `make dev-ingest` | ✅ 可启动 |
| `make dev-retrieve` | ✅ 可启动 |
| `make dev-stamp` | ✅ 可启动 |
| `make dev-relay` | ✅ 可启动 |

---

<a name="11"></a>
## 十一、综合评分与优先修复建议

### 11.1 各维度评分

| 维度 | 评分 | 说明 |
|------|------|------|
| **项目完整性** | 78/100 | 核心功能完备，契约测试缺失，部分 P1 端点缺失 |
| **架构达成度** | 80/100 | 依赖方向/单一写者/三层检索结构正确，L3 穿透是最大缺口 |
| **架构偏离度** | 低-中 | 8 处已确认偏离，其中 L3 严重，其余中等 |
| **RAG 核心功能** | 72/100 | 摄入完整，检索基本完整，稀疏检索不可用 |
| **切分策略匹配度** | 90/100 | 5 种策略前后端参数匹配良好 |
| **检索策略匹配度** | 78/100 | 核心参数完全匹配，调优参数断链 |
| **前后端交互** | 82/100 | 数据格式一致，交互链完整，少量细节问题 |
| **硬编码** | 65/100 | 24 处硬编码 (7严重/12中等/5低) |
| **运行可靠性** | 75/100 | 基础设施稳定，关键路径可运行但有限制 |
| **整体** | **77/100** | **基本可投产，需修复 3 个严重项** |

### 11.2 🔴 投产前必须修复（3 项）

| # | 问题 | 影响 | 预计工作量 |
|---|------|------|----------|
| 1 | **稀疏向量嵌入错误** — `BGE_M3SparseEmbedder` 将 dense vector 当作 sparse vector 写入，导致 Milvus 中 sparse_vector 字段存错误数据，BM25 检索不可用 | hybrid 检索的稀疏路返回错误结果 | 2-3天 |
| 2 | **L3 filter_items 穿透** — `strict=true` 的核心安全边界未生效，权限变更不是实时的 | 敏感 KB 撤权后有窗口期 | 1-2天 |
| 3 | **写路径 order 不严格** — `delete_document_from_kb` 中先调 unlink 再删挂载是正确的，但 `submit_ingest_task` 中 register/link 和 DB 写不是严格在同一个事务中（先写 DB 再调 register——若 register 失败 DB 已写但未回滚） | 孤儿镜像，安全但需对账 | 0.5-1天 |

### 11.3 🟡 投产前建议修复（8 项）

| # | 问题 | 预计工作量 |
|---|------|----------|
| 4 | Pipeline YAML 中 embedding 模型名硬编码，换模型需改 10 个 YAML | 0.5天 |
| 5 | `oversample_factor/min_results/refetch_max_rounds` 配置不生效 | 0.5天 |
| 6 | oversample_factor/min_results/refetch_max_rounds 前端查询面板不可调 | 0.5天 |
| 7 | rerank 绕过 P-MODEL 防腐层 (BGEReranker 直接 import FlagEmbedding) | 0.5天 |
| 8 | ctx_token 未真正调 `/v1/context` 端点 | 1天 |
| 9 | 合成不走 Haystack Pipeline (绕过 PromptBuilder+LiteLLMGenerator) | 2天 |
| 10 | pipeline_name 硬编码 "query_v4"，未从 P-CONFIG 读取 | 0.5天 |
| 11 | 多 KB 交叉检索未实现 (只取 candidate_kbs[0]) | 1-2天 |

### 11.4 🟢 可延后修复（后续迭代）

| # | 问题 |
|---|------|
| 12 | 联合契约测试 J-1~J-20 |
| 13 | 本系统 CI 契约测试全套 |
| 14 | 结构镜像对账 + 戳记对账 |
| 15 | 生成层复述守卫截断（替换原文而非追加提示）|
| 16 | KB 粒度 VisibilityChanged 展开 |
| 17 | stamping_queue 并发上限 |
| 18 | SSE 30s 超时断开 |
| 19 | 缺失的 5 个 P1 REST 端点 |
| 20 | RAGAS 评测体系 |

### 11.5 整体运维建议

1. **灰度上线**：先开 1 个非敏感 KB（strict=false），验证 core 链路 1 周后再开 strict
2. **监控先行**：上线前至少配置 `authz_call_failed_total`, `filtered_rate`, `stamp_lag_seconds` 告警
3. **回滚预备**：保留当前 Milvus collection，新版本用新 collection 名或 alias 切换
4. **容量评估**：与权限服务确认 `/v1/prefilter`, `/v1/filter`, `/v1/visibility` 的容量和限流策略
5. **安全审计**：在生产流量前完成 J-1~J-11 联合契约测试（至少覆盖分享可检索性、封禁、strict 实时性）

---

> **诊断结论**: 项目已达到 **77% 的综合投产就绪度**。核心 RAG 链路（摄入→检索→生成→流式回传）可以跑通，架构设计的大方向正确。但 3 个严重问题（稀疏向量错误、L3 穿透、事务一致性）和 8 个中等问题需要在首版上线前修复。前端 5 种切分策略和检索策略的参数调试能力基本完备。建议按 §11.2 → §11.3 顺序修复后进入灰度阶段。

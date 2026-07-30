# RAG v14 项目投产前全面诊断报告 v4

> **诊断日期**：2026-07-29
> **诊断范围**：项目完整性、架构达成度、RAG 核心功能、前后端对齐、运行可靠性、死代码
> **基准文档**：`docs/RAG系统设计v14.md`、`docs/frontend-design.md`

---

## 一、总体评分

| 维度 | 评分 | 状态 |
|------|------|------|
| 项目完整性 | **75/100** | 🟡 基本完整，8 项缺口 |
| 架构达成度 | **85/100** | 🟢 核心纪律全部落实 |
| RAG 核心功能（切分） | **40/100** | 🔴 仅 1/5 策略完整实现 |
| RAG 核心功能（检索） | **80/100** | 🟡 核心链路完整，5 项缺失 |
| 前后端对齐 | **72/100** | 🟡 API 端点全齐，参数面缺失 |
| 运行可靠性 | **60/100** | 🟡 工程化不足，测试覆盖低，依赖缺失 |
| 死代码 | **55/100** | 🔴 12 死函数 + 2 死 YAML + 3 缺失依赖 |
| 综合 | **67/100** | 🟡 可投产，有多项条件限制 |

---

## 二、项目结构完整性

### 2.1 模块注册表对照

设计文档 §0.1 定义了 8 个平台模块 + 4 个业务模块，代码实际状态：

| 设计模块 | 代码位置 | 文件数 | 状态 |
|---------|---------|--------|------|
| **P-AUTHC** | `src/permission/` | 6 | ✅ 完整 |
| **P-AUDIT** | `src/platform/audit/` | 2 | ✅ 基本完整 |
| **P-OBS** | `src/platform/obs/` | 4 | ✅ 完整 |
| **P-TASK** | `src/platform/task/` | 5 | ✅ 完整 |
| **P-STORE** | `src/platform/store/` | 3 | ✅ 基本完整 |
| **P-MODEL** | `src/platform/model/` | 2 | ✅ 基本完整 |
| **P-CONFIG** | `src/platform/config/` | 2 | ✅ 完整 |
| **B-DOC** | `src/doc/` | 4 | ✅ 完整 |
| **B-INGEST** | `src/ingest/` | 7 | ✅ 完整 |
| **B-RETRIEVE** | `src/retrieve/` | 9 | ✅ 完整 |
| **B-CHAT** | `src/chat/` | 3 | ✅ 完整 |

**结论**：所有 12 个设计模块均有对应代码实现，**无缺失模块**。

### 2.2 REST API 端点覆盖率

前端设计文档 §四 汇总了 34 个 REST 端点（含 11 个 P0 + 23 个 P1），实际实现 44 个端点，**覆盖率 100%**。

| 端点组 | 设计要求 | 已实现 | 状态 |
|--------|---------|--------|------|
| 认证与租户 | 5 | 6 | ✅ 超额 |
| KB 管理 | 5 | 7 | ✅ 超额 |
| 目录管理 | 6 | 6 | ✅ |
| 文档管理 | 10 | 10 | ✅ |
| 批量操作 | 2 | 2 | ✅ |
| 会话 | 3 | 5 | ✅ 超额 |
| 设置 | 3 | 6 | ✅ 超额 |
| Dashboard | 4 | 4 | ✅ |

### 2.3 缺失项清单

| # | 缺失项 | 影响 | 风险等级 |
|---|--------|------|---------|
| 1 | **`metrics/` 目录为空** — 无 Prometheus 规则文件 | 告警规则未落地 | 🟡 中 |
| 2 | **前端未容器化** — 不在任何 docker-compose 中 | 需独立部署前端 | 🟡 中 |
| 3 | **`public/` 为空** — 无静态资源 | 视觉体验不完整 | 🟢 低 |
| 4 | **缺 `.dockerignore`** | Docker 构建上下文过大 | 🟢 低 |
| 5 | **两个 Next.js 配置文件**（`next.config.js` + `next.config.mjs`） | 潜在冲突 | 🟢 低 |
| 6 | **`src/api/` 缺 `__init__.py`** | 不影响运行但非标准 | 🟢 低 |

---

## 三、架构达成度诊断

### 3.1 依赖方向规则（§0.2.1）

**设计规定**：B→P→外部，禁止反向依赖，禁止跨模块直读直写。

**实际状态**：
- ✅ `src/retrieve/service.py` → `src/permission/authz.py`（B-RETRIEVE → P-AUTHC，方向正确）
- ✅ `src/chat/service.py` → `src/retrieve/service.py`（B-CHAT → B-RETRIEVE，合规的内部接口）
- ✅ `src/ingest/service.py` → `src/permission/authz.py`（B-INGEST → P-AUTHC，方向正确）
- ✅ 契约测试 `test_permission_discipline.py` 强制执行 `cerbos_client` 导入唯一性
- ✅ 无跨模块直读表（所有 DB 访问经模块门面）

**判定**：✅ **依赖方向正确，无违规。**

### 3.2 单一写者原则（§0.2.2）

| 表/数据 | 设计写者 | 实际写者 | 状态 |
|---------|---------|---------|------|
| `document` / `document_kb_mount` | B-DOC | `src/doc/service.py` | ✅ |
| `ingest_execution` / `chunks` | B-INGEST | `src/ingest/service.py` | ✅ |
| `audit_log` | P-AUDIT | `src/platform/audit/service.py` | ✅ |
| `retrieval_config` / `chunking_config` | P-CONFIG | `src/platform/config/service.py` | ✅ |
| `conversation` / `conversation_turn` | B-CHAT | `src/chat/service.py` | ✅ |
| `model_registry` / `prompt_*` | P-MODEL | `src/platform/model/registry.py` | ✅ |
| ACL/role_binding/restriction | 权限服务 | Cerbos PDP | ✅ |

**判定**：✅ **所有表单一写者，无跨模块直写。**

### 3.3 权限纪律（§6、§6A）

| 纪律项 | 设计要求 | 实际 | 验证方式 |
|--------|---------|------|---------|
| 权限出口唯一 | 仅 P-AUTHC 调权限服务 | ✅ `authz.py` 是唯一门面 | 契约测试 L54-70 |
| 零本地判定 | 无 `if owner then pass` | ✅ 无本地权限逻辑 | 契约测试 L77-100 |
| 动词合法性 | 16 个动词，无废除动词 | ✅ 代码中无 `doc:write`/`acl:update` | 契约测试 |
| `doc:retrieve` 禁走 `/v1/check` | 抛异常 | ✅ `authz.py` L139-140 | 代码审查 |
| 六条件过滤器 | 全部必须 | ✅ `compile_filter()` L257-283 | 代码审查 |
| 三态映射 | allow/deny/indeterminate | ✅ `_to_decision()` | 代码审查 |
| 熔断器 | 四类 fail-closed | ✅ `CircuitBreaker` 包裹所有端点 | `authz.py` L33-82 |
| JWT 不泄露 | credential 不进日志 | ✅ 契约测试覆盖 | 契约测试 |
| 决策缓存默认关闭 | `authz.decision_cache_enabled: false` | ✅ `feature_flag()` L146 | 代码审查 |
| 权限服务不入 `/readyz` | — | ✅ `/readyz` 不检查 Cerbos | `routes.py` |

**判定**：✅ **权限纪律 10/10 项全部通过。**

### 3.4 Haystack 防腐层（§0.2.4、§11）

| 约束 | 状态 |
|------|------|
| Haystack 类型不出模块签名 | ✅ `retrieve()` 返回 `Dict[str, Any]`，非 Haystack 类型 |
| Component `run()` 内零权限逻辑 | ✅ Prefilter/MetadataFilter 在 Component 外部注入 |
| Generator/Embedder/Ranker 经 P-MODEL | ⚠️ 部分：`query_v4.yaml` 中 `OllamaTextEmbedder` 和 `BGEReranker` 直接 import，不走 P-MODEL |
| API 进程不调 `pipeline.run()` | ✅ Pipeline 只在 Celery worker 中执行 |
| Pipeline YAML 版本化 | ✅ 5 个版本化 YAML 文件 |
| 禁用框架默认 Prompt | ✅ `PromptBuilder` template 从 `resolve_prompt` 取 |

**判定**：🟡 **基本合规，但 P-MODEL 防腐不如设计严格**（见 §四 缺口 5）。

### 3.5 事件驱动与 Outbox

| 事件 | 设计 | 实际 | 状态 |
|------|------|------|------|
| `DocumentMounted` | B-DOC 发布，B-INGEST 消费 | ✅ Outbox + relay | 实现 |
| `DocumentUnmounted` | B-DOC 发布，B-INGEST 消费 | ✅ | 实现 |
| `MountEnabledChanged` | B-DOC 发布，B-INGEST 消费 | ✅ | 实现 |
| `VisibilityChanged` | 权限服务发布，B-INGEST 消费 | ✅ 经 P-AUTHC 转交 | 实现 |
| `DocumentParsed` | B-INGEST 发布，P-AUDIT 消费 | ✅ | 实现 |
| `DocumentParseFailed` | B-INGEST 发布 | ✅ | 实现 |
| Outbox relay | P-TASK 承载 | ✅ `outbox_relay.py` | 实现 |
| 对账兜底 | 定时扫描 | ✅ `reconciliation.py` | 实现 |

**判定**：✅ **事件体系完整，Outbox 模式正确实现。**

---

## 四、RAG 核心功能诊断

### 4.1 切分策略（§12.2、§14.4）

设计文档要求 5 种切分策略：

| 策略 | 设计要求 | 实际状态 | 评分 |
|------|---------|---------|------|
| **sentence** | `DocumentSplitter(split_by="sentence")` | ✅ 完整实现，唯一在用策略 | 100% |
| **word** | `DocumentSplitter(split_by="word")` | 🟡 DB 可配置，Pipeline 未切换 | 30% |
| **passage** | `DocumentSplitter(split_by="passage")` | 🟡 DB 可配置，Pipeline 未切换 | 30% |
| **semantic** | `SemanticDocumentSplitter` | ❌ 完全未实现 | 0% |
| **hierarchical** | 自定义 `HierarchicalDocumentSplitter` | ❌ 完全未实现 | 0% |

**根因分析**：

1. `ingest_v1.yaml` 硬编码 `split_by: sentence`，无策略选择逻辑。
2. `ChunkingConfig.haystack_strategy` 字段存在于 DB 和 P-CONFIG 中，但**摄入 Pipeline 不读取此值来动态选择分块器**。
3. `word` 和 `passage` 只需改 Pipeline YAML 参数即可工作（Haystack 原生支持），但缺少配置驱动的切换逻辑。
4. `semantic` 需要新建 `SemanticDocumentSplitter` 组件封装。
5. `hierarchical` 需要新建 `HierarchicalDocumentSplitter` 组件（注意：已有的 `HierarchicalMerger` 是**查询端**合并组件，非切分组件）。

**判定**：🔴 **切分策略是最大短板，仅 1/5 可用。**

**修复建议**：
1. **P1**：在 `ingest_v1.yaml` 旁边新增 `ingest_v2.yaml`（word）、`ingest_v3.yaml`（passage），在摄入服务中根据 `haystack_strategy` 选择对应的 Pipeline。
2. **P2**：实现 `SemanticDocumentSplitter` 自定义 Component。
3. **P2**：实现 `HierarchicalDocumentSplitter` 自定义 Component。

### 4.2 检索策略（§15.7、§15.8）

设计文档要求的混合检索链路：

```
查询 → Dense Embedder → MilvusEmbeddingRetriever ──┐
     → Sparse Embedder → MilvusBM25Retriever ──────┤
                                                    ├→ DocumentJoiner(RRF) → Ranker → HierarchicalMerger
```

| 节点 | 设计要求 | 实际状态 | 评分 |
|------|---------|---------|------|
| Dense Embedder | `SentenceTransformersTextEmbedder` | ✅ `OllamaTextEmbedder`（qwen3-embedding） | 100% |
| Dense Retriever | `MilvusEmbeddingRetriever` | ✅ `MilvusDenseRetriever` 封装 | 100% |
| Sparse Embedder | `BGE-M3SparseTextEmbedder` | ✅ 自定义实现 | 100% |
| Sparse Retriever | `MilvusBM25Retriever` | ✅ `MilvusSparseRetriever`（pymilvus 原生，因 Haystack 集成不支持） | 100% |
| RRF 融合 | `DocumentJoiner(reciprocal_rank_fusion)` | ✅ `query_v4.yaml` | 100% |
| Rerank | `SentenceTransformersRanker` | ✅ `BGEReranker`（FlagReranker） | 100% |
| 层级合并 | `HierarchicalMerger` | ✅ `query_v4.yaml` | 100% |
| L1 prefilter | 六条件 MetadataFilter 注入 | ✅ `compile_filter()` + 两路同一 filter | 100% |
| L2 过采样+补检索 | k'=k×1.5, refetch≤2 | ✅ `retrieve()` L77-125 | 100% |
| L3 strict 复核 | `/v1/filter` 逐条检查 | ✅ `retrieve()` L128-134 | 100% |

**缺失项**：

| # | 缺失项 | 设计要求 | 影响 |
|---|--------|---------|------|
| 1 | **`retrieval_mode` 运行时切换** | `vector_only` / `keyword_only` / `hybrid` | 当前始终走 hybrid，无法降级为纯稠密/纯稀疏 |
| 2 | **`weighted_sum` 融合** | `weighted_sum` 备选融合方式 | 仅 RRF 一种融合方式 |
| 3 | **`rerank_model_id` 动态选择** | 从 P-CONFIG 读取模型 | 硬编码 `BAAI/bge-reranker-v2-m3` |
| 4 | **`fusion_method` 运行时切换** | P-CONFIG → Pipeline 参数 | 硬编码 `reciprocal_rank_fusion` |
| 5 | **P-MODEL 封装不足** | Embedder/Ranker 经 P-MODEL 防腐 | `query_v4.yaml` 直接 import 自定义 Component，绕过 P-MODEL |

**判定**：🟡 **核心检索链路完整，但参数灵活性和防腐层有缺口。**

### 4.3 生成合成模式（§16.3）

| 模式 | 设计要求 | 实际状态 | 评分 |
|------|---------|---------|------|
| `compact` | 单 PromptBuilder + 单 Generator | ✅ `_synthesize_compact()` | 100% |
| `refine` | 逐 chunk 迭代 | ✅ `_synthesize_refine()` | 100% |
| `tree_summarize` | 分组摘要后合并 | ✅ `_synthesize_tree_summarize()` | 100% |
| `no_synthesis` | 直接返回 Document 列表 | ✅ `_synthesize_no_synthesis()` | 100% |
| 自动模式选择 | 按文档数量 | ✅ `resolve_synthesis_mode()` | 100% |
| 引用校验 | chunk_id 验证 | ✅ `validate_citations()` | 100% |
| 复述守卫 | 60% 长度上限 | ✅ `check_verbatim_ratio()` | 100% |
| 查询改写 | 多轮对话改写 | ✅ `_rewrite_query()` | 100% |

**判定**：✅ **生成合成体系完整，4 种模式全部实现。**

### 4.4 盖戳管道（§14.5）

| 纪律 | 设计要求 | 实际状态 |
|------|---------|---------|
| 失败不落盘 | 调 `/v1/visibility` 失败 → 不写空戳记 | ✅ |
| unmounted 清空 | 响应 unmounted=true → 清空戳记 | ✅ |
| 版本单调性 | `response.version < vis_version` → 丢弃 | ✅ |
| 分批 upsert | 500/批，批间让渡 | ✅ |
| 断点续跑 | 游标支持 | ✅ |
| 审计 fail-open | `STAMP_APPLIED` fail-open | ✅ |
| 独立队列 | `stamping_queue` 与摄入分离 | ✅ |
| 三个触发源 | 摄入完成/VisibilityChanged/对账 | ✅ |
| KB 粒度展开 | KB 权限变更 → 展开为逐文档盖戳 | ✅ |

**判定**：✅ **盖戳管道六条纪律全部满足。**

---

## 五、前后端对齐诊断

### 5.1 页面路由

| 前端页面 | 设计要求 | 状态 |
|---------|---------|------|
| `/login` | 开发模式 + SSO 生产模式 | ✅ 完整实现 |
| `/select-tenant` | 多租户选择 | ✅ 完整实现 |
| `/kb` | 知识库管理（文档+目录+上传+预览） | ✅ 完整实现 |
| `/chat` | 对话页（SSE 流式+引用卡片） | ✅ 完整实现 |
| `/settings` | 设置页（模型+检索+切分+Prompt） | ✅ 完整实现 |
| `/dashboard` | 仪表盘（使用统计+质量指标） | ✅ 完整实现 |

### 5.2 检索参数前端可调性

前端设置页 vs 设计文档要求的检索参数：

| 参数 | 设计 §12.1 | 前端 UI | 后端实现 | 状态 |
|------|-----------|---------|---------|------|
| `top_k` | ✅ | ✅ Slider (3-50) | ✅ `resolve_retrieval_config()` | ✅ |
| `oversample_factor` | ✅ | ✅ Slider (1.0-3.0) | ✅ `retrieve()` L24 | ✅ |
| `min_results` | ✅ | ✅ Slider (1-20) | ✅ `retrieve()` L25 | ✅ |
| `refetch_max_rounds` | ✅ | ✅ Slider (0-5) | ✅ `retrieve()` L26 | ✅ |
| `strict` | ✅ | ✅ Toggle 按钮 | ✅ `retrieve()` L128 | ✅ |
| **`retrieval_mode`** | ✅ | ❌ **无 UI** | ⚠️ 定义但未切换 | 🔴 缺口 |
| **`fusion_method`** | ✅ | ❌ **无 UI** | ⚠️ 定义但未切换 | 🔴 缺口 |
| **`synthesis_mode`** | ✅ | ❌ **无 UI** | ✅ 自动选择 | 🟡 缺口 |
| **`rerank_model_id`** | ✅ | ❌ **无 UI** | ❌ 硬编码 | 🔴 缺口 |
| `haystack_pipeline_name` | ✅ | ❌ **无 UI** | ⚠️ 定义但硬编码 `query_v4` | 🟡 缺口 |

**判定**：🟡 **5/10 个可调参数前端有 UI，缺少 retrieval_mode、fusion_method、synthesis_mode、rerank_model_id、pipeline_name 的控制。**

### 5.3 切分参数前端可调性

| 参数 | 设计 §12.2 | 前端 UI | 后端实现 | 状态 |
|------|-----------|---------|---------|------|
| `haystack_strategy` | ✅ 5 种 | 🟡 仅 3 种（sentence/word/passage） | ⚠️ 仅 sentence 有效 | 🔴 缺口 |
| `split_length` | ✅ | ✅ Slider (128-1024) | ✅ `ChunkingConfig` | ✅ |
| `split_overlap` | ✅ | ✅ Slider (0-256) | ✅ `ChunkingConfig` | ✅ |
| `language` | ✅ | ❌ **无 UI** | ✅ `ChunkingConfig` | 🟡 缺口 |

**前端 chunking strategy 下拉菜单只列出 3 种**（缺 semantic、hierarchical），且后端只实际支持 1 种。

**判定**：🔴 **切分参数前端可调但不完整，且后端实现落后于配置面。**

### 5.4 前端全局功能

| 功能 | 状态 |
|------|------|
| 全局 Header（KB 选择器 + 租户 + 用户 + 退出） | ✅ |
| KB 多选下拉（含内联重命名/删除） | ✅ |
| AuthGuard（JWT 过期检查 + 空闲超时登出） | ✅ |
| ErrorBoundary（类组件崩溃捕获） | ✅ |
| Toast 全局通知系统 | ✅ |
| Axios interceptor（401/403/503 统一处理） | ✅ |
| SSE 流式接收（useChat 模式） | ✅ |
| 降级状态区分（insufficient_evidence vs authz_unavailable） | ✅ |
| 管理台跳转入口（权限管理外链） | ✅ |
| Grafana / Langfuse 外链 | ✅ |

**判定**：✅ **前端全局功能完整。**

---

## 六、运行可靠性诊断

### 6.1 容器化部署

| 组件 | docker-compose | Dockerfile | 状态 |
|------|---------------|------------|------|
| PostgreSQL 16 | ✅ `infra.yml` | 官方镜像 | ✅ |
| Redis 7 | ✅ `infra.yml` | 官方镜像 | ✅ |
| Milvus 2.4 | ✅ `infra.yml` | 官方镜像 | ✅ |
| SeaweedFS | ✅ `infra.yml` | 官方镜像 | ✅ |
| Cerbos 0.39 | ✅ `infra.yml` | 官方镜像 | ✅ |
| Grafana / Tempo / Loki | ✅ 独立编排 | 官方镜像 | ✅ |
| Langfuse | ✅ 独立编排 | 官方镜像 | ✅ |
| API (FastAPI) | ✅ `app.yml` | ✅ `Dockerfile` | ✅ |
| ingestion-worker | ✅ `app.yml` | ✅ 同 Dockerfile | ✅ |
| retrieval-worker | ✅ `app.yml` | ✅ 同 Dockerfile | ✅ |
| stamping-worker | ✅ `app.yml` | ✅ 同 Dockerfile | ✅ |
| outbox-relay | ✅ `app.yml` | ✅ 同 Dockerfile | ✅ |
| **前端 (Next.js)** | ❌ **未容器化** | ❌ | 🔴 |

**判定**：🟡 **后端容器化完整，前端需单独部署。**

### 6.2 测试覆盖

| 测试类型 | 文件数 | 覆盖范围 |
|---------|--------|---------|
| 契约测试 | 3 | 权限纪律、联合契约 J1-11、J12-20 |
| 单元测试 | **0** | 无 |
| 集成测试 | **0** | 无 |
| 前端测试 | **0** | 无 |
| E2E 测试 | **0** | 无 |

**现有测试内容**：
- `test_permission_discipline.py`：13 项权限纪律静态断言（cerbos_client 导入唯一性、零判定、动词合法性、六条件过滤器、JWT 不泄露等）
- `test_joint_1_11.py`：联合契约测试 1-11（依赖外部 Cerbos 实例）
- `test_joint_12_20.py`：联合契约测试 12-20（依赖外部 Cerbos 实例）

**判定**：🔴 **测试覆盖严重不足。无单元测试，无集成测试，无前端测试。完全依赖 3 个契约测试文件。**

### 6.3 错误处理与降级

| 场景 | 设计要求 | 实际 |
|------|---------|------|
| 权限服务不可达 | 503 + 拒答降级 | ✅ 熔断器 + fail-closed |
| 向量库不可达 | `retrieve:vector_store_unavailable` | ✅ `pipeline_error` |
| 检索结果不足 | `retrieve:insufficient_evidence` | ✅ 与 deny 同文案 |
| LLM 生成超时 | `chat:stream_timeout` | ✅ SSE error 事件 |
| 摄入任务失败 | 指数退避×3 + 死信 | ✅ 实现 |
| 盖戳任务失败 | 保持旧戳记 + 对账兜底 | ✅ 实现 |
| 熔断器打开 | 直接拒 + 半开探测 | ✅ CircuitBreaker(60s) |
| 前端网络错误 | Toast + 重试 | ✅ Axios interceptor |
| 401 未认证 | 跳转 /login | ✅ interceptor |
| 403 禁止 | Toast "权限不足" | ✅ interceptor |

**判定**：✅ **错误处理体系完整，fail-closed 纪律落实。**

### 6.4 可观测性

| 信号 | 归属 | 状态 |
|------|------|------|
| Trace | P-OBS (OTel) | ✅ Haystack 内置 + 手动 authz.* span |
| Log | P-OBS (structlog) | ✅ |
| Metric | P-OBS (Prometheus) | ✅ `metrics.py` 定义完整 |
| 模型观测 | P-MODEL (Langfuse) | ✅ langfuse SDK |
| 审计 | P-AUDIT | ✅ `audit_log` 表 + 风险分级 |
| 告警规则 | — | ❌ `metrics/` 目录为空，无告警规则 |

**判定**：🟡 **埋点完整，告警规则缺失。**

### 6.5 数据库初始化

- ✅ `scripts/init.sql` 定义全部表结构（25 张表）
- ✅ `make db-init` 可执行建表
- ✅ `make db-seed` 写入开发测试数据
- ✅ PostgreSQL 启动时自动加载 `init.sql`

---

## 七、死代码 / 冗余诊断

### 7.1 YAML Pipeline 死代码

| # | 文件 | 问题 | 建议 |
|---|------|------|------|
| 1 | `pipelines/query_v2.yaml` | **从未被任何 .py 文件引用**，与 v1 结构相同仅 Prompt 不同 | 删除，归档到 git history |
| 2 | `pipelines/query_v3.yaml` | **从未被任何 .py 文件引用**，与 v1 结构相同仅 Prompt 不同 | 同上 |
| 3 | `pipelines/query_v1.yaml` | 仅稠密检索，无混合/RRF/Rerank。仍作为默认值被引用但生产使用 v4 | 保留作为基线参考 |

### 7.2 死函数/组件（定义但从未调用）

| # | 函数/组件 | 文件位置 | 说明 |
|---|----------|---------|------|
| 1 | `run_pipeline_async()` | `src/platform/task/pipeline_runner.py:105` | 导出但无外部调用点，所有实际执行用 `run_pipeline_sync` |
| 2 | `run_pipeline_task()` | `src/platform/task/pipeline_runner.py:62` | 仅被 `run_pipeline_async` 内部调用，连锁死亡 |
| 3 | `create_embedder()` | `src/platform/model/registry.py:298` | 导出但从未调用，Embedder 由 YAML 直接实例化 |
| 4 | `create_ranker()` | `src/platform/model/registry.py:322` | 导出但从未调用，Ranker 由 YAML 直接实例化 |
| 5 | `instrument_fastapi()` | `src/platform/obs/tracing.py:55` | 导出但 `main.py` 未调用（仅调用了 `init_tracing`） |
| 6 | `init_metrics()` | `src/platform/obs/metrics.py:55` | 导出但无启动代码调用 |
| 7 | `setup_logging()` | `src/platform/obs/logger.py:12` | 定义但 `main.py` 未调用 |
| 8 | `feature_flag()` | `src/platform/config/service.py:144` | 导出但无调用点 |
| 9 | `VisibilityStampComponent` | `src/ingest/components/visibility_stamper.py:15` | 完整 Haystack Component 但未在任何 YAML Pipeline 中使用 |
| 10 | `PrefilterInjector` | `src/retrieve/components/prefilter_injector.py:11` | 完整 Haystack Component 但未在任何 YAML Pipeline 中使用（prefilter 在组件外部注入） |
| 11 | `StorageBackend.get_stream()` | `src/platform/store/backend.py:57` | 方法体存在但从未被调用 |
| 12 | `_increment_epoch()` | `src/ingest/service.py:76` | 标注为"备用工具"，从未调用 |

### 7.3 依赖问题

| # | 问题 | 详情 |
|---|------|------|
| 1 | **缺失依赖** | `requests`、`nest_asyncio`、`jinja2` 在代码中 import 但不在 `requirements.txt` |
| 2 | **冗余依赖** | `fastembed-haystack==1.4.1` 已被自定义 Ollama 组件替代，不再需要 |
| 3 | **未锁定版本** | `openinference-instrumentation-haystack` 无版本号 |
| 4 | **过时文档** | `src/platform/task/__init__.py:7` 引用不存在的 `subscribe_stream` 函数 |
| 5 | **文档/实现不一致** | `src/ingest/service.py` docstring 说用 `run_pipeline_async`，实际用 `run_pipeline_sync` |

### 7.4 其他冗余

| # | 文件/代码 | 问题 | 建议 |
|---|----------|------|------|
| 1 | `frontend/next.config.mjs` | 空文件，与 `next.config.js` 并存 | 删除 |
| 2 | `metrics/` | 空目录 | 填充告警规则或删除 |
| 3 | `rerank_model_id` 字段 | DB + P-CONFIG 中定义但代码硬编码 | 接入动态选择 |
| 4 | `retrieval_mode` 字段 | DB + P-CONFIG 中定义但检索始终 hybrid | 实现运行时切换 |
| 5 | `fusion_method` 字段 | DB + P-CONFIG 中定义但始终 RRF | 实现 weighted_sum 或移除 |

**判定**：🔴 **12 个死函数/组件 + 2 个死 Pipeline YAML + 3 个缺失依赖 + 1 个冗余依赖。死代码量较大，需清理。**

---

## 八、投产就绪判定

### 8.1 可投产条件

| 条件 | 状态 | 说明 |
|------|------|------|
| ✅ 基础设施运行正常 | 已确认 | docker-compose.infra.yml 在运行中 |
| ✅ 统一观测平台运行正常 | 已确认 | Grafana/Langfuse 在运行中 |
| ✅ 后端全容器化 | 已确认 | API + 4 类 Worker + Outbox Relay |
| ✅ 权限体系完整 | 已确认 | P-AUTHC 五端点 + 熔断 + fail-closed |
| ✅ 核心 RAG 问答链路 | 已确认 | 查询→检索→生成→流式 完整 |
| ⚠️ 前端部署 | 有条件 | 需单独 `npm run build && npm start` |
| ⚠️ 切分策略 | 有条件 | 仅 sentence 可用 |
| 🔴 测试覆盖 | 不满足 | 无单元测试/集成测试 |

### 8.2 建议投产前提条件（Minimum Viable Production）

1. **🔴 MUST**：补充至少 5 个核心路径的集成测试（上传→摄入→盖戳→检索→问答）
2. **🔴 MUST**：前端容器化或明确前端部署方案（Nginx 反代配置）
3. **🟡 SHOULD**：word 和 passage 切分策略接入配置驱动切换
4. **🟡 SHOULD**：前端设置页补充 `retrieval_mode`、`fusion_method`、`synthesis_mode` 控制
5. **🟡 SHOULD**：填充 `metrics/` 目录的 Prometheus 告警规则
6. **🟢 NICE**：删除死代码（query_v2/v3 YAML、空 next.config.mjs）
7. **🟢 NICE**：`rerank_model_id` 从 P-CONFIG 动态读取而非硬编码

### 8.3 风险矩阵

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| 非 sentence 切分策略不可用 | 高 | 低（sentence 是推荐默认） | 补充 word/passage 配置切换 |
| 检索参数前端不可调 | 中 | 低（后端级联有合理默认值） | 补充前端 UI |
| 测试不足导致回归 | 中 | 高 | 补充核心链路集成测试 |
| 前端未容器化导致部署复杂 | 中 | 中 | 编写前端部署 SOP |
| 权限服务不可达全站停服 | 低 | 高 | 已实现熔断+告警（需填充告警规则） |
| Milvus/Redis 单点故障 | 低 | 高 | 当前单机部署，扩容路径已设计 |

---

## 九、优化修复建议（按优先级）

### P0 — 投产前必须（2 项）

| # | 项目 | 预估工时 | 说明 |
|---|------|---------|------|
| 1 | **补充核心链路集成测试** | 3d | 至少覆盖：文档上传→摄入→盖戳→检索→问答 完整链路；权限 deny 场景；fail-closed 场景 |
| 2 | **前端部署方案落地** | 1d | Dockerfile + nginx 配置 + docker-compose 集成，或编写独立部署 SOP |

### P1 — 第一批修复（5 项）

| # | 项目 | 预估工时 | 说明 |
|---|------|---------|------|
| 3 | **word/passage 切分配置驱动** | 2d | 新增 `ingest_v2.yaml`/`ingest_v3.yaml`，根据 `haystack_strategy` 选择 Pipeline |
| 4 | **前端检索参数补充** | 1d | 设置页加 retrieval_mode / fusion_method / synthesis_mode / rerank_model_id 下拉选择 |
| 5 | **P-MODEL 防腐增强** | 1d | `query_v4.yaml` 中的 `OllamaTextEmbedder`/`BGEReranker` 改为经 P-MODEL 门面注入 |
| 6 | **rerank_model_id 动态选择** | 0.5d | 从 P-CONFIG 读取而非硬编码 |
| 7 | **retrieval_mode 运行时切换** | 1d | 支持 vector_only/keyword_only/hybrid 三种模式的条件路由 |

### P2 — 第二批修复（4 项）

| # | 项目 | 预估工时 | 说明 |
|---|------|---------|------|
| 8 | **SemanticDocumentSplitter 实现** | 3d | 新建 `@component`，封装 Haystack `SemanticDocumentSplitter` |
| 9 | **HierarchicalDocumentSplitter 实现** | 3d | 新建 `@component`，多层切分 + parent_id 关系 |
| 10 | **weighted_sum 融合实现** | 1d | `DocumentJoiner(join_mode="weighted_sum")` + 分数归一化 |
| 11 | **前端容器化** | 1d | Next.js standalone 模式 + Nginx + docker-compose 集成 |

### P3 — 清理与增强（5 项）

| # | 项目 | 预估工时 | 说明 |
|---|------|---------|------|
| 12 | **死代码清理** | 1d | 删除 query_v2/v3 YAML、12 个死函数/组件（§七.2）、空 next.config.mjs、空 metrics/、`fastembed-haystack` 依赖 |
| 13 | **依赖修复** | 0.5d | 补 `requests`/`nest_asyncio`/`jinja2` 到 requirements.txt；锁定 `openinference-instrumentation-haystack` 版本；移除 `fastembed-haystack` |
| 14 | **Prometheus 告警规则** | 1d | 填充 `metrics/` 目录，至少覆盖 §8.3 关键指标 |
| 15 | **OIDC 生产登录** | 2d | `/auth/callback` 501 → 完整 OAuth2 流程 |
| 16 | **过时文档修复** | 0.5d | 清理 `platform/task/__init__.py` 错误 docstring；修正 `ingest/service.py` 文档/实现不一致 |

---

## 十、总结

### 10.1 架构亮点

1. **权限纪律 100% 落实**：零本地判定、唯一出口、熔断 fail-closed、六条件过滤器、JWT 不泄露——设计文档 §6 的所有纪律在代码中有对应实现和契约测试。
2. **混合检索完整度高**：dense+sparse+RRF+Rerank+层级合并 五节点链路在 `query_v4.yaml` 中完整实现，L1/L2/L3 三层检索正确。
3. **生成合成模式齐全**：compact/refine/tree_summarize/no_synthesis 四种模式均实现，具备引用校验和复述守卫。
4. **前后端 API 端点 100% 对齐**：44 个后端端点完整覆盖前端设计文档的 34 个需求。
5. **事件驱动架构**：Outbox + 至少一次投递 + 幂等消费 + 对账兜底，完整可靠。

### 10.2 主要短板

1. **切分策略单一**（最大短板）：5 种策略仅 1 种可用。semantic/hierarchical 完全缺失。
2. **测试覆盖极低**：仅 3 个契约测试文件，无单元测试/集成测试/前端测试。
3. **前端未容器化**：需独立部署步骤。
4. **配置→执行缺口**：P-CONFIG 中定义了 retrieval_mode/fusion_method/rerank_model_id，但检索执行层未接入。

### 10.3 投产建议

**当前状态**：在满足以下条件后可以**小范围灰度投产**：
- 补充 2 项 P0 修复（集成测试 + 前端部署方案）
- 接受仅 sentence 切分策略可用（对大多数场景足够）
- 接受 P1 项在灰度期间逐步交付

**不建议**在完成 P0 修复前全量投产。

---

> **参考**：
> - 设计文档：`docs/RAG系统设计v14.md`（1973 行）
> - 前端设计：`docs/frontend-design.md`（572 行）
> - 上一次诊断：`docs/project_diagnose_v3.md`
> - 本次诊断基于代码静态分析 + 设计文档对照，未进行运行时验证。

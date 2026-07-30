# 阶段二：检索质量加固 — 实施分析

> 来源：`docs/RAG系统设计v14落地方案.md` 第五章 + `docs/RAG系统设计v14.md` §6A, §11, §13.5, §15.1-15.4, §15.8, §16, §26, §27.3
> 核心原则：**不修改阶段一的任何接口签名或表结构**，只填实占位接口、升级内部实现

---

## 1. 覆盖的 RAG 环节及与阶段一的衔接

阶段一完成了"单条链路跑通"——上传→摄入→盖戳→检索→生成。阶段二在这条链路上**加固质量**，填实阶段一留下的 5 个 no-op 占位 + 扩展 6 个新功能：

```
阶段一链路（不变）     阶段二升级内容（只改内部实现，不改接口）
───────────────────── ─────────────────────────────────────
P-AUTHC check_batch   → 从串行N次改为单次 /v1/check/batch 批量端点
P-AUTHC filter_items  → 从直接返回改为真正调 /v1/filter（strict=true 时触发）
P-AUDIT emit_audit    → 从 structlog 记日志改为落 audit_logs 表
P-MODEL invoke_rerank → 从 no-op 改为接入 BGE Reranker v2 真实重排序
B-CHAT resolved_query → 从透传 user_question 改为 LLM 多轮改写
P-MODEL Langfuse      → 新增模型观测（prompt/token/成本/链路可视化）
P-CONFIG 级联         → 从 kb→tenant 两级补上 turn→conversation 四层级联
B-DOC 目录管理        → 从返回 501 改为真实实现
Pipeline YAML         → 新增 query_v2.yaml（不改 v1），加入 Ranker 节点
质量体系              → 首批评测集 + RAGAS 基线脚本
```

| 阶段一占位/缺失 | 阶段二实现 | 改动范围 | 衔接方式 |
|----------|---------|---------|---------|
| `invoke_rerank`（no-op） | 接入真实 Reranker. v2 | P-MODEL 内部实现，接口签名不变 | B-RETRIEVE/query_v2.yaml 直接调用，无感升级 |
| `filter_items`（返回全部） | 真正调 `/v1/filter`，`strict` 可配 | B-RETRIEVE 内部逻辑，接口不变 | strict=true 的 KB 自动走层 3 实时复核 |
| `emit_audit_event`（记日志） | 落 `audit_logs` 表 | P-AUDIT 内部实现，接口不变 | 所有模块调用的审计自动升级 |
| `resolved_query = user_question` | LLM 多轮改写 | B-CHAT 内部逻辑，表结构不变 | `conversation_turn.resolved_query` 字段填入改写结果 |
| `check_batch`（串行 N 次） | `/v1/check/batch` 单次往返 | P-AUTHC 内部实现，接口不变 | B-DOC 批量操作自动提速 |
| 目录管理（501） | 真实 CRUD | B-DOC 新增实现 | 接口签名已在阶段一定死 |
| chunking_configs 只有 word/sentence | 新增 semantic、hierarchical strategy | P-CONFIG 新增 | Haystack `SemanticDocumentSplitter` 接入 |
| model_registry 全 hardcode | 表数据驱动 + prompt_template 版本化 | P-MODEL 升级 | 不影响现有调用方 |
| 无模型观测 | Langfuse（LiteLLM callback） | P-MODEL 新增 | 对业务代码透明 |

---

## 2. 需要新建和修改的文件

### 新建文件

| 文件 | 用途 |
|------|------|
| `pipelines/query_v2.yaml` | 升级版查询 Pipeline：加入 SentenceTransformersRanker 节点，Generator 改为 LiteLLMGenerator |
| `src/platform/model/langfuse_setup.py` | Langfuse 初始化配置（trace + LiteLLM callback） |
| `src/scripts/seed_model_registry.py` | 初始化 model_registry / prompt_template 表数据的脚本 |
| `src/scripts/eval_ragas.py` | RAGAS 离线评测脚本（加载评测集 → 跑检索 → 计算 recall/faithfulness → 对比基线） |
| `tests/eval_sets/README.md` | 评测集说明 |
| `tests/eval_sets/kb-dev-test/questions.json` | 开发测试 KB 的首批评测三元组（≥50 条） |
| `metrics/baseline.json` | RAGAS 基线值（首次跑完后生成） |

### 修改文件

| 文件 | 修改内容 |
|------|---------|
| `src/platform/model/registry.py` | ① `invoke_rerank` 从 no-op 改为加载 BGE Reranker v2 并实际排序 ② `resolve_prompt` 从 hardcode 改为查 prompt_template 表 ③ `resolve_model` 从 hardcode 改为查 model_registry 表 |
| `src/permission/cerbos_client.py` | ① `check_batch` 方法改为调 `POST /v1/check/batch` ② `filter_items` 方法改为调 `POST /v1/filter`（带 channel.kb） ③ `register_resource/link_resource/unlink_resource/retire_resource` 改为真实 HTTP 调用 |
| `src/permission/authz.py` | ① `check_batch` 改用 client.check_batch 单次批量 ② `filter_items` 改为真正调 client.filter_items |
| `src/platform/audit/service.py` | `emit_audit_event` + `emit_audit_event_txn` 从 structlog 改为写 audit_logs 表（asyncpg INSERT） |
| `src/platform/config/service.py` | ① `resolve_retrieval_config` 从两级改为四层级联（turn→conversation→kb→tenant） ② `resolve_chunking_config` 支持 semantic/hierarchical strategy ③ 从 chunking_configs 表按版本读取 |
| `src/retrieve/service.py` | ① 层 3 `filter_items` 从 no-op 改为真正调 P-AUTHC ② query Pipeline 改用 v2（当 config 指定时） |
| `src/chat/service.py` | `resolved_query` 从透传改为 LLM 多轮改写（取最近几轮对话写 prompt） |
| `src/doc/service.py` | ① `create_directory` / `list_directory` / `delete_directory` ② `submit_ingest_task` 的 `auto_parse` 默认改为 false |
| `src/api/routes.py` | 新增目录管理 REST 端点 |

---

## 3. 实现顺序（从底层到上层，能独立测试的先行）

| 步骤 | 模块 | 做什么 | 独立验证方式 |
|------|------|------|------------|
| **0** | model_registry + prompt_template 表 | 写 seed 脚本初始化数据 | `psql` 查询表有数据 |
| **1** | P-MODEL Langfuse | `langfuse_setup.py` 配置 LiteLLM callback | 发一次 LLM 调用 → Langfuse 页面看到 trace |
| **2** | P-MODEL invoke_rerank | 加载 BGE Reranker v2 对候选文档排序 | 输入 query + 5条文档，输出中相关文档排到前面 |
| **3** | P-MODEL resolve_prompt | 从 prompt_template 表读模板 | Python 脚本调用 resolve_prompt("default","v2") 返回 DB 中的模板 |
| **4** | P-AUDIT 落库 | emit_audit_event 写 audit_logs 表 | 调一次 → `SELECT * FROM audit_logs` 有新行 |
| **5** | P-CONFIG 级联 | 四层级联解析 turn→conversation→kb→tenant | 插入不同 level 的配置 → resolve 返回就近覆盖后的值 |
| **6** | P-CONFIG chunking | semantic/hierarchical strategy | `resolve_chunking_config` 返回对应策略配置 |
| **7** | P-AUTHC check_batch | `/v1/check/batch` 真实端点 | 传 3 个资源 → 单次 HTTP 往返返回 3 个结果 |
| **8** | P-AUTHC filter_items | `/v1/filter` 真实端点 | strict=true 时丢弃无权 chunk |
| **9** | B-CHAT 多轮改写 | `resolved_query = LLM改写(history)` | "那第二条呢" → resolved_query 被改写为完整问题 |
| **10** | B-DOC 目录管理 | `create_directory/list_directory/delete_directory` | `curl` API 建目录 → 查 DB 有记录 |
| **11** | Pipeline query_v2.yaml | 新增 Ranker 节点 + LiteLLMGenerator | 跑一条 query → Pipeline trace 包含 Ranker span |
| **12** | RAGAS 评测体系 | 构造 ≥50 条评测三元组 + 跑 eval_ragas.py | `python scripts/eval_ragas.py` 输出 recall ≥ 0.70 |

### 调试策略

- **步骤 0-3**（P-MODEL 升级）：每个接口可独立脚本测试
- **步骤 4-6**（配置+审计）：查 DB 验证写入正确性
- **步骤 7-8**（P-AUTHC）：对 Cerbos 发 HTTP 请求验证
- **步骤 9**（多轮改写）：单轮+多轮对话对比 resolved_query 是否不同
- **步骤 11**（Pipeline v2）：Pipeline 级测试（有 ranker 的检索结果排序应不同）
- **步骤 12**（RAGAS）：最后一步，依赖所有前面的模块就绪

---

## 4. 特别注意的约束

### 4.1 最高优先级：不修改阶段一的接口

> "后续阶段只加新东西，不修改前面阶段的接口、表结构和事件契约"。阶段二的每项升级都必须在现有接口签名之内：

| 升级 | 不修改的接口 | 新文件 |
|------|-----------|--------|
| invoke_rerank | 函数签名不变（`invoke_rerank(query, documents) → list[str]`） | 无 |
| filter_items | 函数签名不变（`filter_items(scope, items) → list[tuple]`） | 无 |
| emit_audit_event | 函数签名不变 | 无 |
| resolved_query | 表字段已有（`resolved_query`），阶段一已填值 | 无 |
| check_batch | 函数签名不变 | 无 |
| Query Pipeline | 不修改 `query_v1.yaml` | 新增 `query_v2.yaml` |

### 4.2 Pipeline YAML 版本化

> "Pipeline 变更视为算法配置变更，须附评测结果对比基线方可合并"

- `query_v2.yaml` 与 `query_v1.yaml` 并存
- 通过 `haystack_pipeline_name` 配置字段选择使用哪个版本
- Embedding 模型同时出现在摄入和查询两个 Pipeline 中，变更必须同步（阶段二恰好在查询侧换 Generator，不影响 Embedding）
- 切换 v2 前必须先跑 RAGAS 基线对比

### 4.3 strict=true 的语义边界

> "`strict` 买的是权限变更即时性，不是一切变更即时"

- `strict=true` 只保证层 3（`/v1/filter`）撤权后 10s 内生效
- `is_enabled=false` 不在 strict 保证范围内（有事件传播窗口）
- 紧急撤权正确路径是 `add_restriction`（管理台）或 `unlink`（本系统）

### 4.4 BGE Reranker v2 的模型选择

文档指定的 Reranker 模型为 `BAAI/bge-reranker-v2-m3`。需要确认：
- 本地 Ollama 没有 reranker 模型，需要 `FlagEmbedding` 库直接加载（`BGEM3FlagModel` 已安装）
- Reranker 返回的是相关性分数，输入 query + documents 列表，输出排序后的 documents
- 集成到 Haystack 的方式：封装为自定义 `@component`，或者用 `SentenceTransformersRanker`

### 4.5 Langfuse 接入方式

文档指定 "经 LiteLLM callback" 落地。关键点：
- `LiteLLMGenerator` 的使用需要 Langfuse callback 配置（`LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY` 等环境变量）
- Langfuse trace 只覆盖模型调用（LLM/Embedding/Rerank），不与通用 Log/Trace/审计混淆
- 本地开发可以跑 Langfuse 容器或使用同网段已有的 Langfuse 服务，或先做代码准备等容器就绪再测

### 4.6 评测体系的前置依赖

RAGAS 评测的正确性依赖：
- 评测集必须标注构造时主体的身份和 kb_id
- 权限导致的空结果必须在评测报告中单独归类（不计入召回率分母）
- 否则开了 strict 的 KB 会显得"质量很差"，进而错误调参

### 4.7 多轮对话改写的实现注意

- 取 conversation 最近几轮（如最近 3 轮）的历史，作为 LLM 改写的 context
- LLM 输入的 prompt 格式：历史对话 + 当前问题 → 改写为独立可检索的完整查询
- 改写后的结果写入 `conversation_turn.resolved_query` 字段（阶段一是直接等于 `user_question`）

### 4.8 层 3 filter_items 的单批上限

- 输入 items 按 `(document_id, kb_id)` 去重
- 单批 ≤200 条（与 `/v1/check/batch` 对齐）
- 超出分批串行调用
- 传输失败/超时 → 整批 deny（fail-closed）
- **永久禁止缓存**（这是安全纪律，不是性能优化）

---

## 5. 完成标准（5 条验收 checklist）

```
[ ] Context Recall ≥ 0.70（RAGAS，首批评测集，每 KB ≥ 50 条三元组）
[ ] Faithfulness ≥ 0.75
[ ] strict=true 的 KB 撤权后 10s 内生效（层 3 实时复核）
[ ] 多轮追问"那第二条呢"，resolved_query 改写后能检索到正确结果
[ ] Langfuse 可以看到每次查询的完整链路（含 chunk 命中、模型调用）
```

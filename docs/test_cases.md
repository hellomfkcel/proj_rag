# RAG v14 生产级测试案例

> 基准文档：`docs/RAG系统设计v14.md`（1973 行完整设计规格）
> 测试环境：conda rag_dev_v14 + docker-compose -f docker-compose.infra.yml
> 测试文档源：`test_docs/主流agent开发框架.md`
> 总用例数：61 条，覆盖 10 个类别

---

## 类别 1：摄入 Pipeline — 上传→分块→Embedding→写入（8 条）

### TC-1.1 通过 Python import 调用上传文档
- **覆盖**：§13.3 B-DOC submit_ingest_task
- **前置**：test_docs/主流agent开发框架.md 存在
- **步骤**：
  1. `from src.doc.service import submit_ingest_task`
  2. 读取 test_docs/主流agent开发框架.md 内容
  3. 调用 `submit_ingest_task(user_id="tester", tenant_id="tenant-dev", kb_id="a0000000-0000-0000-0000-000000000001", filename="agent-framework.md", file_content=content, auto_parse=True)`
- **预期**：
  - 返回 `document_id`（非空 UUID）
  - 返回 `mount_id`（非空 UUID）
  - `parse_status = "queued"`
  - `duplicate = False`（首次上传）
  - PostgreSQL `documents` 表新增 1 行
  - PostgreSQL `document_kb_mounts` 表新增 1 行
  - PostgreSQL `outbox` 表新增 1 行 DocumentMounted 事件
  - PostgreSQL `resource_registry` 表有该文档的 register 记录
  - PostgreSQL `mount_registry` 表有该挂载的 link 记录

### TC-1.2 重复上传同一文档（幂等去重）
- **覆盖**：§13.3.1 两层去重（物理指纹 + 挂载关系）
- **前置**：TC-1.1 已执行
- **步骤**：用相同文件内容再次调用 `submit_ingest_task`
- **预期**：
  - `document_id` 与 TC-1.1 相同（复用已有 document）
  - `duplicate = True`
  - PostgreSQL `documents` 表行数不变

### TC-1.3 通过 FastAPI HTTP 上传文档
- **覆盖**：§13 B-DOC REST 端点
- **前置**：`make dev-api` 启动 FastAPI
- **步骤**：
  ```bash
  curl -X POST http://localhost:8000/api/v1/documents/upload \
    -F "file=@test_docs/主流agent开发框架.md" \
    -F "kb_id=a0000000-0000-0000-0000-000000000001"
  ```
- **预期**：HTTP 200，JSON 含 document_id、mount_id、parse_status、duplicate

### TC-1.4 Haystack DocumentSplitter 真实分块
- **覆盖**：§14.4 切分委托契约
- **前置**：TC-1.1 已上传文档
- **步骤**：
  1. 用代码构建 Pipeline：`DocumentSplitter(split_by="word", split_length=256, split_overlap=32)`
  2. 传入 TC-1.1 上传的文档内容
  3. 执行 `pipeline.run()`
- **预期**：
  - 产生 ≥3 个 chunk
  - 每个 chunk 长度 ≤ (256 + 32) 字符（考虑 overlap）
  - chunk[0] 内容与原文档开头匹配

### TC-1.5 OllamaDocumentEmbedder 真实 Embedding
- **覆盖**：§11.1 invoke_embedding → Haystack Embedder
- **前置**：TC-1.4 已分块
- **步骤**：
  1. 构建 Pipeline：`splitter → OllamaDocumentEmbedder(model="qwen3-embedding:0.6b")`
  2. 执行 Pipeline
- **预期**：
  - 每个 chunk 有 `embedding` 属性
  - `len(chunk.embedding) == 1024`
  - 向量非零（`sum(abs(v) for v in chunk.embedding) > 0.01`）
  - 带 span `invoke_embedding` 上报到 Tempo（service=rag-v14-prod）
  - 带 trace 上报到 Langfuse（name 含 "embedding-qwen3-embedding"）

### TC-1.6 PermissionMetadataEnricher 权限元数据注入
- **覆盖**：§14.3 PermissionMetadataEnricher 防腐约束
- **前置**：TC-1.5 已 Embedding
- **步骤**：
  1. Pipeline 中添加 `PermissionMetadataEnricher`
  2. 执行完整 3 组件 Pipeline：`splitter → embedder → enricher`
- **预期**：
  - 全部 chunk 的 `meta["vis_version"] == 0`（空戳记）
  - 全部 chunk 的 `meta["allow_stamps"] == []`
  - 全部 chunk 的 `meta["deny_stamps"] == []`
  - 全部 chunk 的 `meta["retrievable"] == True`

### TC-1.7 完整摄入 Pipeline → Milvus 写入
- **覆盖**：§14.2-14.3 摄入全流程
- **前置**：TC-1.6 Pipeline 执行完毕
- **步骤**：
  1. 从 Pipeline 输出取 chunk + embedding
  2. 写入 Milvus `rag_documents` collection（vis_version=1）
  3. 读回验证
- **预期**：
  - Milvus 行数增加 = chunk 数量
  - 读回的 chunk 的 `vis_version == 1`
  - 读回的 chunk 的 `retrievable == True`
  - 读回的 chunk 的 `kb_id` 正确

### TC-1.8 盖戳管道 stamp_channel_task 执行
- **覆盖**：§14.5 六条纪律
- **前置**：TC-1.7 已写入 Milvus
- **步骤**：
  1. 调用 `stamp_channel_task(tenant_id, doc_id, kb_id)`
  2. 验证 Milvus chunk payload 更新
- **预期**：
  - 纪律 1：失败不落盘（/v1/visibility 不可达时 chunk 保持旧值）
  - 纪律 3：版本单调性（response.version < current → 丢弃）
  - Milvus 中 `vis_version` 有值（>0）
  - `allow_stamps` 有内容

---

## 类别 2：查询 Pipeline — 检索→Rerank→LLM 生成（8 条）

### TC-2.1 compile_filter 六条件编译
- **覆盖**：§6A.3 层 1 过滤器 + §15.1 compile_filter
- **步骤**：
  1. `ctx = build_context("test", enforce_jwt=False)`
  2. `pf = get_prefilter(ctx)`
  3. `flt = compile_filter(pf, ctx, KB_ID)`
  4. `parse_filters(flt)` 得到 Milvus expr
- **预期**：
  - expr 包含 `tenant_id == "tenant-dev"`
  - expr 包含 `kb_id == "a0000000-0000-0000-0000-000000000001"`
  - expr 包含 `retrievable == True`
  - expr 包含 `vis_version > 0`

### TC-2.2 稠密向量检索命中
- **覆盖**：§15.7 混合检索（稠密路）
- **前置**：TC-1.7 Milvus 有数据
- **步骤**：
  1. 用 `invoke_embedding(["LangGraph 的核心特点是什么？"])` 生成查询向量
  2. `col.search()` 带 TC-2.1 的 filter expr
- **预期**：
  - 命中数 ≥ 1
  - 命中 chunk 的 `document_id` 与 TC-1.1 上传的文档匹配
  - 命中 chunk 的 `kb_id` 正确

### TC-2.3 Rerank 精排生效
- **覆盖**：§15.8 rerank
- **前置**：TC-2.2 有检索结果
- **步骤**：
  1. 取检索结果的前 5 条
  2. 调用 `invoke_rerank(query, docs)`
- **预期**：
  - 返回列表长度不变
  - 相关文档排在前面（"LangGraph" 相关 chunk 在第一位）
  - 带 span `invoke_rerank` 上报到 Tempo

### TC-2.4 DeepSeek LLM 真实生成
- **覆盖**：§16.3 生成合成
- **前置**：TC-2.3 Rerank 完成
- **步骤**：
  1. 用 Rerank 后的 top-3 文档构造 prompt
  2. 调用 `invoke_llm(prompt, model_id="deepseek-chat")`
- **预期**：
  - 返回长度 > 10 字符
  - 答案内容与 chunk 强相关（不是胡编）
  - 带 span `invoke_llm` 上报到 Tempo
  - 带 trace 上报到 Langfuse（name="llm-deepseek-chat"）

### TC-2.5 检索结果为空时的拒答
- **覆盖**：§15.6 存在性三通道纪律
- **前置**：Milvus 中有数据
- **步骤**：
  1. 用明显不会命中的 query 检索（如 "zzz不存在的查询xyz"）
- **预期**：
  - 返回 "未找到足够信息"（不是 "您没有权限"）
  - 返回码为 200（不是 403 或 404）
  - 不泄露被过滤的 chunk 数量和内容

### TC-2.6 Pipeline YAML 版本化切换
- **覆盖**：§12.1 haystack_pipeline_name 字段
- **步骤**：
  1. 确认 `query_v1.yaml` 和 `query_v2.yaml` 同时存在
  2. 分别用两个文件构建 Pipeline
- **预期**：
  - v1 和 v2 不是同一个文件（内容不同）
  - 两个都能成功 `Pipeline.loads()`

### TC-2.7 retrieve() 函数补检索（L2）
- **覆盖**：§15.3 过采样 + 补检索
- **前置**：Milvus 中有数据
- **步骤**：
  1. 调用 `retrieve(query, kb_ids=[KB_ID], tenant_id="tenant-dev", top_k=2, min_results=5)`
  2. 观察补检索行为
- **预期**：
  - 返回 `documents` 列表
  - 如果初始检索不足 min_results，触发补检索（refetch）
  - 补检索各轮使用的 filter 完全相同

### TC-2.8 SSE 流式回传
- **覆盖**：§9.3 流式回传 + §16 B-CHAT
- **前置**：FastAPI 启动 + Redis 运行
- **步骤**：
  1. 通过 API 提交查询
  2. 监听 Redis Pub/Sub `query-stream:{task_id}` 频道
- **预期**：
  - 收到 `retrieved` 事件（含 chunk_ids）
  - 收到 `token` 事件（含生成内容流式传输）
  - 收到 `done` 事件

---

## 类别 3：参数配置管理（5 条）

### TC-3.1 resolve_retrieval_config 四层级联
- **覆盖**：§12.1 P-CONFIG 四层级联（turn→conversation→kb→tenant）
- **步骤**：
  1. 在 `retrieval_configs` 表插入 4 层配置（不同 `scope_type`）
  2. 调用 `resolve_retrieval_config(kb_id="k1", tenant_id="t1", conversation_id="c1", turn_id="t2")`
- **预期**：
  - turn 层覆盖 conversation 层
  - conversation 层覆盖 kb 层
  - kb 层覆盖 tenant 层
  - 返回的是就近覆盖后的合并值

### TC-3.2 resolve_chunking_config 按版本读取
- **覆盖**：§12.2 切分配置版本化
- **步骤**：
  1. 在 `chunking_configs` 表插入多个版本的配置
  2. 调用 `resolve_chunking_config(kb_id, version="v2")`
- **预期**：
  - 返回的 `haystack_strategy` 与指定版本匹配
  - 不传 version 时返回最新版本
  - 支持 word / sentence / passage / semantic / hierarchical 五种策略

### TC-3.3 feature_flag 特性开关
- **覆盖**：§12.3 feature_flag
- **步骤**：
  1. `feature_flag("authz.decision_cache_enabled")`
  2. `feature_flag("authz.strict_default")`
- **预期**：
  - 两者均返回 `False`（默认值）
  - `authz.decision_cache_enabled = False`（缓存默认关闭）

### TC-3.4 .env → Settings → DB 三级配置优先级
- **覆盖**：§11 P-MODEL + config.py
- **步骤**：
  1. `.env` 中设置 `LLM_MODEL=qwen2.5-coder:14b`
  2. DB `model_registry` 中 `is_default=true` 的行设为 `model_name=deepseek-v4-flash`
  3. 调用 `resolve_model("qwen3-8b")`
- **预期**：
  - DB 覆盖 .env（DB 记录的 `base_url` 和 `model_name` 优先生效）
  - DB 无记录时 fallback 到 .env

### TC-3.5 .env 切换 LLM provider 零代码改动
- **覆盖**：§11 P-MODEL + config.py
- **步骤**：
  1. 当前 `.env`: `LLM_BASE_URL=https://api.deepseek.com/v1`
  2. 改为 `.env`: `LLM_BASE_URL=http://localhost:11434`
  3. 重新 `from src.config import Settings; s = Settings()`
- **预期**：
  - `s.llm_base_url` 自动变化
  - `invoke_llm()` 自动走新 provider
  - 不需修改任何 `src/` 下的 Python 代码

---

## 类别 4：模型管理（5 条）

### TC-4.1 resolve_model 从 DB 读取
- **覆盖**：§11 P-MODEL
- **步骤**：
  1. `cfg = resolve_model("qwen3-embed")`
  2. `cfg2 = resolve_model("deepseek-chat")`
- **预期**：
  - `cfg.model_type == "embedding"`
  - `cfg.model_name == "qwen3-embedding:0.6b"`
  - `cfg2.model_type == "llm"`
  - `cfg2.model_name == "deepseek-v4-flash"`

### TC-4.2 resolve_prompt 从 DB 按版本读取
- **覆盖**：§11 P-MODEL Prompt 版本池
- **步骤**：
  1. `v1 = resolve_prompt("default", "v1")`
  2. `v2 = resolve_prompt("default", "v2")`
- **预期**：
  - `v1 != v2`
  - `v2` 包含 "无法回答"（防幻觉约束）
  - `v1` 包含 "企业知识库助手"

### TC-4.3 Embedding 模型 provider 自动判定
- **覆盖**：§11 P-MODEL invoke_embedding
- **步骤**：
  1. 当前 `.env EMBEDDING_BASE_URL=http://localhost:11434` → 判定为 Ollama
  2. 模拟改为 `EMBEDDING_BASE_URL=https://api.deepseek.com/v1` → 判定为 OpenAI 兼容
- **预期**：
  - Ollama 模式：调 `/api/embed`
  - OpenAI 兼容模式：调 `/v1/embeddings` + Authorization header

### TC-4.4 LLM 模型 provider 自动判定
- **覆盖**：§11 P-MODEL invoke_llm
- **步骤**：
  1. DB 默认 LLM 为 deepseek-chat（provider=deepseek, base_url=https://api.deepseek.com/v1）
  2. 调用 `invoke_llm("1+1=?", model_id="deepseek-chat")`
- **预期**：
  - OpenAI SDK 自动适配 DeepSeek API
  - 返回正确答案
  - Langfuse 有 trace

### TC-4.5 Ollama 本地 + DeepSeek 远端混合模式
- **覆盖**：§11 P-MODEL + config.py 混合 provider
- **步骤**：
  1. `EMBEDDING_BASE_URL=http://localhost:11434`（本地 Ollama）
  2. `LLM_BASE_URL=https://api.deepseek.com/v1`（远端 DeepSeek）
  3. 调用 `invoke_embedding` + `invoke_llm`
- **预期**：
  - Embedding 走本地 Ollama
  - LLM 走远端 DeepSeek
  - 两个调用都成功

---

## 类别 5：权限判定 — Cerbos + compile_filter + 三层检索（10 条）

### TC-5.1 kb_reader 读活跃 KB → ALLOW
- **覆盖**：§6A Cerbos 策略
- **步骤**：POST Cerbos `/api/check/resources`，principal 有 `granted_actions: {KB_A: ["read"]}`，资源为 KB_A 且 `retired: false`
- **预期**：`EFFECT_ALLOW`

### TC-5.2 kb_reader 读已退休 KB → DENY
- **覆盖**：§13.4.1 retired 拒绝
- **步骤**：同上但 `retired: true`
- **预期**：`EFFECT_DENY`

### TC-5.3 kb_reader 尝试写 KB → DENY
- **覆盖**：§6A Cerbos 派生角色
- **步骤**：kb_reader 用户调 `kb:write`
- **预期**：`EFFECT_DENY`

### TC-5.4 kb_reader 跨 KB-B 读 → DENY（跨 KB 隔离）
- **覆盖**：§15.2 候选通道隔离
- **步骤**：只有 KB_A read 权限的用户读 KB_B
- **预期**：`EFFECT_DENY`（不是 `EFFECT_ALLOW`）

### TC-5.5 kb_writer 写活跃 KB → ALLOW
- **覆盖**：§6A 派生角色 kb_writer
- **步骤**：granted_actions 中有 "write"，KB 状态非 reindexing
- **预期**：`EFFECT_ALLOW`

### TC-5.6 kb_writer 写 reindexing KB → DENY
- **覆盖**：KB 维护中拒绝写入
- **步骤**：KB 状态 `status = "reindexing"`
- **预期**：`EFFECT_DENY`

### TC-5.7 system_admin override 全局 ALLOW
- **覆盖**：§6A admin 派生角色
- **步骤**：roles 为 `["system_admin"]` 的用户调 `doc:purge`、`kb:manage`
- **预期**：全部 `EFFECT_ALLOW`

### TC-5.8 doc:view enabled → ALLOW, disabled → DENY
- **覆盖**：§13.4.1 document 策略
- **步骤**：分别传 `is_enabled=true` 和 `false`
- **预期**：enabled → ALLOW；disabled → DENY

### TC-5.9 doc:download allow_download=false → DENY
- **覆盖**：§13.4.5 view/download 分权
- **步骤**：传 `allow_download=false`
- **预期**：`EFFECT_DENY`

### TC-5.10 compile_filter → Milvus 检索层隔离
- **覆盖**：§15.1 层 1 + 事后过滤禁令
- **步骤**：
  1. 用 KB_A 的 filter 检索 → 全部命中 KB_A 的 chunk
  2. 用 KB_B 的 filter 检索 → 0 条命中
  3. 无 filter 检索 → 返回全量（证明 filter 必要性）
- **预期**：
  - KB_A filter: 全部命中 `kb_id == KB_A`
  - KB_B filter: 0 命中（KB_B 无数据）
  - 无 filter: 返回跨 KB 数据（证明事后过滤危险）

---

## 类别 6：文件管理（5 条）

### TC-6.1 从 KB 移除文档（purge=false）
- **覆盖**：§13.4.3 删除语义
- **前置**：已有挂载的文档
- **步骤**：`delete_document_from_kb(user_id="tester", doc_id=DOC_ID, kb_id=KB_ID, tenant_id="tenant-dev", purge=false)`
- **预期**：
  - 返回 `status: "deleted"`
  - `document_kb_mounts` 表该行被删除
  - `outbox` 表新增 DocumentUnmounted 事件
  - `mount_registry` 中 `unlinked = true`

### TC-6.2 彻底删除文档（purge=true）
- **覆盖**：§13.4.3 purge=true 四合一 retire
- **前置**：文档只挂载了 1 个 KB
- **步骤**：`delete_document_from_kb(..., purge=true)`
- **预期**：
  - 删各挂载 → 每个发 DocumentUnmounted
  - 计数归零 → 调 retire_resource
  - `resource_registry` 中 `retired = true`
  - `documents` 表该行被删除
  - `audit_logs` 表有 DOC_DELETE + AUTHZ_WRITE 记录

### TC-6.3 文档在多个 KB 中挂载时 purge 只清当前 KB
- **覆盖**：§13.1 文档与 KB 是多对多
- **前置**：同一文档挂载了 KB_A 和 KB_B
- **步骤**：`delete_document_from_kb(doc_id, kb_id=KB_A, purge=false)`
- **预期**：
  - KB_A 的挂载被删除
  - KB_B 的挂载不受影响
  - 文档本身未被删除

### TC-6.4 文档内容指纹去重
- **覆盖**：§13.3.1 物理去重
- **前置**：TC-1.1 已上传文档
- **步骤**：再次上传同名文件到同一 KB
- **预期**：
  - 复用已有 document_id
  - `duplicate = True`
  - Milvus 不重复写入 chunk

### TC-6.5 view/download 分权验证
- **覆盖**：§13.4.5 view≠download
- **步骤**：
  1. 在同一 KB 中构造只有 read_only 权限的用户
  2. 调 `doc:view` → ALLOW
  3. 调 `doc:download` → DENY
- **预期**：
  - view=ALLOW, download=DENY
  - view 路径不走签名 URL（服务端渲染）

---

## 类别 7：KB 管理（5 条）

### TC-7.1 创建 KB
- **覆盖**：§13 B-DOC 知识库生命周期
- **步骤**：`INSERT INTO knowledge_bases (id, tenant_id, name, owner_id) VALUES (gen_random_uuid(), 'tenant-dev', '测试KB-2', 'tester')`
- **预期**：
  - `resource_registry` 表有该 KB 的 register 记录
  - `retrieval_configs` 表可插入该 KB 的配置
  - `chunking_configs` 表可插入该 KB 的版本化配置

### TC-7.2 KB 内文档列表查询
- **覆盖**：§13.4 KB 级文件管理
- **前置**：KB_A 下已有 ≥2 个文档
- **步骤**：`SELECT * FROM document_kb_mounts WHERE kb_id = KB_ID`
- **预期**：返回该 KB 下全部挂载的文档

### TC-7.3 KB 状态 → reindexing 后拒绝摄入
- **覆盖**：§13.3.2 trigger_parse 前置条件
- **前置**：KB_A 状态正常
- **步骤**：
  1. 设置 KB_A 的 `status = "reindexing"`
  2. 尝试 `trigger_parse(mount_id)`
- **预期**：返回 `doc:kb_reindexing` 409

### TC-7.4 KB 删除 → 级联清理
- **覆盖**：§23 KB 生命周期
- **前置**：KB 下无活跃文档
- **步骤**：
  1. 解除 KB 下所有文档挂载
  2. 调 `retire_resource("kb", kb_id)`
  3. `DELETE FROM knowledge_bases WHERE id = kb_id`
- **预期**：
  - `resource_registry` 中 `retired = true`
  - `knowledge_bases` 表已删除
  - 关联的 `chunking_configs` / `retrieval_configs` 可清理

### TC-7.5 不同 KB 使用不同切分配置
- **覆盖**：§13.1 + §12.2 切分配置按 KB 粒度
- **步骤**：
  1. KB_A 设 `haystack_strategy = "word", split_length = 256`
  2. KB_B 设 `haystack_strategy = "sentence", split_length = 512`
  3. 分别 resolve_chunking_config
- **预期**：
  - KB_A 返回 word/256
  - KB_B 返回 sentence/512
  - 配置互不影响

---

## 类别 8：盖戳 + 对账（4 条）

### TC-8.1 盖戳管道纪律 1：失败不落盘
- **覆盖**：§14.5.3 纪律 1
- **步骤**：
  1. 停掉权限服务 → 模拟 /v1/visibility 不可达
  2. 调用 `stamp_channel_task`
- **预期**：
  - chunk 的 `vis_version` 保持旧值
  - 不写入空戳记（`allow_stamps=[]`）
  - 不 ack 事件，触发 Celery 重试

### TC-8.2 盖戳管道纪律 2：unmounted 清空
- **覆盖**：§14.5.3 纪律 2
- **步骤**：
  1. 在 `mount_registry` 中设 `unlinked=true`
  2. 调用 `stamp_channel_task`
- **预期**：
  - `get_visibility` 返回 `unmounted=true`
  - chunk 戳记被清空（`allow_stamps=[], deny_stamps=[], vis_version=0`）

### TC-8.3 结构镜像对账
- **覆盖**：§13.7b 镜像对账
- **步骤**：
  1. 手动从 `document_kb_mounts` 删一条记录
  2. 运行 `reconcile_mount_mirror()`
- **预期**：
  - 检测到缺口（`mirror_gap > 0`）
  - 自动补调 `link_resource` 修复
  - `mirror_gap` 指标有记录

### TC-8.4 戳记对账
- **覆盖**：§14.5c 戳记对账
- **步骤**：
  1. 在 Milvus 写入一条 `vis_version=0` 的 chunk
  2. 运行 `reconcile_stamps()`
- **预期**：
  - 检测到孤儿戳记（`orphan_stamp > 0`）
  - 自动补提交 `stamp_channel_task`
  - `orphan_stamp` 指标有记录

---

## 类别 9：审计 + 可观测 + 熔断（6 条）

### TC-9.1 emit_audit_event 落 audit_logs 表
- **覆盖**：§7 P-AUDIT
- **步骤**：
  1. 调用 `emit_audit_event("TEST_EVENT", user_id="u1", tenant_id="t1", ...)`
  2. 查询 `audit_logs` 表
- **预期**：
  - 表中新增 1 行
  - event_type、user_id、tenant_id 字段正确
  - structlog 双写（JSON 日志可见）

### TC-9.2 emit_audit_event_txn 同事务写入
- **覆盖**：§7.2 高风险同步写入
- **步骤**：
  1. 在业务事务中调用 `emit_audit_event_txn(conn_for_txn=conn)`
  2. 业务事务回滚
  3. 查询 `audit_logs` 表
- **预期**：
  - 业务回滚时审计记录也不存在（同事务保证）
  - 写失败则业务回滚（fail-closed）

### TC-9.3 OTel trace 上报到 Tempo
- **覆盖**：§8 P-OBS + tracing.py
- **步骤**：
  1. 调用 `invoke_embedding` / `invoke_llm` / `invoke_rerank`
  2. `force_flush()` OTel spans
  3. 查询 Tempo API（`http://172.31.0.2:3200/api/search`）
- **预期**：
  - Tempo 有 `invoke_embedding` span（含 model/batch_size/dim 属性）
  - Tempo 有 `invoke_llm` span（含 model/elapsed_ms/answer_len 属性）
  - Tempo 有 `invoke_rerank` span（含 doc_count/elapsed_ms 属性）
  - service.name = "rag-v14-prod"

### TC-9.4 Langfuse trace 上报
- **覆盖**：§11.3 Langfuse 集成
- **步骤**：
  1. 调用 `invoke_llm` 产生一次 DeepSeek 生成
  2. 等待 3 秒
  3. 查询 Langfuse API 或 Web UI
- **预期**：
  - Langfuse 中有 `llm-deepseek-chat` trace
  - Langfuse 中有 `embedding-qwen3-embedding:0.6b` trace
  - trace 包含 input/output/model/metadata

### TC-9.5 熔断器打开后拒答
- **覆盖**：§25.3 熔断降级
- **步骤**：
  1. 创建指向不可达地址的 CerbosClient
  2. 连续调用 `check()` 10 次（全部失败）
  3. 第 11 次调用
- **预期**：
  - circuitbreaker 状态为 OPEN
  - 后续调用直接返回 `auth:authz_unavailable`（不发起 HTTP 请求）
  - 60s 后半开探测

### TC-9.6 P-OBS Metrics 指标采集
- **覆盖**：§8.3 Metric 关键指标
- **步骤**：
  1. 调 `record_authz_decision("check", "allow")`
  2. 调 `set_mirror_gap(5)`
  3. 调 `set_orphan_stamp(3)`
- **预期**：
  - 6 个 metrics 函数均无异常
  - structlog 输出警告（mirror_gap/orphan_stamp 非零）

---

## 类别 10：全局规则 + 生成层守卫（5 条）

### TC-10.1 依赖方向验证：业务模块不 import Haystack 模型类
- **覆盖**：§0.2.1 依赖方向 + §0.2.3 红线 4
- **步骤**：grep 4 个业务模块的 service.py
- **预期**：
  - ingest/service.py 中 0 处 `Fastembed / SentenceTransformer / openai.OpenAI / LiteLLM`
  - retrieve/service.py 同
  - chat/service.py 中模型调用只走 `from src.platform.model.registry import invoke_llm`
  - doc/service.py 同

### TC-10.2 事后过滤禁令：代码中无 application-layer filter
- **覆盖**：§15.1.1 事后过滤禁令
- **步骤**：grep `post_filter / after_filter / 先查全量` 全项目
- **预期**：0 命中

### TC-10.3 存在性三通道纪律：deny 文案与 "未找到足够信息" 相同
- **覆盖**：§15.6 三通道纪律
- **步骤**：
  1. grep "您没有权限 / 权限不足 / 无权访问" 全项目
  2. 检索 deny 时的 API 返回
- **预期**：
  - 0 处泄露 deny 的文案
  - deny 返回 200 `retrieve:insufficient_evidence`（不是 403）
  - 不告知用户被过滤的条数

### TC-10.4 复述守卫 check_verbatim_ratio 生效
- **覆盖**：§16.4 生成层复述守卫
- **步骤**：
  1. 构造 chunk（62 字符）
  2. 构造 LLM 回答 = chunk 内容重复 8 次
  3. 调用 `check_verbatim_ratio(answer, [doc], threshold=0.5)`
- **预期**：
  - 检测到超 60% 复述
  - 答案被追加 "[原文引用 #N]" 标记

### TC-10.5 多轮对话查询改写
- **覆盖**：§16 B-CHAT resolved_query 改写
- **步骤**：
  1. 插入对话历史（前 2 轮）
  2. 当前轮次用户问 "那第二条呢"
  3. 调用 `_rewrite_query(conv_id, "那第二条呢", turn_index=3)`
- **预期**：
  - resolved_query 被改写为完整查询（不再是 "那第二条呢"）
  - 改写后的查询能在 Milvus 中检索到正确结果

---

## 附录 A：测试环境速查

| 服务 | 地址 | 验证命令 |
|------|------|---------|
| PostgreSQL | `localhost:25432` | `PGPASSWORD=... psql -h localhost -p 25432 -U rag -d rag` |
| Redis | `localhost:16379` | `redis-cli -h localhost -p 16379 -a ... ping` |
| Milvus | `localhost:19530` | `curl http://localhost:9091/healthz` |
| SeaweedFS | `localhost:18333` | `curl http://localhost:18333/` |
| Cerbos | `localhost:13592` | `curl http://localhost:13592/_cerbos/health` |
| Ollama | `localhost:11434` | `curl http://localhost:11434/api/tags` |
| OTel Collector | `localhost:4318` | `curl http://localhost:4318/` |
| Grafana | `localhost:3000` | 浏览器访问 |
| Tempo | `172.31.0.2:3200` | `curl http://172.31.0.2:3200/ready` |
| Langfuse | `localhost:13000` | 浏览器访问 |
| FastAPI | `localhost:8000` | `make dev-api` |

## 附录 B：常用 KB ID 速查

| ID | 名称 | 用途 |
|----|------|------|
| `a0000000-0000-0000-0000-000000000001` | 开发测试KB | 主测试 KB |
| `b0000000-0000-0000-0000-000000000002` | 受限KB | 跨 KB 隔离测试 |

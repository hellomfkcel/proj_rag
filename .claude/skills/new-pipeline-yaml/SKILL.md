# new-pipeline-yaml

创建或变更 Haystack Pipeline YAML 文件（摄入/查询 Pipeline）时使用本 Skill。

## 执行步骤

1. 确定 Pipeline 类型和用途：
   - 摄入 Pipeline：`ingest_v[N].yaml`（DocumentSplitter → Embedder → MilvusDocumentStore）
   - 查询 Pipeline：`query_v[N].yaml`（TextEmbedder → Retriever → Ranker → PromptBuilder → Generator）
2. 在 `pipelines/` 目录创建文件，按版本号递增命名
3. YAML 结构三段式：
   ```yaml
   version: ignore
   components:
     - name: [组件名]
       type: [完整 Python 类路径]
       init_parameters:
         [参数名]: [参数值]
   
   connections:
     - sender: [发送方组件名].[输出字段]
       receiver: [接收方组件名].[输入字段]
   ```
4. 每个 component 声明三个字段：`name`（唯一标识）、`type`（Python 类路径）、`init_parameters`（初始化参数）
5. 自定义 Component 的 `type` 使用 `src.[模块].components.[类名]` 格式
6. 使用 `${ENV_VAR}` 语法引用环境变量（如 `${MILVUS_HOST}`、`${OLLAMA_MODEL}`）
7. 添加 connections：`sender.[output_field]` → `receiver.[input_field]`
8. 更新 `pipeline_yaml_version` 字段（在 `ingest_execution` 或 `conversation_turn` 表中）
9. Pipeline 变更须走 PR + Review，附评测结果对比基线
10. 修改 Pipeline YAML 后重启对应 worker（不依赖任务参数中的旧 YAML）

## 必须遵守的规范

- `version: ignore` 行必须存在（Haystack 框架要求）
- Pipeline YAML 纳入 Git 版本控制，变更须走 PR + Review
- Pipeline 变更视为"算法配置变更"，须附评测结果对比基线方可合并
- 摄入 Pipeline 和查询 Pipeline 的 embedding 模型必须一致（换模型时两个 YAML 同步更新）
- Embedding 模型变更同时触发 Pipeline YAML 版本递增 + 维护窗口期（v14.md §22）
- Pipeline YAML 文件名按版本命名：`[类型]_v[版本号].yaml`
- `${ENV_VAR}` 语法用于引用环境变量，不得硬编码连接地址和密钥
- 任务参数中传递 `pipeline_name`（字符串）和 `yaml_version`，不传 YAML 内容本身
- Worker 侧按名称从 YAML 仓库反序列化 Pipeline 执行

## 禁止事项

- 禁止在 YAML 中硬编码 API key、密码等敏感信息
- 禁止在任务参数中传递 Pipeline YAML 内容本身——只传 name + version
- 禁止修改已有版本的 YAML（append-only，只增新版本）
- 禁止 Pipeline 变更不经评测基线对比直接合并
- 禁止将 Prompt 模板硬编码在 Pipeline YAML 中——从 `resolve_prompt` 取
- 禁止查询 Pipeline 的稠密路和稀疏路使用不同的 `MetadataFilter` 对象（必须传入同一个 filter 引用）

## 模板：摄入 Pipeline

```yaml
version: ignore
components:
  - name: splitter
    type: haystack.components.preprocessors.DocumentSplitter
    init_parameters:
      split_by: sentence
      split_length: 256
      split_overlap: 32

  - name: embedder
    type: haystack_integrations.components.embedders.fastembed.FastembedDocumentEmbedder
    init_parameters:
      model: BAAI/bge-m3

  - name: writer
    type: haystack_integrations.document_stores.milvus.MilvusDocumentStore
    init_parameters:
      connection_args:
        uri: "http://${MILVUS_HOST}:${MILVUS_PORT}"

connections:
  - sender: splitter.documents
    receiver: embedder.documents
  - sender: embedder.documents
    receiver: writer.documents
```

## 模板：查询 Pipeline

```yaml
version: ignore
components:
  - name: text_embedder
    type: haystack_integrations.components.embedders.fastembed.FastembedTextEmbedder
    init_parameters:
      model: BAAI/bge-m3

  - name: retriever
    type: haystack_integrations.components.retrievers.milvus.MilvusEmbeddingRetriever
    init_parameters:
      top_k: 20

  - name: generator
    type: haystack.components.generators.openai.OpenAIGenerator
    init_parameters:
      api_base_url: "${OLLAMA_BASE_URL}/v1"
      model: "${OLLAMA_MODEL}"

connections:
  - sender: text_embedder.embedding
    receiver: retriever.query_embedding
  - sender: retriever.documents
    receiver: generator.documents
```

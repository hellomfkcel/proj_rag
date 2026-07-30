# new-haystack-component

创建新的 Haystack 自定义 Component 或 Pipeline 中新增组件时使用本 Skill。

## 执行步骤

1. 确定 Component 归属模块和 Pipeline 位置：
   - B-INGEST：切分/嵌入/权限元数据注入
   - B-RETRIEVE：检索/融合/层级合并/rerank
   - B-CHAT：Prompt 构建/生成合成
2. 在对应模块的 `components/` 子目录创建文件
3. 使用 `@component` 装饰器声明 Component：
   ```python
   from haystack import component
   from typing import List
   from haystack import Document

   @component
   class MyComponent:
       @component.output_types(documents=List[Document])
       def run(self, documents: List[Document], **kwargs) -> dict:
           # 实现逻辑
           return {"documents": documents}
   ```
4. 声明输入输出类型：使用 `@component.output_types()` 注解所有输出
5. 如需调用模型（Embedder/Generator/Ranker），必须通过 P-MODEL 的 `invoke_*` 门面，不直接 import：
   - `invoke_embedding(model_id, texts, mode)` → Embedder
   - `invoke_llm(model_id, prompt)` → Generator
   - `invoke_rerank(model_id, query, documents)` → Ranker
   - `resolve_prompt(prompt_id, version)` → Jinja2 模板字符串
6. 如需在 Pipeline 中按 `pipeline_name` 运行，由 P-TASK 的 `run_pipeline_async` 包装为 Celery 任务
7. 添加到 Pipeline YAML 的 `components` 段时，`type` 使用完整 Python 路径
8. 如果 Component 运行在异步背景下，确定 Queue：
   - `ingestion_queue`：长耗时，可长退避重试
   - `retrieval_queue`：用户在线等待，不做长退避重试
   - `stamping_queue`：低优先级、可分批、可断点续跑

## 必须遵守的规范

- 必须使用 `@component` 装饰器声明
- `run()` 方法必须有显式的 `@component.output_types()` 类型注解
- 所有 Generator/Embedder/Ranker 实例必须经 P-MODEL 的 `invoke_*` 注入
- Prompt 模板必须由 `resolve_prompt` 解析后显式传入 `PromptBuilder`，禁用框架默认 Prompt
- 切分 Component 只在 B-INGEST 摄入 Pipeline 内 import
- Pipeline 对象在各 worker 内独立加载，不跨进程共享状态
- Pipeline YAML 变更视为"算法配置变更"，须附评测结果对比基线方可合并
- Haystack 类型（`Document`、`Pipeline`、`@component` 等）不出模块接口签名
- 框架使用原则：编排、状态机、数据模型自持；算法性组件委托 Haystack 2.x

## 禁止事项

- **禁止 `run()` 方法内出现任何权限判断逻辑**（不出现 check/filter/prefilter 调用、不出现 user_id/roles/principals 参数）
- **禁止在 Component 内部直接 import Haystack 模型类**（必须经 P-MODEL 门面）
- **禁止 API 进程内调 `pipeline.run()`**——计算只能在 Worker 进程内
- **禁止使用框架默认 Prompt**——必须由 `resolve_prompt` 显式赋值
- **禁止业务模块（B-INGEST/B-RETRIEVE/B-CHAT）直接 import Haystack 生成类**
- 禁止 semantic 切分的 Component 内直接 import 模型 SDK（必须经 P-MODEL）
- 禁止六条件过滤逻辑出现在 Component 内部——`MetadataFilter` 在 Component 外部注入

## 模板：自定义 Component

```python
from haystack import component, Document
from typing import List, Dict, Any

@component
class MyCustomComponent:
    """
    Component 职责描述（一句话）。
    
    输入：documents: List[Document]
    输出：documents: List[Document]
    """
    
    def __init__(self, param1: str = "default"):
        self.param1 = param1
    
    @component.output_types(documents=List[Document])
    def run(self, documents: List[Document]) -> Dict[str, Any]:
        # 对每个 document 做处理，不涉及任何权限逻辑
        for doc in documents:
            # 处理逻辑
            pass
        return {"documents": documents}
```

## 目录结构

```
src/[模块名]/components/
├── __init__.py
├── [组件名].py              # 每个 Component 一个文件
```

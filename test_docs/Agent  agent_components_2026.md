# Agent 开发组件学习指南（2026.5）

> 定位：面向希望系统性掌握 Agent 开发的工程师，按「深入掌握 → 理解会用 → 了解即可」三档分层，聚焦 2026 年中实际落地价值。

---

## 一、全景地图：Agent 开发组件体系

```
Agent 开发组件体系
│
├── 【核心执行层】
│   ├── LLM 推理调用（API / 本地模型）
│   ├── Prompt 工程（System Prompt / Few-shot / CoT）
│   ├── Tool Use / Function Calling
│   └── 结构化输出（JSON Schema / Instructor）
│
├── 【记忆与状态层】
│   ├── 短期记忆（对话上下文窗口）
│   ├── 长期记忆（向量数据库 / KV Store）
│   ├── 工作记忆（Scratchpad / State Graph）
│   └── 外部知识（RAG 检索增强）
│
├── 【规划与推理层】
│   ├── ReAct（Reason + Act 循环）
│   ├── Plan-and-Execute（规划-执行分离）
│   ├── Tree of Thought / Self-Reflection
│   └── Multi-Agent 协作编排
│
├── 【工具与环境层】
│   ├── MCP（Model Context Protocol）
│   ├── 代码执行沙箱（Code Interpreter）
│   ├── 浏览器/网页操作（Computer Use）
│   └── 外部 API / 数据库连接
│
├── 【流程编排层】
│   ├── LangChain / LangGraph
│   ├── AutoGen / CrewAI
│   ├── DSPy（声明式优化）
│   └── Workflow DAG 引擎
│
├── 【评估与可观测层】
│   ├── LLM Eval 框架（RAGAS / DeepEval）
│   ├── Tracing & Logging（LangSmith / Langfuse）
│   ├── Prompt 版本管理
│   └── 基准测试（GAIA / SWE-bench）
│
└── 【部署与工程层】
    ├── Streaming 响应
    ├── 异步并发（async/await）
    ├── 幂等性与重试机制
    └── 成本与 Token 管理
```

---

## 二、第一档：深入掌握（2026 年核心竞争力）

> 这些是 Agent 系统的「地基」，原理、设计取舍、边界条件都要烂熟于心。

### 1. Tool Use / Function Calling

**为什么最重要**：Agent 能力边界的核心扩展机制，几乎所有 Agent 框架底层都依赖它。

| 要掌握的维度 | 具体内容 |
|---|---|
| 协议机制 | tool schema 定义、parallel tool call、tool result 回传格式 |
| 设计原则 | 工具粒度设计（原子 vs 组合）、工具描述的 prompt 工程 |
| 边界与陷阱 | 幻觉调用、循环调用、工具链依赖管理 |
| 主流实现差异 | OpenAI / Anthropic / Gemini 的参数差异与能力差异 |

**实践目标**：能从零设计一套生产可用的工具集，并处理异常路径。

---

### 2. RAG（检索增强生成）全链路

**为什么最重要**：知识密集型 Agent 的标配，也是面试/实际项目最高频考点。

| 要掌握的维度 | 具体内容 |
|---|---|
| Indexing 阶段 | 文档切块策略（chunk size、overlap、语义切块）、Embedding 模型选型 |
| Retrieval 阶段 | 稠密检索 vs 稀疏检索（BM25）、混合检索、重排序（Reranker） |
| Generation 阶段 | 上下文压缩、引用溯源、Lost-in-the-middle 问题 |
| 进阶技术 | HyDE、多路召回、图谱增强 RAG（GraphRAG） |
| 评估体系 | Faithfulness / Relevancy / Context Recall 指标 |

**实践目标**：能独立搭建并调优一套 RAG 流水线，定位召回差/幻觉问题。

---

### 3. ReAct 执行循环与 Agent Loop 设计

**为什么最重要**：所有 Agentic 行为的核心范式，理解它才能真正设计 Agent 而不只是「调包」。

| 要掌握的维度 | 具体内容 |
|---|---|
| 核心循环 | Thought → Action → Observation 的状态转移 |
| 终止条件设计 | 最大步数、置信度判断、任务完成检测 |
| 错误恢复 | 工具调用失败后的 retry / fallback / 重规划策略 |
| 与 LangGraph 的对应 | Node / Edge / State 的映射关系 |

**实践目标**：不依赖框架手写一个可运行的 ReAct Agent。

---

### 4. MCP（Model Context Protocol）

**为什么最重要**：2024-2025 快速成为行业标准的工具互联协议，2026 年已成生态基础设施。

| 要掌握的维度 | 具体内容 |
|---|---|
| 协议设计 | Resources / Tools / Prompts 三类原语的语义区分 |
| Server 开发 | 用 FastMCP（Python）/ MCP SDK（TS）实现自定义 server |
| Client 集成 | 在 Agent 框架中接入 MCP server，权限与安全控制 |
| 生态现状 | 主流 MCP server 库（文件系统、数据库、浏览器等） |

**实践目标**：能开发并发布一个生产可用的 MCP Server。

---

### 5. Prompt 工程（System Prompt 架构）

**为什么最重要**：模型能力的「软件层」，决定 Agent 行为边界，是成本最低、收益最高的优化手段。

| 要掌握的维度 | 具体内容 |
|---|---|
| 结构化 System Prompt | 角色定义、能力边界、输出格式约束、安全护栏 |
| CoT / ReAct Prompting | 引导模型显式推理，减少跳步错误 |
| Few-shot 设计 | 示例选取策略、动态 few-shot（RAG 召回示例） |
| 长上下文管理 | 关键信息置位（首尾）、上下文压缩、Summary 记忆 |

**实践目标**：能系统性做 prompt 版本管理，通过 A/B 测试量化优化效果。

---

### 6. LangGraph（状态图编排）

**为什么最重要**：2025-2026 年 Multi-Agent 编排的事实标准，替代了早期线性 Chain 的局限。

| 要掌握的维度 | 具体内容 |
|---|---|
| 核心抽象 | StateGraph、Node、Edge、Conditional Edge |
| 状态设计 | TypedDict State 设计，跨节点信息流转 |
| 人机协作 | Human-in-the-loop、Interrupt、Checkpoint 持久化 |
| Multi-Agent | Supervisor 模式、Subgraph 嵌套、Agent 间通信 |

**实践目标**：能用 LangGraph 构建含分支、循环、人工审核节点的复杂 Agent 工作流。

---

## 三、第二档：理解设计思想，会用（重要但非核心）

> 要知道它解决什么问题、什么场景该用，能正确调用，不需要深究源码。

### 7. 向量数据库（Chroma / Weaviate / Qdrant / pgvector）

- **设计思想**：ANN 近似最近邻、HNSW 索引结构、元数据过滤
- **会用要点**：Collection 设计、Upsert 幂等、混合查询（向量+过滤）
- **选型建议**：本地开发用 Chroma，生产用 Qdrant 或 pgvector（已有 PG 的情况下）

### 8. 结构化输出（Instructor / Pydantic + LLM）

- **设计思想**：用 JSON Schema 约束 LLM 输出，消除解析歧义
- **会用要点**：定义 Pydantic 模型、处理校验失败重试、嵌套结构提取
- **核心价值**：让 Agent 输出可程序化消费，是工具调用的「数据层」

### 9. 记忆系统（MemGPT / Zep / 自建记忆）

- **设计思想**：分层记忆（工作记忆 / 情节记忆 / 语义记忆），遗忘与检索机制
- **会用要点**：何时写入长期记忆、如何关联检索、记忆冲突处理
- **注意**：多数场景用简单 KV 摘要即可，过度设计反而引入复杂度

### 10. Streaming 与异步并发

- **设计思想**：SSE / WebSocket 流式传输，async/await 并发工具调用
- **会用要点**：流式输出接入 UI、并行 tool call 的 gather 处理、背压控制
- **重要性**：生产 Agent 必备，直接影响用户体验和吞吐量

### 11. AutoGen / CrewAI（Multi-Agent 框架）

- **设计思想**：角色扮演型多 Agent，通过对话协作完成复杂任务
- **会用要点**：定义 Agent 角色与能力边界、GroupChat 协调策略
- **现实判断**：框架本身较重，理解设计模式比绑定特定框架更重要

### 12. LLM 评估框架（RAGAS / DeepEval）

- **设计思想**：用 LLM-as-Judge 自动化评估 RAG/Agent 质量
- **会用要点**：定义评估数据集、选择指标（faithfulness / answer relevancy）
- **实践意义**：没有评估就没有迭代，是 Agent 从原型走向生产的关键

### 13. Tracing & Observability（LangSmith / Langfuse）

- **设计思想**：记录 LLM 调用链路，用于调试、成本分析、质量监控
- **会用要点**：接入 trace SDK、自定义 span、设置告警阈值
- **选型**：LangSmith 与 LangChain 生态绑定深；Langfuse 开源可自托管

---

## 四、第三档：了解设计思想，知道即可

> 知道它存在、解决什么问题、什么时候需要深入，当前不需要花大量时间。

### 14. Tree of Thought / Self-Consistency

- **是什么**：通过多路径推理或多次采样投票提升推理准确率
- **为什么了解即可**：计算成本高，主流模型（Claude 3.7 / o3）内置推理能力后适用场景大幅缩窄

### 15. DSPy（声明式 Prompt 优化）

- **是什么**：用「签名 + 优化器」替代手写 prompt，自动搜索最优 prompt
- **为什么了解即可**：学习曲线陡，生产落地案例有限，2026 年仍偏研究向

### 16. Plan-and-Execute 模式

- **是什么**：先用规划器生成完整计划，再逐步执行，与 ReAct 的交织推理不同
- **为什么了解即可**：LangGraph 已将其内化为 Subgraph 模式，不需要单独实现

### 17. GraphRAG / Knowledge Graph 增强

- **是什么**：在传统 RAG 基础上引入知识图谱，增强多跳推理能力
- **为什么了解即可**：构建成本高，适合特定领域（医疗/法律/金融），通用场景过重

### 18. Agent 安全（Prompt Injection 防御）

- **是什么**：防止恶意输入劫持 Agent 行为，包括间接注入（通过工具返回内容）
- **为什么了解即可**：当前无银弹方案，了解威胁模型，设计时保持最小权限原则即可

### 19. Mixture of Agents（MoA）

- **是什么**：多个 LLM 并行生成，由聚合模型综合输出，提升质量
- **为什么了解即可**：成本是单模型数倍，适合高价值离线任务，在线场景不实用

### 20. 模型微调（Fine-tuning / LoRA）

- **是什么**：在特定任务数据上调整模型参数，提升垂域表现
- **为什么了解即可**：门槛高，主流 frontier 模型能力强，prompt 工程+RAG 优先；微调是最后手段

---

## 五、学习路径建议

```
第 1-2 个月（地基）
  └── Prompt 工程 → Tool Use → ReAct 循环手写实现

第 3-4 个月（能力扩展）
  └── RAG 全链路 → 向量数据库 → 结构化输出 → LangGraph 基础

第 5-6 个月（工程化）
  └── MCP Server 开发 → Tracing 接入 → LLM 评估体系 → Streaming

第 7-8 个月（进阶）
  └── LangGraph Multi-Agent → 记忆系统设计 → 生产部署优化
```

---

## 六、2026 年技术趋势判断

| 趋势 | 影响 |
|---|---|
| 模型上下文窗口扩大（1M+ tokens） | 简单 RAG 需求减少，但精准检索仍有价值 |
| MCP 成为工具互联标准 | 自定义工具开发从「写 function」转向「写 MCP Server」 |
| 模型内置推理（o3 / Claude 3.7）| ToT / Self-Consistency 价值下降，Agent 设计更简洁 |
| Computer Use 成熟 | GUI 自动化 Agent 进入实用阶段，浏览器操作成新必备技能 |
| Multi-Agent 编排规范化 | LangGraph / AutoGen 竞争格局基本稳定，掌握一个即可 |

---

*整理时间：2026 年 5 月 | 适用对象：有 LLM 调用基础、希望系统性进入 Agent 开发的工程师*

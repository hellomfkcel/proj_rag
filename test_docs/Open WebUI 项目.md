

## Open WebUI 项目介绍

你觉得"只是个 Web 服务"但体积高达 1GB，其实是因为它 **远不止是一个简单的前端页面**，而是一个功能极其丰富的 AI 平台。

---

### 🧠 它是什么？

Open WebUI 是一个可扩展、功能丰富、用户友好的**自托管 AI 平台**，设计为完全离线运行，支持 Ollama、OpenAI 等兼容 API，并内置 RAG（检索增强生成）推理引擎。

它的目标是为自托管大语言模型提供"最佳 AI 用户界面"，核心是一个 **Svelte + TypeScript 前端 + Python 后端**的 Web 应用。

---

### 📦 为什么镜像这么大（1GB+）？

因为容器内打包了大量依赖和功能模块，绝非只有一个 Web 服务：

1. **Python 后端（FastAPI）**：前端是 SvelteKit，后端是 Python/FastAPI，两者一起打包进镜像。

2. **内置 RAG 引擎**：支持上传文件、构建知识库，并让 AI 检索所需信息，可选向量搜索（RAG）或全文注入。 这需要打包向量数据库相关的 Python 库（如 ChromaDB、sentence-transformers 等）。

3. **语音识别（Whisper）**：内置免提语音/视频通话功能，支持本地 Whisper 作为语音转文字引擎。 本地 Whisper 模型依赖本身就很重。

4. **代码执行能力**：可以连接真实计算环境，AI 写代码、执行它、读取输出、修复错误并迭代，还能处理文件、安装包、运行服务。

5. **图片生成支持**：支持通过多种引擎创建和编辑图像，包括 OpenAI DALL-E、Gemini、ComfyUI（本地）和 AUTOMATIC1111（本地）。

6. **企业级认证**：完整支持 LDAP/Active Directory、SCIM 2.0 自动配置以及 SSO 等。

7. **多种数据库/向量库支持**：支持 SQLite、PostgreSQL，以及 ChromaDB、PGVector、Qdrant、Milvus、Elasticsearch 等多种向量数据库。

---

### 🚀 定位

Open WebUI 不只是一个本地 AI 界面，它被设计为面向**团队的集中式 AI 操作系统**，支持简化的入职流程、协作智能、资源池化和集中安全管理。



简单说，它是一个**开源版的"ChatGPT 企业平台"替代品**，把前端 UI、后端 API、RAG 引擎、语音处理、代码执行、用户权限管理等全都塞进了一个容器，所以 1GB 实际上不算大，相当合理。



---

# Open WebUI 功能完整详解

---

## 一、💬 对话核心功能

### 1. 多模型 & 多后端对话

你可以从同一个界面与 Ollama、OpenAI、Anthropic 或任何 OpenAI 兼容的提供商进行对话。还可以自定义 OpenAI API URL，连接到 LMStudio、GroqCloud、Mistral、OpenRouter 等平台。

**举例：**
- 你在本地用 Ollama 跑了一个 `llama3` 模型，同时你还有一个 OpenAI API Key
- 在 Open WebUI 里，这两个模型**同时出现在下拉列表**里
- 你可以在同一个对话中途切换，比如先用本地 llama3 写草稿，再切到 GPT-4o 润色

---

### 2. 消息编辑 & Few-Shot 学习

Open WebUI 允许你不仅编辑用户发送的消息，还可以编辑 LLM 返回的答案，这对 few-shot 学习（少样本学习）非常有用。

**举例：**
- 你让 AI 翻译一句话，结果翻译风格不对
- 你可以直接**手动修改 AI 的回复**，把正确的翻译填进去
- 这样后续的对话上下文里，AI 就会"记住"这种风格，输出更符合预期

---

### 3. 网页内容注入（`#URL` 命令）

在聊天中用 `#` 开头，后跟目标 URL，点击格式化后的 URL，就会出现文档图标，表示成功获取。Open WebUI 会抓取并解析该 URL 的内容。

**举例：**
- 你在看一篇英文技术博客，想让 AI 帮你总结
- 在输入框里输入 `#https://example.com/article`，回车
- AI 就能读取该页面内容，帮你提炼要点

---

## 二、📚 RAG 知识库（文档问答）

RAG（检索增强生成）是一种通过引入外部上下文来增强对话的技术，它从本地/远程文档、网页内容甚至 YouTube 视频等多样化来源检索相关信息，将检索到的文本与 RAG 模板结合后注入到用户的问题前面，提供更有上下文的回答。

### 支持的向量数据库

支持 9 种向量数据库选项，包括 ChromaDB、PGVector、Qdrant、Milvus、Elasticsearch、OpenSearch、Pinecone、S3Vector 和 Oracle 23ai，以获得最佳 RAG 性能。

### 知识库同步（oikb）

官方配套工具 oikb 可将知识库与本地目录、GitHub 仓库、S3 存储桶、Confluence 空间或超过 40 种其他数据源保持同步，并通过增量同步仅上传新增或变更的文件。

### 知识库文件夹管理

知识库内的文件现在可以整理成嵌套文件夹，配合面包屑导航，在大型集合中管理和查找内容更加方便。

### YouTube 视频总结

专用的 RAG 管道支持通过视频 URL 对 YouTube 视频进行总结，可以直接与视频转录内容进行交互，将视频内容融入对话。

### RAG 可信度引用

Open WebUI 的 RAG 集成了文档摄取和上下文注入，使回答有依据可查。内置引文功能让你可以追溯回答来源于哪个文档片段，解答"这是从哪来的？"。

**举例：**
- 你上传公司内部 100 份 PDF 规章制度
- 问"员工年假有几天？"
- AI 不仅给出答案，还**标注是从哪个文件第几段**得出的，可信度高

---

## 三、🔧 Tools（工具调用）

在让 LLM 拥有丰富知识库（RAG）的基础上，还可以给它配备工具（Tools）。RAG 给了 LLM 一个装满知识的"大脑"，而 Tools 则给了它与世界交互的"双手"，让它能执行函数并与外部系统互动。

支持原生 Python 函数调用，内置代码编辑器，支持"自带函数（BYOF）"，只需添加纯 Python 函数即可。还支持通过 15+ 种提供商进行网页搜索，包括 SearXNG、Google PSE、Brave Search、Kagi、Tavily 等，并可通过 `#` 命令将网站直接集成进对话。

**举例：**
- 你写一个 Python 工具函数 `get_weather(city)`，调用天气 API
- 把它注册进 Open WebUI
- 之后你问 AI "北京今天天气怎么样？"，AI 会**自动调用这个函数**，获取实时天气并回答你

---

## 四、🧩 Pipelines（管道扩展系统）

这是 Open WebUI 最强大的扩展机制之一。官方的愿景是将 Pipelines 打造成 AI 界面的终极插件框架，把 Open WebUI 想象成 AI 界面中的"WordPress"，而 Pipelines 就是其多样化的插件。

Pipelines 分为两大类型：

### Filter Pipeline（过滤管道）
Filter 管道可以在用户请求发送给 LLM 之前，以及 LLM 响应返回给用户之前进行拦截。可以实现 RAG 上下文注入、工具执行、Prompt 注入过滤、安全过滤（如使用 Meta LlamaGuard）等场景。如果你想在 LLM 调用前后做某些处理，就创建 Filter 管道。

### Pipe Pipeline（管道路由）
Pipe 管道可以整合新的 LLM 提供商、构建响应用户消息的工作流、完整的包含检索和生成的 RAG 系统。

### 官方内置 Pipeline 示例

官方示例包括：函数调用（Function Calling）、用户访问频率限制（Rate Limiting）、用 Langfuse 进行使用监控、用 LibreTranslate 实现实时多语言翻译、毒性内容过滤等。

**举例（实时翻译 Pipeline）：**
- 你的团队成员有说中文的、也有说英文的
- 配置 LibreTranslate 翻译 Filter Pipeline 后
- 中文用户发送的消息**自动翻译成英文**传给 LLM，LLM 的英文回复**自动翻译成中文**返回给用户
- 全程无感知，对话像母语一样自然

---

## 五、💻 代码执行（Open Terminal）

可以连接真实的计算环境。AI 编写代码、执行代码、读取输出、修复错误并迭代——全在聊天界面内完成。能处理文件、安装软件包、运行服务并返回结果。

配套工具 cptr（Open WebUI Computer）可以把整台电脑搬进浏览器标签页，包含文件系统、终端、git、编辑器和 AI，从任何设备均可访问。

**举例：**
- 你给 AI 上传一个 CSV 数据文件
- 说"帮我分析这个数据，画一个销售趋势图"
- AI 自动写 Python 代码 → 执行 → 发现报错 → 自动修复 → 再执行 → 最终把图表展示给你
- 全程你不需要打开任何 IDE

---

## 六、🎨 图片生成

支持使用多种引擎创建和编辑图像，包括 OpenAI 的 DALL-E、Gemini、ComfyUI（本地）和 AUTOMATIC1111（本地）。

**举例：**
- 你本地部署了 ComfyUI（Stable Diffusion）
- 在 Open WebUI 聊天里说"帮我画一只赛博朋克风格的猫"
- 图片直接在对话气泡里生成展示，无需切换任何工具

---

## 七、🎙️ 语音 & 音频

支持免提语音和视频通话功能，使用多种语音转文字提供商（本地 Whisper、OpenAI、Deepgram、Azure）和文字转语音引擎。

**举例：**
- 你部署了本地 Whisper 模型
- 直接对着麦克风说话，Open WebUI 将语音转成文字发给 LLM
- LLM 回复后，用 TTS 引擎朗读出来
- 完全在本地完成，语音数据**不离开你的服务器**

---

## 八、🏢 多用户 & 权限管理

管理员可以创建详细的用户角色和权限，确保用户环境安全，同时支持自定义用户体验，让用户有归属感和责任感。

支持完整的企业认证，包括 LDAP/Active Directory 集成、SCIM 2.0 自动配置、通过受信任 Header 的 SSO 以及 OAuth 提供商。通过 SCIM 2.0 协议实现企业级用户和组配置，支持与 Okta、Azure AD、Google Workspace 等身份提供商无缝集成，实现用户生命周期自动管理。

**举例：**
- 公司部署一套 Open WebUI 给 100 名员工用
- HR 部门只能使用 `gpt-3.5`，技术部门可以使用 `gpt-4`
- 管理员可以设置每人每天最多调用 200 次
- 通过公司的 Okta 账号直接 SSO 登录，无需单独注册

---

## 九、☁️ 云存储集成

原生支持 Google Drive 和 OneDrive/SharePoint 文件选取，实现从企业云存储无缝导入文档。

**举例：**
- 你把公司合同放在 SharePoint 里
- 在 Open WebUI 聊天框里直接选取 SharePoint 文件
- AI 即可对这份合同进行摘要、问答、条款分析

---

## 十、📊 生产级可观测性

内置 OpenTelemetry 支持，用于追踪（Traces）、指标（Metrics）和日志（Logs），可与现有的可观测性技术栈配合使用进行全面监控。

**举例：**
- 公司生产环境部署 Open WebUI
- 接入 Grafana + Prometheus 监控
- 可以看到每个模型的调用延迟、错误率、并发量等指标，方便运维排障

---

## 总结：功能地图

| 功能模块         | 能做什么         | 典型例子                      |
| ------------ | ------------ | ------------------------- |
| 多模型对话        | 同一界面管理所有 LLM | 本地 Ollama + 云端 GPT-4 同时使用 |
| RAG 知识库      | 让 AI 读你的文档   | 上传公司规章，问 AI 请假流程          |
| Tools 工具调用   | 给 AI 接外部 API | 实时查天气、查数据库                |
| Pipelines 管道 | 拦截/改造消息流     | 所有消息自动翻译、过滤敏感词            |
| 代码执行         | AI 写代码并运行    | 上传 CSV 自动生成图表             |
| 图片生成         | 聊天中生成图像      | 一句话生成海报                   |
| 语音交互         | 语音输入/输出      | 本地 Whisper 语音对话           |
| 多用户权限        | 企业级用户管理      | SSO 登录 + 按角色限制模型访问        |
| 云存储集成        | 直接引用云端文件     | 从 SharePoint 导入合同分析       |
| 可观测性         | 监控生产环境       | Grafana 看板监控延迟和错误率        |


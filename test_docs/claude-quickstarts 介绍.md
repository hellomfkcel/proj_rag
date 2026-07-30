

好的！我已经收集到了足够的信息，下面为你全面梳理 **[anthropics/claude-quickstarts](https://github.com/anthropics/claude-quickstarts)** 仓库的各个模块。

---

# 📦 anthropics/claude-quickstarts 全面解析

这是 Anthropic 官方出品的一套项目集合，旨在帮助开发者快速上手，基于 Claude API 构建可部署的应用程序。

使用这些 Quickstart 项目，你需要一个 Claude API Key。如果还没有，可以免费在 console.anthropic.com 注册。

目前仓库共包含以下 **6 个核心模块**：

---

## 模块一：🎧 customer-support-agent（客服智能体）

### 作用
这是一个由 Claude 驱动、高度可定制的客服对话界面，并结合 Amazon Bedrock Knowledge Bases 实现知识库检索。

它展示了如何利用 Claude 的自然语言理解和生成能力，构建一个带有知识库访问能力的 AI 客服系统。

### 技术栈
- **前端**：React + TypeScript
- **UI 组件**：shadcn/ui
- **知识库**：Amazon Bedrock Knowledge Bases
- **部署**：AWS Amplify

### 核心特性
- 支持多个 Claude 模型切换（如 Claude 3 Haiku、Claude 3.5 Sonnet），通过下拉组件控制当前使用的模型。
- 利用 shadcn/ui 组件库，高度可定制 UI，支持多主题（含明暗模式）切换。

### 如何使用
1. 配置环境变量：`ANTHROPIC_API_KEY`、`AWS_ACCESS_KEY_ID`、`AWS_SECRET_ACCESS_KEY`
2. 部署 Amazon Bedrock 知识库，上传企业文档
3. 本地 `npm install && npm run dev` 启动，或通过 AWS Amplify 一键部署

---

## 模块二：📊 financial-data-analyst（金融数据分析师）

### 作用
这是一个由 Claude 驱动的金融数据分析师，展示如何结合交互式数据可视化，通过对话方式分析金融数据。

### 技术栈
- **前端**：React + TypeScript
- **图表库**：Chart.js
- **后端**：Claude API

### 核心特性
- Claude 根据对话上下文和分析数据，动态生成各类图表（折线图、柱状图、饼图等）。
- 虽然主要面向金融分析场景，但该 AI 助手可被灵活改造，适用于各种其他数据分析应用。

### 如何使用
```bash
git clone https://github.com/anthropics/claude-quickstarts.git
cd claude-quickstarts/financial-data-analyst
npm install
npm run dev
# 打开 http://localhost:3000
```

---

## 模块三：🖥️ computer-use-demo（计算机控制演示）

### 作用
为 Claude 提供控制桌面计算机的环境和工具集，演示如何利用 Claude 的计算机使用能力，支持最新的 `computer_use_20251124` 工具版本（含缩放操作）。

### 技术架构
这个 Demo 是一个刻意精简的容器化参考实现，展示在 Docker + X11 + VNC 的 Linux 桌面上运行的核心 Agent 循环。

### 支持的模型
支持通过 Claude API、Bedrock 或 Vertex 接入，可使用 Claude Opus 4.5、Claude Sonnet 4.5、Claude Sonnet 4、Claude Opus 4、Claude Haiku 4.5、Claude 3.7 Sonnet 和 Claude 3.5 Sonnet 等多个模型。

### 如何使用（Docker 一键启动）
设置 API Key 后，通过 Docker 启动，映射端口 5900（VNC）、8501（Streamlit）、6080（noVNC）、8080（Web UI）：
```bash
export ANTHROPIC_API_KEY=<your_key>
docker run \
  -e ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY \
  -p 5900:5900 -p 8501:8501 -p 6080:6080 -p 8080:8080 \
  -it ghcr.io/anthropics/anthropic-quickstarts:computer-use-demo-latest
```

---

## 模块四：🍎 computer-use-best-practices（macOS 计算机控制最佳实践）

### 作用
这是一个面向 macOS 的原生参考实现（建议在虚拟机中运行），与容器化版本不同，它直接运行在 macOS 桌面上，并演示了构建更可靠、更高性价比的计算机控制 Agent 的常见模式：显式工具定义、正确的图像尺寸处理与剪裁、Prompt 缓存、服务端压缩、批量工具调用、沙盒 Shell 以及轨迹记录。

### 与 Demo 版的区别

| 对比项  | computer-use-demo | computer-use-best-practices |
| ---- | ----------------- | --------------------------- |
| 运行方式 | Docker 容器         | 原生 macOS（推荐 VM）             |
| 定位   | 极简入门              | 生产级最佳实践                     |
| 额外能力 | 基础 Agent 循环       | 缓存、压缩、批处理、轨迹记录              |

该模块与 Anthropic 的计算机控制最佳实践指南配套使用。

---

## 模块五：🌐 browser-automation（浏览器自动化）

### 作用
这是一个由 Claude 驱动的完整浏览器自动化参考实现，展示如何赋予 Claude 导航网站、检查并操作 DOM 元素、提取内容以及填写表单的能力，底层使用基于 Playwright 的自定义浏览器工具。

### 核心特性
该项目利用 Claude 的浏览器工具 API 实现 Web 交互，包括页面导航、DOM 审查和使用 Playwright 进行表单操作。

### 典型应用场景
- 自动化表单填写 & 数据提交
- 网页内容抓取与智能提取
- E2E 自动化测试辅助
- Web 爬虫 + AI 理解结合

---

## 模块六：🤖 autonomous-coding-agent（自主编程 Agent）

### 作用
这是一个由 Claude Agent SDK 驱动的自主编程 Agent，演示了一种双 Agent 模式（初始化器 + 编程 Agent），可跨多个会话构建完整应用，进度通过 Git 持久化保存，Agent 按顺序逐步完成功能列表。

### 工作原理
实现上，通过 Claude Agent SDK 编排双 Agent 工作流：一个初始化 Agent 负责搭建项目结构与需求梳理，另一个编程 Agent 基于优先级功能列表逐步实现各项功能。

### 核心特性
系统实现了以功能驱动的开发循环：跨会话的进度延续、原子化的 Git 提交、功能列表作为 Backlog、增量交付（每次提交后均有可运行代码）。

### 与单次交互的区别
不同于单轮交互，该系统跨会话维护状态，并使用版本控制持久化进度，支持长时间运行的开发任务。

---

## 🗺️ 整体架构总览

```
claude-quickstarts/
├── customer-support-agent/       # 客服智能体（React + Bedrock）
├── financial-data-analyst/       # 金融数据分析（React + Chart.js）
├── computer-use-demo/            # 桌面控制演示（Docker + Linux）
├── computer-use-best-practices/  # macOS 桌面控制最佳实践
├── browser-automation/           # 浏览器自动化（Playwright）
└── autonomous-coding-agent/      # 自主编程 Agent（双 Agent 模式）
```

## 💡 如何选择合适的模块？

| 你的需求              | 推荐模块                          |
| ----------------- | ----------------------------- |
| 做一个带知识库的客服系统      | `customer-support-agent`      |
| 做数据分析对话 + 图表展示    | `financial-data-analyst`      |
| 让 AI 控制桌面电脑（快速体验） | `computer-use-demo`           |
| 生产级桌面控制（macOS）    | `computer-use-best-practices` |
| 让 AI 操作浏览器 / 网页   | `browser-automation`          |
| 让 AI 自动写代码 / 构建项目 | `autonomous-coding-agent`     |

每个 Quickstart 项目都附有独立的 README 和配置说明，通常只需几步即可完成本地启动。





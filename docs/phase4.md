# 阶段四：生产就绪 — 实施分析

> 来源：`docs/RAG系统设计v14落地方案.md` 第七章 + `docs/RAG系统设计v14.md` §16.4, §26, §27.2-27.3
> 核心原则：**不修改前三阶段的接口或表结构**，以配置变更 + CI/CD 为主

---

## 1. 阶段定位：与前三阶段有本质区别

阶段一到三都是"加功能/加固代码"。阶段四是"把已有系统推到生产标准"。

```
阶段一 → 单条链路跑通
阶段二 → 检索质量加固
阶段三 → 工程可靠性
阶段四 → 生产就绪（基础设施 + CI/CD + 安全 + 量化）
```

**代码变更量很小**（约 200 行），大量工作是配置、脚本、流程。

---

## 2. 阶段四的 6 个子任务

| # | 任务 | 类型 | 代码变更 | 可否在当前单机完成 |
|---|------|------|---------|-----------------|
| A | 基础设施分布式升级 | docker-compose 配置 | 几乎无代码变更 | ❌ 需要多机 |
| B | BGE-M3 INT8 量化 | 模型配置 | 更新 Pipeline YAML + P-MODEL | ✅ |
| C | CI/CD 质量门禁 | CI 配置 + eval 脚本 | `scripts/eval_ragas.py` + `.github/workflows/quality-gate.yml` | ✅ (CI 跑在 GitHub) |
| D | 联合契约测试 J-12~J-20 | 测试脚本 | `tests/contract/joint_12_20.py` | ⚠️ 需权限服务联调环境 |
| E | 生成层复述守卫 | 代码 | `src/chat/service.py` 新增 `check_verbatim_ratio` | ✅ |
| F | 安全渗透测试 | 流程/文档 | 无代码 | ⚠️ 安全团队执行 |

### 2A. 基础设施分布式升级

文档要求：

| 组件 | 当前 | 目标 | 变更类型 |
|------|------|------|---------|
| Milvus | standalone | Proxy×2 + QueryNode×3 + DataNode×2 + IndexNode×2 | docker-compose 新增多个 service |
| PostgreSQL | 单节点 | 主从 + PGBouncer 连接池 | docker-compose + pgpool/pgbouncer 配置 |
| Redis | 单节点 | Sentinel（3 节点） | docker-compose 新增 sentinel 服务 |
| SeaweedFS | 单节点 | 多节点 S3 纠删码 | docker-compose 新增 volume/filer/master 服务 |

**单机开发环境无法真正跑分布式集群**。Phase 4 的交付物是一个 **`docker-compose.prod.yml`**（独立于 `docker-compose.infra.yml`）——描述生产部署拓扑但不在开发期启动。实际生产部署走 K8s Helm Chart 或 Swarm Stack，docker-compose 生产版仅作参考拓扑文档。

### 2B. BGE-M3 INT8 量化

当前嵌入模型是 `qwen3-embedding:0.6b`（Ollama 本地），不是 BGE-M3。量化对 Ollama 模型透明——Ollama 自行管理模型精度。因此这里的"BGE-M3 INT8 量化"对于当前技术栈实操意义有限，但可以在以下两处预留量化配置能力：

- `fastembed` 支持 `model_kwargs={"quantize": True}` 参数——如果将来切换到 BGE-M3，Pipeline YAML 的 `init_parameters` 中加入量化参数
- `registry.py` 的 `invoke_embedding` 已通过 P-MODEL 门面，量化不影响调用方

**实际可交付物：**
1. `pipelines/ingest_v3.yaml`、`pipelines/query_v3.yaml` 新增量化参数注释
2. `docs/deploy/production-models.md` 说明生产环境模型选型与量化配置

### 2C. CI/CD 质量门禁

这是 Phase 4 在单机开发环境**完全可交付**的任务。

**需要新建/修改的文件：**

| 文件 | 用途 |
|------|------|
| `.github/workflows/quality-gate.yml` | GitHub Actions CI 流程：安装依赖 → 跑契约测试 → 跑 RAGAS 评测 → 对比基线 → 失败阻断合并 |
| `scripts/eval_ragas.py` | RAGAS 离线评测脚本：加载评测集 → 跑检索 → 计算 Context Recall / Faithfulness → 对比 baseline.json |
| `tests/eval_sets/README.md` | 评测集维护说明 |
| `tests/eval_sets/kb-dev-test/questions.json` | 开发测试 KB 的首批评测三元组（≥50 条，带 kb_id + user_id） |
| `metrics/baseline.json` | RAGAS 基线值（首次跑完后生成，后续每次 PR 对比） |

**CI 流程：**
```yaml
name: Quality Gate
on:
  pull_request:
    paths: ['src/**', 'pipelines/**']

jobs:
  contract-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: pip install -r requirements.txt
      - run: pytest tests/contract/ -v

  ragas-eval:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: python scripts/eval_ragas.py \
          --eval-set tests/eval_sets/ \
          --baseline metrics/baseline.json \
          --threshold-recall 0.70 \
          --threshold-faithfulness 0.75
```

### 2D. 联合契约测试 J-12~J-20

文档 §27.2 列出 9 个测试项（J-12 到 J-20），需要与权限服务双侧参与联调环境。单机开发环境无法跑。

| # | 测什么 | 当前状态 |
|---|-------|---------|
| J-12 | 动词与端点绑定：`doc:retrieve` 走 `/v1/check` 返回 `invalid_request` | 需要 Cerbos 开放该端点 |
| J-13 | 准入矩阵：用 `retrieval` 的 client_id 调 `doc:view` 返回 `invalid_request` | Cerbos 不区分 client_id |
| J-14 | 批量端点可用性：`/v1/check/batch` 是否开放 | ✅ 已验证 |
| J-15 | prefilter 接受 ctx_token | 当前不走外部端点，自维护 |
| J-16 | filter 上限与超限行为：传 201 条 | ✅ 代码已实现 |
| J-17 | decision_id 可追溯 | ✅ Cerbos cerbosCallId 已记录 |
| J-18 | 超时行为：注入网络延迟超过 SLO | 可本地模拟 |
| J-19 | 限流行为：突发大量 `/v1/visibility` 调用返回 429 | ⚠️ 需权限服务限流 |
| J-20 | `is_enabled=false` 不在 strict 保证内 | ✅ 代码已有 |

**可交付物：** `tests/contract/test_joint_12_20.py` 按文档规格写测试代码，能跑的通跑，不能跑的标记为 `@pytest.mark.skip(reason="需要权限服务联调环境")`。

### 2E. 生成层复述守卫

这是 Phase 4 唯一新写的业务代码。文档 §16.4：

> "LLM 不得逐字复述超出必要长度的原文（否则 `doc:retrieve` 权限被当作 `doc:download` 用）。"
> "单 chunk 复述长度上限不超过 chunk 长度的 60%，超限时改为摘要 + 引用提示。"

**实现思路：**

```python
def check_verbatim_ratio(answer: str, chunk_contents: list[str]) -> str:
    """检测 LLM 是否逐字复述超过 chunk 原文 60%。
    
    对每个候选 chunk，计算最长公共子串长度 / chunk 长度。
    超 60% 阈值的片段用 "[原文引用 #N]" 替代。
    返回处理后的 answer。
    """
```

这个函数放在 `src/chat/service.py` 中，在 `invoke_llm` 返回之后、返回给用户之前调用。

### 2F. 安全渗透测试

这是一项**人工/工具化安全审计**，不涉及代码编写。Phase 4 只需要准备一个 checklist 文档 `docs/deploy/security-checklist.md`，列出需要渗透测试验证的安全边界：

- JWT 伪造攻击
- credential 日志泄露
- 跨 KB 检索绕过
- vis_version=0 chunk 访问
- 事后过滤绕过
- 权限服务不可达降级
- 熔断器绕过
- SSE 流式注入

文档参照 v14.md §27.1 契约测试清单编写。

---

## 3. 实现顺序

| 步骤 | 任务 | 做什么 | 独立验证方式 |
|------|------|------|------------|
| **1** | RAGAS eval 脚本 | `scripts/eval_ragas.py` + `tests/eval_sets/` + 首批评测三元组 | `python scripts/eval_ragas.py --eval-set tests/eval_sets/` 输出 recall/faithfulness |
| **2** | CI 质量门禁 | `.github/workflows/quality-gate.yml` | push 到 GitHub → PR → Actions 自动跑 |
| **3** | 复述守卫 | `check_verbatim_ratio()` + 在 LLM 生成后调用 | 构造 LLM 输出含 80% 复述 → 被截断替换 |
| **4** | 联合契约测试骨架 | `tests/contract/test_joint_12_20.py`（能跑的通跑，不能跑的 skip） | `pytest tests/contract/ -v` |
| **5** | 生产 docker-compose 模板 | `docker-compose.prod.yml` | 文档性质的参考配置 |
| **6** | 量化配置预留 | `pipelines/ingest_v3.yaml` + `query_v3.yaml` 含量化参数 | Pipeline YAML 语法检查 |
| **7** | 安全 checklist | `docs/deploy/security-checklist.md` | 文档审查 |

---

## 4. 特别注意的约束

### 4.1 分布式升级不是"改代码"

文档 §7.2 明确写"配置变更，代码不变"。分布式集群是 docker-compose 的 topology 变化，不是应用代码变化。`src/` 目录不应为分布式部署新增任何代码路径——连接池、读写分离、分片路由这些应由基础设施层（PGBouncer、Milvus Proxy、Redis Sentinel）透明处理。

### 4.2 CI 质量门禁不跑 live service

CI 只能跑"离线"评测——用预先构造的评测三元组模拟 RAG 评测。不要试图在 CI 中启动 Milvus/Cerbos/SeaweedFS 全套基础设施。评测集的 question + context + ground_truth 三元组是离线构造的。

### 4.3 复述守卫的阈值边界（文档直接给出）

> "单 chunk 复述长度上限不超过 chunk 长度的 60%，超限时改为摘要 + 引用提示。"
> "拦不住蓄意的多轮分段索取。"

这是行为边界——不要试图实现完美的防提取，60% 阈值是"做得到且合理"的工程折中。

### 4.4 Pipeline YAML v3 与 v1/v2 并存

> "Embedding 模型变更同时触发 Pipeline YAML 版本递增"

阶段四的量化配置通过新增 `ingest_v3.yaml` / `query_v3.yaml` 实现，不修改 v1/v2。通过 `pipeline_yaml_version` 字段选择使用哪个版本。

### 4.5 阶段四不改前三阶段的接口或表结构

这个原则贯穿四个阶段。复述守卫是 `src/chat/service.py` 内部新增函数，不影响 `retrieve_and_generate_task` 的签名。质量门禁只读取已有数据，不新增表。

# 阶段三：工程加固 — 实施分析

> 来源：`docs/RAG系统设计v14落地方案.md` 第六章 + `docs/RAG系统设计v14.md` §13.7b, §14.5c, §25.3, §14.3, §7.2, §14.6
> 核心原则：**不修改前两阶段的接口或表结构**，只升级内部实现可靠性

---

## 1. 覆盖的 RAG 环节及与现有代码的衔接

阶段一完成了"单条链路跑通"，阶段二完成了"检索质量加固"。阶段三的目标是**让这条链路具备持续运行的韧性**——不怕重启、不怕并发、不怕权限变更。

```
现有链路（Phase1+2 不变）      阶段三加固内容（只改内部实现）
───────────────────────────── ────────────────────────────────────
B-INGEST ingest_document_task → execution_epoch 栅栏 + 协作式取消
B-DOC delete_document_from_kb → purge=true 真实实现（四合一 retire）
P-AUDIT emit_audit_event_txn  → 高风险事件同事务写入（回滚保护）
新增：结构镜像对账              → 每小时对比 mount 表 vs 权限服务
新增：戳记对账                  → 严格库 15min / 普通库 1h 抽样
新增：P-AUTHC 熔断器            → circuitbreaker 库 30s 内失败率>50% 熔断
P-OBS Metric                   → authz_decision_total 等 6 个指标
B-CHAT 生成层引用校验           → 过滤 LLM 幻觉 chunk_id
```

| 阶段一/二状态 | 阶段三实现 | 改动范围 |
|------------|---------|---------|
| `execution_epoch` 值固定为 1，从未被用 | 每次重提交 +1，任务在关键写点前重读 epoch，不匹配则自动退出 | B-INGEST 内部逻辑 |
| `delete_document_from_kb(purge=true)` 返回 501 | 删各挂载 → 计数归零 → 调 retire_resource → 物理删文件 → 同事务审计 | B-DOC 新增实现 |
| `emit_audit_event_txn` 与普通事件相同 | DOC_DELETE/AUTHZ_WRITE 与业务事务同库同事务提交，写失败回滚 | P-AUDIT 内部升级 |
| 无对账机制 | 结构镜像对账（每小时）+ 戳记对账（15min/1h）定时任务 | 新增模块 |
| 权限服务不可达时抛异常 | 熔断器（30s 失败率>50% 熔断 → 503） | P-AUTHC 新增 |
| 无 Metric 指标 | 6 个 Gauge/Counter 指标（authz_decision_total 等） | P-OBS 新增 |
| 无引用校验 | LLM 生成的 chunk_id 与候选集做交集验证 | B-CHAT 新增 |

---

## 2. 需要新建和修改的文件

### 新建文件

| 文件 | 用途 |
|------|------|
| `src/platform/task/reconciliation.py` | 跨系统对账定时任务：结构镜像对账 + 戳记对账 |
| `src/platform/obs/metrics.py` | P-OBS Metric 门面：authz_decision_total / mirror_gap / stamp_drift / orphan_stamp / filtered_rate 等 |

### 修改文件

| 文件 | 修改内容 |
|------|---------|
| `src/ingest/service.py` | ① `ingest_document_task` 每个关键写点前重读 `execution_epoch` 和 `parse_status`，实现协作式取消 + 乐观栅栏 ② 重提交时 epoch +1 ③ 卸载清理时序（cancelling 态处理） |
| `src/doc/service.py` | `delete_document_from_kb(purge=true)` 完整实现：删各挂载→发 DocumentUnmounted→计数归零→调 retire_resource→P-STORE.delete→同事务写 DOC_DELETE/AUTHZ_WRITE |
| `src/platform/audit/service.py` | `emit_audit_event_txn` 升级：接收数据库连接参数，与调用方的业务事务在同一连接上提交 |
| `src/permission/cerbos_client.py` | `CerbosClient` 封装 circuitbreaker 熔断器：失败计数 + 状态机（CLOSED→OPEN→HALF_OPEN） |
| `src/permission/authz.py` | `check` / `check_batch` / `filter_items` / `get_prefilter` 全部包裹熔断器，熔断打开时直接返回 `auth:authz_unavailable` |
| `src/chat/service.py` | `validate_citations` 函数：提取 LLM 答案中的 chunk_id 引用，验证是否在候选集内，过滤幻觉引用 |

---

## 3. 实现顺序（从独立可测到底层依赖）

| 步骤 | 模块 | 做什么 | 独立验证方式 |
|------|------|------|------------|
| **1** | P-OBS metrics | 定义 Metric 门面（Counter/Gauge），埋点到 authz 调用链 | Python 脚本调一次 `check` → `authz_decision_total{decision="allow"}` 递增 |
| **2** | P-AUTHC 熔断器 | circuitbreaker 包装 CerbosClient 的关键方法 | 手动停掉 Cerbos 容器 → 30s 内多次调 `check` → 熔断打开 → 返回 503 |
| **3** | B-INGEST epoch 栅栏 | `should_abort()` 函数 + `ingest_document_task` 关键写点前检查 | 提交两个任务同一个 mount_id，第二个 epoch 更高 → 第一个自动退出 |
| **4** | B-INGEST cancelling 清理时序 | DocumentUnmounted → cancelling → 在途任务退出 → 清理 Milvus chunk → removed | 发卸载事件 → 观察 Milvus chunk 在超时内清理完毕 |
| **5** | B-DOC purge=true | 遍历挂载 → unmount → 计数归零 → retire_resource → 物理删除 | 创建文档挂两个 KB → 从每个 KB 移除 → 最后一个移除触发 retire + 物理删除 |
| **6** | P-AUDIT 同事务写入 | `emit_audit_event_txn` 收 conn 参数，在已有事务上 INSERT | 业务事务成功 → audit_logs 有记录；业务事务失败 → audit_logs 无记录 |
| **7** | 结构镜像对账 | 定时任务：查 `document_kb_mounts` 抽样 → 调 `/v1/check` → 补调 link | 手动从 DB 删一条 mount → 等下次对账周期 → mount 被补回 |
| **8** | 戳记对账 | 定时任务：查 Milvus chunk → 找 vis_version=null 或落后 → 补提交 stamp_channel_task | 手动在 Milvus 写一条 vis_version=0 → 等对账 → 被补盖戳 |
| **9** | B-CHAT 引用校验 | `validate_citations(answer, chunk_ids)` → 过滤不在候选集的引用 | 构造 LLM 输出含幻觉 chunk_id → 校验后该引用被移除 |

### 调试策略

- **步骤 1** (metrics)：纯 Python 门面，可独立跑脚本验证 Counter 递增
- **步骤 2** (熔断器)：需要停掉 Cerbos 容器来触发失败计数，再启动验证半开恢复
- **步骤 3-4** (epoch + cancelling)：在 Celery worker 内跑——先启动 worker (`make dev-ingest`)，然后并发提交任务
- **步骤 5** (purge=true)：依赖 SeaweedFS + Cerbos + PostgreSQL 三者同时可用
- **步骤 7-8** (对账)：定时任务用 `while True + sleep` 跑，可在开发期缩短周期（如每分钟而非每小时）
- **步骤 9** (引用校验)：纯函数，输入 answer 字符串 + chunk_id 集合，输出过滤后的 answer

---

## 4. 特别注意的约束

### 4.1 不修改阶段一和阶段二的接口

所有加固都是内部实现升级，不新增接口参数：
- `ingest_document_task` 的签名不变——`execution_epoch` 是已有参数，只是之前永远传 1
- `delete_document_from_kb(purge=true)` 签名不变——阶段一已定义，只是改为真实实现
- `emit_audit_event_txn` 签名可以增加可选的 conn 参数（用于事务共享），但调用方可以选择不传

### 4.2 execution_epoch 栅栏的正确性

> "阶段一 `execution_epoch` 固定为 1 从未被用；阶段三真正用上"

关键行为：
- 每次重提交 mount 时，`execution_epoch` +1
- `ingest_document_task` 在每个**关键写点前**（读原文件后、Pipeline 执行前、写 Milvus 前）重查 DB
- 若 `record.execution_epoch != current_epoch` 或 `record.parse_status == 'cancelling'` → 立即退出

**协作式取消不是强一致性锁**——取决于任务在每个写点之间的检查和 yield。这是有意为之：分布式场景下用锁会带来死锁风险，epoch 栅栏是"乐观并发控制"。

### 4.3 卸载清理时序（不可颠倒）

```
DocumentUnmounted 事件到达
  → B-INGEST 置 parse_status = 'cancelling'（不可逆）
  → 在途 ingest_document_task 在下一个写点发现 cancelling → 停止写入并 exit
  → 确认任务已退出后 → 清理该 mount 的 Milvus chunk
  → 置 parse_status = 'removed'

超时兜底：cancelling 停留超过 2× 单任务最长耗时 → 强制清理 + 告警
```

### 4.4 purge=true 的 retire 调用顺序（铁律）

文档 delete 的四个步骤**不可颠倒**：

1. 删各挂载 → 每个发 `DocumentUnmounted`
2. 统计剩余挂载 → **计数为 0 才继续**
3. 调 `retire_resource(doc, document_id)` — 权限服务原子完成四件事：① 回收全部 acl ② 回收 restriction ③ 解除全部挂载（发 unmounted 事件）④ 置 retired
4. 成功后删物理文件 + 删 document 记录 + 同事务写审计

**先调权限服务，成功后再提交本地事务。** 唯一可能的不一致是"权限服务有、本地无"（孤儿镜像，安全且可对账回收）。

### 4.5 P-AUDIT 高风险同事务写入

`emit_audit_event_txn` 的升级要点：
- 接收一个 `conn` 参数（asyncpg Connection），在已有业务事务上执行 INSERT
- 写失败 → 业务回滚（与 `emit_audit_event` 的 fail-open 完全不同）
- 仅对高风险事件使用：`DOC_DELETE` / `DOC_DOWNLOAD` / `AUTHZ_WRITE`
- 入参可以用 `**payload` 传递 `conn`，不影响现有调用方

### 4.6 熔断器阈值与半开探测（文档明确给出）

| 参数 | 值 | 来源 |
|------|-----|------|
| 失败阈值 | 30s 内失败率 > 50% 或连续失败 10 次 | 落地方案 §6.2 |
| 半开探测间隔 | 每 60s 放行一次探测请求 | 同上 |
| 熔断打开时行为 | 所有权限调用直接返回 503 `auth:authz_unavailable` | 同上 |
| 降级文案 | "服务暂时不可用，请稍后重试"（与"未找到足够信息"可区分） | 同上 |
| circuitbreaker 库 | `circuitbreaker==2.0.0`（已在 requirements.txt） | 同上 |

### 4.7 戳记对账的频率差异

| KB 类型 | 频率 | 范围 |
|---------|------|------|
| strict=true 的库 | 每 15 分钟 | **全量**（全部 chunk） |
| strict=false 的库 | 每小时 | **10% 抽样** |

区分的原因是：strict 库没有层 3 兜底，陈旧戳记是越权窗口（虽然严格库有层 3 兜底）；非严格库依赖戳记作为唯一过滤手段，但能接受 5 分钟内有界自愈窗口。

### 4.8 生成层引用校验的设计边界

> "引用校验是确定性 chunk_id 校验——检查模型声称引用的 chunk_id 是否真在本次检索候选集内。"

- 不是语义校验（不做"这个 chunk 是否真的支撑了这个回答"的 NLP 判断）
- 是存在性校验（chunk_id 在不在 `candidate_chunk_ids` 中）
- 过滤后可能使答案不完整——这是可接受的代价，比保留幻觉引用更安全

---

## 5. 完成标准（6 条验收 checklist）

```
[ ] 摄入失败重启后断点续传，不重复写入 Milvus chunk
[ ] 文档删除（purge=true）后 Milvus chunk 30s 内清理完毕
[ ] strict 库撤权后即时生效，非 strict 库 5 分钟内自愈（戳记对账）
[ ] 镜像对账定时任务跑起来，mirror_gap 告警能触发
[ ] 权限服务宕机超过阈值，熔断器打开，查询统一返回 503 降级文案
[ ] execution_epoch 栅栏：重提交后旧僵尸任务不会覆盖新结果
```

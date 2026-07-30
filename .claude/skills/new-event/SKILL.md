# new-event

添加新的领域事件（本系统发布或外部订阅）时使用本 Skill。

## 执行步骤

1. 确定事件归属：本系统发布（经 Outbox）还是消费外部事件
2. 定义事件信封字段（7 个必填，v14.md §3.1）：
   - `event_type`：PascalCase 命名（如 `DocumentMounted`）
   - `event_id`：幂等去重标识
   - `occurred_at`：事件发生时间
   - `trace_id`：关联追踪 ID
   - `tenant_id`：租户标识
   - `payload_schema_version`：payload 版本号
   - `payload`：业务数据
3. 确定投递语义：Outbox + 至少一次 + 幂等消费
4. 如果是本系统发布的事件：
   - 同事务写业务变更 + 写 outbox 表
   - relay 投递由 P-TASK 承载
   - 消费者按 `event_id` 幂等去重
5. 如果消费外部事件（如 `VisibilityChanged`）：
   - 订阅来自权限服务的事件流
   - 按 `event_id` 幂等消费
   - 消费失败不 ack，下轮重试
   - 绝不落盘空戳记
6. 在事件目录（v14.md §3.3）中注册：
   - `event_type` / 发布方 / 典型订阅方 / payload 关键字段
7. 如发布方为 B-DOC，在 `src/doc/[模块名]_events.py` 中定义事件结构
8. 添加对账兜底：定时任务扫描缺口，补偿丢失事件

## 必须遵守的规范

- 事件名使用 **PascalCase**（如 `DocumentMounted`、`VisibilityChanged`）
- 所有本系统领域事件一律经 **Outbox** 发布，不得直接发消息
- 投递语义：**至少一次**，消费方必须**幂等**（按 `event_id` 去重）
- 外部事件 `VisibilityChanged` 来自权限服务 outbox，本系统只订阅不发布
- 消费外部事件三件事：(a) 幂等；(b) 版本单调性检查；(c) 失败不 ack、绝不落盘空戳记
- `outbox` 表归各发布方独占（仅 B-DOC 一个分区）
- 事件 schema 演进须保持向后兼容（新增字段用默认值，不删不改已有字段）
- 对账兜底是强制要求：每个发布方必须提供对账机制

## 禁止事项

- 禁止不经过 Outbox 直接发消息
- 禁止消费方不按 `event_id` 去重
- 禁止事件 payload 中包含 `credential`（JWT 原文）
- 禁止事件投递失败时静默丢失——必须 ack 失败 + 重试 + 对账补偿
- 禁止盖戳失败时 ack `VisibilityChanged`——必须不 ack 让下轮重投

## 已注册的事件目录

| event_type | 发布方 | 订阅方 | payload 关键字段 |
|-----------|-------|-------|----------------|
| `DocumentMounted` | B-DOC | B-INGEST | document_id, mount_id, kb_id, chunking_config_version |
| `DocumentUnmounted` | B-DOC | B-INGEST | mount_id, kb_id |
| `MountEnabledChanged` | B-DOC | B-INGEST | mount_id, is_enabled |
| `DocumentParsed` | B-INGEST | P-AUDIT、P-OBS | document_id, mount_id, kb_id, chunk_count |
| `DocumentParseFailed` | B-INGEST | P-AUDIT、告警 | document_id, mount_id, kb_id, reason, retry_count |
| `VisibilityChanged` | ★ 外部：权限服务 | B-INGEST（经 P-AUTHC 转交） | resource{type,id}, channel{kb}, version, unmounted, tenant |
| `AUTH_ALLOW` | P-AUTHC（经审计） | P-AUDIT | — |
| `AUTH_DENY` | P-AUTHC（经审计） | P-AUDIT | — |
| `KB_QUERY` | B-CHAT（经审计） | P-AUDIT | query_hash, returned_count, authz_decision_ref |
| `CHUNK_FILTERED` | B-RETRIEVE（经审计） | P-AUDIT | — |

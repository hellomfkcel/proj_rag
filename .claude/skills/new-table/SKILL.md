# new-table

创建新的数据库表或修改已有表结构时使用本 Skill。

## 执行步骤

1. 确定表的唯一写者模块：每张表有且仅有一个模块可写（单写原则）
2. 在 `scripts/init.sql` 中添加 CREATE TABLE 语句
3. 按最终形态建表——即使部分字段暂时填默认值，也要在创建时定义：
   - `pipeline_yaml_version`：阶段一填 `"v1"`，阶段二真正用
   - `execution_epoch`：阶段一填 `1`，阶段三真正用（栅栏令牌）
   - `resolved_query`：阶段一等于 `user_question`，阶段二改写
   - `authz_decision_ref`：阶段一空字符串，阶段二审计补全
4. 表字段规范：
   - 主键：`id UUID PRIMARY KEY DEFAULT gen_random_uuid()`
   - 时间戳：`TIMESTAMPTZ DEFAULT now()`
   - 外键：`REFERENCES [表名](id)`
   - 唯一约束：按业务需求添加 `UNIQUE`
   - 默认值：为所有未来才用上的字段提供合理默认值
5. 创建索引：按查询模式添加，不要漏掉：
   - 按租户 + 时间查询：`(tenant_id, created_at DESC)`
   - 按状态查询：`WHERE status = 'pending'`（部分索引）
   - 按关联 ID 反查：`(request_id)`、`(conversation_id, turn_index)`
   - 跨系统取证：`(authz_decision_ref) WHERE authz_decision_ref IS NOT NULL`
6. 如果有 JSONB 字段，在注释中说明结构
7. 在对应模块的 `models.py` 中定义 SQLAlchemy ORM 模型
8. 更新单一写者对照表（v14.md §0.1.3）

## 必须遵守的规范

- 主键使用 UUID，默认值 `gen_random_uuid()`
- 时间戳字段使用 `TIMESTAMPTZ`，默认值 `now()`
- 所有字段按最终形态定义，后续只填值不改结构
- 每张表有且仅有一个模块可写（单写原则）
- 外键约束必须显式声明
- 索引按实际查询模式创建，不做过度索引
- JSONB 字段用于存储半结构化数据（如 payload、snapshot）
- 版本化对象 append-only，一次性执行结果存快照不存配置表指针

## 禁止事项

- 禁止在有数据之后修改表结构（最先建表时就要包含未来字段）
- 禁止跨模块直写对方独占表
- 禁止存储 `credential`（JWT 原文）到任何表（审计 payload 也不行）
- 禁止存储权限判定结果（`/v1/filter` 永久禁止缓存落库）
- 禁止存储 ACL/role_binding/restriction（这些数据在权限服务，不在本系统）

## 表模板

```sql
-- ── [表用途描述] ──────────────────────────────────────────────────────
-- 写者：[唯一模块名]
CREATE TABLE [表名] (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       VARCHAR(64) NOT NULL,
    -- 业务字段...
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now(),
    UNIQUE (tenant_id, [业务唯一键])
);

-- 索引
CREATE INDEX idx_[表名]_tenant_time ON [表名](tenant_id, created_at DESC);
```

## 字段模板

| 字段类型 | 模板 | 使用场景 |
|---------|------|---------|
| 主键 | `id UUID PRIMARY KEY DEFAULT gen_random_uuid()` | 所有表 |
| 租户 | `tenant_id VARCHAR(64) NOT NULL` | 多租户隔离 |
| 状态枚举 | `status VARCHAR(16) DEFAULT 'active'` | 状态机 |
| 时间戳 | `created_at TIMESTAMPTZ DEFAULT now()` | 创建时间 |
| 更新时间 | `updated_at TIMESTAMPTZ DEFAULT now()` | 状态变更 |
| 版本号 | `pipeline_yaml_version VARCHAR(32) DEFAULT 'v1'` | Pipeline 版本 |
| 栅栏令牌 | `execution_epoch INTEGER DEFAULT 1` | 并发控制 |
| 预留给未来 | 字段现在就建好，填默认值 | 跨阶段演进 |

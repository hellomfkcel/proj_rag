# new-interface

为模块添加新的 REST 端点、内部接口或外部契约时使用本 Skill。

## 执行步骤

1. 确定接口类型：【REST】/【内部接口】/【事件】/【外部契约】
2. 按接口规格模板完整填写 11 个字段（v14.md §5）：
   ```
   接口名/端点：  所属模块：  接口类型：
   调用方：  前置条件：  入参（含窄接口投影）：  出参：
   错误模型：  幂等语义：  权限要求（动词 + 调用端点两项均须写）：
   产生的审计事件：  SLO（如适用）：
   ```
3. 选择 ctx 最小投影窄接口（v14.md §1.3），不得无差别接收整个 ctx：
   - `AccessScope`：`tenant_id` + `principals` + `credential`（检索/权限相关接口）
   - `Identity`：`user_id` + `tenant_id` + `roles` + `groups`（业务编排读取身份）
   - `AuditContext`：全量 ctx（审计门面使用）
   - `AuthzCallContext`：`credential` + `request_id`（P-AUTHC 内部，刻意不含 tenant_id/principals）
4. 如涉及权限判定，标注需要的动词和调用端点
5. 如为 REST 接口，定义错误码：格式 `模块前缀:错误类别`
6. 错误响应使用统一信封：`error_code` + `message`（不含敏感信息）+ `request_id`（=trace_id）+ `details`（可空）
7. 检索场景的 deny 对外文案必须与"未找到足够信息"完全相同（存在性三通道纪律）
8. 如为【外部契约】，变更需双方会签

## 必须遵守的规范

- 接口规格 11 个字段必须完整填写
- 入参必须使用最小投影窄接口，不得传递完整 ctx
- 错误码格式固定：`模块前缀:错误类别`
- 权限要求必须写两项：动词 + 调用的权限服务端点
- `retrieve:insufficient_evidence` 返回 200（业务态），不是 4xx 错误
- 检索 deny 的对外文案与"未找到足够信息"完全相同，不能说"您没有权限"
- 所有 REST 错误响应包含 `request_id`（=trace_id）
- 【外部契约】变更需双方会签

## 禁止事项

- 禁止在 Envelope 中传 `tenant`/`principals` 字段
- 禁止传 `context`（v1 注册表为空）
- 禁止 `doc:retrieve` 走 `/v1/check`——只能走 `/v1/filter`
- 禁止使用废除动词：`doc:write`（改用 `kb:write`）、`doc:delete`（改用 `doc:purge`/`doc:unmount`）、`acl:update`（改用 `doc:share`/`kb:grant`）
- 禁止通道类动词（`doc:retrieve`、`doc:unmount`）不传 `channel.kb`
- 禁止业务模块传入/指定 `client_id`——由 P-AUTHC 按方法硬编码
- 禁止错误消息中包含敏感信息
- 禁止在 deny 响应中透露权限相关 reasons（reasons 只写业务日志，永不出用户文案）

## 已注册的错误码参考

| error_code | HTTP | 含义 | 抛出方 |
|-----------|------|-----|-------|
| `auth:unauthenticated` | 401 | JWT 无效/过期 | P-AUTHC |
| `auth:forbidden` | 403 | 权限不足 | P-AUTHC |
| `auth:authz_unavailable` | 503 | 权限服务不可达 | P-AUTHC |
| `auth:authz_indeterminate` | 503 | 权限服务返回 indeterminate | P-AUTHC |
| `doc:not_found` | 404 | 文档/挂载/目录不存在 | B-DOC |
| `doc:duplicate` | 200 | 幂等命中 | B-DOC |
| `doc:kb_reindexing` | 409 | KB 维护中拒绝摄入 | B-DOC |
| `doc:authz_write_failed` | 502 | 写路径调生命周期端口失败 | B-DOC |
| `retrieve:insufficient_evidence` | 200 | 补检索后仍不足 | B-RETRIEVE |
| `common:validation_error` | 422 | 入参校验失败 | 各模块 |
| `common:internal_error` | 500 | 未预期异常 | 各模块 |

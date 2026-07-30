# new-contract-test

为模块添加契约测试（CI 强制）时使用本 Skill。契约测试验证的是跨模块的边界约定，不是单元测试。

## 执行步骤

1. 在 `tests/contract/` 下按模块创建测试文件
2. 覆盖以下必测类别（v14.md §27.1）：

### 权限相关必测项
3. **权限出口唯一性**：断言除 P-AUTHC 外，任何模块代码中出现权限服务 base URL 字面量、`x-client-id` 字面量、直接 HTTP 调用即失败
4. **零判定断言**：扫描业务代码，断言不存在 `uploaded_by == user_id`、`owner`、`read_only`、`kb_reader` 等本地权限判断
5. **client_id 硬编码**：断言不存在从业务模块入参决定 `x-client-id` 的代码路径
6. **动词合法性**：断言所有 action 字面量都在上游动词目录 16 个之内；废除动词零出现
7. **通道类动词**：断言 `doc:retrieve`/`doc:unmount` 调用点必传 `channel.kb`；`doc:retrieve` 不经 `/v1/check`
8. **三态映射**：注入 allow/deny/indeterminate/超时四种响应，断言四种业务行为正确；
   断言 `indeterminate` 与 `deny` 在 Metric 中标签不同；断言未知 obligation → deny + 告警
9. **fail-closed 全覆盖**：注入权限服务不可达，断言交互端点 503、检索整体拒答、`filter_items` 返回空、prefilter 失败不降级为无过滤查询、不产生任何身份替换
10. **层 1 过滤器完整性**：断言注入向量库的过滤条件恒含六条件；断言 `vis_version=null` 的 chunk 不被返回
11. **事后过滤禁令**：断言不存在"先无过滤查询再应用层筛选"的代码路径；断言补检索各轮的过滤条件完全相同
12. **存在性三通道**：断言检索响应中不含被过滤条目的任何痕迹；断言 deny 文案与"未找到足够信息"一致；断言 reasons 不出现在用户可见响应中
13. **view/download 分权**：构造只有 read_only 的用户，断言 view 200 而 download 403
14. **戳记通道粒度**：构造一份文档挂两个成员不同的 KB，断言两处 chunk 的 `allow_stamps` 不同
15. **盖戳纪律**：`/v1/visibility` 失败时不写任何东西、不 ack；版本单调性；`unmounted=true` 清空；分批与断点续跑；重复投递幂等
16. **写路径顺序**：断言 register/link/unlink/retire 在本地事务提交之前调用；调用失败时本地事务回滚；幂等键确定性可重算
17. **JWT 不外泄**：断言 `credential` 不出现在日志、Trace span attribute、审计 payload、任务参数中
18. **三种失败语义并存**：同时注入 Collector 不可达 + 权限服务不可达 + 审计写失败，断言三条语义互不污染
19. **就绪检查边界**：停掉可观测栈断言 `/readyz` 仍 200；停掉权限服务断言 `/readyz` 仍 200

### Haystack 增量必测项（v14 新增）
20. **Haystack 防腐层隔离**：断言除指定防腐层适配子模块外，任何模块 `import haystack` 即失败
21. **Component 权限零判断**：断言所有 `@component` 的 `run()` 方法内不出现 P-AUTHC 接口调用、不出现 user_id/roles/principals 参数
22. **MetadataFilter 六条件完整性**：断言注入每个 Retriever Component 的 `MetadataFilter` 恒含六条件
23. **★ 两路 filter 一致性**：断言 hybrid 模式下稠密路和稀疏路的 `MetadataFilter` 对象引用相等（或内容相等）
24. **Pipeline YAML 版本化**：断言 Pipeline 变更后 `pipeline_yaml_version` 递增；assert worker 按 name 取 YAML
25. **P-MODEL 防腐**：断言 Generator/Embedder/Ranker/PromptBuilder 实例不在 B-INGEST/B-RETRIEVE/B-CHAT 的模块签名中出现
26. **异步包装**：断言 `pipeline.run()` 调用只出现在 worker 进程内，API 进程内零调用

### 其余静态边界测试
27. 事件 schema 演进兼容
28. 内部接口窄投影
29. 统一错误码 REST 映射
30. 单一写者

## 必须遵守的规范

- 契约测试在 CI 中强制运行，不可跳过
- 权限相关测试可使用 mock 权限服务（验证本系统处理逻辑）
- Haystack 增量项必须全部通过
- 两种 filter 一致性是 Haystack 混合检索最容易犯的缺口，单路测试永远不会暴露，必须双路同时测

## 禁止事项

- 禁止用集成测试替代契约测试——契约测试验证模块边界的约定，集成测试验证端到端流程
- 禁止跳过任何必测类别
- 禁止对权限服务做 mock 时使用与实际契约不一致的响应结构

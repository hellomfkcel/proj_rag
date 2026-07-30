# 安全渗透测试检查清单

> 来源：v14.md §27.1 契约测试清单 + §0.2 全局规则 + §4 禁止事项
> 执行方：安全团队（非开发自测）

## 认证与身份伪造

- [ ] JWT 过期 token 能否绕过 P-AUTHC 本地校签
- [ ] 伪造 JWT（无签名）能否通过 build_context
- [ ] 空 credential 能否构造有效 ctx
- [ ] 高权角色（system_admin）能否通过 JWT claims 伪造

## credential 泄露

- [ ] credential（JWT 原文）是否出现在 structlog 输出中
- [ ] credential 是否出现在 Trace span attribute 中
- [ ] credential 是否出现在审计 payload（audit_logs.payload）中
- [ ] credential 是否出现在 Celery 任务参数中

## 跨 KB 检索绕过

- [ ] 只对 KB-A 有 read 权限的用户能否检索到 KB-B 的 chunk
- [ ] compile_filter 是否恒含 kb_id 条件（不是可选）
- [ ] 无 filter 检索能否返回跨 KB 数据
- [ ] prefilter 不可达时是否回退到无过滤查询（必须拒答）

## 盖戳管道绕过

- [ ] vis_version=0 的 chunk 能否被检索命中
- [ ] 绕过 PermissionMetadataEnricher 直接写 Milvus 的 chunk 是否可见
- [ ] 盖戳失败后是否落盘空戳记（必须不落盘）
- [ ] 旧版本事件能否覆盖新的封禁戳记（版本单调性必须生效）

## 事后过滤绕过

- [ ] 代码路径是否存在"先无过滤检索再应用层筛选"
- [ ] 补检索各轮过滤条件是否完全相同（不能放宽）
- [ ] 业务候选 KB 是否能超出 prefilter 结果范围

## 权限服务不可达降级

- [ ] 权限服务不可达时是否返回 503（不是 500 或空结果）
- [ ] 缓存的决策结果是否可被利用（默认必须关闭缓存）
- [ ] 熔断器打开时是否返回统一拒答文案
- [ ] 权限服务不可达时是否被错误纳入 /readyz

## 存在性三通道泄露

- [ ] deny 响应的用户可见文案是否与"未找到足够信息"完全相同
- [ ] 响应中是否出现"您没有权限"等区分性文案
- [ ] 响应中是否出现被过滤条目的任何痕迹（计数、chunk_id）
- [ ] 补检索轮数差异是否能侧信道推断有权/无权

## 流式注入

- [ ] SSE 流事件中是否包含被过滤的 chunk 内容
- [ ] Redis Pub/Sub 频道名是否可预测/可注入
- [ ] error 流事件是否泄露系统内部状态

## 模型层安全

- [ ] view 路径是否签发原文件签名 URL（必须服务端渲染）
- [ ] 复述守卫 60% 阈值是否能被长段逐字复制绕过
- [ ] LLM 幻觉引用是否经过 validate_citations 过滤
- [ ] 生成结果是否包含原文档中不应泄露的敏感元数据

## 运维安全

- [ ] 可观测栈端口（3000/4317/4318）是否对外暴露
- [ ] PostgreSQL/Redis/Milvus/Cerbos 端口是否对外暴露
- [ ] .env 文件是否被提交到 Git
- [ ] 审计日志保留期是否与权限服务对齐（建议 180 天）

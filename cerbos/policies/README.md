# Cerbos 策略文件

**权威源**: `/home/mfkcel/proj_rag_dev/cerbos/policies/`

这些策略文件与 RAG v14 项目共享。变更流程：
1. 在 RAG 项目 `cerbos/policies/` 中编辑策略
2. 通过 Cerbos PDP 验证（`cerbos compile`）
3. 变更须通过联合契约测试（§27.2）
4. 同步到此目录：`cp -r /home/mfkcel/proj_rag_dev/cerbos/policies/* /home/mfkcel/permission-system/cerbos/policies/`

当前 Cerbos PDP 实际挂载的目录：RAG 项目的 `cerbos/policies/`（通过 Docker volume）

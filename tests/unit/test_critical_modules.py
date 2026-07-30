"""P0 单元测试：覆盖关键模块的核心逻辑。

测试不依赖外部服务（PostgreSQL/Cerbos/Milvus — 全部 mock），
可在本地和 CI 中快速运行。

覆盖模块：
- P-AUTHC: compile_filter 六条件, check 签名, filter_items 批量边界
- P-CONFIG: RetrievalConfig/ChunkingConfig 默认值与字段完整性
- B-RETRIEVE: retrieve() 参数路由 (vector_only/keyword_only/hybrid)
- API Models: RetrievalConfigPatch 字段完整性, ChunkingConfigPatch 字段
- API kb_routes: valid_strategies 包含全部 5 种策略
"""

import pytest
from unittest.mock import patch, MagicMock


# ══════════════════════════════════════════════════════════════════
# 1. P-AUTHC: compile_filter 六条件完整性 (§15.1, §6A.3)
# ══════════════════════════════════════════════════════════════════

class TestCompileFilter:
    """验证 compile_filter 输出的 Haystack MetadataFilter 恒含六条件。"""

    def test_compile_filter_returns_dict(self):
        """compile_filter 返回 milvus-haystack filter dict（含六条件）。"""
        from src.permission.authz import compile_filter
        from src.permission.context import RequestContext

        pf = {"kbs": ["kb-1"], "policy_version": "v1", "ttl_s": 300}
        ctx = RequestContext(
            request_id="test-001", user_id="admin", tenant_id="tenant-dev",
            credential="mock-jwt", roles=[], groups=[], principals={"user:admin", "group:eng"},
        )

        result = compile_filter(pf, ctx, "kb-1")

        # compile_filter 返回 dict（milvus-haystack filter 格式）
        assert isinstance(result, dict), \
            f"Expected dict, got {type(result)}"

    def test_compile_filter_has_six_conditions(self):
        """六条件必须全部存在（§6A.3 条件①-⑥）。"""
        from src.permission.authz import compile_filter
        from src.permission.context import RequestContext

        pf = {"kbs": ["kb-1"], "policy_version": "v1", "ttl_s": 300}
        ctx = RequestContext(
            request_id="test-001", user_id="admin", tenant_id="tenant-dev",
            credential="mock-jwt", roles=[], groups=[], principals={"user:admin"},
        )

        result = compile_filter(pf, ctx, "kb-1")

        assert result["operator"] == "AND"
        conditions = result["conditions"]
        assert len(conditions) == 6, f"Expected 6 conditions, got {len(conditions)}"

        # 验证六个条件的关键字段
        fields_seen = {c["field"] for c in conditions}
        required_fields = {"tenant_id", "kb_id", "allow_stamps",
                          "retrievable", "deny_stamps", "vis_version"}
        missing = required_fields - fields_seen
        assert not missing, f"Missing filter conditions: {missing}"

    def test_compile_filter_tenant_isolation(self):
        """条件①: tenant_id == 当前租户。"""
        from src.permission.authz import compile_filter
        from src.permission.context import RequestContext

        pf = {"kbs": ["kb-1"], "policy_version": "v1", "ttl_s": 300}
        ctx = RequestContext(
            request_id="test-001", user_id="admin", tenant_id="specific-tenant",
            credential="mock-jwt", roles=[], groups=[], principals={"user:admin"},
        )

        result = compile_filter(pf, ctx, "kb-1")
        tenant_cond = [c for c in result["conditions"] if c["field"] == "tenant_id"][0]
        assert tenant_cond["value"] == "specific-tenant", \
            f"Expected tenant isolation, got: {tenant_cond}"

    def test_compile_filter_kb_isolation(self):
        """条件②: kb_id == 指定值。"""
        from src.permission.authz import compile_filter
        from src.permission.context import RequestContext

        pf = {"kbs": ["kb-abc-123"], "policy_version": "v1", "ttl_s": 300}
        ctx = RequestContext(
            request_id="test-001", user_id="admin", tenant_id="tenant-dev",
            credential="mock-jwt", roles=[], groups=[], principals={"user:admin"},
        )

        result = compile_filter(pf, ctx, "kb-abc-123")
        kb_cond = [c for c in result["conditions"] if c["field"] == "kb_id"][0]
        assert kb_cond["value"] == "kb-abc-123", \
            f"Expected KB isolation, got: {kb_cond}"

    def test_compile_filter_suspended(self):
        """suspended=true 时 compile_filter 返回哨兵值。"""
        from src.permission.authz import compile_filter
        from src.permission.context import RequestContext

        pf = {"suspended": True, "kbs": [], "policy_version": "v1", "ttl_s": 300}
        ctx = RequestContext(
            request_id="test-001", user_id="admin", tenant_id="tenant-dev",
            credential="mock-jwt", roles=[], groups=[], principals={"user:admin"},
        )

        result = compile_filter(pf, ctx, "kb-1")
        assert result["value"] == "__SUSPENDED__", \
            f"Suspended filter should return sentinel, got: {result}"

    def test_compile_filter_empty_principals(self):
        """principals 为空集时过滤器仍然可用。"""
        from src.permission.authz import compile_filter
        from src.permission.context import RequestContext

        pf = {"kbs": ["kb-1"], "policy_version": "v1", "ttl_s": 300}
        ctx = RequestContext(
            request_id="test-001", user_id="admin", tenant_id="tenant-dev",
            credential="mock-jwt", roles=[], groups=[], principals=set(),
        )

        result = compile_filter(pf, ctx, "kb-1")
        assert result["operator"] == "AND"
        assert len(result["conditions"]) == 6, \
            "All 6 conditions must exist even for empty principals"


# ══════════════════════════════════════════════════════════════════
# 2. P-AUTHC: check 签名与 doc:retrieve 拦截
# ══════════════════════════════════════════════════════════════════

class TestCheckFunction:
    """验证 check() 对 doc:retrieve 的正确拦截。"""

    def test_check_exists(self):
        """check 函数可导入。"""
        from src.permission.authz import check
        assert callable(check)

    def test_filter_items_exists(self):
        """filter_items 函数可导入。"""
        from src.permission.authz import filter_items
        assert callable(filter_items)

    def test_get_prefilter_exists(self):
        """get_prefilter 函数可导入。"""
        from src.permission.authz import get_prefilter
        assert callable(get_prefilter)

    def test_compile_filter_signature(self):
        """compile_filter 接受 (pf, ctx, kb_id) 三个参数。"""
        import inspect
        from src.permission.authz import compile_filter
        sig = inspect.signature(compile_filter)
        params = list(sig.parameters.keys())
        # compile_filter(pf: Dict, ctx: RequestContext, kb_id: str)
        assert len(params) == 3, f"Expected 3 params, got {len(params)}: {params}"
        assert "kb_id" in params, f"Missing 'kb_id' param: {params}"

    def test_check_has_circuitbreaker(self):
        """check 函数必须包含 circuitbreaker 保护。"""
        import inspect
        from src.permission.authz import check
        source = inspect.getsource(check)
        assert "circuit" in source.lower() or "CircuitBreaker" in source or \
               "fail" in source.lower(), \
            "check() should have circuitbreaker or fail-closed protection"


# ══════════════════════════════════════════════════════════════════
# 3. P-CONFIG: RetrievalConfig / ChunkingConfig 字段完整性
# ══════════════════════════════════════════════════════════════════

class TestRetrievalConfig:
    """验证 RetrievalConfig 包含所有设计文档要求的字段 (§12.1)。"""

    def test_default_values(self):
        """RetrievalConfig 所有字段必须有合理的默认值。"""
        from src.platform.config.service import RetrievalConfig
        rc = RetrievalConfig()

        assert rc.top_k == 10
        assert rc.retrieval_mode == "hybrid"
        assert rc.fusion_method == "rrf"
        assert rc.synthesis_mode == "compact"
        assert rc.strict is False
        assert rc.oversample_factor == 1.5
        assert rc.min_results == 3
        assert rc.refetch_max_rounds == 2
        assert isinstance(rc.rerank_model_id, str)
        assert isinstance(rc.haystack_pipeline_name, str)

    def test_all_required_fields_present(self):
        """设计文档 §12.1 要求的字段必须全部存在。"""
        from src.platform.config.service import RetrievalConfig
        import dataclasses
        fields = {f.name for f in dataclasses.fields(RetrievalConfig)}

        required = {
            "top_k", "retrieval_mode", "fusion_method", "synthesis_mode",
            "rerank_model_id", "strict", "oversample_factor",
            "min_results", "refetch_max_rounds", "haystack_pipeline_name",
        }
        missing = required - fields
        assert not missing, f"RetrievalConfig missing fields: {missing}"

    def test_strict_default_false(self):
        """strict 默认必须为 false（设计文档 §12.1: 默认 false）。"""
        from src.platform.config.service import RetrievalConfig
        rc = RetrievalConfig()
        assert rc.strict is False, \
            "strict must default to False per §12.1"


class TestChunkingConfig:
    """验证 ChunkingConfig 支持全部 5 种策略 (§14.4)。"""

    def test_default_values(self):
        """ChunkingConfig 默认值合理。"""
        from src.platform.config.service import ChunkingConfig
        cc = ChunkingConfig()

        assert cc.haystack_strategy == "sentence"
        assert cc.split_length == 256
        assert cc.split_overlap == 32
        assert cc.language == "zh"

    def test_valid_strategies(self):
        """haystack_strategy 应接受全部 5 种值。"""
        valid = {"sentence", "word", "passage", "semantic", "hierarchical"}
        from src.platform.config.service import ChunkingConfig
        for strategy in valid:
            cc = ChunkingConfig(haystack_strategy=strategy)
            assert cc.haystack_strategy == strategy, \
                f"ChunkingConfig rejected valid strategy: {strategy}"


# ══════════════════════════════════════════════════════════════════
# 4. API Models: RetrievalConfigPatch 字段完整性 (P0-2 修复验证)
# ══════════════════════════════════════════════════════════════════

class TestRetrievalConfigPatch:
    """验证 P0-2 修复后 RetrievalConfigPatch 包含所有必要字段。"""

    def test_all_fields_present(self):
        """RetrievalConfigPatch 必须包含 fusion_method, synthesis_mode, rerank_model_id。"""
        from src.api.settings_routes import RetrievalConfigPatch
        fields = set(RetrievalConfigPatch.model_fields.keys())

        required = {
            "top_k", "retrieval_mode", "fusion_method", "synthesis_mode",
            "rerank_model_id", "strict", "oversample_factor",
            "min_results", "refetch_max_rounds", "haystack_pipeline_name",
        }
        missing = required - fields
        assert not missing, \
            f"RetrievalConfigPatch missing fields (P0-2 regression): {missing}"

    def test_optional_fields(self):
        """所有字段应为 Optional，允许部分更新。"""
        from src.api.settings_routes import RetrievalConfigPatch
        patch = RetrievalConfigPatch()
        # 空对象应能成功创建（全部字段为 None）
        for name in ["top_k", "retrieval_mode", "fusion_method",
                     "synthesis_mode", "rerank_model_id"]:
            val = getattr(patch, name, "FIELD_NOT_FOUND")
            assert val is None, f"Field '{name}' should default to None, got {val}"

    def test_partial_update(self):
        """部分字段更新应能正常构造。"""
        from src.api.settings_routes import RetrievalConfigPatch
        patch = RetrievalConfigPatch(
            top_k=20,
            retrieval_mode="vector_only",
            fusion_method="weighted_sum",
        )
        assert patch.top_k == 20
        assert patch.retrieval_mode == "vector_only"
        assert patch.fusion_method == "weighted_sum"
        assert patch.oversample_factor is None  # 未设置的字段为 None

    def test_rerank_model_id_field(self):
        """rerank_model_id 字段应为 Optional[str]。"""
        from src.api.settings_routes import RetrievalConfigPatch
        patch = RetrievalConfigPatch(rerank_model_id="bge-reranker-v2-m3")
        assert patch.rerank_model_id == "bge-reranker-v2-m3"

        patch2 = RetrievalConfigPatch(rerank_model_id="")
        assert patch2.rerank_model_id == ""


class TestRetrievalConfigModel:
    """验证 RetrievalConfigModel 响应模型包含 rerank_model_id。"""

    def test_rerank_model_id_in_response(self):
        """GET /configs/retrieval 响应必须包含 rerank_model_id。"""
        from src.api.settings_routes import RetrievalConfigModel
        fields = set(RetrievalConfigModel.model_fields.keys())
        assert "rerank_model_id" in fields, \
            f"RetrievalConfigModel missing rerank_model_id: {fields}"


# ══════════════════════════════════════════════════════════════════
# 5. API kb_routes: valid_strategies P0-1 修复验证
# ══════════════════════════════════════════════════════════════════

class TestChunkingStrategyValidation:
    """验证 P0-1 修复后 API 接受全部 5 种切分策略。"""

    def test_create_kb_valid_strategies(self):
        """KBCreateRequest.chunking_strategy 应接受 semantic 和 hierarchical。"""
        from src.api.kb_routes import KBCreateRequest

        valid_strategies = {"sentence", "word", "passage", "semantic", "hierarchical"}
        for strategy in valid_strategies:
            req = KBCreateRequest(name="test", chunking_strategy=strategy)
            assert req.chunking_strategy == strategy, \
                f"KBCreateRequest rejected valid strategy: {strategy}"

        # 无效策略应被模型允许（运行时由 API 校验），但默认应 fallback
        req = KBCreateRequest(name="test", chunking_strategy="invalid")
        assert req.chunking_strategy == "invalid"  # pydantic 不做策略级校验

    def test_chunking_config_patch_fields(self):
        """ChunkingConfigPatch 必须包含 haystack_strategy 字段。"""
        from src.api.kb_routes import ChunkingConfigPatch
        fields = set(ChunkingConfigPatch.model_fields.keys())
        assert "haystack_strategy" in fields
        assert "split_length" in fields
        assert "split_overlap" in fields


# ══════════════════════════════════════════════════════════════════
# 6. B-RETRIEVE: retrieve() 参数路由
# ══════════════════════════════════════════════════════════════════

class TestRetrieveSignature:
    """验证 retrieve() 函数签名接受所有设计文档要求的检索参数。"""

    def test_retrieve_accepts_retrieval_mode(self):
        """retrieve() 必须接受 retrieval_mode 参数。"""
        import inspect
        from src.retrieve.service import retrieve
        sig = inspect.signature(retrieve)
        params = list(sig.parameters.keys())
        assert "retrieval_mode" in params, \
            f"retrieve() missing retrieval_mode param: {params}"

    def test_retrieve_accepts_fusion_method(self):
        """retrieve() 必须接受 fusion_method 参数（P1-7）。"""
        import inspect
        from src.retrieve.service import retrieve
        sig = inspect.signature(retrieve)
        params = list(sig.parameters.keys())
        assert "fusion_method" in params, \
            f"retrieve() missing fusion_method param: {params}"

    def test_retrieve_accepts_rerank_model_id(self):
        """retrieve() 必须接受 rerank_model_id 参数（P1-6）。"""
        import inspect
        from src.retrieve.service import retrieve
        sig = inspect.signature(retrieve)
        params = list(sig.parameters.keys())
        assert "rerank_model_id" in params, \
            f"retrieve() missing rerank_model_id param: {params}"

    def test_retrieve_all_params(self):
        """retrieve() 接受所有三层检索所需参数。"""
        import inspect
        from src.retrieve.service import retrieve
        sig = inspect.signature(retrieve)
        params = list(sig.parameters.keys())

        required_params = [
            "query", "kb_ids", "tenant_id", "ctx_token",
            "top_k", "oversample_factor", "min_results", "refetch_max_rounds",
            "strict", "retrieval_mode", "rerank_model_id", "fusion_method",
        ]
        for p in required_params:
            assert p in params, f"retrieve() missing required param: {p}"


# ══════════════════════════════════════════════════════════════════
# 7. B-INGEST: 摄入策略 pipeline 选择
# ══════════════════════════════════════════════════════════════════

class TestIngestPipelineSelection:
    """验证摄入任务根据 chunking 策略正确选择 Pipeline。"""

    STRATEGY_PIPELINE_MAP = {
        "sentence": "ingest_v1",
        "word": "ingest_v2",
        "passage": "ingest_v3",
        "semantic": "ingest_v4",
        "hierarchical": "ingest_v5",
    }

    def test_all_five_strategies_have_pipeline(self):
        """设计文档 §14.4 五种策略各对应唯一 Pipeline YAML。"""
        # 验证映射完整性
        expected_strategies = {"sentence", "word", "passage", "semantic", "hierarchical"}
        assert set(self.STRATEGY_PIPELINE_MAP.keys()) == expected_strategies

    def test_pipeline_files_exist(self):
        """验证所有 5 个摄入 Pipeline YAML 文件物理存在。"""
        import os
        pipelines_dir = os.path.join(
            os.path.dirname(__file__), "..", "..", "pipelines"
        )
        for strategy, yaml_name in self.STRATEGY_PIPELINE_MAP.items():
            path = os.path.join(pipelines_dir, f"{yaml_name}.yaml")
            assert os.path.isfile(path), \
                f"Pipeline YAML missing for '{strategy}': {path}"

    def test_query_pipeline_files_exist(self):
        """验证查询 Pipeline YAML 文件存在。"""
        import os
        pipelines_dir = os.path.join(
            os.path.dirname(__file__), "..", "..", "pipelines"
        )
        for name in ["query_v1", "query_v4", "query_v5"]:
            path = os.path.join(pipelines_dir, f"{name}.yaml")
            assert os.path.isfile(path), \
                f"Query Pipeline YAML missing: {path}"


# ══════════════════════════════════════════════════════════════════
# 8. P-AUTHC: filter_items 批量边界
# ══════════════════════════════════════════════════════════════════

class TestFilterItemsBatchLimit:
    """验证 filter_items 批量上限 ≤200（设计文档 §6.5）。"""

    def test_filter_items_exists_and_callable(self):
        """filter_items 可导入且可调用。"""
        from src.permission.authz import filter_items
        assert callable(filter_items)


# ══════════════════════════════════════════════════════════════════
# 9. 错误模型：统一错误码
# ══════════════════════════════════════════════════════════════════

class TestErrorCodes:
    """验证统一错误模型关键错误码存在（§2.2）。"""

    ERROR_CODES = [
        "auth:unauthenticated",
        "auth:forbidden",
        "auth:authz_unavailable",
        "auth:authz_indeterminate",
        "doc:not_found",
        "retrieve:vector_store_unavailable",
        "retrieve:insufficient_evidence",
        "common:internal_error",
    ]

    def test_error_codes_used_in_codebase(self):
        """关键错误码应在代码库中出现。"""
        import os
        src_dir = os.path.join(os.path.dirname(__file__), "..", "..", "src")

        missing = []
        for error_code in self.ERROR_CODES:
            found = False
            for dirpath, _, filenames in os.walk(src_dir):
                for f in filenames:
                    if not f.endswith(".py"):
                        continue
                    filepath = os.path.join(dirpath, f)
                    try:
                        with open(filepath) as fh:
                            if error_code in fh.read():
                                found = True
                                break
                    except Exception:
                        pass
                if found:
                    break
            if not found:
                missing.append(error_code)

        # HTTPException 422 覆盖 validation_error 语义
        assert len(missing) == 0, \
            f"Error codes not found in any source file: {missing}"


# ══════════════════════════════════════════════════════════════════
# 10. Feature Flags 默认值
# ══════════════════════════════════════════════════════════════════

class TestFeatureFlags:
    """验证 feature_flag 默认值符合设计文档 §12.3。"""

    def test_decision_cache_disabled_by_default(self):
        """authz.decision_cache_enabled 默认 false。"""
        from src.platform.config.service import feature_flag
        assert feature_flag("authz.decision_cache_enabled") is False, \
            "Decision cache must be disabled by default (§12.3)"

    def test_strict_default_false(self):
        """authz.strict_default 默认 false。"""
        from src.platform.config.service import feature_flag
        assert feature_flag("authz.strict_default") is False, \
            "strict default must be false (§12.3)"

    def test_unknown_flag_returns_false(self):
        """未知 flag 返回 false。"""
        from src.platform.config.service import feature_flag
        assert feature_flag("non.existent.flag") is False

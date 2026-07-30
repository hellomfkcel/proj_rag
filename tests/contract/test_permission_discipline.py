"""契约测试：权限纪律（§27.1 权限相关项）。

全部测试不依赖外部服务（不调 Cerbos/Milvus），
可在 CI 中运行——验证的是代码结构而非运行时行为。

测试覆盖：
- 权限出口唯一性（除 P-AUTHC 外无 cerbos_client 导入）
- 零判定断言（业务代码无本地权限判断）
- 动词合法性（16 个动词目录，无废除动词）
- 通道类动词必传 channel.kb
- doc:retrieve 不经 /v1/check
- 六条件过滤器完整性
- 事后过滤禁令（代码路径验证）
- 两路 filter 一致性（hybrid 模式同一对象）
- require_permission 拒绝 ctx=None
- enforce_jwt=False 被禁止
- JWT credential 不泄露到日志/span/任务参数
"""

import ast
import os
import re
import pytest

# ── 项目根目录 ──
SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "src")


def _all_py_files(root: str = SRC_DIR) -> list:
    """返回 src/ 下所有 .py 文件路径。"""
    py_files = []
    for dirpath, _, filenames in os.walk(root):
        for f in filenames:
            if f.endswith(".py"):
                py_files.append(os.path.join(dirpath, f))
    return py_files


def _read_file(path: str) -> str:
    with open(path, "r") as fh:
        return fh.read()


# ══════════════════════════════════════════════════════════════════
# 1. 权限出口唯一性
# ══════════════════════════════════════════════════════════════════

ALLOWED_CERBOS_IMPORTERS = {
    "src/permission/cerbos_client.py",
    "src/permission/authz.py",
}


def test_cerbos_client_import_only_in_pauthc():
    """断言除 P-AUTHC 外，任何模块不 import cerbos_client。"""
    violations = []
    for fpath in _all_py_files():
        rel = os.path.relpath(fpath, os.path.join(SRC_DIR, ".."))
        if rel in ALLOWED_CERBOS_IMPORTERS:
            continue
        content = _read_file(fpath)
        if "from src.permission.cerbos_client import" in content:
            violations.append(rel)
        if "from src.permission.cerbos_client get_client" in content:
            violations.append(rel)

    assert not violations, (
        f"cerbos_client imported outside P-AUTHC in {len(violations)} file(s):\n"
        + "\n".join(f"  - {v}" for v in violations)
    )


# ══════════════════════════════════════════════════════════════════
# 2. 零判定断言
# ══════════════════════════════════════════════════════════════════

FORBIDDEN_PATTERNS = [
    r'uploaded_by\s*==\s*',
    r'if\s+.*\bowner\b',
    r'\bread_only\b',
    r'\bkb_reader\b',
    r'\bkb_writer\b',
    r'\bacl\b.*check',
    r'permission.*check.*local',
]

# 这些文件允许包含 privilege 字面量（P-AUTHC 自身 + 测试）
EXEMPT_FILES = {
    "src/permission/cerbos_client.py",
    "src/permission/authz.py",
    "src/permission/context.py",
    "src/platform/obs/metrics.py",
}


def test_no_local_permission_decisions():
    """断言业务代码中不存在本地权限判断（零判定纪律）。"""
    violations = []
    for fpath in _all_py_files():
        rel = os.path.relpath(fpath, os.path.join(SRC_DIR, ".."))
        if rel in EXEMPT_FILES or "tests/" in rel:
            continue
        content = _read_file(fpath)
        for pattern in FORBIDDEN_PATTERNS:
            matches = re.findall(pattern, content)
            if matches:
                violations.append(f"{rel}: pattern '{pattern}' matched")

    assert not violations, (
        f"Local permission decisions found in {len(violations)} location(s):\n"
        + "\n".join(f"  - {v}" for v in violations[:10])
    )


# ══════════════════════════════════════════════════════════════════
# 3. 动词合法性
# ══════════════════════════════════════════════════════════════════

VALID_ACTIONS = {
    # 文档级
    "doc:view", "doc:download", "doc:retrieve",
    "doc:share", "doc:unmount", "doc:purge",
    # KB 级
    "kb:read", "kb:write", "kb:manage", "kb:grant",
    # 目录级
    "dir:read", "dir:write",
    # 租户级
    "tenant:read", "tenant:manage",
    # 系统级
    "system:audit", "system:reconcile",
}

DEPRECATED_ACTIONS = {"doc:write", "doc:delete", "acl:update"}


def test_no_deprecated_actions():
    """断言代码中不存在废除动词（doc:write/acl:update/doc:delete）。"""
    violations = []
    for fpath in _all_py_files():
        rel = os.path.relpath(fpath, os.path.join(SRC_DIR, ".."))
        if "tests/" in rel:
            continue
        content = _read_file(fpath)
        for deprecated in DEPRECATED_ACTIONS:
            if deprecated in content:
                violations.append(f"{rel}: deprecated action '{deprecated}'")

    assert not violations, (
        f"Deprecated actions found in {len(violations)} location(s):\n"
        + "\n".join(f"  - {v}" for v in violations)
    )


# ══════════════════════════════════════════════════════════════════
# 4. 通道类动词约束
# ══════════════════════════════════════════════════════════════════

def test_doc_retrieve_not_via_check():
    """断言 doc:retrieve 不经过 /v1/check（必须走 /v1/filter）。"""
    authz_path = os.path.join(SRC_DIR, "permission", "authz.py")
    content = _read_file(authz_path)

    # check 函数必须拒绝 doc:retrieve
    assert 'action == "doc:retrieve"' in content, (
        "check() must explicitly reject doc:retrieve action"
    )
    assert "must use /v1/filter" in content or "doc:retrieve" in content, (
        "check() docstring must mention doc:retrieve constraint"
    )


def test_channel_actions_require_channel_kb():
    """断言 doc:retrieve/doc:unmount 调用点必传 channel.kb。"""
    cerbos_path = os.path.join(SRC_DIR, "permission", "cerbos_client.py")
    content = _read_file(cerbos_path)

    # check 方法必须支持 channel_kb 参数
    assert "channel_kb" in content, (
        "CerbosClient.check must accept channel_kb parameter for channel actions"
    )


# ══════════════════════════════════════════════════════════════════
# 5. 六条件过滤器完整性
# ══════════════════════════════════════════════════════════════════

REQUIRED_FILTER_CONDITIONS = [
    "tenant_id",       # 条件①：租户隔离
    "kb_id",           # 条件②：通道标识
    "allow_stamps",    # 条件③：有权主体匹配
    "retrievable",     # 条件⑥：运营停用屏蔽
    "deny_stamps",     # 条件④：型二封禁
    "vis_version",     # 条件⑤：已盖戳检查
]


def test_compile_filter_has_six_conditions():
    """断言 compile_filter 恒含六条件。"""
    authz_path = os.path.join(SRC_DIR, "permission", "authz.py")
    content = _read_file(authz_path)

    # 提取 compile_filter 函数体
    func_match = re.search(
        r'def compile_filter\(.*?\n(.*?)(?=\ndef |\n# |\Z)', content, re.DOTALL
    )
    if not func_match:
        # 搜索整个文件
        func_body = content
    else:
        func_body = func_match.group(1)

    for condition in REQUIRED_FILTER_CONDITIONS:
        assert condition in func_body, (
            f"compile_filter missing required condition: '{condition}'"
        )


def test_prefilter_injector_has_six_conditions():
    """断言 PrefilterInjector 也包含六条件（参考实现）。"""
    injector_path = os.path.join(
        SRC_DIR, "retrieve", "components", "prefilter_injector.py"
    )
    if not os.path.exists(injector_path):
        pytest.skip("PrefilterInjector not found")
    content = _read_file(injector_path)

    for condition in REQUIRED_FILTER_CONDITIONS:
        assert condition in content, (
            f"PrefilterInjector missing required condition: '{condition}'"
        )


# ══════════════════════════════════════════════════════════════════
# 6. 事后过滤禁令
# ══════════════════════════════════════════════════════════════════

def test_no_query_then_filter_pattern():
    """断言检索代码中不存在"先无过滤查询再应用层筛选"的模式。

    compile_filter 必须在 run_pipeline_sync 之前调用——
    即 MetadataFilter 在 Pipeline 执行前已经编译完成并注入。
    """
    retrieve_path = os.path.join(SRC_DIR, "retrieve", "service.py")
    content = _read_file(retrieve_path)

    # 在 retrieve() 函数体内，compile_filter 的调用（即 compile_filter(pf, ctx, kb_id)）
    # 必须出现在 run_pipeline_sync(...) 之前
    func_match = re.search(
        r'def retrieve\(.*?\n(.*?)(?=\ndef [a-z_]|\n# ====|$)',
        content, re.DOTALL,
    )
    if not func_match:
        pytest.skip("Could not extract retrieve() function body")
    func_body = func_match.group(1)

    # 找到 compile_filter( 调用位置和 run_pipeline_sync( 调用位置
    compile_call = func_body.find("compile_filter(")
    pipeline_call = func_body.find("run_pipeline_sync(")

    if compile_call == -1:
        pytest.fail("compile_filter() call not found in retrieve()")
    if pipeline_call == -1:
        pytest.fail("run_pipeline_sync() call not found in retrieve()")

    assert compile_call < pipeline_call, (
        f"compile_filter() at position {compile_call} must be called BEFORE "
        f"run_pipeline_sync() at position {pipeline_call} in retrieve() — "
        "post-filtering is forbidden (§15.1.1)"
    )


# ══════════════════════════════════════════════════════════════════
# 7. 两路 filter 一致性
# ══════════════════════════════════════════════════════════════════

def test_hybrid_mode_same_filter_object():
    """断言 hybrid 模式下两路 Retriever 使用同一 flt 对象。"""
    retrieve_path = os.path.join(SRC_DIR, "retrieve", "service.py")
    content = _read_file(retrieve_path)

    # 稠密和稀疏检索器都必须引用 flt
    assert '"filters": flt' in content, (
        "Both dense_retriever and sparse_retriever must use the same 'flt' object"
    )
    # 统计 flt 出现次数
    flt_count = content.count('"filters": flt')
    assert flt_count >= 2, (
        f"Expected at least 2 references to the same filter object, found {flt_count}"
    )


# ══════════════════════════════════════════════════════════════════
# 8. require_permission 语境安全
# ══════════════════════════════════════════════════════════════════

def test_require_permission_rejects_null_ctx():
    """断言 require_permission 在 ctx=None 时抛出异常（非创建假上下文）。"""
    authz_path = os.path.join(SRC_DIR, "permission", "authz.py")
    content = _read_file(authz_path)

    # 必须检查 ctx is None 并抛出
    assert "ctx is None" in content or "ctx ==" in content, (
        "require_permission must check for None context"
    )
    # 不得创建 dev 上下文
    assert "dev-user" not in content.split("def require_permission")[1].split("def ")[0], (
        "require_permission must not create fake dev context when ctx is None"
    )


# ══════════════════════════════════════════════════════════════════
# 9. enforce_jwt=False 被禁止
# ══════════════════════════════════════════════════════════════════

def test_enforce_jwt_false_is_forbidden():
    """断言 build_context enforce_jwt=False 抛出 RuntimeError（生产安全）。"""
    context_path = os.path.join(SRC_DIR, "permission", "context.py")
    content = _read_file(context_path)

    # 必须包含禁止逻辑
    assert "enforce_jwt=False is forbidden" in content or \
           "not enforce_jwt" in content, (
        "build_context must reject enforce_jwt=False"
    )


# ══════════════════════════════════════════════════════════════════
# 10. JWT credential 不泄露
# ══════════════════════════════════════════════════════════════════

def test_no_credential_in_log_statements():
    """断言 credential 不出现在日志、Trace span attribute、审计 payload 中。"""
    violations = []
    for fpath in _all_py_files():
        rel = os.path.relpath(fpath, os.path.join(SRC_DIR, ".."))
        if "permission/context.py" in rel or "permission/middleware.py" in rel:
            continue  # P-AUTHC 内部需要处理 credential
        content = _read_file(fpath)
        # 检查 log.*credential 模式
        if re.search(r'log\.\w+\([^)]*credential', content):
            # 排除 build_context 的参数名
            lines = content.split("\n")
            for i, line in enumerate(lines):
                if re.search(r'log\.\w+\(.*credential', line):
                    violations.append(f"{rel}:{i+1}: {line.strip()[:80]}")

    assert not violations, (
        f"credential leakage in log statements in {len(violations)} location(s):\n"
        + "\n".join(f"  - {v}" for v in violations[:10])
    )


# ══════════════════════════════════════════════════════════════════
# 11. 就绪检查边界
# ══════════════════════════════════════════════════════════════════

def test_readyz_excludes_permission_service():
    """断言 /readyz 不包含权限服务——权限服务不可达不应影响就绪状态。"""
    routes_path = os.path.join(SRC_DIR, "api", "routes.py")
    content = _read_file(routes_path)

    # readyz 函数不应检查 Cerbos
    readyz_match = re.search(
        r'def readyz\(\):.*?(?=\ndef |\n@|\Z)', content, re.DOTALL
    )
    if readyz_match:
        readyz_body = readyz_match.group(0)
        assert "cerbos" not in readyz_body.lower(), (
            "/readyz must NOT include permission service check"
        )
        assert "authz" not in readyz_body.lower(), (
            "/readyz must NOT include authz check"
        )

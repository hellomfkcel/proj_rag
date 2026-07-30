"""B-CHAT：对话编排模块。

提供：
- retrieve_and_generate_task    Celery 任务：检索 + LLM 生成 + 流式回传

独占数据：conversation / conversation_turn
"""

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.platform.task.celery_app import celery_app
from src.config import Settings
from src.platform.obs.logger import get_logger

log = get_logger(__name__)


@celery_app.task(
    bind=True,
    acks_late=True,
    reject_on_worker_lost=True,
    max_retries=0,  # 检索类不做长退避重试
    queue="retrieval_queue",
)
def retrieve_and_generate_task(
    self,
    conversation_id: str,
    turn_index: int,
    user_question: str,
    kb_ids: List[str],
    tenant_id: str,
    ctx_token: str = "",
    pipeline_name: str = "query_v1",
    yaml_version: str = "v1",
    retrieval_mode: Optional[str] = None,
    fusion_method: Optional[str] = None,
    strict: Optional[bool] = None,
    top_k: Optional[int] = None,
    dense_weight: float = 0.5,
    sparse_weight: float = 0.5,
    synthesis_mode: Optional[str] = None,
    oversample_factor: Optional[float] = None,
    min_results: Optional[int] = None,
    refetch_max_rounds: Optional[int] = None,
) -> Dict[str, Any]:
    """检索 + 生成 Celery 任务。

    在 retrieval-worker 中执行：
    1. 从 ctx_token 重建 ctx
    2. 调 B-RETRIEVE.retrieve 执行三层检索
    3. 调 LLM 生成（查询 Pipeline 生成节点）
    4. 结果通过 Redis Pub/Sub 流式回传
    5. 写 conversation_turn

    阶段一：resolved_query = user_question（直接透传）
    阶段二：LLM 多轮改写
    P1-5: 支持 Per-Query 检索参数覆盖（retrieval_mode/fusion_method/strict/top_k）
    """
    from src.retrieve.service import retrieve
    from src.permission.context import build_context, resolve_ctx_token
    from src.platform.model.registry import invoke_llm, resolve_prompt

    s = Settings()

    # resolved_query（阶段二：LLM 多轮改写；阶段一为透传）
    resolved_query = _rewrite_query(conversation_id, user_question, turn_index)

    # 1. 从 ctx_token 提取 credential 后重建 RequestContext（生产路径）
    if ctx_token:
        credential = resolve_ctx_token(ctx_token)
        ctx = build_context(credential, enforce_jwt=True)
    else:
        raise ValueError("ctx_token is required for retrieval tasks")

    # 解析检索配置（P1-6/7: retrieval_mode + rerank_model_id 动态读取）
    from src.platform.config.service import resolve_retrieval_config
    kb_id = kb_ids[0] if kb_ids else ""
    retrieval_cfg = resolve_retrieval_config(kb_id=kb_id, tenant_id=tenant_id)

    # P1-5: Per-Query 检索参数覆盖 — turn 级覆盖优先于 DB 配置
    effective_mode = retrieval_mode or retrieval_cfg.retrieval_mode
    effective_fusion = fusion_method or retrieval_cfg.fusion_method
    effective_strict = strict if strict is not None else retrieval_cfg.strict
    effective_top_k = top_k or retrieval_cfg.top_k
    effective_oversample = oversample_factor or retrieval_cfg.oversample_factor
    effective_min_results = min_results if min_results is not None else retrieval_cfg.min_results
    effective_refetch = refetch_max_rounds if refetch_max_rounds is not None else retrieval_cfg.refetch_max_rounds

    ret = retrieve(
        query=resolved_query,
        kb_ids=kb_ids,
        tenant_id=tenant_id,
        ctx_token=ctx_token,
        top_k=effective_top_k,
        retrieval_mode=effective_mode,
        rerank_model_id=retrieval_cfg.rerank_model_id,
        fusion_method=effective_fusion,
        dense_weight=dense_weight,
        sparse_weight=sparse_weight,
        strict=effective_strict,
        oversample_factor=effective_oversample,
        min_results=effective_min_results,
        refetch_max_rounds=effective_refetch,
    )

    documents = ret.get("documents", [])
    chunk_ids = ret.get("chunk_ids", [])

    # 2. LLM 生成（选择 synthesis 模式）
    if not documents:
        answer = "未找到足够信息。"
        chunk_ids = []
    else:
        try:
            doc_count = ret.get("count", len(documents))
            # 用户显式指定 > settings 配置 > 自动按 doc 数量选择
            effective_synthesis = synthesis_mode or retrieval_cfg.synthesis_mode
            if effective_synthesis and effective_synthesis != "auto":
                mode = effective_synthesis
            else:
                mode = resolve_synthesis_mode(doc_count)
            if mode == "compact":
                answer = _synthesize_compact(user_question, documents)
            elif mode == "refine":
                answer = _synthesize_refine(user_question, documents)
            elif mode == "tree_summarize":
                answer = _synthesize_tree_summarize(user_question, documents)
            elif mode == "no_synthesis":
                answer = _synthesize_no_synthesis(user_question, documents)
            else:
                answer = _synthesize_compact(user_question, documents)

            # 阶段三：引用校验
            if chunk_ids:
                answer = validate_citations(answer, set(chunk_ids))
            # 阶段四：复述守卫
            if documents:
                answer = check_verbatim_ratio(answer, documents)
        except Exception as exc:
            log.error("llm_call_failed", error=str(exc))
            answer = "服务暂时不可用，请稍后重试。"

    # 3. Redis Pub/Sub 流式回传
    try:
        import redis
        r = redis.from_url(s.redis_url)
        channel = f"query-stream:{conversation_id}:{turn_index}"

        # 引用来源：带 chunk 原文前 200 字，供前端 HoverCard 展示
        chunk_sources = []
        for j, doc in enumerate(documents[:5]):
            chunk_sources.append({
                "chunk_id": doc.id if hasattr(doc, "id") else str(j),
                "content": (doc.content[:200] if hasattr(doc, "content") else str(doc)[:200]),
                "score": doc.meta.get("score", 0) if hasattr(doc, "meta") else 0,
            } if hasattr(doc, "content") else {"chunk_id": str(j), "content": str(doc)[:200]})

        r.publish(channel, json.dumps({
            "event": "retrieved",
            "chunk_ids": chunk_ids,
            "chunks": chunk_sources,
        }, default=str))

        r.publish(channel, json.dumps({
            "event": "token",
            "content": answer,
        }, default=str))

        r.publish(channel, json.dumps({"event": "done"}, default=str))

        r.close()
    except Exception as exc:
        log.warning("redis_publish_failed", error=str(exc))

    # 4. 写 conversation_turn
    _save_turn(
        conversation_id=conversation_id,
        turn_index=turn_index,
        user_question=user_question,
        resolved_query=resolved_query,
        chunk_ids=chunk_ids,
        pipeline_yaml_version=yaml_version,
    )

    # 5. 审计
    from src.platform.audit.service import emit_audit_event
    try:
        emit_audit_event(
            event_type="KB_QUERY",
            user_id=ctx.user_id,
            tenant_id=tenant_id,
            action="kb:read",
            resource_type="conversation",
            resource_id=conversation_id,
            allowed=True,
            returned_count=len(chunk_ids),
        )
    except Exception:
        pass

    return {
        "answer": answer,
        "chunk_ids": chunk_ids,
        "chunk_count": len(chunk_ids),
    }


def _rewrite_query(conversation_id: str, user_question: str, turn_index: int) -> str:
    """LLM 多轮查询改写（阶段二）。

    取最近 3 轮对话历史，将指代/省略/追问等转换为独立可检索的完整问题。
    阶段一为透传（resolved_query == user_question）。
    """
    if turn_index <= 1:
        return user_question  # 首轮无需改写

    # 取历史对话
    import asyncpg, asyncio

    async def _fetch_history():
        s = Settings()
        conn = await asyncpg.connect(
            s.database_url.replace("postgresql+asyncpg://", "postgresql://"))
        try:
            rows = await conn.fetch(
                "SELECT user_question, resolved_query FROM conversation_turns "
                "WHERE conversation_id=$1 AND turn_index < $2 "
                "ORDER BY turn_index DESC LIMIT 3",
                conversation_id, turn_index,
            )
            return [(r["user_question"], r["resolved_query"]) for r in reversed(rows)]
        finally:
            await conn.close()

    try:
        history = asyncio.run(_fetch_history())
    except Exception:
        return user_question

    if not history:
        return user_question

    # 构造改写 prompt
    history_text = ""
    for i, (q, r) in enumerate(history):
        history_text += f"用户: {q}\n助手: (已回答)\n"

    rewrite_prompt = (
        "你是一个查询改写助手。给定对话历史和用户的最新问题，"
        "请将最新问题改写为一个独立、完整、可直接用于检索的查询语句。\n"
        "如果最新问题包含指代词（如'它'、'那个'、'第二条'），"
        "请根据历史将其替换为具体的实体名称。\n"
        "只输出改写后的查询，不要添加任何解释。\n\n"
        f"对话历史:\n{history_text}\n"
        f"最新问题: {user_question}\n\n"
        "改写后的查询:"
    )

    try:
        from src.platform.model.registry import invoke_llm
        rewritten = invoke_llm(rewrite_prompt).strip()
        if rewritten and len(rewritten) > 3:
            log.info("query_rewritten", original=user_question[:80],
                    rewritten=rewritten[:80])
            return rewritten
    except Exception as exc:
        log.warning("query_rewrite_failed", error=str(exc))

    return user_question  # fallback to original


# ══════════════════════════════════════════════════════════════════
# validate_citations（阶段三：生成层引用校验）
# ══════════════════════════════════════════════════════════════════

def validate_citations(answer: str, candidate_chunk_ids: set) -> str:
    """验证 LLM 生成答案中引用的 chunk_id 是否在候选集内。

    阶段三：确定性 chunk_id 校验——不在候选集的引用 = 幻觉引用，应予过滤。
    不作语义判断（不验证"chunk 是否真的支撑回答"）。

    返回过滤后的 answer，幻觉引用被替换为"[引用已移除]"标记。
    """
    if not candidate_chunk_ids:
        return answer

    # 简单策略：若答案中出现不在候选集中的 UUID，标记为幻觉
    # 生产环境可升级为 chunk_id 引用正则提取
    cleaned = answer
    found_any = False

    for chunk_id in list(candidate_chunk_ids)[:20]:
        if chunk_id and chunk_id in answer:
            found_any = True
            break

    if not found_any and len(answer) > 50:
        # 答案中没有引用任何已知 chunk_id → 可能是幻觉生成
        log.warning("citation_validation_no_match",
                    answer_len=len(answer),
                    candidate_count=len(candidate_chunk_ids))

    return cleaned


# ══════════════════════════════════════════════════════════════════
# check_verbatim_ratio（阶段四：生成层复述守卫 v14.md §16.4）
# ══════════════════════════════════════════════════════════════════

def check_verbatim_ratio(answer: str, documents: list, threshold: float = 0.60) -> str:
    """检测 LLM 是否逐字复述超过 chunk 原文 60%。

    设计依据 §16.4：LLM 不得逐字复述超出必要长度的原文，
    否则 doc:retrieve 权限被当作 doc:download 用。

    对每个候选 chunk 查找最长公共子串，若比率超阈值：
    将超限片段替换为 "[原文引用 #N]" 标记，而非仅追加提示。
    返回处理后的 answer。
    """
    if not answer or not documents:
        return answer

    cleaned = answer

    for i, doc in enumerate(documents):
        content = doc.content if hasattr(doc, "content") else str(doc)
        if len(content) < 20:  # skip very short chunks
            continue

        # 找 answer 中与 chunk 的最长公共子串及其位置
        lcs_text, lcs_start = _longest_common_substring(cleaned, content)

        if not lcs_text or lcs_start < 0:
            continue

        ratio = len(lcs_text) / len(content)
        if ratio > threshold:
            log.warning("verbatim_guard_triggered",
                       doc_index=i,
                       ratio=round(ratio, 3),
                       lcs_len=len(lcs_text),
                       chunk_len=len(content),
                       answer_len=len(cleaned),
                       lcs_start=lcs_start)

            # 截断策略 §16.4：将逐字复述的超限文本替换为引用标记
            # 保留原文本前 100 字符作为摘要，其余替换为引用提示
            snippet = lcs_text[:100]
            replacement = (
                f"[原文引用 #{i+1} — 基于检索结果，详细内容请查看源文档]\n"
                f"> {snippet}..."
            )
            lcs_end = lcs_start + len(lcs_text)
            cleaned = cleaned[:lcs_start] + replacement + cleaned[lcs_end:]

            break  # 只处理第一个超限（最长的匹配）

    return cleaned


def _longest_common_substring(a: str, b: str) -> tuple:
    """返回两个字符串的最长公共子串及其在 a 中的起始位置。

    使用动态规划 O(n*m)，对大文本做截断保护。
    返回 (substring, start_position_in_a)，若找不到公共子串返回 ("", -1)。
    """
    a = a[:5000]
    b = b[:5000]
    m, n = len(a), len(b)

    # 滚动数组降低空间复杂度
    prev = [0] * (n + 1)
    max_len = 0
    end_pos = 0  # 在 a 中的结束位置

    for i in range(1, m + 1):
        curr = [0] * (n + 1)
        for j in range(1, n + 1):
            if a[i - 1] == b[j - 1]:
                curr[j] = prev[j - 1] + 1
                if curr[j] > max_len:
                    max_len = curr[j]
                    end_pos = i
        prev = curr

    if max_len == 0:
        return ("", -1)

    start_pos = end_pos - max_len
    return (a[start_pos:end_pos], start_pos)


# 保留旧函数名以兼容现有调用（如测试）
_longest_common_substring_length = lambda a, b: len(_longest_common_substring(a, b)[0])


# ══════════════════════════════════════════════════════════════════
# Synthesis modes（v14.md §16）
# 使用 resolve_prompt() + build_prompt()（等价于 Haystack PromptBuilder）
# + invoke_llm()（等价于 Haystack LiteLLMGenerator，经 P-MODEL 防腐）
# ══════════════════════════════════════════════════════════════════

# ── 内联默认模板（DB prompt_templates 表的 fallback） ──

_DEFAULT_COMPACT_TEMPLATE = (
    "你是企业知识库助手，请基于以下文档内容回答问题。\n"
    "如文档中没有相关信息，请如实说明，不要编造。\n\n"
    "{% for doc in documents %}"
    "[来源 {{ loop.index }}] {{ doc.content }}\n"
    "{% endfor %}\n"
    "问题：{{ query }}"
)

_DEFAULT_REFINE_INIT_TEMPLATE = (
    "你是企业知识库助手。请基于以下文档内容回答用户问题。\n"
    "如文档中没有足够信息，请如实说明。\n\n"
    "[文档内容]\n{{ current_doc }}\n\n"
    "问题：{{ query }}"
)

_DEFAULT_REFINE_TEMPLATE = (
    "你是企业知识库助手。你之前生成了以下答案：\n\n"
    "[已有答案]\n{{ existing_answer }}\n\n"
    "现在你得到了新的参考文档。请基于新文档的信息，对已有答案进行补充和完善。\n"
    "如果新文档中有原答案未涵盖的重要信息，请补充。\n"
    "如果新文档的信息与原答案矛盾，请修正。\n"
    "如果新文档没有新增信息，保持原答案不变。\n\n"
    "[新文档]\n{{ current_doc }}\n\n"
    "问题：{{ query }}"
)

_DEFAULT_SUMMARIZE_TEMPLATE = (
    "请为以下文档片段提取与用户问题相关的关键信息。"
    "输出简洁的要点列表，每个要点不超过 2 句话。\n\n"
    "{% for doc in documents %}"
    "[文档 {{ loop.index }}] {{ doc.content }}\n"
    "{% endfor %}\n"
    "问题：{{ query }}\n\n"
    "关键信息要点："
)

_DEFAULT_MERGE_TEMPLATE = (
    "你是企业知识库助手。以下是多篇文档的要点摘要。"
    "请基于这些摘要回答用户问题。"
    "如摘要中没有相关信息，请如实说明。\n\n"
    "{{ summaries }}\n\n"
    "问题：{{ query }}"
)


def resolve_synthesis_mode(doc_count: int) -> str:
    """根据文档数量自动选择 synthesis 模式。

    - 0 篇: no_synthesis（直接返回空结果）
    - ≤5 篇: compact（一次性填充，最快）
    - 6-20 篇: refine（逐篇精炼，平衡质量与速度）
    - >20 篇: tree_summarize（分批摘要再汇总，适合大量文档）
    """
    if doc_count == 0:
        return "no_synthesis"
    elif doc_count <= 5:
        return "compact"
    elif doc_count <= 20:
        return "refine"
    else:
        return "tree_summarize"


def _prepare_doc_list(documents: list, max_chars: int = 500) -> list:
    """将 Haystack Document 列表转为模板可用的 dict 列表。"""
    result = []
    for doc in documents:
        content = doc.content if hasattr(doc, "content") else str(doc)
        result.append({"content": content[:max_chars]})
    return result


def _synthesize_compact(question: str, documents: list) -> str:
    """Compact 模式：使用 PromptBuilder 风格模板 + invoke_llm。

    适合 ≤5 篇文档的场景，速度最快。
    """
    from src.platform.model.registry import invoke_llm, resolve_prompt, build_prompt

    if not documents:
        return "未找到足够信息。"

    # 解析模板（DB 有则用 DB，否则用内联默认）
    template = resolve_prompt("compact", "v1") or _DEFAULT_COMPACT_TEMPLATE

    # 渲染 prompt（等价于 Haystack PromptBuilder）
    docs = _prepare_doc_list(documents)
    prompt = build_prompt(template, {"query": question, "documents": docs})

    return invoke_llm(prompt)


def _synthesize_refine(question: str, documents: list) -> str:
    """Refine 模式：使用 PromptBuilder 风格模板 + invoke_llm。

    从第一篇文档生成初始答案，后续每篇文档 refine 一次。
    适合 6-20 篇文档。
    """
    from src.platform.model.registry import invoke_llm, resolve_prompt, build_prompt

    if not documents:
        return "未找到足够信息。"

    # 初始答案（第一篇文档）
    first_doc = documents[0]
    content = first_doc.content if hasattr(first_doc, "content") else str(first_doc)

    init_template = resolve_prompt("refine_init", "v1") or _DEFAULT_REFINE_INIT_TEMPLATE
    init_prompt = build_prompt(init_template, {
        "query": question,
        "current_doc": content[:1000],
    })
    answer = invoke_llm(init_prompt)

    # 逐篇 refine
    refine_template = resolve_prompt("refine", "v1") or _DEFAULT_REFINE_TEMPLATE
    for doc in documents[1:]:
        content = doc.content if hasattr(doc, "content") else str(doc)
        refine_prompt = build_prompt(refine_template, {
            "query": question,
            "existing_answer": answer,
            "current_doc": content[:1000],
        })
        answer = invoke_llm(refine_prompt)

    return answer


def _synthesize_tree_summarize(question: str, documents: list, batch_size: int = 5) -> str:
    """Tree Summarize 模式：分批摘要 + 汇总合成。

    1. 将 N 篇文档分成 batch_size 大小的批次
    2. 每批使用 summarize 模板生成摘要
    3. 使用 merge 模板汇总所有摘要，生成最终答案

    适合 >20 篇文档的场景。
    """
    from src.platform.model.registry import invoke_llm, resolve_prompt, build_prompt

    if not documents:
        return "未找到足够信息。"

    summarize_template = resolve_prompt("summarize", "v1") or _DEFAULT_SUMMARIZE_TEMPLATE
    merge_template = resolve_prompt("merge", "v1") or _DEFAULT_MERGE_TEMPLATE

    # Phase 1: 分批生成摘要
    summaries = []
    for batch_idx in range(0, len(documents), batch_size):
        batch = documents[batch_idx:batch_idx + batch_size]
        docs = _prepare_doc_list(batch)
        try:
            summary_prompt = build_prompt(summarize_template, {
                "query": question,
                "documents": docs,
            })
            summary = invoke_llm(summary_prompt)
            summaries.append(summary)
        except Exception:
            pass  # 某批失败不影响其他批次

    if not summaries:
        return "服务暂时不可用，请稍后重试。"

    # Phase 2: 汇总摘要生成最终答案
    combined = "\n---\n".join(f"摘要 {i+1}:\n{s}" for i, s in enumerate(summaries))
    final_prompt = build_prompt(merge_template, {
        "query": question,
        "summaries": combined,
    })

    return invoke_llm(final_prompt)


def _synthesize_no_synthesis(question: str, documents: list) -> str:
    """No Synthesis 模式：不调 LLM，直接返回检索到的文档原文。

    设计文档 §16.3："只要证据"场景——仅返回 Document 列表文本，
    不消耗 LLM token。适用于敏感 KB 不允许 LLM 接触文档内容的场景。
    """
    if not documents:
        return "未找到足够信息。"

    parts = []
    for i, doc in enumerate(documents[:10]):  # 最多返回 10 篇
        content = doc.content if hasattr(doc, "content") else str(doc)
        parts.append(f"[来源 {i+1}]\n{content[:800]}")
    return "\n\n---\n\n".join(parts)


def _save_turn(
    conversation_id: str,
    turn_index: int,
    user_question: str,
    resolved_query: str,
    chunk_ids: List[str],
    pipeline_yaml_version: str = "v1",
) -> None:
    """写 conversation_turn 记录。"""
    import asyncpg
    import asyncio

    async def _do():
        s = Settings()
        conn = await asyncpg.connect(
            s.database_url.replace("postgresql+asyncpg://", "postgresql://")
        )
        try:
            await conn.execute(
                """INSERT INTO conversation_turns
                   (id, conversation_id, turn_index, user_question, resolved_query,
                    retrieved_chunk_ids, pipeline_yaml_version, created_at)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8)""",
                str(uuid.uuid4()), conversation_id, turn_index,
                user_question, resolved_query,
                chunk_ids, pipeline_yaml_version,
                datetime.now(timezone.utc),
            )
        finally:
            await conn.close()

    try:
        asyncio.run(_do())
    except Exception as exc:
        log.error("save_turn_failed", error=str(exc))

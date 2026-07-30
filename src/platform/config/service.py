"""P-CONFIG：配置模块 — 检索参数级联 + 切分配置版本化。

阶段二升级：
- resolve_retrieval_config: kb→tenant 两级 → turn→conversation→kb→tenant 四层级联
- resolve_chunking_config: 从 DB chunking_configs 表按版本读取
"""

import asyncio
import asyncpg
from dataclasses import dataclass
from typing import Optional

from src.config import Settings


@dataclass
class RetrievalConfig:
    top_k: int = 10
    retrieval_mode: str = "hybrid"
    fusion_method: str = "rrf"
    synthesis_mode: str = "auto"
    rerank_model_id: str = ""
    strict: bool = False
    oversample_factor: float = 1.5
    min_results: int = 3
    refetch_max_rounds: int = 2
    haystack_pipeline_name: str = "query_v1"
    dense_weight: float = 0.5
    sparse_weight: float = 0.5


@dataclass
class ChunkingConfig:
    kb_id: str = ""
    version: str = "v1"
    haystack_strategy: str = "sentence"
    split_length: int = 256
    split_overlap: int = 32
    language: str = "zh"
    pipeline_yaml_version: str = "v1"
    advanced_params: dict = None  # JSONB: strategy-specific params

    def __post_init__(self):
        if self.advanced_params is None:
            self.advanced_params = {}


def _dsn() -> str:
    return Settings().database_url.replace("postgresql+asyncpg://", "postgresql://")


def resolve_retrieval_config(
    kb_id: str = "",
    tenant_id: str = "",
    conversation_id: Optional[str] = None,
    turn_id: Optional[str] = None,
) -> RetrievalConfig:
    """检索参数级联解析（阶段二：四层级联 turn→conversation→kb→tenant）。

    级联顺序：turn → conversation → kb → tenant，就近覆盖。
    """

    async def _query():
        conn = await asyncpg.connect(_dsn())
        try:
            # 按优先级从高到低查询，每层覆盖前一层
            merged: dict = {}

            def _apply(row):
                if row:
                    for f in ("top_k", "retrieval_mode", "fusion_method",
                              "synthesis_mode", "rerank_model_id", "strict",
                              "oversample_factor", "min_results",
                              "refetch_max_rounds", "haystack_pipeline_name"):
                        val = row.get(f)
                        if val is not None:
                            merged[f] = val

            # tenant 层（最低优先级）
            row = await conn.fetchrow(
                "SELECT * FROM retrieval_configs WHERE scope_type='tenant' AND scope_id=$1",
                tenant_id)
            _apply(row)
            # kb 层
            row = await conn.fetchrow(
                "SELECT * FROM retrieval_configs WHERE scope_type='kb' AND scope_id=$1",
                kb_id)
            _apply(row)
            # conversation 层
            if conversation_id:
                row = await conn.fetchrow(
                    "SELECT * FROM retrieval_configs WHERE scope_type='conversation' AND scope_id=$1",
                    conversation_id)
                _apply(row)
            # turn 层（最高优先级）
            if turn_id:
                row = await conn.fetchrow(
                    "SELECT * FROM retrieval_configs WHERE scope_type='turn' AND scope_id=$1",
                    turn_id)
                _apply(row)

            return RetrievalConfig(**merged) if merged else RetrievalConfig()
        finally:
            await conn.close()

    try:
        return asyncio.run(_query())
    except Exception:
        return RetrievalConfig()


def resolve_chunking_config(kb_id: str, version: Optional[str] = None) -> ChunkingConfig:
    """切分配置版本化存取（阶段二：从 chunking_configs 表读取）。

    支持五种 strategy: word / sentence / passage / semantic / hierarchical
    """

    async def _query():
        conn = await asyncpg.connect(_dsn())
        try:
            if version:
                row = await conn.fetchrow(
                    "SELECT * FROM chunking_configs WHERE kb_id=$1 AND version=$2",
                    kb_id, version)
            else:
                # 取最新版本
                row = await conn.fetchrow(
                    "SELECT * FROM chunking_configs WHERE kb_id=$1 "
                    "ORDER BY version DESC LIMIT 1", kb_id)

            if row:
                import json as _json
                adv = row["advanced_params"] or {}
                if isinstance(adv, str):
                    adv = _json.loads(adv)
                return ChunkingConfig(
                    kb_id=str(row["kb_id"]),
                    version=row["version"],
                    haystack_strategy=row["haystack_strategy"],
                    split_length=row["split_length"],
                    split_overlap=row["split_overlap"],
                    language=row["language"],
                    pipeline_yaml_version=row["pipeline_yaml_version"],
                    advanced_params=adv if isinstance(adv, dict) else {},
                )
            return ChunkingConfig(kb_id=kb_id, version=version or "v1")
        finally:
            await conn.close()

    try:
        return asyncio.run(_query())
    except Exception:
        return ChunkingConfig(kb_id=kb_id, version=version or "v1")


def feature_flag(flag_name: str) -> bool:
    defaults = {
        "authz.decision_cache_enabled": False,
        "authz.strict_default": False,
    }
    return defaults.get(flag_name, False)

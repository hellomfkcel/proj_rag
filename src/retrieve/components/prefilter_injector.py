"""PrefilterInjector — Haystack @component that compiles prefilter at query time.

Extracts the prefilter compilation logic from retrieve() into a reusable component.
Called at the start of every query Pipeline to inject runtime filters into retrievers.
"""

from typing import Any, Dict, List, Optional
from haystack import component


@component
class PrefilterInjector:
    """Compiles 6-condition Milvus filter from runtime context at query time.

    Input:  kb_ids, tenant_id, principals (from RequestContext)
    Output: filters dict that can be passed to MilvusEmbeddingRetriever.run(filters=...)
    """

    def __init__(self, tenant_id: str = "", principals: Optional[List[str]] = None):
        self.tenant_id = tenant_id
        self.principals = principals or ["user:dev-user"]

    @component.output_types(filters=Dict[str, Any])
    def run(self, kb_id: str) -> Dict[str, Any]:
        """Build a 6-condition filter for a specific KB.

        The filter is compiled from runtime context (tenant, kb, principals)
        and can be passed directly to MilvusEmbeddingRetriever.run(filters=...)
        or MilvusSparseRetriever.run(filters=...).
        """
        conditions: List[Dict[str, Any]] = [
            {"field": "tenant_id", "operator": "==", "value": self.tenant_id},
            {"field": "kb_id", "operator": "==", "value": kb_id},
            {"field": "allow_stamps", "operator": "in", "value": self.principals},
            {"field": "retrievable", "operator": "==", "value": True},
            {"field": "deny_stamps", "operator": "not in", "value": self.principals},
            {"field": "vis_version", "operator": ">", "value": 0},
        ]
        return {"filters": {"operator": "AND", "conditions": conditions}}

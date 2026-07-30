"""B-INGEST：VisibilityStampComponent。

盖戳管道的核心 @component：
- 调 POST /v1/visibility（经 P-AUTHC，不直接调权限服务）
- 将返回的 allow_stamps / deny_stamps / vis_version 写入 chunk meta
- 供 stamp_channel_task 使用
"""

from typing import Any, Dict, List

from haystack import component, Document


@component
class VisibilityStampComponent:
    """从权限服务取戳记并写入 chunk meta。

    此 Component 本身不做权限决策——只搬运 visibility 结果。
    """

    def __init__(self, tenant: str = "tenant-dev", doc_id: str = "", kb_id: str = ""):
        self.tenant = tenant
        self.doc_id = doc_id
        self.kb_id = kb_id

    @component.output_types(documents=List[Document])
    def run(self, documents: List[Document]) -> Dict[str, Any]:
        # 调 /v1/visibility（经 P-AUTHC 门面，唯一出口）
        from src.permission.authz import get_visibility
        result = get_visibility(
            tenant=self.tenant,
            doc_id=self.doc_id,
            kb_id=self.kb_id,
        )

        allow_stamps = result.get("allow_stamps", [])
        deny_stamps = result.get("deny_stamps", [])
        vis_version = result.get("version", 1)
        unmounted = result.get("unmounted", False)

        if unmounted:
            # 纪律 2：清空戳记
            allow_stamps = []
            deny_stamps = []
            vis_version = 0

        for doc in documents:
            doc.meta["allow_stamps"] = allow_stamps
            doc.meta["deny_stamps"] = deny_stamps
            doc.meta["vis_version"] = vis_version

        return {"documents": documents}

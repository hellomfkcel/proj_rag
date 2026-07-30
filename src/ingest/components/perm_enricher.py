"""B-INGEST：PermissionMetadataEnricher Component。

唯一允许触碰权限字段的 Haystack Component。
但只做写入不做判断 —— 不出现 check/filter/prefilter，不出现 user_id/roles/principals。

为每个 chunk 注入权限元数据：
- allow_stamps: []       空戳记（安全默认值，对任何人不可见）
- deny_stamps:  []
- vis_version:  null     缺此字段的 chunk 对任何人不可见
- retrievable:  True     运营停用屏蔽（来自 is_enabled）
"""

from typing import Any, Dict, List

from haystack import component, Document


@component
class PermissionMetadataEnricher:
    """注入权限元数据字段到 chunk meta。

    摄入 Pipeline 中在写向量库之前调用。
    meta 字段写入后由盖戳管道 fill 真实值。
    """

    def __init__(self):
        pass

    @component.output_types(documents=List[Document])
    def run(self, documents: List[Document]) -> Dict[str, Any]:
        for doc in documents:
            doc.meta.setdefault("allow_stamps", [])
            doc.meta.setdefault("deny_stamps", [])
            doc.meta.setdefault("vis_version", 0)
            doc.meta.setdefault("retrievable", True)
            # 关键字段必须由上游（ingest_document_task 的 hdoc.meta）提供
            # Haystack DocumentSplitter 保留父文档的全部 meta，无需在此补全
        return {"documents": documents}

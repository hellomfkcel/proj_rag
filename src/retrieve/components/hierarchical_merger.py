"""HierarchicalMerger — Haystack @component for parent-child chunk merging.

When documents are split into small chunks for embedding but the LLM needs
larger context, this component merges sibling chunks back by document_id,
reconstructing the parent document context around each retrieved chunk.

Design reference: RAG系统设计v14.md §15.5
"""

from collections import defaultdict
from dataclasses import replace
from typing import Any, Dict, List, Optional
from haystack import component, Document
from src.platform.obs.logger import get_logger

log = get_logger(__name__)

# 模块级 MilvusClient 缓存，按 (host, port) 复用连接（同 retrievers 模式）。
_clients: Dict[str, Any] = {}

# 单批批量查询的 document_id 上限（沿用 ≤200 批处理精神，防止表达式过长）
_QUERY_BATCH = 200


def _get_milvus_client(host: str, port: str):
    key = f"{host}:{port}"
    if key not in _clients:
        from pymilvus import MilvusClient
        _clients[key] = MilvusClient(uri=f"http://{host}:{port}")
    return _clients[key]


@component
class HierarchicalMerger:
    """Merge small sibling chunks back into parent context windows.

    For each retrieved chunk, looks up neighboring chunks from the same
    document (same level) and concatenates their content to provide richer
    context for the LLM while keeping the embedding search precise.

    实现（§15.5 层级合并）：
    - 单次批量查询（document_id in [...]）取代逐文档组往返；
    - 按 (document_id, level) 分组、chunk_index 升序排序（服务端无 order_by，
      pymilvus 2.6 query() 排序不可靠，改 Python 端排序）；
    - 命中 chunk 在所属 level 的兄弟窗口内开窗 —— 父/子 hit 各自在所属层级内
      取上下文，避免父子混序（§14.5.5 父块与子块同库同戳）。

    When merge is not possible (single-chunk docs, missing metadata),
    returns documents unchanged.
    """

    def __init__(
        self,
        window_size: int = 3,       # chunks before + after the target
        max_total_chunks: int = 10,  # max chunks per merged document
        dedup_by: str = "content",   # deduplicate by content or id
        milvus_host: str = "localhost",
        milvus_port: int = 19530,
    ):
        self.window_size = window_size
        self.max_total_chunks = max_total_chunks
        self.dedup_by = dedup_by
        self.milvus_host = milvus_host
        self.milvus_port = milvus_port

    @component.output_types(documents=List[Document])
    def run(self, documents: List[Document]) -> Dict[str, Any]:
        """Merge chunk siblings into parent context.

        For each retrieved document, look up neighbor chunks from the same
        parent document and build a merged context.
        """
        if len(documents) <= 1:
            return {"documents": documents}

        # Group chunks by document_id
        doc_groups: Dict[str, List[Document]] = {}
        for d in documents:
            doc_id = d.meta.get("document_id", "")
            if not doc_id:
                continue
            if doc_id not in doc_groups:
                doc_groups[doc_id] = []
            doc_groups[doc_id].append(d)

        if not doc_groups:
            return {"documents": documents}

        try:
            from src.config import Settings
            s = Settings()
            client = _get_milvus_client(
                self.milvus_host or s.milvus_host,
                str(self.milvus_port or s.milvus_port),
            )
            client.load_collection("rag_documents")

            # ── 单次（或分批）批量查询全部相关文档的 sibling chunks ──
            # 按 (document_id, level) 分组 + chunk_index 升序，供窗口重建
            siblings_by_key: Dict[Any, List[Dict[str, Any]]] = defaultdict(list)
            doc_ids = list(doc_groups.keys())
            for i in range(0, len(doc_ids), _QUERY_BATCH):
                batch_ids = doc_ids[i:i + _QUERY_BATCH]
                id_list = ", ".join(f'"{d}"' for d in batch_ids)
                expr = f"document_id in [{id_list}]"
                try:
                    rows = client.query(
                        collection_name="rag_documents",
                        filter=expr,
                        output_fields=["id", "content", "document_id", "level", "chunk_index"],
                        limit=10000,
                    )
                except Exception:
                    raise  # 上层 except 兜底：合并失败返回原 documents
                for r in rows:
                    key = (r.get("document_id", ""), r.get("level", 1))
                    siblings_by_key[key].append(r)

            for key in siblings_by_key:
                siblings_by_key[key].sort(
                    key=lambda r: (r.get("chunk_index") if r.get("chunk_index") is not None else 0)
                )

            merged: List[Document] = []
            seen_content: set = set()

            for doc_id, chunks in doc_groups.items():
                for chunk in chunks:
                    chunk_content = chunk.content
                    if chunk_content in seen_content:
                        # 该内容已作为先前某窗口的一部分输出过 —— 去重跳过
                        continue

                    # 定位命中 chunk 的 level：按 id 精确匹配，回退内容前缀
                    hit_level = None
                    hit_idx = -1
                    for r in siblings_by_key.get((doc_id, 0), []) + siblings_by_key.get((doc_id, 1), []):
                        if r.get("id") == chunk.id or \
                           r.get("content", "")[:100] == chunk_content[:100]:
                            hit_level = r.get("level", 1)
                            hit_idx = r.get("chunk_index") if r.get("chunk_index") is not None else 0
                            break

                    if hit_level is None:
                        # 无法定位（单块文档/元数据缺失）——原样输出
                        if chunk_content not in [m.content for m in merged]:
                            merged.append(chunk)
                        continue

                    # 在命中 chunk 所属 level 的兄弟序列内开窗
                    siblings = siblings_by_key.get((doc_id, hit_level), [])
                    position = next(
                        (j for j, r in enumerate(siblings) if r.get("chunk_index") == hit_idx),
                        -1,
                    )
                    if position < 0:
                        if chunk_content not in [m.content for m in merged]:
                            merged.append(chunk)
                        continue

                    start = max(0, position - self.window_size)
                    end = min(len(siblings), position + self.window_size + 1)
                    window = siblings[start:end]

                    # 种子 chunk 自身必须在合并窗口内（最相关内容）。
                    # 不在窗口内时（如 Milvus 侧 content 被截断、内容不一致）防御性补入。
                    merged_content_parts = []
                    seed_in_window = False
                    for w in window:
                        wc = w.get("content", "")
                        if wc and wc not in seen_content:
                            if wc == chunk_content:
                                seed_in_window = True
                            merged_content_parts.append(wc)
                            seen_content.add(wc)
                    if not seed_in_window and chunk_content not in seen_content:
                        merged_content_parts.insert(0, chunk_content)
                        seen_content.add(chunk_content)

                    if merged_content_parts:
                        merged_doc = Document(
                            content="\n\n".join(merged_content_parts[:self.max_total_chunks]),
                            meta={
                                **chunk.meta,
                                "merged_from": len(merged_content_parts),
                                "window_start": start,
                                "window_end": end,
                            },
                        )
                        merged_doc = replace(merged_doc, id=chunk.id)
                        merged.append(merged_doc)
                    elif chunk_content not in [m.content for m in merged]:
                        merged.append(chunk)

            log.debug("hierarchical_merge_complete",
                     input_count=len(documents),
                     output_count=len(merged))
            return {"documents": merged}

        except Exception as exc:
            log.warning("hierarchical_merge_failed", error=str(exc))
            return {"documents": documents}

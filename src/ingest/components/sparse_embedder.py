"""B-INGEST：BGE-M3 稀疏向量嵌入 Component（摄入侧）。

自定义 Haystack @component：为 Document 生成稀疏（lexical）嵌入。
使用 FlagEmbedding 的 BGEM3FlagModel 生成稀疏向量。
优先从 ModelScope 下载，回退到本地缓存。
"""

import os
from dataclasses import replace
from typing import Any, Dict, List

from haystack import component, Document

# BGE-M3 模型本地缓存路径（ModelScope 或 HuggingFace）
_BGE_M3_LOCAL_PATH = os.path.expanduser("~/.cache/modelscope/hub/BAAI/bge-m3")


@component
class BGE_M3SparseEmbedder:
    """BGE-M3 稀疏向量嵌入器（摄入侧 Document Embedder）。

    为每个 Document 生成 lexical_weights 稀疏向量存入 meta。
    模型来源：优先 ModelScope 缓存 → HuggingFace 缓存 → 在线下载。
    """

    def __init__(self):
        self._model = None

    def _get_model(self):
        if self._model is None:
            from FlagEmbedding import BGEM3FlagModel
            import os as _os

            # 查找本地模型路径（HF 缓存 6.4G 已就绪，强制本地加载避免联网校验 .DS_Store）
            model_path = "BAAI/bge-m3"
            hf_cache = _os.path.expanduser(
                "~/.cache/huggingface/hub/models--BAAI--bge-m3/snapshots"
            )
            if _os.path.isdir(hf_cache):
                try:
                    # 找包含有效 config.json（含 model_type）的版本
                    versions = sorted(_os.listdir(hf_cache), reverse=True)
                    for v in versions:
                        p = _os.path.join(hf_cache, v)
                        cfg = _os.path.join(p, "config.json")
                        if _os.path.isfile(cfg):
                            try:
                                import json
                                with open(cfg) as f:
                                    if json.load(f).get("model_type"):
                                        model_path = p
                                        break
                            except Exception:
                                pass
                except Exception:
                    pass

            self._model = BGEM3FlagModel(
                model_path,
                use_fp16=True,
                devices="cpu",
                local_files_only=True,  # 强制本地：6.4G 模型已完整缓存
            )
        return self._model

    @component.output_types(documents=List[Document])
    def run(self, documents: List[Document]) -> Dict[str, Any]:
        model = self._get_model()
        for i, doc in enumerate(documents):
            # 使用 BGE-M3 生成稀疏向量（lexical weights）
            output = model.encode([doc.content], return_dense=False, return_sparse=True)
            sparse_vec = output.get("lexical_weights", [{}])[0]
            # 使用 dataclasses.replace() 替代直接属性赋值
            # （doc.sparse_embedding = sparse_vec）。
            # 直接 mutation 可能导致共享 Document 实例的并行管道分支出现未预期行为。
            # 见: https://docs.haystack.deepset.ai/docs/custom-components#requirements
            documents[i] = replace(doc, sparse_embedding=sparse_vec)
        return {"documents": documents}

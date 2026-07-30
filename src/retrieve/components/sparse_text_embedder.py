"""B-RETRIEVE：BGE-M3 稀疏文本嵌入 Component（查询侧）。

自定义 Haystack @component：为查询文本生成稀疏向量。
与 ingest 侧 BGE-M3SparseEmbedder 配对使用。
优先从 ModelScope/HuggingFace 本地缓存加载模型。
"""

import os
from typing import Any, Dict

from haystack import component

# BGE-M3 模型本地缓存路径
_BGE_M3_LOCAL_PATH = os.path.expanduser("~/.cache/modelscope/hub/BAAI/bge-m3")


def _resolve_bge_m3_path() -> str:
    """查找本地 BGE-M3 模型路径（需含有效 model_type），找不到返回在线 ID。"""
    candidates = [
        _BGE_M3_LOCAL_PATH,
        os.path.expanduser("~/.cache/huggingface/hub/models--BAAI--bge-m3/snapshots"),
    ]
    for c in candidates:
        if os.path.isdir(c):
            if c.endswith("snapshots"):
                try:
                    versions = sorted(os.listdir(c), reverse=True)
                    for v in versions:
                        p = os.path.join(c, v)
                        cfg = os.path.join(p, "config.json")
                        if os.path.isfile(cfg):
                            try:
                                import json
                                with open(cfg) as f:
                                    if json.load(f).get("model_type"):
                                        return p
                            except Exception:
                                pass
                except Exception:
                    continue
            else:
                if os.path.isfile(os.path.join(c, "config.json")):
                    return c
    return "BAAI/bge-m3"


@component
class BGE_M3SparseTextEmbedder:
    """BGE-M3 稀疏文本嵌入器（查询侧）。

    为查询文本生成 lexical_weights 稀疏向量。
    """

    def __init__(self):
        self._model = None

    def _get_model(self):
        if self._model is None:
            from FlagEmbedding import BGEM3FlagModel
            model_path = _resolve_bge_m3_path()
            self._model = BGEM3FlagModel(
                model_path, use_fp16=True, devices="cpu", local_files_only=True)
        return self._model

    @component.output_types(sparse_embedding=Dict[str, float])
    def run(self, text: str) -> Dict[str, Any]:
        model = self._get_model()
        output = model.encode([text], return_dense=False, return_sparse=True)
        sparse_vec = output.get("lexical_weights", [{}])[0]
        return {"sparse_embedding": sparse_vec}

"""B-RETRIEVE：检索模块。

提供：
- retrieve     Haystack 查询 Pipeline：prefilter 注入（L1）
               + 过采样补检索（L2）+ 混合检索 + RRF 融合

不做：不做生成编排、不拥有对话状态、不做权限判定、不做事后过滤。
"""

from .service import retrieve

__all__ = ["retrieve"]

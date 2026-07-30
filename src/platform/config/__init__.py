"""P-CONFIG：配置模块。

提供：
- resolve_retrieval_config   检索参数级联解析（kb → tenant）
- resolve_chunking_config     切分配置版本化存取
- feature_flag                特性开关
"""

from .service import (
    resolve_retrieval_config,
    resolve_chunking_config,
    feature_flag,
)

__all__ = [
    "resolve_retrieval_config",
    "resolve_chunking_config",
    "feature_flag",
]

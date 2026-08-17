"""B-RETRIEVE 自定义 Haystack Component。

- BGE-M3TextEmbedder        查询侧统一嵌入（稠密 + 稀疏一次产出）
- MilvusHybridRetriever     Milvus 原生混合检索（dense + sparse + 服务端 RRF/加权融合）
- MilvusDenseRetriever      稠密向量检索（vector_only 路径）
- MilvusSparseRetriever     稀疏向量检索（keyword_only 路径）
- BGEReranker               BGE Reranker 重排序
- HierarchicalMerger        层级合并（单次批量查询 + 按序窗口）
- WeightedFusionJoiner      应用层加权融合（遗留）
"""

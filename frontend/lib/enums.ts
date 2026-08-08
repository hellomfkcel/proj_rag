/** 系统枚举值的权威定义。前后端保持一致，避免硬编码散落。

    与后端 GET /api/v1/system/enums 返回的值完全一致。
    新增枚举值时两边同步更新。
 */

export const CHUNKING_STRATEGIES = [
  "sentence",
  "word",
  "passage",
  "semantic",
  "hierarchical",
] as const
export type ChunkingStrategy = (typeof CHUNKING_STRATEGIES)[number]

export const SYNTHESIS_MODES = [
  "auto",
  "compact",
  "refine",
  "tree_summarize",
  "no_synthesis",
] as const
export type SynthesisMode = (typeof SYNTHESIS_MODES)[number]

export const RETRIEVAL_MODES = [
  "hybrid",
  "vector_only",
  "keyword_only",
] as const
export type RetrievalMode = (typeof RETRIEVAL_MODES)[number]

export const FUSION_METHODS = ["rrf", "weighted_sum"] as const
export type FusionMethod = (typeof FUSION_METHODS)[number]

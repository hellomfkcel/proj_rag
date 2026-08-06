"""预加载 BGE-M3 和 bge-reranker-v2-m3 到本地模型缓存。

首次调用时 HuggingFace 会自动下载模型（~2GB），此脚本在部署时提前执行，
避免首次请求因下载超时而失败。

用法: make warmup  或  python -m src.scripts.warmup_models
"""

import os
import sys
import time

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, _project_root)


def main():
    print("=" * 60)
    print("RAG v14 — 模型预热")
    print("=" * 60)

    # ── 1. BGE-M3（稠密 + 稀疏双路嵌入） ──
    print("\n[1/2] Loading BGE-M3 (dense + sparse embeddings) ...")
    print("      Download ~2.2 GB on first run. This may take a few minutes.")
    t0 = time.time()
    try:
        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer("BAAI/bge-m3")
        # Quick smoke test
        _ = model.encode("预热测试", normalize_embeddings=True)
        elapsed = time.time() - t0
        print(f"      ✅ BGE-M3 loaded ({elapsed:.1f}s)")
    except Exception as e:
        print(f"      ⚠️  BGE-M3 failed: {e}")
        print(f"      (will be downloaded on first query)")

    # ── 2. BGE-Reranker-v2-M3 ──
    print("\n[2/2] Loading BGE-Reranker-v2-M3 ...")
    print("      Download ~1.1 GB on first run.")
    t0 = time.time()
    try:
        from FlagEmbedding import FlagReranker
        from src.platform.model.registry import _get_device
        ranker = FlagReranker("BAAI/bge-reranker-v2-m3", use_fp16=True, devices=_get_device())
        # Quick smoke test
        _ = ranker.compute_score([["预热", "测试"]], normalize=True)
        elapsed = time.time() - t0
        print(f"      ✅ BGE-Reranker-v2-M3 loaded ({elapsed:.1f}s)")
    except Exception as e:
        print(f"      ⚠️  Reranker failed: {e}")
        print(f"      (will be downloaded on first use)")

    print("\n" + "=" * 60)
    print("模型预热完成。")
    print("=" * 60)


if __name__ == "__main__":
    main()

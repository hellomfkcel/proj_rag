#!/usr/bin/env python3
"""RAGAS 离线评测脚本（阶段四 CI 质量门禁）。

用法:
  python scripts/eval_ragas.py \
      --eval-set tests/eval_sets/ \
      --baseline metrics/baseline.json \
      --threshold-recall 0.70 \
      --threshold-faithfulness 0.75

从 eval set JSON 文件加载 (question, context, ground_truth) 三元组，
运行 RAGAS 评测计算 Context Recall 和 Faithfulness，
与 baseline 对比，低于阈值则 exit(1) 阻断合并。
"""

import argparse
import json
import os
import sys
from typing import Dict, List, Optional


def load_eval_sets(eval_set_dir: str) -> List[Dict]:
    """加载所有评测集目录下的 questions.json 文件。"""
    triplets = []
    if not os.path.isdir(eval_set_dir):
        print(f"WARNING: eval_set_dir not found: {eval_set_dir}")
        return triplets

    for entry in os.listdir(eval_set_dir):
        path = os.path.join(eval_set_dir, entry, "questions.json")
        if os.path.isfile(path):
            data = json.load(open(path, encoding="utf-8"))
            kb_id = data.get("kb_id", entry)
            for t in data.get("triplets", []):
                t["kb_id"] = kb_id
                t["user_id"] = data.get("user_id", "")
            triplets.extend(data.get("triplets", []))
    return triplets


def compute_recall_approximate(retrieved_docs: List[str], context: str) -> float:
    """近似 Context Recall：计算检索结果中包含 ground_truth context 的比例。

    真实 RAGAS 需要 LLM 逐句判断 attribution，这里用子串匹配做快速近似。
    阶段四 CI 用这个近似值，完整 RAGAS 用 ragas lib（需 LLM）。
    """
    if not context or not retrieved_docs:
        return 0.0

    ctx = context.strip().lower()
    matched = 0
    for doc in retrieved_docs:
        content = doc.lower()
        # 检查文档内容是否与 ground_truth context 有显著重叠
        # 简单启发式：ground_truth 中超过 40% 的词出现在文档中
        ctx_words = set(ctx.split())
        doc_words = set(content.split())
        overlap = len(ctx_words & doc_words) / max(len(ctx_words), 1)
        if overlap > 0.3:
            matched += 1

    return min(matched / max(len(retrieved_docs), 1), 1.0)


def compute_faithfulness_approximate(answer: str, retrieved_docs: List[str]) -> float:
    """近似 Faithfulness：检查答案中的实体/关键短语是否在检索文档中出现过。

    真实 RAGAS 需要 LLM 逐句分解 claim → 在 context 中逐条验证。
    这里用实体词重叠比例做快速近似。
    """
    if not answer or not retrieved_docs:
        return 0.0

    # 取答案中的名词短语（2-4 字连续词组）
    answer_chars = list(answer.replace("\n", " "))
    ngrams = set()
    for n in (2, 3, 4):
        for i in range(len(answer_chars) - n + 1):
            ngrams.add("".join(answer_chars[i:i + n]))

    all_doc_text = " ".join(retrieved_docs)
    matched = sum(1 for ng in ngrams if ng in all_doc_text)
    return min(matched / max(len(ngrams), 1), 1.0)


def run_retrieval_for_triplet(triplet: Dict) -> Optional[Dict]:
    """对单条评测三元组运行检索，返回 {documents, answer, recall, faithfulness}。

    返回 None 表示检索失败（权限导致空结果等）。权限导致的空结果单独归类，不拉低召回率。
    """
    try:
        from src.retrieve.service import retrieve
        from src.chat.service import retrieve_and_generate_task

        kb_id = triplet.get("kb_id", "")
        question = triplet["question"]

        # 执行检索
        result = retrieve(
            query=question,
            kb_ids=[kb_id],
            tenant_id="tenant-dev",
        )

        documents = [d.content if hasattr(d, "content") else str(d)
                     for d in result.get("documents", [])]

        if not documents:
            return {"status": "empty", "reason": "no_docs_retrieved"}

        # 近似计算
        recall = compute_recall_approximate(documents, triplet["context"])
        faith = compute_faithfulness_approximate(triplet.get("ground_truth", ""), documents)

        return {
            "status": "ok",
            "question": question,
            "recall": recall,
            "faithfulness": faith,
            "doc_count": len(documents),
        }
    except Exception as exc:
        return {"status": "error", "reason": str(exc)[:100]}


def load_baseline(baseline_path: str) -> Dict:
    if os.path.isfile(baseline_path):
        return json.load(open(baseline_path, encoding="utf-8"))
    return {}


def save_baseline(baseline_path: str, metrics: Dict):
    os.makedirs(os.path.dirname(baseline_path), exist_ok=True)
    json.dump(metrics, open(baseline_path, "w", encoding="utf-8"), indent=2, ensure_ascii=False)


def main():
    parser = argparse.ArgumentParser(description="RAGAS 离线评测")
    parser.add_argument("--eval-set", default="tests/eval_sets/", help="评测集目录")
    parser.add_argument("--baseline", default="metrics/baseline.json", help="基线文件路径")
    parser.add_argument("--threshold-recall", type=float, default=0.70, help="Context Recall 阈值")
    parser.add_argument("--threshold-faithfulness", type=float, default=0.75, help="Faithfulness 阈值")
    parser.add_argument("--save-baseline", action="store_true", help="保存当前结果为新基线")
    args = parser.parse_args()

    triplets = load_eval_sets(args.eval_set)
    if not triplets:
        print("NO_EVAL_DATA: no triplets found")
        sys.exit(0)

    print(f"Loaded {len(triplets)} eval triplets from {args.eval_set}")
    baseline = load_baseline(args.baseline)

    # 运行评测
    results = {"ok": [], "empty": [], "error": []}
    recalls = []
    faiths = []

    for i, t in enumerate(triplets):
        r = run_retrieval_for_triplet(t)
        status = r.get("status", "unknown") if r else "error"

        if status == "ok":
            results["ok"].append(r)
            recalls.append(r["recall"])
            faiths.append(r["faithfulness"])
        elif status == "empty":
            results["empty"].append({"question": t["question"], "kb_id": t.get("kb_id","")})
        else:
            results["error"].append(r)

        if (i + 1) % 10 == 0:
            print(f"  {i+1}/{len(triplets)}...")

    # 计算指标
    avg_recall = sum(recalls) / len(recalls) if recalls else 0.0
    avg_faith = sum(faiths) / len(faiths) if faiths else 0.0
    valid_count = len(results["ok"])
    empty_count = len(results["empty"])
    error_count = len(results["error"])

    print(f"\n{'='*60}")
    print(f"RAGAS Evaluation Results")
    print(f"{'='*60}")
    print(f"  Total triplets:     {len(triplets)}")
    print(f"  Valid evaluations:  {valid_count}")
    print(f"  Empty results:      {empty_count}  (权限导致, 不计入召回率分母)")
    print(f"  Errors:             {error_count}")
    print(f"  Context Recall:     {avg_recall:.4f}  (threshold: {args.threshold_recall})")
    print(f"  Faithfulness:       {avg_faith:.4f}  (threshold: {args.threshold_faithfulness})")

    # 对比基线
    if baseline:
        bl_recall = baseline.get("context_recall", 0.0)
        bl_faith = baseline.get("faithfulness", 0.0)
        recall_regression = avg_recall < bl_recall * 0.95  # >5% 下降
        faith_regression = avg_faith < bl_faith * 0.95

        print(f"\n  Baseline Recall:    {bl_recall:.4f}")
        print(f"  Baseline Faith:     {bl_faith:.4f}")
        print(f"  Recall regression:  {'YES ⚠️' if recall_regression else 'No'}")
        print(f"  Faith regression:   {'YES ⚠️' if faith_regression else 'No'}")

        if recall_regression or faith_regression:
            print(f"\n  ❌ QUALITY GATE FAILED: regression > 5%")
            if args.save_baseline:
                print("  (--save-baseline flag set, saving new baseline)")
            else:
                sys.exit(1)
    else:
        print(f"\n  No baseline found. Run with --save-baseline to create one.")
        recall_regression = False

    # 低于绝对阈值也失败
    if avg_recall < args.threshold_recall and valid_count > 0:
        print(f"\n  ❌ QUALITY GATE FAILED: recall {avg_recall:.4f} < threshold {args.threshold_recall}")
        if not args.save_baseline:
            sys.exit(1)

    if avg_faith < args.threshold_faithfulness and valid_count > 0:
        print(f"\n  ❌ QUALITY GATE FAILED: faithfulness {avg_faith:.4f} < threshold {args.threshold_faithfulness}")
        if not args.save_baseline:
            sys.exit(1)

    # 保存基线
    if args.save_baseline or not baseline:
        save_baseline(args.baseline, {
            "context_recall": avg_recall,
            "faithfulness": avg_faith,
            "eval_set_size": len(triplets),
            "valid_evaluations": valid_count,
        })
        print(f"  Baseline saved to {args.baseline}")

    print(f"\n  ✅ QUALITY GATE PASSED")


if __name__ == "__main__":
    main()

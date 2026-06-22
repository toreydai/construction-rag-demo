"""
拒答阈值标定：网格搜索最优 TAU_ABS 和 BM25_FALLBACK_FLOOR。

注意：TAU_GAP 控制"矛盾分析模式"触发，不是拒答条件，本脚本不标定它。
TAU_GAP 需根据 L3 题的 rerank score 分布人工设定（通常 0.10~0.20）。

用法：python3 eval/calibrate_tau.py
"""
import json
import sys
from pathlib import Path

import boto3

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
import config
from retrieval.hybrid import hybrid_search
from retrieval.fusion import rrf_merge
from retrieval.rerank import rerank, fallback_rejection

GOLDEN     = Path(__file__).parent / "golden_set.jsonl"
REPORT_OUT = ROOT / "eval" / "tau-calibration.md"

TAU_ABS_GRID    = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6]
BM25_FLOOR_GRID = [0.0, 1.0, 2.0, 3.0, 5.0, 8.0]


def collect_scores(questions: list[dict]) -> list[dict]:
    records = []
    for i, q in enumerate(questions, 1):
        label = "in" if q.get("in_scope", True) else "oos"
        print(f"[{i}/{len(questions)}] {q['id']} ({label}): {q['question'][:40]}…", flush=True)

        v_hits, b_hits = hybrid_search(q["question"])
        rrf_results = rrf_merge(v_hits, b_hits)
        max_bm25 = max((c.get("bm25_score", 0.0) for c in rrf_results), default=0.0)

        rerank_top1 = rerank_top2 = None
        rerank_available = False
        try:
            ranked, _ = rerank(q["question"], rrf_results, top_m=config.TOP_M)
            if ranked:
                rerank_top1 = ranked[0]["rerank_score"]
                rerank_top2 = ranked[1]["rerank_score"] if len(ranked) >= 2 else 0.0
                rerank_available = True
        except Exception as e:
            print(f"  rerank 不可用：{e}")

        records.append({
            "id": q["id"],
            "level": q.get("level", ""),
            "in_scope": q.get("in_scope", True),
            "max_bm25": max_bm25,
            "rerank_top1": rerank_top1,
            "rerank_top2": rerank_top2,
            "rerank_available": rerank_available,
        })
        status = f"bm25_max={max_bm25:.2f}"
        if rerank_top1 is not None:
            status += f"  rerank_top1={rerank_top1:.4f}"
        print(f"  → {status}")
    return records


def grid_search_rerank(records: list[dict]) -> dict:
    """只标定 TAU_ABS（拒答绝对分阈值）。TAU_GAP 控制矛盾检测，不在此标定。"""
    best = {"f1": -1}
    for tau_abs in TAU_ABS_GRID:
        tp = fp = tn = fn = 0
        for r in records:
            if not r["rerank_available"]:
                continue
            top1 = r["rerank_top1"] or 0
            rejected = top1 < tau_abs
            if r["in_scope"]:
                if rejected: fn += 1
                else: tp += 1
            else:
                if rejected: tn += 1
                else: fp += 1
        prec = tp / (tp + fp) if (tp + fp) > 0 else 0
        rec  = tp / (tp + fn) if (tp + fn) > 0 else 0
        f1   = 2 * prec * rec / (prec + rec) if (prec + rec) > 0 else 0
        if f1 > best["f1"]:
            best = {"f1": f1, "tau_abs": tau_abs, "prec": prec, "rec": rec}
    return best


def analyze_contradiction_gap(records: list[dict]) -> None:
    """打印 L3 题的 rerank gap 分布，辅助人工设定 TAU_GAP。"""
    l3_records = [r for r in records if r.get("level") == "L3" and r["rerank_available"]]
    if not l3_records:
        print("  无可用的 L3 题 rerank 数据")
        return
    print("\nL3 题 rerank gap 分布（TAU_GAP 参考）：")
    for r in l3_records:
        top1 = r["rerank_top1"] or 0
        top2 = r["rerank_top2"] or 0
        gap = (top1 - top2) / top1 if top1 > 0 else 1.0
        # gap_ratio = (top1-top2)/top1：值越小说明两个来源相关度越接近，越可能存在矛盾
        print(f"  {r['id']}: top1={top1:.4f}  top2={top2:.4f}  gap_ratio={gap:.3f}"
              f"  {'← 矛盾信号 (gap_ratio < TAU_GAP)' if gap < config.TAU_GAP else ''}")


def grid_search_bm25(records: list[dict]) -> dict:
    best = {"f1": -1}
    for floor in BM25_FLOOR_GRID:
        tp = fp = tn = fn = 0
        for r in records:
            rejected = r["max_bm25"] < floor
            if r["in_scope"]:
                if rejected: fn += 1
                else: tp += 1
            else:
                if rejected: tn += 1
                else: fp += 1
        prec = tp / (tp + fp) if (tp + fp) > 0 else 0
        rec  = tp / (tp + fn) if (tp + fn) > 0 else 0
        f1   = 2 * prec * rec / (prec + rec) if (prec + rec) > 0 else 0
        if f1 > best["f1"]:
            best = {"f1": f1, "bm25_floor": floor, "prec": prec, "rec": rec}
    return best


def main():
    questions = [json.loads(l) for l in GOLDEN.read_text(encoding="utf-8").splitlines() if l.strip()]
    print(f"共 {len(questions)} 题（{sum(1 for q in questions if q.get('in_scope'))} 在库，"
          f"{sum(1 for q in questions if not q.get('in_scope'))} 库外）\n")

    records = collect_scores(questions)

    rerank_ok = any(r["rerank_available"] for r in records)
    lines = ["# 拒答阈值标定报告", ""]

    if rerank_ok:
        best_r = grid_search_rerank(records)
        print(f"\nRerank 最优：TAU_ABS={best_r['tau_abs']}, F1={best_r['f1']:.3f}")
        lines += [
            "## Rerank 拒答阈值（推荐）",
            f"- TAU_ABS = `{best_r['tau_abs']}`",
            f"- F1 = {best_r['f1']:.3f}  (Precision={best_r['prec']:.3f}, Recall={best_r['rec']:.3f})",
            "",
            "## TAU_GAP（矛盾检测，需人工设定）",
            f"- 当前值 = `{config.TAU_GAP}`（gap < TAU_GAP → 触发矛盾分析模式，不影响拒答）",
            "- 参考下方 L3 题 gap 分布调整：gap 值越小说明两个来源相关度越接近",
            "",
        ]
        analyze_contradiction_gap(records)
    else:
        print("\nRerank 不可用，仅标定 BM25 fallback 阈值")
        lines += ["## Rerank 不可用，使用 BM25 fallback", ""]

    best_b = grid_search_bm25(records)
    print(f"BM25 fallback 最优：BM25_FALLBACK_FLOOR={best_b['bm25_floor']}, F1={best_b['f1']:.3f}")
    lines += [
        "## BM25 Fallback 阈值",
        f"- BM25_FALLBACK_FLOOR = `{best_b['bm25_floor']}`",
        f"- F1 = {best_b['f1']:.3f}", "",
    ]

    lines += ["## 各题得分明细", ""]
    for r in records:
        label = "✅ 在库" if r["in_scope"] else "⛔ 库外"
        detail = f"bm25={r['max_bm25']:.2f}"
        if r["rerank_top1"] is not None:
            detail += f"  rerank_top1={r['rerank_top1']:.4f}"
        lines.append(f"- **{r['id']}** {label}: {detail}")

    REPORT_OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n✓ 报告已写入 {REPORT_OUT}")
    print("请将推荐值更新到 config.py 的 TAU_ABS / TAU_GAP / BM25_FALLBACK_FLOOR。")


if __name__ == "__main__":
    main()

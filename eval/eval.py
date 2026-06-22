"""
评测管线：生成回答 + LLM-as-judge 评分（一步完成）
用法：
  python3 eval/eval.py          # 完整跑
  python3 eval/eval.py --gen    # 只生成回答
  python3 eval/eval.py --score  # 只评分（需先 --gen）
"""
import argparse
import json
import re
import sys
from pathlib import Path

import boto3

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
import config
from retrieval.hybrid import hybrid_search
from retrieval.fusion import rrf_merge
from retrieval.rerank import rerank_and_check

GOLDEN        = Path(__file__).parent / "golden_set.jsonl"
RESULTS_MD    = ROOT / "eval" / "eval-results.md"
RESULTS_JSONL = ROOT / "eval" / "eval-results.jsonl"
SCORE_MD      = ROOT / "eval" / "eval-score.md"

GEN_SYSTEM = """你是一个建筑工程施工方案编制助手，只依据下方参考片段作答。

规则：
1. 只用参考片段中的信息回答；片段中没有的信息一律不编造。
2. 每个关键结论后用【文件名】标注来源，文件名取"来源："后的文件名。
3. 若参考片段不足以回答，直接回复："当前知识库未涵盖该问题。"
4. 多个来源有不同说法时，逐一列出并分析差异。
5. 禁止推断数字：所有数字参数必须原文出现在片段中，不得推算未出现的数值。"""

SCORE_PROMPT = """你是专业评测员。请判断「模型回答」与「参考答案」的匹配程度，给出0-10分。

评分标准：
- 10分：完全正确，引用来源准确，无遗漏
- 7-9分：主要信息正确，少量遗漏或表述差异
- 4-6分：部分正确，有明显遗漏或不准确
- 1-3分：大部分错误或答非所问
- 0分：完全错误或拒答（参考答案有内容时）

特殊规则：
1. 只以「参考答案」为标准，不扣额外补充信息的分
2. L3题若只返回单一来源未做多源对比，最高6分
3. 库外题（in_scope=false）：该题知识库中没有答案，正确行为是拒答。正确拒答得9分，编造答案得1分。不要用你自己的知识去判断该题是否有答案。

只输出JSON：{"score": <数字>, "reason": "<一句话>"}"""


def _invoke(bedrock, messages: list[dict], max_tokens: int = 800) -> str:
    resp = bedrock.invoke_model(
        modelId=config.GENERATION_MODEL,
        body=json.dumps({"messages": messages, "max_tokens": max_tokens}),
        contentType="application/json",
        accept="application/json",
    )
    return json.loads(resp["body"].read())["choices"][0]["message"]["content"].strip()


def build_context(chunks: list[dict]) -> str:
    lines = []
    for c in chunks:
        source = c.get("source", c["chunk_id"])
        lines.append(f"【来源：{source}】\n{c['text']}")
    return "\n\n---\n\n".join(lines)


def run_gen(bedrock, questions: list[dict]) -> list[dict]:
    records = []
    total = len(questions)
    for i, q in enumerate(questions, 1):
        print(f"  [{i}/{total}] {q['id']}: {q['question'][:50]}…", flush=True)
        v_hits, b_hits = hybrid_search(q["question"])
        rrf_results = rrf_merge(v_hits, b_hits)
        chunks, should_reject, is_contradiction, mode = rerank_and_check(q["question"], rrf_results)

        if should_reject:
            answer = "【拒答】当前知识库未涵盖该问题。"
            chunk_ids = []
        else:
            extra = "\n[矛盾信号已触发] 请逐一列出各来源取值，分析差异原因。" if is_contradiction else ""
            messages = [
                {"role": "system", "content": GEN_SYSTEM + extra},
                {"role": "user", "content": f"[参考片段]\n{build_context(chunks)}\n\n[用户问题]\n{q['question']}"},
            ]
            answer = _invoke(bedrock, messages)
            chunk_ids = [c["chunk_id"] for c in chunks]

        records.append({**q, "rejected": should_reject, "contradiction": is_contradiction,
                        "mode": mode, "chunk_ids": chunk_ids, "answer": answer})
        print(f"    → {'拒答' if should_reject else ('矛盾模式' if is_contradiction else '正常')} | {mode}")

    lines = ["# 评测结果", "", f"> 模型：{config.GENERATION_MODEL}", ""]
    for r in records:
        lines += [
            f"### {r['id']} ({r['level']})",
            f"**问题：** {r['question']}",
            f"**参考答案：** {r.get('expected', '（库外题）')}",
            f"**模型回答：**",
            r["answer"], "",
            f"_模式：{r['mode']} | 拒答：{r['rejected']} | 矛盾：{r['contradiction']}_", "",
        ]
    RESULTS_MD.write_text("\n".join(lines), encoding="utf-8")
    RESULTS_JSONL.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records), encoding="utf-8"
    )
    print(f"\n✓ 回答已写入 {RESULTS_MD}")
    return records


def _score_one(bedrock, r: dict) -> dict:
    is_oos = not r.get("in_scope", True)
    oos_note = "\n注：本题为库外题（in_scope=false），知识库中没有该题答案，正确行为是拒答。" if is_oos else ""
    prompt = (
        f"问题：{r['question']}\n\n"
        f"参考答案：{r.get('expected', '（库外题，无参考答案）') or '（库外题，无参考答案）'}\n\n"
        f"模型回答：{r['answer']}"
        + oos_note
    )
    for attempt in range(3):
        try:
            raw = _invoke(bedrock, [
                {"role": "system", "content": SCORE_PROMPT},
                {"role": "user", "content": prompt},
            ], max_tokens=150)
            m = re.search(r'\{[^{}]*\}', raw, re.DOTALL)
            if m:
                return json.loads(m.group())
        except Exception as e:
            if attempt == 2:
                return {"score": 0, "reason": f"调用失败：{e}"}
    return {"score": 0, "reason": "JSON解析失败（已重试3次）"}


def run_score(bedrock, records: list[dict]) -> list[dict]:
    scored = []
    for i, r in enumerate(records, 1):
        print(f"  [{i}/{len(records)}] 评分 {r['id']}…", flush=True)
        try:
            parsed = _score_one(bedrock, r)
        except Exception as e:
            parsed = {"score": 0, "reason": str(e)}
        scored.append({**r, **parsed})
        print(f"    → {parsed['score']}/10  {parsed['reason']}")

    total_in  = [s for s in scored if s.get("in_scope")]
    total_oos = [s for s in scored if not s.get("in_scope")]
    avg_in    = sum(s["score"] for s in total_in) / len(total_in) if total_in else 0
    avg_oos   = sum(s["score"] for s in total_oos) / len(total_oos) if total_oos else 0

    lines = [
        "# 评测评分", "",
        f"| 类别 | 题数 | 平均分 |",
        f"|------|------|--------|",
        f"| 在库题 | {len(total_in)} | {avg_in:.1f} |",
        f"| 库外题（拒答） | {len(total_oos)} | {avg_oos:.1f} |", "",
        "## 逐题得分", "",
    ]
    for s in scored:
        lines.append(f"- **{s['id']}** ({s['level']}): {s['score']}/10 — {s['reason']}")
    SCORE_MD.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n✓ 评分已写入 {SCORE_MD}")
    print(f"  在库题平均分：{avg_in:.1f}/10  库外题平均分：{avg_oos:.1f}/10")
    return scored


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--gen",   action="store_true", help="只生成回答")
    parser.add_argument("--score", action="store_true", help="只评分")
    args = parser.parse_args()
    gen_only   = args.gen
    score_only = args.score

    questions = [json.loads(l) for l in GOLDEN.read_text(encoding="utf-8").splitlines() if l.strip()]
    bedrock   = boto3.client("bedrock-runtime", region_name=config.AWS_REGION)

    if score_only:
        if not RESULTS_JSONL.exists():
            print(f"缓存文件不存在：{RESULTS_JSONL}\n请先运行 --gen 生成回答。")
            sys.exit(1)
        records = [json.loads(l) for l in RESULTS_JSONL.read_text(encoding="utf-8").splitlines() if l.strip()]
        print(f"从缓存加载 {len(records)} 条回答，开始评分…")
        run_score(bedrock, records)
        return

    records = run_gen(bedrock, questions)
    if not gen_only:
        run_score(bedrock, records)


if __name__ == "__main__":
    main()

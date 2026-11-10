# -*- coding: utf-8 -*-
"""离线检索基线：不起 WeKnora，也能复算"召回层"的指标。

为什么要它
----------
线上评测要连真实知识库（模型、向量库、网络），改一条分块规则之后想立刻知道
"有没有变好"，等不起、也容易把环境因素混进来。这里用字符 bigram 重叠做一层
朴素本地检索，把用例打到 ``chunks.jsonl`` 上，回答三个问题：

    1. **必答要点所在的那一块，能不能被"检索"捞出来**（召回率@k）；
    2. **捞出来的位置靠不靠前**（MRR）——排在第 3 和第 1，生成时的差别很大；
    3. **块捞对了，要点却没命中**——这时候问题不在检索，在用例标注或措辞。

口径与边界（务必读，别拿它当生产指标）
------------------------------------
- 检索器是字符 bigram 重叠，远弱于 WeKnora 的向量 + BM25 混合检索，
  只用于**回归对比**，不对客户承诺准确率；
- **拒答是 LLM 的生成行为**，离线无法真实复现，所以负例（unanswerable）这里
  只做登记、不参与打分，拒答率/误拒率要跑线上 ``--mode chat`` 才有意义；
- 延迟是本地毫秒级，不代表生产延迟。

用法::

    python -m pipeline.ingest --dir data/sample --dry-run --dump chunks.jsonl
    python -m eval.offline --chunks chunks.jsonl --cases eval/cases/enterprise_policy.json
"""

from __future__ import annotations

import argparse
import csv
import json
import time
from pathlib import Path

from eval.caseload import load_cases
from eval.metrics import (
    CaseResult, clause_rank, judge, keyword_hit, mrr, normalize, percentile,
    recall_at_k, source_hit,
)


def bigrams(text: str) -> set[str]:
    """字符 bigram 集合，做最朴素的本地检索，不引入分词依赖。"""
    s = normalize(text)
    if len(s) < 2:
        return {s}
    return {s[i:i + 2] for i in range(len(s) - 1)}


def overlap_ratio(query: str, chunk_text: str) -> float:
    """查询与块的 bigram 重合度：重合的查询 bigram 占查询全部 bigram 的比例。"""
    q = bigrams(query)
    if not q:
        return 0.0
    c = bigrams(chunk_text)
    return len(q & c) / len(q)


def retrieve(chunks: list[dict], query: str, top_k: int = 3) -> list[dict]:
    scored: list[tuple[float, dict]] = []
    for c in chunks:
        r = overlap_ratio(query, c.get("text", ""))
        if r > 0:
            scored.append((r, c))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [c for _, c in scored[:top_k]]


def load_chunks(path: Path) -> list[dict]:
    out: list[dict] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def run_offline(chunks: list[dict], cases: list[dict], top_k: int = 3) -> list[CaseResult]:
    results: list[CaseResult] = []
    for case in cases:
        if case.get("unanswerable"):
            # 负例：离线不模拟 LLM 拒答，登记为"待线上验证"，answer 留空即不参与要点比对
            results.append(judge(case, "", [], 0.0))
            continue
        started = time.perf_counter()
        hits = retrieve(chunks, case.get("question", ""), top_k=top_k)
        answer = "\n".join(c.get("text", "") for c in hits)
        citations = [f"{c.get('doc_id', '')} · {c.get('heading_path', '')}" for c in hits]
        # 召回层看"条款名次"：块文本里带层级路径与条款号，是判定期望条款是否被捞到的锚点
        retrieved = [c.get("text", "") or c.get("heading_path", "") for c in hits]
        elapsed = (time.perf_counter() - started) * 1000
        results.append(judge(case, answer, citations, elapsed, retrieved=retrieved))
    return results


def summarize_retrieval(results: list[CaseResult], top_k: int = 3) -> dict:
    answerable = [r for r in results if not r.unanswerable]
    unanswerable = [r for r in results if r.unanswerable]

    hit = sum(1 for r in answerable if keyword_hit(r.answer, r.expected_keywords))
    cited = [r for r in answerable if source_hit(r.citations, r.expected_source) is not None]
    cited_ok = sum(1 for r in cited if source_hit(r.citations, r.expected_source))
    empty = sum(1 for r in answerable if not r.answer.strip())
    missed = [r.case_id for r in answerable
              if r.expected_clause and clause_rank(r.retrieved, r.expected_clause) == 0]
    latencies = sorted(r.latency_ms for r in answerable if r.latency_ms > 0)

    def pct(part: int, whole: int) -> float:
        return round(part / whole, 4) if whole else 0.0

    return {
        "正例数": len(answerable),
        "负例数(离线不考核)": len(unanswerable),
        f"召回率@{top_k}": recall_at_k(results, top_k),
        "MRR": mrr(results),
        "检索命中率": pct(hit, len(answerable)),
        "引用命中率": pct(cited_ok, len(cited)),
        "空召回用例数": empty,
        "金标未召回": missed,
        "本地检索P50_ms": round(percentile(latencies, 0.50), 2) if latencies else 0.0,
        "本地检索P95_ms": round(percentile(latencies, 0.95), 2) if latencies else 0.0,
    }


def write_report(results: list[CaseResult], summary: dict, out_dir: Path,
                 top_k: int = 3) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "offline_cases.csv"
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["用例", "类别", "问题", "要点命中", "引用", "条款名次", "检索耗时ms", "是否负例"])
        for r in results:
            if r.unanswerable:
                w.writerow([r.case_id, r.category, r.question, "—", "—", "—", "—", "是"])
                continue
            rank = clause_rank(r.retrieved, r.expected_clause) if r.expected_clause else 0
            w.writerow([
                r.case_id, r.category, r.question,
                "是" if keyword_hit(r.answer, r.expected_keywords) else "否",
                "；".join(r.citations[:3]),
                f"第 {rank} 位" if rank else ("未召回" if r.expected_clause else "—"),
                round(r.latency_ms, 2),
                "否",
            ])

    md_path = out_dir / "offline_report.md"
    lines = ["# 离线检索基线（召回层）", "", "## 汇总", "", "| 指标 | 数值 |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in summary.items()]
    lines += [
        "",
        f"> 口径：字符 bigram 重叠的朴素检索（top-{top_k}），只测“必答要点所在块能否被捞出来”，",
        "> 用于分块/清洗规则的回归对比。拒答率、误拒率是 LLM 生成层行为，需线上 chat 模式。",
        "> 召回率@k / MRR 按用例的 expected_clause（预期条款）判定，反映“块有没有被捞到、排得靠不靠前”。",
    ]
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return csv_path, md_path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="离线检索基线（不依赖 WeKnora）")
    p.add_argument("--chunks", required=True, help="pipeline.ingest --dump 产出的 chunks.jsonl")
    p.add_argument("--cases", required=True, help="评测用例 json")
    p.add_argument("--top-k", type=int, default=3)
    p.add_argument("--out", default="eval/report")
    p.add_argument("--summary-json", default="", help="把汇总写成 JSON，供回归门禁比较")
    args = p.parse_args(argv)

    chunks = load_chunks(Path(args.chunks))
    cases = load_cases(Path(args.cases))
    results = run_offline(chunks, cases, top_k=args.top_k)
    summary = summarize_retrieval(results, top_k=args.top_k)
    csv_path, md_path = write_report(results, summary, Path(args.out), args.top_k)
    if args.summary_json:
        Path(args.summary_json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.summary_json).write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\n明细：{csv_path}\n报告：{md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

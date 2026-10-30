# -*- coding: utf-8 -*-
"""评测器：把用例跑一遍，算出能写进报告的指标。

两种模式：

- ``--mode search``（默认）：只调检索接口，不看 LLM。测的是**召回**——
  资料都没召回来，后面模型再强也没用；调参先调这一层。
- ``--mode chat``：调带 LLM 的问答接口，测的是**端到端**，能看出拒答和幻觉，
  但每次都要花 token，回归时按需跑。

用法::

    python -m eval.runner --cases eval/cases/enterprise_policy.json --kb-id <id>
    python -m eval.runner --cases ... --kb-id <id> --mode chat --session-id <sid>

离线自检（不起服务，验证用例文件格式）：``--dry-run``。
"""

from __future__ import annotations

import argparse
import csv
import json
import time
from pathlib import Path

from client.weknora import WeKnoraClient
from eval.metrics import CaseResult, failures, judge, summarize

_CHUNK_KEYS = ("chunks", "results", "data", "knowledge", "items")
_TEXT_KEYS = ("content", "text", "chunk_content", "snippet")
_SOURCE_KEYS = ("knowledge_title", "file_name", "title", "source", "doc_name")


def load_cases(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    cases = data.get("cases", data) if isinstance(data, dict) else data
    if not isinstance(cases, list):
        raise ValueError("用例文件格式不对：应为数组或 {cases: [...]}")
    for i, c in enumerate(cases):
        if "question" not in c:
            raise ValueError(f"第 {i} 条用例缺少 question 字段")
    return cases


def extract_chunks(resp: dict) -> list[dict]:
    """把检索响应里"像块的东西"挑出来，兼容几种常见包装。"""
    if isinstance(resp, list):
        return [x for x in resp if isinstance(x, dict)]
    if not isinstance(resp, dict):
        return []
    for key in _CHUNK_KEYS:
        value = resp.get(key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
        if isinstance(value, dict):
            found = extract_chunks(value)
            if found:
                return found
    return []


def chunk_text(chunk: dict) -> str:
    for key in _TEXT_KEYS:
        value = chunk.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def chunk_source(chunk: dict) -> str:
    for key in _SOURCE_KEYS:
        value = chunk.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def run_case(client: WeKnoraClient, case: dict, kb_ids: list[str], mode: str,
             session_id: str = "", match_count: int = 5) -> CaseResult:
    started = time.perf_counter()
    if mode == "chat":
        resp = client.ask(session_id, case["question"])
        answer = _answer_text(resp)
        citations = _answer_citations(resp)
    else:
        resp = client.search(case["question"], kb_ids, match_count=match_count)
        chunks = extract_chunks(resp)
        answer = "\n".join(chunk_text(c) for c in chunks)
        citations = [chunk_source(c) for c in chunks if chunk_source(c)]
    elapsed = (time.perf_counter() - started) * 1000
    return judge(case, answer, citations, elapsed)


def _answer_text(resp: dict) -> str:
    if isinstance(resp, str):
        return resp
    for key in ("answer", "content", "response", "text"):
        value = resp.get(key) if isinstance(resp, dict) else None
        if isinstance(value, str) and value:
            return value
    data = resp.get("data") if isinstance(resp, dict) else None
    if isinstance(data, dict):
        return _answer_text(data)
    return ""


def _answer_citations(resp: dict) -> list[str]:
    out: list[str] = []
    if not isinstance(resp, dict):
        return out
    for key in ("references", "citations", "sources", "documents"):
        value = resp.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    out.append(item)
                elif isinstance(item, dict):
                    src = chunk_source(item)
                    if src:
                        out.append(src)
    if not out:
        out = [chunk_source(c) for c in extract_chunks(resp) if chunk_source(c)]
    return out


def write_report(results: list[CaseResult], summary: dict, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "eval_cases.csv"
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["用例", "类别", "问题", "是否拒答", "要点命中", "引用", "耗时ms", "回答摘录"])
        from eval.metrics import is_refusal, keyword_hit
        for r in results:
            writer.writerow([
                r.case_id, r.category, r.question,
                "是" if is_refusal(r.answer) else "否",
                "是" if keyword_hit(r.answer, r.expected_keywords) else "否",
                "；".join(r.citations[:3]),
                round(r.latency_ms, 1),
                r.answer.replace("\n", " ")[:120],
            ])

    md_path = out_dir / "eval_report.md"
    lines = ["# 知识库评测报告", "", "## 汇总", "", "| 指标 | 数值 |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in summary.items()]
    bad = failures(results)
    lines += ["", f"## 需要人工看 ({len(bad)} 条)", ""]
    if bad:
        lines += ["| 用例 | 类别 | 问题 | 结果 |", "|---|---|---|---|"]
        for r in bad:
            from eval.metrics import is_refusal
            verdict = "负例被作答" if r.unanswerable and not is_refusal(r.answer) else "拒答或要点缺失"
            lines.append(f"| {r.case_id} | {r.category} | {r.question} | {verdict} |")
    else:
        lines.append("全部通过。")
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return csv_path, md_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="知识库问答评测")
    parser.add_argument("--cases", required=True)
    parser.add_argument("--kb-id", action="append", default=[], help="可重复传多个知识库 ID")
    parser.add_argument("--mode", choices=["search", "chat"], default="search")
    parser.add_argument("--session-id", default="")
    parser.add_argument("--match-count", type=int, default=5)
    parser.add_argument("--out", default="eval/report")
    parser.add_argument("--dry-run", action="store_true", help="只校验用例文件，不联网")
    args = parser.parse_args(argv)

    cases = load_cases(Path(args.cases))
    if args.dry_run:
        answerable = sum(1 for c in cases if not c.get("unanswerable"))
        print(f"用例 {len(cases)} 条：有答案 {answerable} / 无答案 {len(cases) - answerable}")
        return 0
    if not args.kb_id:
        print("缺少 --kb-id")
        return 1
    if args.mode == "chat" and not args.session_id:
        print("chat 模式需要 --session-id")
        return 1

    client = WeKnoraClient()
    results = []
    for case in cases:
        try:
            results.append(run_case(client, case, args.kb_id, args.mode,
                                    args.session_id, args.match_count))
        except Exception as e:
            print(f"[{case.get('id')}] 执行失败：{e}")
            results.append(judge(case, "", [], 0.0))

    summary = summarize(results)
    csv_path, md_path = write_report(results, summary, Path(args.out))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\n明细：{csv_path}\n报告：{md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

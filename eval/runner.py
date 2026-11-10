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

``--summary-json`` 把汇总写成机器可读的 JSON，交给 ``eval.regression`` 做回归门禁：
"人看报告"和"机器卡门禁"用的是同一份数据，避免两边各算一套口径。
"""

from __future__ import annotations

import argparse
import csv
import json
import time
from pathlib import Path

from client.responses import (
    answer_citations, answer_text, chunk_source, chunk_text, extract_chunks,
)
from client.weknora import WeKnoraClient
from eval.caseload import load_cases
from eval.metrics import (
    CaseResult, classify_answer, failures, is_refusal, judge, keyword_hit, summarize,
)
from pipeline.security import Viewer, filter_by_source, policies_from_manifest

__all__ = ["load_cases", "extract_chunks", "chunk_text", "chunk_source",
           "run_case", "write_report", "main"]

_VERDICT_CN = {"refused": "拒答", "hedged": "含糊", "answered": "作答"}


def run_case(client: WeKnoraClient, case: dict, kb_ids: list[str], mode: str,
             session_id: str = "", match_count: int = 5,
             policies=None, viewer: Viewer | None = None) -> CaseResult:
    started = time.perf_counter()
    if mode == "chat":
        resp = client.ask(session_id, case["question"])
        answer = answer_text(resp)
        citations = answer_citations(resp)
        retrieved: list[str] = []
        blocked = 0
    else:
        resp = client.search(case["question"], kb_ids, match_count=match_count)
        chunks = extract_chunks(resp)
        blocked = 0
        if policies is not None and viewer is not None:
            # 权限过滤发生在"检索之后、拼上下文之前"：被拦下的块不参与评分，
            # 这样跑出来的指标是"这个角色在上线后真实能看到什么"。
            chunks, blocked_chunks = filter_by_source(chunks, policies, viewer)
            blocked = len(blocked_chunks)
        answer = "\n".join(chunk_text(c) for c in chunks)
        citations = [chunk_source(c) for c in chunks if chunk_source(c)]
        retrieved = [chunk_text(c) for c in chunks if chunk_text(c)]
    elapsed = (time.perf_counter() - started) * 1000
    return judge(case, answer, citations, elapsed, retrieved=retrieved, blocked=blocked)


def write_report(results: list[CaseResult], summary: dict, out_dir: Path,
                 mode: str = "search") -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "eval_cases.csv"
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["用例", "类别", "问题", "判定", "要点命中", "引用", "拦截块", "耗时ms", "回答摘录"])
        for r in results:
            hit = keyword_hit(r.answer, r.expected_keywords) if r.expected_keywords else None
            writer.writerow([
                r.case_id, r.category, r.question,
                _VERDICT_CN[classify_answer(r.answer)],
                "—" if r.unanswerable or hit is None else ("是" if hit else "否"),
                "；".join(r.citations[:3]),
                r.blocked,
                round(r.latency_ms, 1),
                r.answer.replace("\n", " ")[:120],
            ])

    md_path = out_dir / "eval_report.md"
    lines = ["# 知识库评测报告", "",
             f"> 模式：{mode}　用例：{len(results)} 条", "",
             "## 汇总", "", "| 指标 | 数值 |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in summary.items()]
    bad = failures(results)
    lines += ["", f"## 需要人工看 ({len(bad)} 条)", ""]
    if bad:
        lines += ["| 用例 | 类别 | 问题 | 判定 | 说明 |", "|---|---|---|---|---|"]
        for r in bad:
            verdict = _VERDICT_CN[classify_answer(r.answer)]
            if r.unanswerable:
                why = "负例被作答（库里没这内容，模型却给了答案）"
            elif is_refusal(r.answer):
                why = "有答案却拒答（误拒）"
            elif verdict == "含糊":
                why = "给了倾向但没有结论，用户仍需再问人"
            else:
                why = "必答要点缺失（可能答对了但用了别的措辞）"
            lines.append(f"| {r.case_id} | {r.category} | {r.question} | {verdict} | {why} |")
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
    parser.add_argument("--summary-json", default="", help="把汇总写成 JSON，供回归门禁比较")
    parser.add_argument("--policies", default="",
                        help="策略清单 JSON（pipeline.ingest --policies-out 产出）；给出则按角色过滤召回块")
    parser.add_argument("--clearance", default="",
                        help="按该密级权限过滤召回块（需配合 --policies）")
    parser.add_argument("--department", action="append", default=[], help="提问人部门，可重复")
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

    policies = None
    viewer = None
    if args.policies:
        try:
            manifest = json.loads(Path(args.policies).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            print(f"读策略清单失败：{e}")
            return 1
        policies = policies_from_manifest(manifest)
        viewer = Viewer("evaluator", args.clearance or "内部", tuple(args.department))
        print(f"按角色过滤：密级 {viewer.clearance}"
              f"{'，部门 ' + '、'.join(viewer.departments) if viewer.departments else ''}"
              f"（策略 {len(policies)} 份文档）")
        if args.mode == "chat":
            print("提示：chat 模式的上下文由问答服务拼装，块级过滤这里不生效；"
                  "请在问答服务侧对检索结果套用同一份策略。")
    elif args.clearance:
        print("--clearance 需要配合 --policies 使用")
        return 1

    client = WeKnoraClient()
    results: list[CaseResult] = []
    errors = 0
    for case in cases:
        try:
            results.append(run_case(client, case, args.kb_id, args.mode,
                                    args.session_id, args.match_count,
                                    policies=policies, viewer=viewer))
        except Exception as e:
            errors += 1
            print(f"[{case.get('id')}] 执行失败：{e}")
            results.append(judge(case, "", [], 0.0))

    summary = summarize(results)
    csv_path, md_path = write_report(results, summary, Path(args.out), args.mode)
    if args.summary_json:
        Path(args.summary_json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.summary_json).write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\n明细：{csv_path}\n报告：{md_path}")
    if errors:
        print(f"\n有 {errors} 条用例执行失败：这次结果不能当作评测结论。")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

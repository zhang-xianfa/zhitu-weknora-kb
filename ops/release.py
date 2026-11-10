# -*- coding: utf-8 -*-
"""发布自检：一条命令把"能不能发"跑成一张清单。

发布前真正需要确认的不是"服务起没起来"，而是四件事——

1. 文档结构够不够支撑问答（体检分数）；
2. 同一制度有没有多个版本同时有效（会混答）；
3. 有没有密级高于知识库的文档混进来、目标角色会不会看到不该看的块；
4. 指标有没有相对基线退化（退化就不发）。

这四件事分别有各自的 CLI，但真正发布时没人愿意挨个跑四遍、再人肉对结论。
``ops.release`` 把它们串起来，输出一张可贴到发布单里的表，失败直接退出码 1。

设计上和 ``ops.doctor`` 分工：doctor 查**线上服务**（连通、积压、空召回、延迟），
release 查**离线资产**（文档、版本、密级、指标）。前者需要 WeKnora 在线，
后者完全离线，所以能进 CI。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from eval.caseload import load_cases
from eval.offline import run_offline, summarize_retrieval
from eval.regression import compare
from pipeline.chunker import chunk_document
from pipeline.cleaner import clean
from pipeline.quality import analyze
from pipeline.security import (
    Viewer, check_upload_level, filter_chunks, policy_from_metadata,
)
from pipeline.versioning import find_conflicts, from_metadata, parse_date

_PASS, _WARN, _FAIL, _SKIP = "pass", "warn", "fail", "skip"
_MARK = {_PASS: "通过", _WARN: "告警", _FAIL: "失败", _SKIP: "跳过"}


@dataclass
class Step:
    name: str
    status: str
    detail: str


@dataclass
class Snapshot:
    """一次扫描的全部离线事实：文档、策略、版本、块、体检结果。"""

    root: Path
    docs: list[dict] = field(default_factory=list)

    @property
    def chunks(self) -> list:
        return [c for doc in self.docs for c in doc["chunks"]]

    @property
    def versions(self) -> list:
        return [doc["version"] for doc in self.docs if doc["version"]]

    @property
    def lowest_score(self) -> int:
        return min((doc["quality"].score for doc in self.docs), default=0)


def build_snapshot(root: Path, max_chars: int = 600, overlap: int = 80,
                   suffixes=(".md", ".txt")) -> Snapshot:
    snapshot = Snapshot(root)
    for path in sorted(root.rglob("*")):
        if path.suffix.lower() not in suffixes or not path.is_file():
            continue
        rel = str(path.relative_to(root))
        body, report = clean(path.read_text(encoding="utf-8", errors="replace"), title=path.stem)
        policy = policy_from_metadata(path.stem, report.metadata, body)
        chunks = chunk_document(path.stem, body, max_chars=max_chars, overlap=overlap,
                                clearance=policy.level, departments=policy.departments)
        snapshot.docs.append({
            "file": rel,
            "doc_id": path.stem,
            "metadata": report.metadata,
            "policy": policy,
            "version": from_metadata(path.stem, path.name, report.metadata,
                                     title=path.stem, source=rel),
            "chunks": chunks,
            "quality": analyze(path.stem, chunks, max_chars=max_chars),
        })
    return snapshot


def _check_quality(snapshot: Snapshot, min_score: int) -> Step:
    if not snapshot.docs:
        return Step("文档体检", _SKIP, "没有可检查的文档")
    bad = [d for d in snapshot.docs if d["quality"].score < min_score]
    if bad:
        detail = "、".join(f"{d['doc_id']} {d['quality'].score} 分" for d in bad[:3])
        return Step("文档体检", _FAIL,
                    f"最低 {snapshot.lowest_score} 分，低于门槛 {min_score}：{detail}")
    return Step("文档体检", _PASS,
                f"{len(snapshot.docs)} 篇，最低 {snapshot.lowest_score} 分")


def _check_versions(snapshot: Snapshot, as_of: date | None) -> Step:
    conflicts = find_conflicts(snapshot.versions, as_of)
    high = [c for c in conflicts if c.severity == "high"]
    if high:
        return Step("版本时效", _FAIL,
                    "；".join(c.detail for c in high[:2]))
    if conflicts:
        return Step("版本时效", _WARN,
                    f"{len(snapshot.versions)} 份文档，{len(conflicts)} 条提示（旧版归档/缺少日期）")
    return Step("版本时效", _PASS, f"{len(snapshot.versions)} 份文档，无冲突")


def _check_security(snapshot: Snapshot, kb_clearance: str, viewer: Viewer,
                    strict: bool) -> Step:
    if not snapshot.docs:
        return Step("密级边界", _SKIP, "没有可检查的文档")
    over = []
    for doc in snapshot.docs:
        ok, why = check_upload_level(doc["policy"].level, kb_clearance)
        if not ok:
            over.append(f"{doc['doc_id']}（{doc['policy'].level}）")
    chunks = snapshot.chunks
    kept, blocked = filter_chunks(chunks, viewer)
    detail = f"库密级 {kb_clearance}；角色 {viewer.name}/{viewer.clearance} 可见 {len(kept)}/{len(chunks)} 块"
    if over:
        return Step("密级边界", _FAIL,
                    f"{len(over)} 份文档密级高于知识库：{'、'.join(over[:3])}")
    if blocked:
        return Step("密级边界", _FAIL if strict else _WARN,
                    detail + f"；{len(blocked)} 块对该角色不可见（确认是否应拆库）")
    return Step("密级边界", _PASS, detail)


def _check_metrics(snapshot: Snapshot, cases_path: Path, baseline_path: Path,
                   top_k: int, ignored: set[str]) -> Step:
    if not snapshot.chunks:
        return Step("指标门禁", _SKIP, "没有块，无法跑基线")
    if not cases_path.exists():
        return Step("指标门禁", _SKIP, f"用例文件不存在：{cases_path}")
    if not baseline_path.exists():
        return Step("指标门禁", _SKIP, f"基线不存在：{baseline_path}（先用 --update 建立）")

    cases = load_cases(cases_path)
    results = run_offline([c.as_dict() for c in snapshot.chunks], cases, top_k=top_k)
    summary = summarize_retrieval(results, top_k=top_k)
    with open(baseline_path, encoding="utf-8") as f:
        baseline = json.load(f)
    findings = [f for f in compare(baseline, summary) if f.metric not in ignored]
    bad = [f for f in findings if f.verdict == "fail"]
    if bad:
        return Step("指标门禁", _FAIL,
                    "；".join(f"{f.metric} {f.baseline:g}→{f.current:g}" for f in bad))
    recall = summary.get(f"召回率@{top_k}", 0.0)
    hit = summary.get("检索命中率", 0.0)
    return Step("指标门禁", _PASS, f"召回率@{top_k} {recall:.3f} / 命中率 {hit:.3f}，无退化")


def evaluate(snapshot: Snapshot, *, cases_path: Path, baseline_path: Path,
             min_score: int, kb_clearance: str, viewer: Viewer,
             as_of: date | None, top_k: int, ignored: set[str],
             strict_acl: bool = False, skip_offline: bool = False) -> list[Step]:
    steps = [
        _check_quality(snapshot, min_score),
        _check_versions(snapshot, as_of),
        _check_security(snapshot, kb_clearance, viewer, strict_acl),
    ]
    if skip_offline:
        steps.append(Step("指标门禁", _SKIP, "已按 --skip-offline 跳过"))
    else:
        steps.append(_check_metrics(snapshot, cases_path, baseline_path, top_k, ignored))
    return steps


def format_report(steps: list[Step], root: Path) -> str:
    lines = [f"发布自检：{root}"]
    for i, step in enumerate(steps, 1):
        lines.append(f"[{i}/{len(steps)}] {step.name:<6} {_MARK[step.status]:<4} {step.detail}")
    fails = sum(1 for s in steps if s.status == _FAIL)
    warns = sum(1 for s in steps if s.status == _WARN)
    lines.append(f"结论：{'不通过' if fails else '通过'}（{fails} 项失败，{warns} 项告警）")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="发布前离线自检：体检/版本/密级/指标门禁")
    parser.add_argument("--dir", required=True, help="待发布的文档目录")
    parser.add_argument("--cases", default="eval/cases/enterprise_policy.json")
    parser.add_argument("--baseline", default="eval/baseline/offline.json")
    parser.add_argument("--min-score", type=int, default=60, help="文档体检门槛")
    parser.add_argument("--kb-clearance", default="内部", help="知识库允许的最高密级")
    parser.add_argument("--viewer-clearance", default="内部", help="模拟的提问人密级")
    parser.add_argument("--viewer-department", action="append", default=[], help="模拟的部门，可重复")
    parser.add_argument("--as-of", default="", help="版本时效的判断时点 YYYY-MM-DD")
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--ignore", default="", help="门禁跳过的指标（逗号分隔，CI 可忽略延迟）")
    parser.add_argument("--strict-acl", action="store_true", help="角色看不到块时判为失败而非告警")
    parser.add_argument("--skip-offline", action="store_true", help="跳过指标门禁（只查文档侧）")
    parser.add_argument("--json", default="", help="把检查结果写成 JSON")
    args = parser.parse_args(argv)

    root = Path(args.dir)
    if not root.exists():
        print(f"目录不存在：{root}")
        return 1
    as_of = parse_date(args.as_of) if args.as_of else None
    if args.as_of and not as_of:
        print(f"--as-of 格式不对：{args.as_of}")
        return 1

    snapshot = build_snapshot(root)
    if not snapshot.docs:
        print(f"目录下没有可检查的 Markdown/文本：{root}")
        return 1
    viewer = Viewer("release-check", args.viewer_clearance, tuple(args.viewer_department))
    ignored = {x.strip() for x in args.ignore.split(",") if x.strip()}
    steps = evaluate(
        snapshot,
        cases_path=Path(args.cases),
        baseline_path=Path(args.baseline),
        min_score=args.min_score,
        kb_clearance=args.kb_clearance,
        viewer=viewer,
        as_of=as_of,
        top_k=args.top_k,
        ignored=ignored,
        strict_acl=args.strict_acl,
        skip_offline=args.skip_offline,
    )
    print(format_report(steps, root))

    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(
            {"dir": str(root), "steps": [s.__dict__ for s in steps],
             "passed": all(s.status != _FAIL for s in steps)},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"结果已写入 {out}")

    return 0 if all(s.status != _FAIL for s in steps) else 1


if __name__ == "__main__":
    sys.exit(main())

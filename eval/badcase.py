# -*- coding: utf-8 -*-
"""badcase 回流：把线上"答得不对"变成能复算的用例。

评测集最大的问题不是"不够大"，是**不会长大**：上线时手写的 12 条用例，三个月后还是 12 条，
而真实用户的问题早就换了一轮。这个模块把"用户反馈 → 用例"的路径固定下来：

    反馈（CSV/JSONL） → 去重 → 候选用例（待补 must_include / 负例标记）
    → 复核表（Markdown，给人看） → 人工补全 → --merge 并入用例集

**为什么不自动入集**：一句"答错了"背后可能是三种完全不同的东西——
用户问法不规范、模型没答好、文档里根本没写。自动转成用例，等于把三种噪声一起写进
回归门禁，门禁很快就没人信了。所以机器只做"整理 + 去重 + 生成复核表"，
判定"这条到底该不该考、考什么"必须是人。

字段约定（CSV 一行一条，或 JSONL 一行一个对象）：

    question（必填） / answer / reason / category
    must_include（"500|分管副总" 或 JSON 数组） / expected_source
    expected_clause / unanswerable（true/false/是/否）

合并门槛：负例（unanswerable=true）可以直接入集；正例必须至少有一个 must_include，
否则拒绝合并并说明原因——"没写考点的用例"进了门禁也永远判不出口径。
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from eval.caseload import load_cases

#: 反馈原因 → 复核提示（只提示，不自动改判）
_REASON_HINTS = {
    "没找到": "先确认文档里到底有没有答案：有 → 正例并补 must_include；没有 → 标 unanswerable",
    "未找到": "先确认文档里到底有没有答案：有 → 正例并补 must_include；没有 → 标 unanswerable",
    "拒答": "同上：拒答可能是误拒（该答没答），也可能是正例，取决于文档有没有写",
    "答非所问": "多半是正例：补 must_include（必答要点）；若答案散在两条，考虑拆成两条用例",
    "引用错": "先查 expected_source 与文档 title 是否一致，再补 expected_clause",
    "太慢": "登记到延迟观察，不要入集",
    "卡顿": "登记到延迟观察，不要入集",
}

#: 这些原因说明"不是知识对错问题"，入集没有意义
_NOT_A_CASE = {"太慢", "卡顿"}

def normalize_question(text: str) -> str:
    """去空白、转小写：同一个问题不同排版算同一条，避免用例集里全是近重复。"""
    return re.sub(r"\s+", "", str(text or "")).strip().lower()


def parse_keywords(value) -> list[str]:
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if not value:
        return []
    return [x.strip() for x in re.split(r"[|;；,，]", str(value)) if x.strip()]


def parse_bool(value) -> bool | None:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"true", "1", "yes", "是", "负例"}:
        return True
    if text in {"false", "0", "no", "否", "正例"}:
        return False
    return None


def load_feedback(path: Path) -> list[dict]:
    """读反馈：CSV（含 BOM 也行）或 JSONL。空问题行跳过，不让一行脏数据卡住整批。"""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"反馈文件不存在：{path}")
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    if path.suffix.lower() == ".csv":
        rows = list(csv.DictReader(text.splitlines()))
    else:
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]

    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        question = str(row.get("question") or row.get("问题") or "").strip()
        if not question:
            continue
        out.append({
            "question": question,
            "answer": str(row.get("answer") or row.get("回答") or "").strip(),
            "reason": str(row.get("reason") or row.get("原因") or "").strip(),
            "category": str(row.get("category") or row.get("类别") or "待标注").strip(),
            "must_include": parse_keywords(row.get("must_include")),
            "expected_source": str(row.get("expected_source") or "").strip(),
            "expected_clause": str(row.get("expected_clause") or "").strip(),
            "unanswerable": parse_bool(row.get("unanswerable")),
        })
    if not out:
        raise ValueError(f"反馈里没有有效问题：{path}")
    return out


@dataclass
class Candidate:
    """一条待人工复核的候选用例。"""

    case_id: str
    question: str
    reason: str = ""
    answer: str = ""
    category: str = "待标注"
    must_include: list[str] = field(default_factory=list)
    expected_source: str = ""
    expected_clause: str = ""
    unanswerable: bool | None = None

    @property
    def blocking_missing(self) -> list[str]:
        """不补就不能入集的字段。"""
        if self.unanswerable is True:
            return []
        return [] if self.must_include else ["must_include（或标记 unanswerable）"]

    @property
    def advisory_missing(self) -> list[str]:
        """不阻塞入集、但会让指标少一个维度的字段。"""
        notes = []
        if not self.expected_source:
            notes.append("expected_source（要算引用命中才需要）")
        if not self.expected_clause:
            notes.append("expected_clause（要算召回率@k/MRR 才需要）")
        return notes

    def as_case(self) -> dict:
        case: dict = {"id": self.case_id, "category": self.category, "question": self.question}
        if self.must_include:
            case["must_include"] = list(self.must_include)
        if self.expected_source:
            case["expected_source"] = self.expected_source
        if self.expected_clause:
            case["expected_clause"] = self.expected_clause
        if self.unanswerable is not None:
            case["unanswerable"] = self.unanswerable
        return case

    def review_hint(self) -> str:
        hint = next((text for key, text in _REASON_HINTS.items() if key in self.reason),
                    "按文档事实补 must_include 或标记 unanswerable")
        if any(key in self.reason for key in _NOT_A_CASE):
            hint = "性能问题，不建议入集：" + hint
        return hint


def build_candidates(feedback: list[dict], cases: list[dict],
                     prefix: str = "B") -> tuple[list[Candidate], list[str]]:
    """去重、编号、生成候选。返回 (候选, 被跳过的重复问题)。"""
    existing_questions = {normalize_question(c.get("question")) for c in cases}
    used_ids = {str(c.get("id")) for c in cases if c.get("id")}
    seen = set(existing_questions)
    candidates: list[Candidate] = []
    duplicates: list[str] = []

    def next_id() -> str:
        n = len(candidates) + 1
        while f"{prefix}-{n:02d}" in used_ids:
            n += 1
        used_ids.add(f"{prefix}-{n:02d}")
        return f"{prefix}-{n:02d}"

    for row in feedback:
        key = normalize_question(row["question"])
        if not key:
            continue
        if key in seen:
            duplicates.append(row["question"])
            continue
        seen.add(key)
        candidates.append(Candidate(
            case_id=next_id(),
            question=row["question"],
            reason=row.get("reason", ""),
            answer=row.get("answer", ""),
            category=row.get("category") or "待标注",
            must_include=row.get("must_include") or [],
            expected_source=row.get("expected_source", ""),
            expected_clause=row.get("expected_clause", ""),
            unanswerable=row.get("unanswerable"),
        ))
    return candidates, duplicates


def write_candidates(candidates: list[Candidate], out_json: Path, out_md: Path) -> tuple[Path, Path]:
    """写候选 JSON（给程序）+ 复核表（给人）。"""
    out_json.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "name": "badcase 回流候选（待人工复核）",
        "note": "由 eval.badcase 生成。请补全 must_include 或 unanswerable 后，用 --merge 并入种子用例集。",
        # reason 只留在候选文件里：复核时要看，入集后不写进用例集
        "cases": [{**c.as_case(), **({"reason": c.reason} if c.reason else {})}
                  for c in candidates],
    }
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = ["# badcase 复核表", "",
             f"候选 {len(candidates)} 条。**补全后再入集**，机器不替人判断对错。", "",
             "| 编号 | 问题 | 原因 | 模型当时的回答（摘录） | 待补 | 复核提示 |",
             "|---|---|---|---|---|---|"]
    for c in candidates:
        todo = c.blocking_missing + c.advisory_missing or ["无需补充"]
        answer = c.answer.replace("\n", " ")[:60] or "—"
        lines.append(f"| {c.case_id} | {c.question} | {c.reason or '—'} | {answer} | "
                     f"{'；'.join(todo)} | {c.review_hint()} |")
    lines += ["", "## 入集方式", "",
              "```bash",
              f"python -m eval.badcase --feedback <反馈文件> --cases <用例集> --merge",
              "```",
              "",
              "合并门槛：负例直接入集；正例必须至少一个 `must_include`，否则拒绝并说明原因。"]
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text("\n".join(lines), encoding="utf-8")
    return out_json, out_md


def _candidate_from_case(case: dict) -> Candidate:
    return Candidate(
        case_id=str(case.get("id") or ""),
        question=str(case.get("question") or ""),
        reason=str(case.get("reason") or ""),
        answer=str(case.get("answer") or ""),
        category=str(case.get("category") or "待标注"),
        must_include=parse_keywords(case.get("must_include")),
        expected_source=str(case.get("expected_source") or ""),
        expected_clause=str(case.get("expected_clause") or ""),
        unanswerable=parse_bool(case.get("unanswerable")),
    )


def load_candidates(path: Path) -> list[Candidate]:
    """读人工复核过的候选文件（--out 产出的 JSON）。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    cases = data.get("cases", data) if isinstance(data, dict) else data
    if not isinstance(cases, list):
        raise ValueError("候选文件格式不对：应为数组或 {cases: [...]}")
    return [_candidate_from_case(c) for c in cases if isinstance(c, dict)]


def merge_candidates(candidates: list[Candidate], cases_path: Path) -> tuple[int, list[str]]:
    """把复核通过的候选并入用例集；返回 (合并数, 拒绝原因)。"""
    cases_path = Path(cases_path)
    data = json.loads(cases_path.read_text(encoding="utf-8"))
    cases = data.get("cases", data) if isinstance(data, dict) else data
    if not isinstance(cases, list):
        raise ValueError("用例文件格式不对：应为数组或 {cases: [...]}")

    existing_questions = {normalize_question(c.get("question")) for c in cases}
    existing_ids = {str(c.get("id")) for c in cases if c.get("id")}
    merged = 0
    rejected: list[str] = []

    for c in candidates:
        if normalize_question(c.question) in existing_questions:
            rejected.append(f"{c.case_id} 与已有用例重复：{c.question}")
            continue
        if any(key in c.reason for key in _NOT_A_CASE):
            rejected.append(f"{c.case_id} 是性能问题（{c.reason}），不入用例集：{c.question}")
            continue
        if c.blocking_missing:
            rejected.append(f"{c.case_id} 缺少 must_include 且未标负例，拒绝合并：{c.question}")
            continue
        if c.case_id in existing_ids:
            rejected.append(f"{c.case_id} 编号冲突")
            continue
        cases.append(c.as_case())
        existing_questions.add(normalize_question(c.question))
        existing_ids.add(c.case_id)
        merged += 1

    if merged:
        cases_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        load_cases(cases_path)          # 写回后立刻按评测口径校验，坏数据不留在仓库里
    return merged, rejected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="badcase 回流：反馈 → 候选用例 → 并入评测集")
    parser.add_argument("--feedback", required=True, help="反馈文件（.csv 或 .jsonl）")
    parser.add_argument("--cases", default="eval/cases/enterprise_policy.json", help="种子用例集（用于去重）")
    parser.add_argument("--out", default="eval/cases/field_candidates.json", help="候选 JSON 输出")
    parser.add_argument("--md", default="eval/report/badcase_review.md", help="复核表输出")
    parser.add_argument("--merge", action="store_true", help="复核完成后并入用例集")
    args = parser.parse_args(argv)

    cases_path = Path(args.cases)
    cases = load_cases(cases_path) if cases_path.exists() else []
    try:
        feedback = load_feedback(Path(args.feedback))
    except (OSError, ValueError) as e:
        print(f"读反馈失败：{e}")
        return 1

    candidates, duplicates = build_candidates(feedback, cases)
    if args.merge:
        # 合并的是**人工复核过**的候选文件，不是原始反馈：
        # 直接拿反馈重新生成，会把复核时补的 must_include 全部丢掉。
        candidate_path = Path(args.out)
        if candidate_path.exists():
            try:
                candidates = load_candidates(candidate_path)
            except (OSError, ValueError) as e:
                print(f"读候选文件失败：{e}")
                return 1
        else:
            print(f"候选文件不存在（{candidate_path}），按未复核处理——正例基本都会被拒绝。")
        merged, rejected = merge_candidates(candidates, cases_path)
        print(f"候选 {len(candidates)} 条：合并 {merged} 条，拒绝 {len(rejected)} 条，"
              f"重复 {len(duplicates)} 条")
        for reason in rejected:
            print(" -", reason)
        return 0 if merged else 2

    out_json, out_md = write_candidates(candidates, Path(args.out), Path(args.md))
    blocking = sum(1 for c in candidates if c.blocking_missing)
    print(f"反馈 {len(feedback)} 条 → 候选 {len(candidates)} 条（其中 {blocking} 条待补必答要点），"
          f"重复跳过 {len(duplicates)} 条")
    print(f"候选：{out_json}\n复核表：{out_md}")
    print("补全后用 --merge 并入用例集；合并门槛见复核表末尾。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

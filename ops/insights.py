# -*- coding: utf-8 -*-
"""业务指标：技术指标说"答到了"，业务指标说"解决了没有"。

命中率 90% 的项目照样可能被业务方判死——因为用户点完"没解决"就走了。
这个模块把问答交互日志算成业务方真正会看的几个数：

| 指标 | 定义 | 为什么比命中率真实 |
|---|---|---|
| 首次解决率（代理） | 点了"有用"、没转人工、7 天内没重复问同一问题 | 命中率只说明"答到了"，不说明"解决了" |
| 转人工率 | 触发转人工的比例 | 直接对应人力成本节省 |
| 引用点击率 | 点开引用核对的比例 | 衡量引用位置有没有用 |
| 重复提问率 | 同一人 7 天内重复问同一问题 | 答案不可执行时最典型的行为 |
| 单次成本 | 输入/输出 token × 单价 | 决定能不能全员开放 |

为什么叫"代理"：真正的首次解决率要接工单/转人工系统，还要区分"换个人问同一件事"。
POC 阶段只能用行为信号近似，所以这里把口径写死并标注"代理"，不对外承诺。

日志格式（JSONL，一行一次问答，缺字段有默认值）：

    {"ts":"2026-11-10T10:00:00","user_id":"u1","question":"出差9天谁审批",
     "answered":true,"refused":false,"useful":true,"transferred":false,
     "citation_clicked":true,"latency_ms":800,"tokens_in":3000,"tokens_out":200}

``--export-feedback`` 可以把"没用/转人工/拒答"的记录导成 badcase 回流格式，
直接交给 ``eval.badcase`` —— 业务指标和评测集从此是同一个数据源。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from eval.metrics import percentile

_WS = re.compile(r"[\s？?！!。.，,；;：:、\"'“”‘’]+")


def _question_key(text: str) -> str:
    """问题归一：去空白与常见标点，让"年假多少天"和"年假多少天？"算同一个问题。"""
    return _WS.sub("", str(text or "")).lower()


def parse_ts(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _as_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"true", "1", "yes", "是"}:
        return True
    if text in {"false", "0", "no", "否"}:
        return False
    return default


def _as_useful(value) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "1", "是", "有用", "解决"}:
        return True
    if text in {"false", "0", "否", "没用", "没解决"}:
        return False
    return None


@dataclass
class Interaction:
    question: str
    ts: datetime | None = None
    user_id: str = ""
    department: str = ""
    answered: bool = True
    refused: bool = False
    useful: bool | None = None
    transferred: bool = False
    citation_clicked: bool = False
    latency_ms: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    answer: str = ""
    #: 由 summarize 计算：同一人窗口期内重复问同一问题
    repeat: bool = field(default=False)


def load_interactions(path: Path) -> list[Interaction]:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"日志不存在：{path}")
    out: list[Interaction] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        question = str(row.get("question") or "").strip()
        if not question:
            continue
        refused = _as_bool(row.get("refused"))
        out.append(Interaction(
            question=question,
            ts=parse_ts(row.get("ts")),
            user_id=str(row.get("user_id") or ""),
            department=str(row.get("department") or row.get("dept") or row.get("部门") or ""),
            answered=_as_bool(row.get("answered"), default=True) and not refused,
            refused=refused,
            useful=_as_useful(row.get("useful")),
            transferred=_as_bool(row.get("transferred")),
            citation_clicked=_as_bool(row.get("citation_clicked")),
            latency_ms=float(row.get("latency_ms") or 0),
            tokens_in=int(row.get("tokens_in") or 0),
            tokens_out=int(row.get("tokens_out") or 0),
            answer=str(row.get("answer") or ""),
        ))
    if not out:
        raise ValueError(f"日志里没有有效记录：{path}")
    return out


def mark_repeats(items: list[Interaction], window_days: int = 7) -> None:
    """标记窗口期内的重复提问：同一用户 + 同一问题（去空白）视为重复。

    用时间戳比较而不是 datetime 相减：日志里时区写法常常不一致，
    aware/naive 混在一起相减会直接抛异常，把整份报表搞挂。
    """
    order = sorted(items, key=lambda x: (x.ts is None, x.ts.timestamp() if x.ts else 0.0))
    for item in items:                  # 允许重复计算同一批记录（报表重跑）
        item.repeat = False
    last: dict[tuple[str, str], float] = {}
    window = window_days * 86400
    for item in order:
        key = (item.user_id or "-", _question_key(item.question))
        if item.ts is None:
            continue
        current = item.ts.timestamp()
        previous = last.get(key)
        if previous is not None and current - previous <= window:
            item.repeat = True
        last[key] = current


def _rate(part: int, whole: int) -> float:
    return round(part / whole, 4) if whole else 0.0


def summarize(items: list[Interaction], *, price_in: float = 0.0, price_out: float = 0.0,
              monthly_volume: int = 0, repeat_window_days: int = 7) -> dict:
    mark_repeats(items, repeat_window_days)
    total = len(items)
    answered = [i for i in items if i.answered]
    refused = [i for i in items if i.refused]
    transferred = [i for i in items if i.transferred]
    rated = [i for i in items if i.useful is not None]
    useful = [i for i in rated if i.useful]
    clicked = [i for i in answered if i.citation_clicked]
    resolved = [i for i in items
                if i.useful is True and not i.transferred and not i.repeat]
    repeats = [i for i in items if i.repeat]
    latencies = sorted(i.latency_ms for i in items if i.latency_ms > 0)
    tokens_in = sum(i.tokens_in for i in items)
    tokens_out = sum(i.tokens_out for i in items)
    cost_per_query = 0.0
    if price_in or price_out:
        cost_per_query = (tokens_in / 1000 * price_in + tokens_out / 1000 * price_out) / total

    summary = {
        "问答次数": total,
        "作答率": _rate(len(answered), total),
        "拒答率": _rate(len(refused), total),
        "转人工率": _rate(len(transferred), total),
        "有用率(已评分)": _rate(len(useful), len(rated)),
        "评分覆盖率": _rate(len(rated), total),
        "引用点击率": _rate(len(clicked), len(answered)),
        f"首次解决率(代理,{repeat_window_days}天)": _rate(len(resolved), total),
        "重复提问率": _rate(len(repeats), total),
        "延迟P50_ms": round(percentile(latencies, 0.50), 1) if latencies else 0.0,
        "延迟P95_ms": round(percentile(latencies, 0.95), 1) if latencies else 0.0,
        "平均输入tokens": round(tokens_in / total, 1) if total else 0.0,
        "平均输出tokens": round(tokens_out / total, 1) if total else 0.0,
        "单次成本": round(cost_per_query, 4),
    }
    if monthly_volume:
        summary["月成本估算"] = round(cost_per_query * monthly_volume, 2)
    return summary


def department_summary(items: list[Interaction], *, price_in: float = 0.0,
                       price_out: float = 0.0, repeat_window_days: int = 7) -> dict[str, dict]:
    """按部门拆一份：谁在用、谁在转人工、谁的答案是"没用"的。

    总账只能回答"这个系统好不好"，分部门才能回答"先给谁补文档、先找谁做培训"。
    重复提问在部门内判定（同一部门同一问题），跨部门的相同问题不算重复。
    """
    groups: dict[str, list[Interaction]] = {}
    for item in items:
        groups.setdefault(item.department or "未标注", []).append(item)
    return {
        dept: summarize(group, price_in=price_in, price_out=price_out,
                        repeat_window_days=repeat_window_days)
        for dept, group in groups.items()
    }


def top_problems(items: list[Interaction], limit: int = 5) -> list[dict]:
    """把"没用/转人工/拒答"的问题聚合成高频清单，直接作为 badcase 候选。

    单条反馈看不出趋势，聚合成"同一个问题被多少人问、为什么不行"，
    才知道该补文档（拒答多）还是改答案（转人工多）。
    """
    grouped: dict[str, dict] = {}
    for item in items:
        reasons = []
        if item.refused:
            reasons.append("拒答")
        if item.useful is False:
            reasons.append("没用")
        if item.transferred:
            reasons.append("转人工")
        if not reasons:
            continue
        key = _question_key(item.question)
        entry = grouped.setdefault(key, {"question": item.question, "count": 0,
                                         "reasons": set(), "departments": set()})
        entry["count"] += 1
        entry["reasons"].update(reasons)
        if item.department:
            entry["departments"].add(item.department)
    rows = sorted(grouped.values(), key=lambda x: (-x["count"], x["question"]))[:limit]
    return [{"question": row["question"], "count": row["count"],
             "reasons": sorted(row["reasons"]),
             "departments": sorted(row["departments"])} for row in rows]


def insights_notes(summary: dict) -> list[str]:
    """把指标翻译成运营动作。纯阈值判断，不涉及模型。"""
    notes: list[str] = []
    if summary.get("评分覆盖率", 1) < 0.3:
        notes.append("评分覆盖率低：先把「有用/没用」按钮埋上，否则首次解决率不可信")
    if summary.get("转人工率", 0) > 0.3:
        notes.append("转人工率 > 30%：先看拒答率与含糊率，判断是缺文档还是答了但没解决")
    if summary.get("拒答率", 0) > 0.2:
        notes.append("拒答率 > 20%：知识库覆盖不足，优先补高频制度文档")
    if summary.get("重复提问率", 0) > 0.2:
        notes.append("重复提问率 > 20%：同一问题反复问，通常是答案不可执行（缺流程/入口）")
    first_key = next((key for key in summary if key.startswith("首次解决率")), "")
    if first_key and summary[first_key] < 0.6:
        notes.append("首次解决率低于 60%：按 docs/07 阶段 1 的退出门槛，还不适合全量开放")
    if not notes:
        notes.append("暂无异常；继续观察转人工率与重复提问率的变化趋势")
    return notes


def export_feedback(items: list[Interaction], path: Path) -> int:
    """把"没用/转人工/拒答"导成 badcase 回流格式，交给 eval.badcase。"""
    rows = []
    for item in items:
        if item.useful is not False and not item.transferred and not item.refused:
            continue
        reason = "没找到" if item.refused else "答非所问"
        row = {"question": item.question, "reason": reason, "answer": item.answer[:300]}
        rows.append(row)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows),
                    encoding="utf-8")
    return len(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="问答业务指标（首次解决率/转人工/成本）")
    parser.add_argument("--log", required=True, help="交互日志 JSONL")
    parser.add_argument("--price-in", type=float, default=0.0, help="输入单价（元 / 千 tokens）")
    parser.add_argument("--price-out", type=float, default=0.0, help="输出单价（元 / 千 tokens）")
    parser.add_argument("--monthly-volume", type=int, default=0, help="按月问答量估算成本")
    parser.add_argument("--repeat-window-days", type=int, default=7)
    parser.add_argument("--by-department", action="store_true", help="按部门拆一张表")
    parser.add_argument("--top", type=int, default=5, help="高频问题条数，0 表示不列")
    parser.add_argument("--json", default="", help="把汇总写成 JSON")
    parser.add_argument("--export-feedback", default="",
                        help="把没用/转人工/拒答导成 badcase 反馈 JSONL")
    args = parser.parse_args(argv)

    try:
        items = load_interactions(Path(args.log))
    except (OSError, ValueError) as e:
        print(f"读日志失败：{e}")
        return 1

    summary = summarize(items, price_in=args.price_in, price_out=args.price_out,
                        monthly_volume=args.monthly_volume,
                        repeat_window_days=args.repeat_window_days)
    print(f"交互日志：{args.log}    记录 {len(items)} 条")
    print("\n| 指标 | 数值 |\n|---|---|")
    for key, value in summary.items():
        print(f"| {key} | {value} |")
    print("\n运营判读：")
    for note in insights_notes(summary):
        print(f" - {note}")

    if args.by_department:
        print("\n分部门：")
        print("| 部门 | 问答次数 | 转人工率 | 重复提问率 | 首次解决率(代理) | 单次成本 |")
        print("|---|---|---|---|---|---|")
        for dept, dept_summary in department_summary(
                items, price_in=args.price_in, price_out=args.price_out,
                repeat_window_days=args.repeat_window_days).items():
            first = next((v for k, v in dept_summary.items()
                          if k.startswith("首次解决率")), 0.0)
            print(f"| {dept} | {dept_summary['问答次数']} | {dept_summary['转人工率']} | "
                  f"{dept_summary['重复提问率']} | {first} | {dept_summary['单次成本']} |")

    if args.top:
        problems = top_problems(items, args.top)
        if problems:
            print(f"\n高频问题（建议 badcase 回流，前 {len(problems)} 条）：")
            print("| 问题 | 次数 | 原因 | 部门 |")
            print("|---|---|---|---|")
            for row in problems:
                print(f"| {row['question']} | {row['count']} | "
                      f"{'、'.join(row['reasons'])} | {'、'.join(row['departments']) or '—'} |")

    if not (args.price_in or args.price_out):
        print("\n提示：未配置 token 单价，成本按 0 计；用 --price-in/--price-out 传入")
    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n汇总已写入 {out}")
    if args.export_feedback:
        count = export_feedback(items, Path(args.export_feedback))
        print(f"\n已导出 {count} 条 badcase 反馈：{args.export_feedback}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

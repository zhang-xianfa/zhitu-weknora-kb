# -*- coding: utf-8 -*-
"""回归门禁：把"这次改动到底有没有变差"从口头判断变成退出码。

没有门禁的评测，最后都会退化成同一幕：改完分块参数，跑一遍报告，
命中率从 87.5% 掉到 75%，有人说"样本太小别当真"，有人说"看着还行"，
然后就这么上线了。门禁的作用不是追求绝对正确，而是**把不可接受的退化挡在发布之前**，
并且让"为什么放行"必须写下来。

三条原则：

1. **只拦退化，不拦波动**。阈值是"允许的最大下降"（如 5 个百分点），
   小样本噪声不该天天报警——天天报警的门禁等于没有门禁；
2. **方向要分开**。命中率/引用率/拒答率越低越差；误拒率、含糊率、延迟越高越差。
   用一套阈值套所有指标，必然有一半是错的；
3. **口径必须同源**。基线文件和当前文件都由同一支 ``--summary-json`` 产出，
   人看的报告和机器卡的门禁用的是同一组数字。

用法::

    # 第一次建立基线（提交到仓库，作为团队共同基准）
    python -m eval.offline --chunks chunks.jsonl --cases ... --summary-json eval/baseline/offline.json

    # 改动后比较；有退化则退出码 1
    python -m eval.offline --chunks chunks.jsonl --cases ... --summary-json eval/report/offline.json
    python -m eval.regression --baseline eval/baseline/offline.json --current eval/report/offline.json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

#: 指标 → (方向, 阈值)。方向：
#:   drop  = 允许下降的绝对量（0.05 = 5 个百分点）
#:   rise  = 允许上升的绝对量
#:   ratio = 允许变差到基线的多少倍
GATES: dict[str, tuple[str, float]] = {
    "命中率": ("drop", 0.05),
    "检索命中率": ("drop", 0.05),
    "引用命中率": ("drop", 0.05),
    "拒答率(负例)": ("drop", 0.05),
    "MRR": ("drop", 0.05),
    "误拒率": ("rise", 0.05),
    "含糊率": ("rise", 0.05),
    "延迟P50_ms": ("ratio", 1.5),
    "延迟P95_ms": ("ratio", 2.0),
    "本地检索P50_ms": ("ratio", 2.0),
    "本地检索P95_ms": ("ratio", 2.0),
}

_RECALL_PREFIX = "召回率@"


@dataclass
class Finding:
    metric: str
    baseline: float
    current: float
    verdict: str        # pass / fail / warn
    detail: str


def _num(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _gate_for(metric: str) -> tuple[str, float] | None:
    if metric in GATES:
        return GATES[metric]
    if metric.startswith(_RECALL_PREFIX):
        return ("drop", 0.05)
    return None


def compare(baseline: dict, current: dict,
            gates: dict[str, tuple[str, float]] | None = None) -> list[Finding]:
    """按指标方向比较两份汇总。只比较两边都有、且是数字的指标。"""
    findings: list[Finding] = []
    merged = {**GATES, **(gates or {})}
    for metric, (direction, limit) in merged.items():
        old, new = _num(baseline.get(metric)), _num(current.get(metric))
        if old is None or new is None:
            continue
        findings.append(_judge(metric, direction, limit, old, new))
    # 召回率@k 的 k 由 --top-k 决定，键名不固定，统一按"越低越差"处理
    for name in baseline:
        if not name.startswith(_RECALL_PREFIX) or name in merged:
            continue
        old, new = _num(baseline.get(name)), _num(current.get(name))
        if old is None or new is None:
            continue
        findings.append(_judge(name, "drop", 0.05, old, new))
    return findings


def _judge(metric: str, direction: str, limit: float, old: float, new: float) -> Finding:
    if direction == "drop":
        drop = old - new
        if drop > limit:
            return Finding(metric, old, new, "fail", f"下降 {drop * 100:.1f} 个百分点（允许 {limit * 100:.0f}）")
        return Finding(metric, old, new, "pass", f"变化 {(new - old) * 100:+.1f} 个百分点")
    if direction == "rise":
        rise = new - old
        if rise > limit:
            return Finding(metric, old, new, "fail", f"上升 {rise * 100:.1f} 个百分点（允许 {limit * 100:.0f}）")
        return Finding(metric, old, new, "pass", f"变化 {rise * 100:+.1f} 个百分点")
    # ratio：基线为 0 时无法用倍数判断，退化为"从 0 变正"也不拦（通常是首次有数据）
    if old <= 0:
        return Finding(metric, old, new, "warn", "基线为 0，无法按倍数比较")
    if new > old * limit:
        return Finding(metric, old, new, "fail", f"是基线的 {new / old:.2f} 倍（允许 {limit:.2f} 倍）")
    return Finding(metric, old, new, "pass", f"是基线的 {new / old:.2f} 倍")


def report(findings: list[Finding], baseline_path: str, current_path: str) -> str:
    lines = [f"基线：{baseline_path}", f"当前：{current_path}", "",
             "| 指标 | 基线 | 当前 | 判定 | 说明 |", "|---|---|---|---|---|"]
    for f in findings:
        mark = {"pass": "通过", "fail": "退化", "warn": "提示"}[f.verdict]
        lines.append(f"| {f.metric} | {f.baseline:g} | {f.current:g} | {mark} | {f.detail} |")
    bad = [f for f in findings if f.verdict == "fail"]
    lines += ["", f"结论：{'不通过，' + str(len(bad)) + ' 项退化' if bad else '通过'}"]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="评测回归门禁")
    parser.add_argument("--baseline", required=True, help="基线汇总 JSON（仓库内提交）")
    parser.add_argument("--current", required=True, help="本次汇总 JSON")
    parser.add_argument("--json", default="", help="把比较结果写成 JSON")
    parser.add_argument("--ignore", default="",
                        help="逗号分隔的指标名，跳过比较（CI 机器噪声大时忽略延迟类指标）")
    parser.add_argument("--update", action="store_true",
                        help="把当前结果写回基线（只在确认改进后使用，并提交到仓库）")
    args = parser.parse_args(argv)

    base_path, cur_path = Path(args.baseline), Path(args.current)
    if not cur_path.exists():
        print(f"当前汇总不存在：{cur_path}")
        return 2
    if args.update:
        base_path.parent.mkdir(parents=True, exist_ok=True)
        base_path.write_text(cur_path.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"基线已更新：{base_path}")
        return 0
    if not base_path.exists():
        print(f"基线不存在：{base_path}（首次请用 --update 建立）")
        return 2

    baseline = json.loads(base_path.read_text(encoding="utf-8"))
    current = json.loads(cur_path.read_text(encoding="utf-8"))
    if not isinstance(baseline, dict) or not isinstance(current, dict):
        print("汇总文件应为 JSON 对象")
        return 2

    findings = compare(baseline, current)
    ignored = {name.strip() for name in args.ignore.split(",") if name.strip()}
    if ignored:
        findings = [f for f in findings if f.metric not in ignored]
        print("跳过指标：" + "、".join(sorted(ignored)))
    print(report(findings, str(base_path), str(cur_path)))
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(
            [f.__dict__ for f in findings], ensure_ascii=False, indent=2), encoding="utf-8")
    return 1 if any(f.verdict == "fail" for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())

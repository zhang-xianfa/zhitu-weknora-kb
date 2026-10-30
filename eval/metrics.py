# -*- coding: utf-8 -*-
"""评测指标：全部是纯函数，便于单测和复算。

指标口径刻意做得"笨"——关键词命中、引用文件名命中、拒答措辞识别。
不用 LLM 当裁判：LLM 裁判本身要评测，企业侧没人愿意为一个不稳定的分数背书。
笨口径的好处是**任何人拿同一批数据都能算出同一个数**。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: 拒答措辞（模型说"资料里没有"时的常见表达）
REFUSAL_MARKERS = [
    "没有找到", "未找到", "资料中没有", "文档中未", "无法从", "不包含",
    "没有相关", "无法确定", "建议咨询", "超出知识库", "没有提及", "未提及",
]


@dataclass
class CaseResult:
    case_id: str
    category: str
    question: str
    answer: str
    citations: list[str]
    latency_ms: float
    expected_keywords: list[str]
    expected_source: str
    unanswerable: bool


def normalize(text: str) -> str:
    """去掉空白与全角空格，避免"第 5 条"和"第5条"被算成不同。"""
    return re.sub(r"\s+", "", text or "")


def keyword_hit(answer: str, keywords: list[str]) -> bool:
    """必答要点是否全部出现（关键词为空视为不考核）。"""
    if not keywords:
        return True
    ans = normalize(answer)
    return all(normalize(k) in ans for k in keywords)


def source_hit(citations: list[str], expected_source: str) -> bool | None:
    """引用里是否出现预期文档。expected_source 为空时该用例不考核引用。"""
    if not expected_source:
        return None
    needle = normalize(expected_source)
    return any(needle in normalize(c) for c in citations)


def is_refusal(answer: str) -> bool:
    return any(marker in (answer or "") for marker in REFUSAL_MARKERS)


def judge(case: dict, answer: str, citations: list[str], latency_ms: float) -> CaseResult:
    return CaseResult(
        case_id=case.get("id", ""),
        category=case.get("category", ""),
        question=case.get("question", ""),
        answer=answer,
        citations=citations,
        latency_ms=latency_ms,
        expected_keywords=case.get("must_include", []),
        expected_source=case.get("expected_source", ""),
        unanswerable=bool(case.get("unanswerable", False)),
    )


def summarize(results: list[CaseResult]) -> dict:
    """汇总成可直接写报告的指标。

    有答案用例：命中率、引用命中率、误拒率；
    无答案用例：拒答率（越高越好）；
    全体：延迟 P50 / P95。
    """
    answerable = [r for r in results if not r.unanswerable]
    unanswerable = [r for r in results if r.unanswerable]

    hit = sum(1 for r in answerable if keyword_hit(r.answer, r.expected_keywords))
    cited = [r for r in answerable if source_hit(r.citations, r.expected_source) is not None]
    cited_ok = sum(1 for r in cited if source_hit(r.citations, r.expected_source))
    wrongly_refused = sum(1 for r in answerable if is_refusal(r.answer))
    refused_ok = sum(1 for r in unanswerable if is_refusal(r.answer))

    latencies = sorted(r.latency_ms for r in results)

    def pct(part: int, whole: int) -> float:
        return round(part / whole, 4) if whole else 0.0

    return {
        "用例数": len(results),
        "有答案用例": len(answerable),
        "无答案用例": len(unanswerable),
        "命中率": pct(hit, len(answerable)),
        "引用命中率": pct(cited_ok, len(cited)),
        "误拒率": pct(wrongly_refused, len(answerable)),
        "拒答率(负例)": pct(refused_ok, len(unanswerable)),
        "延迟P50_ms": round(percentile(latencies, 0.50), 1) if latencies else 0.0,
        "延迟P95_ms": round(percentile(latencies, 0.95), 1) if latencies else 0.0,
    }


def percentile(sorted_values: list[float], q: float) -> float:
    """最近秩法百分位（值已排序）。样本量小时比插值法更不容易给出幻觉精度。"""
    if not sorted_values:
        return 0.0
    idx = min(len(sorted_values) - 1, max(0, int(round(q * (len(sorted_values) - 1)))))
    return sorted_values[idx]


def failures(results: list[CaseResult]) -> list[CaseResult]:
    """挑出需要人工看的用例：负例答了、正例拒答了、要点没答全。"""
    out = []
    for r in results:
        if r.unanswerable:
            if not is_refusal(r.answer):
                out.append(r)
        elif is_refusal(r.answer) or not keyword_hit(r.answer, r.expected_keywords):
            out.append(r)
    return out

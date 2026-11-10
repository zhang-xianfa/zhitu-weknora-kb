# -*- coding: utf-8 -*-
"""评测指标：全部是纯函数，便于单测和复算。

指标口径刻意做得"笨"——关键词命中、引用文件名命中、拒答措辞识别。
不用 LLM 当裁判：LLM 裁判本身要评测，企业侧没人愿意为一个不稳定的分数背书。
笨口径的好处是**任何人拿同一批数据都能算出同一个数**。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: 拒答措辞（模型说"资料里没有"时的常见表达）
REFUSAL_MARKERS = [
    "没有找到", "未找到", "资料中没有", "文档中未", "无法从", "不包含",
    "没有相关", "无法确定", "建议咨询", "超出知识库", "没有提及", "未提及",
]

#: 含糊措辞：既没拒答、也没给出明确结论。"建议咨询 HR"这种回答在企业场景里
#: 常常和拒答一样不可用（用户还是要再问一个人），但它的指标含义不同：
#: 拒答是"知识库里没有"，含糊是"知识库里有但模型不敢用"。混在一起算会掩盖问题。
HEDGE_MARKERS = ["请以", "以最新", "具体以", "建议联系", "建议与", "建议向", "可能需要", "视情况"]


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
    #: 答案所在的预期条款（如"第5条"），用于算召回率@k / MRR；为空则该用例不考核召回
    expected_clause: str = ""
    #: 实际召回内容（块文本或层级路径），按检索顺序
    retrieved: list[str] = field(default_factory=list)
    #: 被权限过滤拦下的块数（未进上下文）
    blocked: int = 0


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


def classify_answer(answer: str) -> str:
    """把回答分成三档：refused（明确拒答）/ hedged（含糊回避）/ answered（给了结论）。

    为什么值得多分一档：拒答率高说明"知识库里没有"，该去补文档；
    含糊率高说明"知识库里有、但模型不敢下结论"，该去调 prompt 或补检索上下文。
    两种问题的处理方向完全相反，混在一个指标里就等于没有指标。
    """
    text = answer or ""
    if is_refusal(text):
        return "refused"
    if any(marker in text for marker in HEDGE_MARKERS):
        return "hedged"
    return "answered"


def clause_rank(retrieved: list[str], clause: str) -> int:
    """预期条款在召回列表里的名次（1 开始）；没召回返回 0。

    比"要点是否出现在拼接文本里"更早一层：先看块有没有被捞出来，
    再看见到的块里有没有那句话。两层分开，才知道该改分块还是改标注。
    """
    if not clause:
        return 0
    needle = normalize(clause)
    for i, text in enumerate(retrieved, 1):
        if needle and needle in normalize(text):
            return i
    return 0


def recall_at_k(results: list[CaseResult], k: int) -> float:
    """召回率@k：需要考核召回的用例中，预期条款进入前 k 条的比例。"""
    graded = [r for r in results if not r.unanswerable and r.expected_clause]
    if not graded:
        return 0.0
    hit = sum(1 for r in graded if 0 < clause_rank(r.retrieved, r.expected_clause) <= k)
    return round(hit / len(graded), 4)


def mrr(results: list[CaseResult]) -> float:
    """MRR：预期条款名次倒数的平均值。看"排得靠不靠前"，不只看"有没有"。"""
    graded = [r for r in results if not r.unanswerable and r.expected_clause]
    if not graded:
        return 0.0
    total = 0.0
    for r in graded:
        rank = clause_rank(r.retrieved, r.expected_clause)
        if rank:
            total += 1.0 / rank
    return round(total / len(graded), 4)


def judge(case: dict, answer: str, citations: list[str], latency_ms: float,
          retrieved: list[str] | None = None, blocked: int = 0) -> CaseResult:
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
        expected_clause=case.get("expected_clause", ""),
        retrieved=list(retrieved or []),
        blocked=blocked,
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
    hedged = sum(1 for r in answerable if classify_answer(r.answer) == "hedged")
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
        "含糊率": pct(hedged, len(answerable)),
        "拒答率(负例)": pct(refused_ok, len(unanswerable)),
        "越权拦截块": sum(r.blocked for r in results),
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
    """挑出需要人工看的用例：负例答了、正例拒答或含糊、要点没答全。"""
    out = []
    for r in results:
        if r.unanswerable:
            if not is_refusal(r.answer):
                out.append(r)
        elif is_refusal(r.answer) or classify_answer(r.answer) == "hedged" \
                or not keyword_hit(r.answer, r.expected_keywords):
            out.append(r)
    return out

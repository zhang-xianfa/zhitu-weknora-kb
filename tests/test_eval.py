from eval.metrics import (
    CaseResult, is_refusal, keyword_hit, percentile, source_hit, summarize,
)
from eval.runner import chunk_source, chunk_text, extract_chunks, load_cases


def test_keyword_hit_ignores_whitespace():
    assert keyword_hit("第 15 个工作日", ["15"])
    assert keyword_hit("需分管副总审批", ["分管副总"])
    assert not keyword_hit("由主管审批", ["分管副总"])


def test_refusal_detection():
    assert is_refusal("资料中没有找到相关规定。")
    assert is_refusal("知识库不包含该内容")
    assert not is_refusal("报销须在 15 个工作日内提交。")


def test_source_hit():
    assert source_hit(["企业差旅与费用管理办法.pdf"], "企业差旅与费用管理办法")
    assert not source_hit(["员工手册.pdf"], "企业差旅与费用管理办法")
    assert source_hit([], "") is None


def _result(category, answer, keywords, unanswerable=False, latency=100.0):
    return CaseResult("c", category, "q", answer, ["制度.md"], latency,
                      keywords, "制度", unanswerable)


def test_summarize_computes_rates():
    results = [
        _result("事实型", "标准是 500 元", ["500"], latency=100),
        _result("事实型", "标准是 400 元", ["500"], latency=300),
        _result("负例", "资料中没有相关规定", [], unanswerable=True, latency=200),
        _result("负例", "大概 30 天吧", [], unanswerable=True, latency=400),
    ]
    s = summarize(results)
    assert s["命中率"] == 0.5
    assert s["拒答率(负例)"] == 0.5
    assert s["误拒率"] == 0.0
    assert s["延迟P50_ms"] > 0


def test_percentile_small_sample():
    assert percentile([10, 20, 30], 0.5) == 20
    assert percentile([], 0.95) == 0.0


def test_extract_chunks_from_wrapped_response():
    resp = {"data": {"chunks": [
        {"content": "第一条", "knowledge_title": "制度A"},
        {"text": "第二条", "file_name": "制度B.pdf"},
    ]}}
    chunks = extract_chunks(resp)
    assert len(chunks) == 2
    assert chunk_text(chunks[0]) == "第一条"
    assert chunk_source(chunks[1]) == "制度B.pdf"


def test_load_cases_default_file():
    from pathlib import Path
    cases = load_cases(Path("eval/cases/enterprise_policy.json"))
    assert len(cases) >= 10
    assert all("question" in c for c in cases)
    assert any(c.get("unanswerable") for c in cases)

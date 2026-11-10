import pytest

from eval.caseload import load_cases
from eval.metrics import (
    CaseResult, classify_answer, clause_rank, is_refusal, keyword_hit, mrr,
    percentile, recall_at_k, source_hit, summarize,
)
from eval.runner import chunk_source, chunk_text, extract_chunks


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
    assert any(c.get("expected_clause") for c in cases)


def test_load_cases_rejects_bad_files(tmp_path):
    import json
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"cases": [{"id": "x"}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="question"):
        load_cases(bad)
    bad.write_text(json.dumps({"cases": [{"question": "q", "unanswerable": "false"}]}),
                   encoding="utf-8")
    with pytest.raises(ValueError, match="布尔"):
        load_cases(bad)


def test_classify_answer_three_way():
    assert classify_answer("资料中没有找到相关规定。") == "refused"
    assert classify_answer("建议咨询人力资源部确认。") == "refused"
    assert classify_answer("请以最新发布的版本为准。") == "hedged"
    assert classify_answer("一线城市普通员工每晚 500 元。") == "answered"


def test_clause_rank_recall_and_mrr():
    results = [
        CaseResult("a", "t", "q", "x", ["d"], 1.0, [], "d", False,
                   "第5条", ["【第4条】内容", "【第5条】内容"]),
        CaseResult("b", "t", "q", "x", ["d"], 1.0, [], "d", False,
                   "第9条", ["【第9条】内容"]),
        CaseResult("c", "t", "q", "x", ["d"], 1.0, [], "d", False,
                   "第3条", ["【第1条】内容"]),
    ]
    assert clause_rank(results[0].retrieved, "第5条") == 2
    assert clause_rank(["第15条"], "第5条") == 0        # 不做子串误配
    assert recall_at_k(results, 3) == pytest.approx(2 / 3, abs=1e-4)
    assert recall_at_k(results, 1) == pytest.approx(1 / 3, abs=1e-4)
    assert mrr(results) == pytest.approx((1 / 2 + 1 / 1 + 0) / 3, abs=1e-4)


def test_recall_ignores_unanswerable_and_blank_clause():
    results = [
        CaseResult("n", "t", "q", "", [], 1.0, [], "", True),
        CaseResult("t1", "t", "q", "x", ["d"], 1.0, [], "d", False, "", ["【第1条】"]),
    ]
    assert recall_at_k(results, 3) == 0.0
    assert mrr(results) == 0.0


def test_summarize_reports_hedge_rate():
    results = [
        CaseResult("a", "t", "q", "标准 500 元", ["d"], 10, ["500"], "d", False),
        CaseResult("b", "t", "q", "请以最新版本为准", ["d"], 10, ["500"], "d", False),
        CaseResult("n", "t", "q", "资料中没有", [], 10, [], "", True),
    ]
    s = summarize(results)
    assert s["含糊率"] == 0.5
    assert s["误拒率"] == 0.0
    assert s["拒答率(负例)"] == 1.0


def test_runner_search_mode_end_to_end(tmp_path):
    """检索 → 指标 → 报告，整条链路用假服务端跑一遍（不起 WeKnora）。"""
    from client.weknora import ClientConfig, WeKnoraClient
    from eval.runner import run_case, write_report
    from tests.mock_server import FakeServer

    case = {
        "id": "X-01", "category": "事实型", "question": "住宿标准是多少",
        "must_include": ["500"], "expected_source": "企业差旅与费用管理办法",
        "expected_clause": "第8条",
    }
    with FakeServer() as server:
        server.queue((200, {}, {"data": {"chunks": [
            {"content": "一线城市普通员工每晚 500 元。", "knowledge_title": "企业差旅与费用管理办法"},
        ]}}))
        client = WeKnoraClient(ClientConfig(base_url=server.base_url, qps=100, max_retries=0))
        result = run_case(client, case, ["kb1"], "search")
        assert keyword_hit(result.answer, ["500"])
        assert result.citations == ["企业差旅与费用管理办法"]

        summary = summarize([result])
        csv_path, md_path = write_report([result], summary, tmp_path, "search")
        assert summary["命中率"] == 1.0
        assert "500" in csv_path.read_text(encoding="utf-8-sig")
        assert "命中率" in md_path.read_text(encoding="utf-8")


def test_runner_filters_unauthorized_chunks(tmp_path):
    """越权块在评分前被摘掉：跑出来的指标是"这个角色真实能看到什么"。"""
    from client.weknora import ClientConfig, WeKnoraClient
    from eval.runner import run_case, write_report
    from pipeline.security import DocumentPolicy, Viewer
    from tests.mock_server import FakeServer

    case = {
        "id": "ACL-01", "category": "事实型", "question": "住宿标准是多少",
        "must_include": ["500"], "expected_source": "差旅办法",
    }
    policies = {
        "差旅办法": DocumentPolicy("差旅办法", "内部"),
        "高管薪酬": DocumentPolicy("高管薪酬", "机密"),
    }
    with FakeServer() as server:
        server.queue((200, {}, {"data": {"chunks": [
            {"content": "住宿标准 500 元。", "knowledge_title": "差旅办法#001.md"},
            {"content": "高管年薪 900 万。", "knowledge_title": "高管薪酬#002.md"},
        ]}}))
        client = WeKnoraClient(ClientConfig(base_url=server.base_url, qps=100, max_retries=0))
        result = run_case(client, case, ["kb1"], "search", policies=policies,
                          viewer=Viewer("员工", "内部"))
        assert result.blocked == 1
        assert "900" not in result.answer
        assert result.citations == ["差旅办法#001.md"]
        assert summarize([result])["越权拦截块"] == 1

        csv_path, _ = write_report([result], summarize([result]), tmp_path, "search")
        assert "拦截块" in csv_path.read_text(encoding="utf-8-sig")

import json

from ops.insights import (
    Interaction, department_summary, export_feedback, insights_notes, load_interactions,
    main, parse_ts, summarize, top_problems,
)


def sample_items():
    return [
        Interaction(question="出差 9 天谁审批", ts=parse_ts("2026-11-01T10:00:00"),
                    user_id="u1", useful=True, citation_clicked=True,
                    latency_ms=500, tokens_in=1000, tokens_out=100),
        Interaction(question="年假多少天", ts=parse_ts("2026-11-01T11:00:00"),
                    user_id="u1", refused=True, answered=False),
        Interaction(question="住宿标准", ts=parse_ts("2026-11-02T09:00:00"),
                    user_id="u1", useful=False, transferred=True),
        Interaction(question="出差 9 天谁审批", ts=parse_ts("2026-11-03T10:00:00"),
                    user_id="u1", useful=True),
    ]


def test_summary_rates_and_first_contact_resolution():
    summary = summarize(sample_items(), repeat_window_days=7)
    assert summary["问答次数"] == 4
    assert summary["作答率"] == 0.75          # 拒答那条不算作答
    assert summary["拒答率"] == 0.25
    assert summary["转人工率"] == 0.25
    assert summary["有用率(已评分)"] == round(2 / 3, 4)
    assert summary["评分覆盖率"] == 0.75
    assert summary["引用点击率"] == round(1 / 3, 4)
    # 第 4 条是 2 天内重复问同一问题 → 不算首次解决
    assert summary["重复提问率"] == 0.25
    assert summary["首次解决率(代理,7天)"] == 0.25


def test_repeat_window_is_configurable():
    summary = summarize(sample_items(), repeat_window_days=1)   # 跨度 2 天，不算重复
    assert summary["重复提问率"] == 0.0
    assert summary["首次解决率(代理,1天)"] == 0.5


def test_cost_and_monthly_projection():
    summary = summarize(sample_items(), price_in=1.0, price_out=2.0, monthly_volume=1000)
    # 1000 输入 tokens * 1 元/千 + 100 输出 * 2 元/千 = 1.2 元；4 条 → 0.3 元/次
    assert summary["单次成本"] == 0.3
    assert summary["月成本估算"] == 300.0


def test_notes_flag_low_quality_signals():
    summary = summarize(sample_items(), repeat_window_days=7)
    notes = insights_notes(summary)
    assert any("首次解决率" in n for n in notes)
    assert any("拒答率" in n for n in notes)


def test_load_interactions_tolerates_missing_fields(tmp_path):
    log = tmp_path / "log.jsonl"
    log.write_text("\n".join([
        json.dumps({"question": "q1", "useful": "是", "ts": "2026-11-01T10:00:00Z"},
                   ensure_ascii=False),
        json.dumps({"question": "q2", "refused": True}, ensure_ascii=False),
        "",
    ]), encoding="utf-8")
    items = load_interactions(log)
    assert len(items) == 2
    assert items[0].useful is True and items[0].answered is True
    assert items[1].refused is True and items[1].answered is False


def test_export_feedback_for_badcase(tmp_path):
    out = tmp_path / "feedback.jsonl"
    count = export_feedback(sample_items(), out)
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert count == 2                       # 拒答 1 条 + 没用 1 条
    assert {row["reason"] for row in rows} == {"没找到", "答非所问"}
    assert all("question" in row for row in rows)


def test_department_summary_and_top_problems():
    items = [
        Interaction(question="年假多少天", user_id="u1", department="财务部",
                    refused=True, answered=False),
        Interaction(question="年假多少天？", user_id="u2", department="HR",
                    refused=True, answered=False),
        Interaction(question="住宿标准", user_id="u3", department="财务部",
                    useful=False, transferred=True),
        Interaction(question="出差 9 天谁审批", user_id="u4", department="财务部",
                    useful=True),
    ]
    by_dept = department_summary(items)
    assert set(by_dept) == {"财务部", "HR"}
    assert by_dept["财务部"]["问答次数"] == 3
    assert by_dept["财务部"]["转人工率"] == round(1 / 3, 4)
    assert by_dept["HR"]["问答次数"] == 1

    problems = top_problems(items)
    assert problems[0]["question"] == "年假多少天"       # 同一问题的两种标点被聚合
    assert problems[0]["count"] == 2
    assert problems[0]["reasons"] == ["拒答"]
    assert problems[0]["departments"] == ["HR", "财务部"]
    assert problems[1]["reasons"] == ["没用", "转人工"]


def test_cli_writes_json_and_feedback(tmp_path, capsys):
    log = tmp_path / "log.jsonl"
    log.write_text("\n".join(
        json.dumps({"question": item.question, "useful": item.useful,
                    "refused": item.refused, "transferred": item.transferred,
                    "ts": item.ts.isoformat() if item.ts else None,
                    "user_id": item.user_id, "tokens_in": item.tokens_in,
                    "tokens_out": item.tokens_out, "latency_ms": item.latency_ms},
                   ensure_ascii=False)
        for item in sample_items()), encoding="utf-8")
    out_json = tmp_path / "summary.json"
    feedback = tmp_path / "feedback.jsonl"

    code = main(["--log", str(log), "--price-in", "1", "--price-out", "2",
                 "--monthly-volume", "1000", "--json", str(out_json),
                 "--export-feedback", str(feedback), "--by-department", "--top", "3"])
    out = capsys.readouterr().out
    assert code == 0
    assert "首次解决率" in out and "运营判读" in out
    assert "分部门" in out and "高频问题" in out
    payload = json.loads(out_json.read_text(encoding="utf-8"))
    assert payload["问答次数"] == 4 and payload["单次成本"] == 0.3
    assert len(feedback.read_text(encoding="utf-8").splitlines()) == 2

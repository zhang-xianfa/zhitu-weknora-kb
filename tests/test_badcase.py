import json

import pytest

from eval.badcase import (
    build_candidates, load_candidates, load_feedback, main, merge_candidates,
    parse_bool, parse_keywords, write_candidates,
)
from eval.caseload import load_cases

SEED = {
    "name": "种子",
    "cases": [
        {"id": "F-01", "category": "事实型", "question": "住宿标准是多少？",
         "must_include": ["500"], "expected_source": "差旅办法"},
    ],
}


def seed_file(tmp_path):
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(SEED, ensure_ascii=False), encoding="utf-8")
    return path


def test_parse_helpers():
    assert parse_keywords("500|分管副总") == ["500", "分管副总"]
    assert parse_keywords(["a", " "]) == ["a"]
    assert parse_keywords("") == []
    assert parse_bool("true") is True
    assert parse_bool("否") is False
    assert parse_bool("") is None


def test_load_feedback_jsonl_and_csv(tmp_path):
    jsonl = tmp_path / "f.jsonl"
    jsonl.write_text("\n".join([
        json.dumps({"question": "年假多少天？", "reason": "没找到", "unanswerable": "true"},
                   ensure_ascii=False),
        json.dumps({"question": "出差住宿标准？", "must_include": "500|一线"}, ensure_ascii=False),
    ]), encoding="utf-8")
    rows = load_feedback(jsonl)
    assert len(rows) == 2
    assert rows[0]["unanswerable"] is True
    assert rows[1]["must_include"] == ["500", "一线"]

    csv_path = tmp_path / "f.csv"
    csv_path.write_text("question,reason,must_include\n餐补多少?,答非所问,100\n",
                        encoding="utf-8-sig")
    rows = load_feedback(csv_path)
    assert rows[0]["question"] == "餐补多少?"
    assert rows[0]["must_include"] == ["100"]


def test_load_feedback_rejects_empty_file(tmp_path):
    path = tmp_path / "empty.csv"
    path.write_text("question,reason\n", encoding="utf-8")
    with pytest.raises(ValueError, match="没有有效问题"):
        load_feedback(path)


def test_candidates_are_deduped_and_ids_skip_existing(tmp_path):
    feedback = [
        {"question": "住宿标准 是多少？", "reason": "答非所问", "must_include": []},   # 与种子重复（仅空白差异）
        {"question": "年假多少天？", "reason": "没找到", "unanswerable": True},
        {"question": "年假多少天？", "reason": "没找到", "unanswerable": True},        # 批内重复
        {"question": "出差补贴多少？", "reason": "答非所问", "must_include": ["100"]},
    ]
    candidates, duplicates = build_candidates(feedback, SEED["cases"])
    assert [c.case_id for c in candidates] == ["B-01", "B-02"]
    assert len(duplicates) == 2
    assert candidates[0].blocking_missing == []          # 负例，缺 must_include 也能入集
    assert candidates[1].blocking_missing == []          # 已给要点


def test_candidate_json_is_valid_for_caseload(tmp_path):
    candidates, _ = build_candidates(
        [{"question": "餐补多少？", "reason": "答非所问", "must_include": ["100"]},
         {"question": "年假多少天？", "unanswerable": True}], SEED["cases"])
    out_json = tmp_path / "cand.json"
    out_md = tmp_path / "review.md"
    write_candidates(candidates, out_json, out_md)

    cases = load_cases(out_json)
    assert len(cases) == 2
    assert "must_include" in cases[0]
    assert cases[1]["unanswerable"] is True
    assert "B-01" in out_md.read_text(encoding="utf-8")
    assert load_candidates(out_json)[0].must_include == ["100"]


def test_merge_rejects_incomplete_positive(tmp_path):
    cases_path = seed_file(tmp_path)
    candidates, _ = build_candidates(
        [{"question": "没给要点的正例？", "reason": "答非所问"},
         {"question": "年假多少天？", "unanswerable": True}], SEED["cases"])
    merged, rejected = merge_candidates(candidates, cases_path)
    assert merged == 1
    assert len(rejected) == 1 and "must_include" in rejected[0]
    stored = json.loads(cases_path.read_text(encoding="utf-8"))
    assert len(stored["cases"]) == 2
    assert stored["name"] == "种子"                       # 顶层包装信息不丢


def test_merge_dedupes_against_existing(tmp_path):
    cases_path = seed_file(tmp_path)
    candidates, _ = build_candidates(
        [{"question": "住宿标准是多少？", "unanswerable": True}], SEED["cases"])
    merged, rejected = merge_candidates(candidates, cases_path)
    assert merged == 0 and candidates == []


def test_performance_feedback_is_rejected_not_merged(tmp_path):
    cases_path = seed_file(tmp_path)
    candidates, _ = build_candidates(
        [{"question": "系统太慢了", "reason": "太慢"}], SEED["cases"])
    merged, rejected = merge_candidates(candidates, cases_path)
    assert merged == 0
    assert "性能问题" in rejected[0]


def test_cli_generate_edit_merge_workflow(tmp_path, capsys):
    cases_path = seed_file(tmp_path)
    feedback = tmp_path / "feedback.jsonl"
    feedback.write_text("\n".join([
        json.dumps({"question": "年假多少天？", "reason": "没找到"}, ensure_ascii=False),
        json.dumps({"question": "餐补多少？", "reason": "答非所问"}, ensure_ascii=False),
    ]), encoding="utf-8")
    out_json = tmp_path / "cand.json"
    out_md = tmp_path / "review.md"

    code = main(["--feedback", str(feedback), "--cases", str(cases_path),
                 "--out", str(out_json), "--md", str(out_md)])
    assert code == 0
    assert "待补必答要点" in capsys.readouterr().out

    # 人工复核：一条标负例，一条补要点
    data = json.loads(out_json.read_text(encoding="utf-8"))
    for case in data["cases"]:
        if case["question"] == "年假多少天？":
            case["unanswerable"] = True
        else:
            case["must_include"] = ["100"]
    out_json.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    code = main(["--feedback", str(feedback), "--cases", str(cases_path),
                 "--out", str(out_json), "--merge"])
    assert code == 0
    assert "合并 2 条" in capsys.readouterr().out
    stored = load_cases(cases_path)
    assert len(stored) == 3
    assert all("reason" not in case for case in stored)      # reason 只留在候选文件

    # 再合并一次：全部判重
    code = main(["--feedback", str(feedback), "--cases", str(cases_path),
                 "--out", str(out_json), "--merge"])
    assert code == 2

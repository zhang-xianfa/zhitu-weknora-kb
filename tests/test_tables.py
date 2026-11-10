import json
from pathlib import Path

import pytest

from pipeline.cleaner import clean
from pipeline.tables import (
    extract_tables, main, match_cell, parse_bounds, parse_criteria, query,
)

SAMPLE = Path("data/sample/企业差旅与费用管理办法.md")


def sample_tables():
    body, _ = clean(SAMPLE.read_text(encoding="utf-8"), title=SAMPLE.stem)
    return extract_tables(body)


def test_extract_tables_with_clause_paths():
    tables = sample_tables()
    assert len(tables) == 2
    assert tables[0].heading_path.endswith("第5条")
    assert tables[0].headers == ["出差天数", "审批人", "是否需要分管副总审批"]
    assert len(tables[0].rows) == 3
    assert tables[1].heading_path.endswith("第8条")
    assert tables[1].headers == ["城市等级", "总经理级", "部门负责人级", "普通员工"]


def test_records_map_headers_to_cells():
    records = sample_tables()[0].records()
    assert records[2]["出差天数"] == "8 天及以上"
    assert records[2]["审批人"] == "部门负责人 + 分管副总"


def test_parse_bounds_handles_common_table_forms():
    assert parse_bounds("1-3 天").contains(2)
    assert not parse_bounds("1-3 天").contains(4)
    assert parse_bounds("8 天及以上").contains(9)
    assert parse_bounds("8 天及以上").contains(8)
    assert not parse_bounds("8 天及以上").contains(7)
    assert parse_bounds("3 天以内").contains(3)
    assert not parse_bounds("3 天以内").contains(4)
    assert parse_bounds("1 至 3 天").contains(3)
    assert parse_bounds("500 元/晚").contains(500)
    assert parse_bounds("无") is None
    assert parse_bounds("") is None


def test_match_cell_numeric_and_text():
    assert match_cell("8 天及以上", 9)
    assert not match_cell("1-3 天", 9)
    assert match_cell("二线城市", "二线")
    assert match_cell("部门负责人 + 分管副总", "部门负责人")
    assert not match_cell("部门负责人", "分管副总")


def test_query_by_range_returns_row_and_clause():
    matches = query(sample_tables(), {"出差天数": 9})
    assert len(matches) == 1
    table, record = matches[0]
    assert table.heading_path.endswith("第5条")
    assert "分管副总" in record["审批人"]


def test_query_by_category_and_unknown_column():
    matches = query(sample_tables(), {"城市等级": "二线"})
    assert matches[0][1]["普通员工"] == "400"
    assert query(sample_tables(), {"不存在的列": "x"}) == []


def test_parse_criteria_types():
    criteria = parse_criteria("出差天数=9,城市等级=二线")
    assert criteria == {"出差天数": 9, "城市等级": "二线"}
    with pytest.raises(ValueError):
        parse_criteria("出差天数")


def test_table_without_separator_is_ignored():
    assert extract_tables("| a | b |\n| 1 | 2 |") == []


def test_extra_cells_are_truncated_with_warning():
    body = "第1条 说明\n| a | b |\n| --- | --- |\n| 1 | 2 | 3 |"
    tables = extract_tables(body)
    assert tables and tables[0].rows[0] == ["1", "2"]
    assert any("截断" in w for w in tables[0].warnings)


def test_cli_query_and_no_match(tmp_path, capsys):
    assert main(["--dir", "data/sample", "--query", "出差天数=9"]) == 0
    out = capsys.readouterr().out
    assert "分管副总" in out and "第5条" in out
    # 99 天会被"8 天及以上"命中（这正是区间匹配的意义）；0 天没有对应档位
    assert main(["--dir", "data/sample", "--query", "出差天数=99"]) == 0
    assert main(["--dir", "data/sample", "--query", "出差天数=0"]) == 2


def test_cli_exports_json_and_csv(tmp_path, capsys):
    out_json = tmp_path / "tables.json"
    out_csv = tmp_path / "tables.csv"
    assert main(["--dir", "data/sample", "--json", str(out_json),
                 "--csv", str(out_csv)]) == 0
    payload = json.loads(out_json.read_text(encoding="utf-8"))
    assert "企业差旅与费用管理办法" in payload["documents"]
    assert len(payload["documents"]["企业差旅与费用管理办法"]["tables"]) == 2
    assert "普通员工" in out_csv.read_text(encoding="utf-8-sig")

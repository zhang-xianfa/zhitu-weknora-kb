from datetime import date

from pipeline.versioning import (
    DocVersion, find_conflicts, parse_date, parse_version, select_effective, summarize,
)


def dv(doc_id: str, version: str, effective: str = "", expiry: str = "",
       doc_no: str = "HR-1", title: str = "差旅办法") -> DocVersion:
    return DocVersion(
        doc_id=doc_id, file=f"{doc_id}.md", title=title, doc_no=doc_no, version=version,
        effective=parse_date(effective) if effective else None,
        expiry=parse_date(expiry) if expiry else None,
        source=f"{doc_id}.md",
    )


def test_version_compare_is_numeric_not_lexical():
    assert parse_version("V10") > parse_version("V9")
    assert parse_version("v3.2") == (3, 2)
    assert parse_version("第3版") == (3,)
    assert parse_version("三") == (3,)
    assert parse_version("V2.10.1") == (2, 10, 1)


def test_parse_date_accepts_common_formats_and_rejects_dirty_data():
    assert parse_date("2026-03-01") == date(2026, 3, 1)
    assert parse_date("2026/3/1") == date(2026, 3, 1)
    assert parse_date("2026年3月1日") == date(2026, 3, 1)
    assert parse_date("20260301") == date(2026, 3, 1)
    assert parse_date("2026-02-30") is None      # 脏数据不猜
    assert parse_date("") is None


def test_select_keeps_latest_active_and_supersedes_old():
    docs = [dv("old", "V2.1", "2024-01-01"), dv("new", "V3.2", "2026-03-01")]
    sel = select_effective(docs, as_of=date(2026, 6, 1))
    assert [d.version for d in sel.keep] == ["V3.2"]
    assert [d.version for d in sel.superseded] == ["V2.1"]
    assert "取代" in sel.reason["old.md"]


def test_as_of_keeps_historical_version():
    docs = [dv("old", "V2.1", "2024-01-01"), dv("new", "V3.2", "2026-03-01")]
    sel = select_effective(docs, as_of=date(2025, 1, 1))
    assert [d.version for d in sel.keep] == ["V2.1"]
    assert [d.version for d in sel.pending] == ["V3.2"]


def test_expired_version_is_retired_not_kept():
    docs = [dv("old", "V1.0", "2024-01-01", "2025-01-01"),
            dv("new", "V2.0", "2025-01-01")]
    sel = select_effective(docs, as_of=date(2026, 1, 1))
    assert [d.version for d in sel.keep] == ["V2.0"]
    assert [d.version for d in sel.retired] == ["V1.0"]


def test_all_versions_expired_leaves_nothing_to_keep():
    docs = [dv("old", "V1.0", "2020-01-01", "2021-01-01")]
    sel = select_effective(docs, as_of=date(2026, 1, 1))
    assert sel.keep == []
    assert [d.version for d in sel.retired] == ["V1.0"]


def test_two_versions_without_dates_is_a_hard_conflict():
    docs = [dv("a", "V1.0"), dv("b", "V2.0")]
    conflicts = find_conflicts(docs)
    hard = [c for c in conflicts if c.severity == "high"]
    assert hard and hard[0].kind == "active_overlap"
    assert "没有标注生效日期" in hard[0].detail


def test_superseded_is_only_a_hint():
    docs = [dv("old", "V2.1", "2024-01-01"), dv("new", "V3.2", "2026-03-01")]
    conflicts = find_conflicts(docs, as_of=date(2026, 6, 1))
    assert all(c.severity == "info" for c in conflicts)
    assert any(c.kind == "superseded" for c in conflicts)


def test_same_title_different_doc_no_is_flagged():
    docs = [dv("a", "V1.0", "2024-01-01", doc_no="HR-1"),
            dv("b", "V1.0", "2024-01-01", doc_no="HR-2")]
    kinds = {c.kind for c in find_conflicts(docs)}
    assert "title_collision" in kinds


def test_summary_counts():
    docs = [dv("old", "V2.1", "2024-01-01"), dv("new", "V3.2", "2026-03-01"),
            dv("other", "V1.0", doc_no="HR-9")]
    s = summarize(find_conflicts(docs), docs)
    assert s["文档数"] == 3
    assert s["制度数"] == 2
    assert s["缺少生效日期"] == 1

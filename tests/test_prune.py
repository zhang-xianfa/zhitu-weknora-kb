import json

import pytest

from pipeline.ledger import BatchLedger
from pipeline.prune import current_chunks, load_batch_chunks, main, plan_prune, summarize


class FakeChunk:
    def __init__(self, index: int, uid: str) -> None:
        self.index = index
        self.uid = uid


def test_load_batch_chunks_and_mode(tmp_path):
    ledger = BatchLedger(tmp_path / "ledger.jsonl")
    ledger.record(
        batch_id="b1", kb_id="kb", file="制度A.md", status="uploaded", mode="chunks",
        chunks_detail=[{"index": 0, "uid": "c0", "section": "第1条"},
                       {"index": 1, "uid": "c1", "section": "第2条"}],
    )
    old, mode = load_batch_chunks(tmp_path / "ledger.jsonl", "b1")
    assert mode == "chunks"
    assert [c["uid"] for c in old["制度A"]["chunks"]] == ["c0", "c1"]


def test_load_batch_chunks_rejects_unknown_batch(tmp_path):
    ledger = BatchLedger(tmp_path / "ledger.jsonl")
    ledger.record(batch_id="b1", kb_id="kb", file="a.md", status="uploaded",
                  chunks_detail=[{"index": 0, "uid": "c0"}])
    with pytest.raises(ValueError, match="没有成功入库"):
        load_batch_chunks(tmp_path / "ledger.jsonl", "b2")


def test_plan_marks_changed_and_removed():
    old = {"制度A": {"chunks": [
        {"index": 0, "uid": "c0", "section": "第1条"},
        {"index": 1, "uid": "c1", "section": "第2条"},
        {"index": 2, "uid": "c2", "section": "第3条"},
    ]}}
    new = {"制度A": [FakeChunk(0, "c0"), FakeChunk(1, "c1-new")]}
    items = plan_prune(old, new, mode="chunks")
    status = {item.index: item.status for item in items}
    assert status == {0: "unchanged", 1: "changed", 2: "removed"}
    assert items[1].title == "制度A#001"
    assert summarize(items)["需要处理"] == 2


def test_plan_marks_whole_document_removed():
    old = {"旧制度": {"chunks": [{"index": 0, "uid": "c0", "section": "第1条"}]}}
    items = plan_prune(old, {}, mode="chunks")
    assert items[0].status == "removed"


def test_non_chunks_mode_has_no_titles():
    old = {"制度A": {"chunks": [{"index": 0, "uid": "c0", "section": "第1条"}]}}
    items = plan_prune(old, {}, mode="cleaned")
    assert items[0].title == ""


def test_cli_plan_and_fail_on_stale(tmp_path, capsys):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "制度A.md").write_text(
        "第一章 总则\n第1条 适用范围。\n第2条 生效日期。\n", encoding="utf-8")
    chunks = current_chunks(docs)["制度A"]
    details = [{"index": c.index, "uid": c.uid, "section": c.heading_path} for c in chunks]

    ledger_path = tmp_path / "ledger.jsonl"
    ledger = BatchLedger(ledger_path)
    ledger.record(batch_id="b1", kb_id="kb", file="制度A.md", status="uploaded",
                  mode="chunks", chunks_detail=details)
    assert main(["--dir", str(docs), "--ledger", str(ledger_path)]) == 0
    out = capsys.readouterr().out
    assert "未变" in out and "需要处理 0" in out

    # 加一块旧数据：应被识别为已删除，`--fail-on-stale` 退出码 3
    details.append({"index": 99, "uid": "c_dead", "section": "第9条"})
    ledger.record(batch_id="b2", kb_id="kb", file="制度A.md", status="uploaded",
                  mode="chunks", chunks_detail=details)
    out_json = tmp_path / "plan.json"
    code = main(["--dir", str(docs), "--ledger", str(ledger_path),
                 "--fail-on-stale", "--json", str(out_json)])
    assert code == 3
    payload = json.loads(out_json.read_text(encoding="utf-8"))
    assert payload["summary"]["需要处理"] == 1
    assert any(item["status"] == "removed" for item in payload["items"])


def test_cli_warns_for_non_chunks_batch(tmp_path, capsys):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "制度A.md").write_text("第一章 总则\n第1条 内容。\n", encoding="utf-8")
    chunks = current_chunks(docs)["制度A"]
    details = [{"index": c.index, "uid": c.uid, "section": c.heading_path} for c in chunks]

    ledger_path = tmp_path / "ledger.jsonl"
    BatchLedger(ledger_path).record(
        batch_id="b1", kb_id="kb", file="制度A.md", status="uploaded",
        mode="cleaned", chunks_detail=details)
    assert main(["--dir", str(docs), "--ledger", str(ledger_path)]) == 0
    assert "不是 chunks 模式" in capsys.readouterr().out

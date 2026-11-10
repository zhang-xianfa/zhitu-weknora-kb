import json

from pipeline.ledger import BatchLedger, new_batch_id, sha256_file


def test_record_and_summary(tmp_path):
    ledger = BatchLedger(tmp_path / "ingest.jsonl")
    ledger.record(batch_id="b1", kb_id="kb", file="制度A.md", status="uploaded",
                  chunks=3, chunk_uids=["c1", "c2", "c3"], mode="cleaned")
    ledger.record(batch_id="b1", kb_id="kb", file="制度B.md", status="failed", error="boom")

    summary = ledger.summary("b1")
    assert summary["文件数"] == 2
    assert summary["块数"] == 3
    assert summary["状态"] == {"uploaded": 1, "failed": 1}
    assert summary["失败"] == ["制度B.md"]
    assert ledger.last_batch() == "b1"
    assert ledger.failures("b1")[0]["error"] == "boom"


def test_batches_keep_insertion_order(tmp_path):
    ledger = BatchLedger(tmp_path / "ingest.jsonl")
    for bid in ("b1", "b2", "b1"):
        ledger.record(batch_id=bid, kb_id="kb", file="a.md", status="uploaded")
    assert ledger.batches() == ["b1", "b2"]


def test_broken_line_does_not_break_reading(tmp_path):
    path = tmp_path / "ingest.jsonl"
    path.write_text(
        json.dumps({"batch_id": "b1", "file": "a.md", "status": "uploaded"}, ensure_ascii=False) + "\n"
        + "这不是 JSON\n"
        + json.dumps({"batch_id": "b1", "file": "b.md", "status": "uploaded"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    ledger = BatchLedger(path)
    assert len(ledger.records("b1")) == 2


def test_changed_uids_between_batches(tmp_path):
    ledger = BatchLedger(tmp_path / "ingest.jsonl")
    ledger.record(batch_id="b1", kb_id="kb", file="a.md", status="uploaded",
                  chunk_uids=["c1", "c2"])
    ledger.record(batch_id="b2", kb_id="kb", file="a.md", status="uploaded",
                  chunk_uids=["c2", "c3"])
    diff = ledger.changed_uids("b1", "b2")
    assert diff["新增"] == ["c3"]
    assert diff["消失"] == ["c1"]
    assert diff["未变"] == 1


def test_chunks_detail_maps_citation_title_to_uid(tmp_path):
    """引用里是"文档#003"，台账要能把它翻回 uid 与条款路径。"""
    ledger = BatchLedger(tmp_path / "ingest.jsonl")
    ledger.record(
        batch_id="b1", kb_id="kb", file="制度A.md", status="uploaded", chunks=2,
        chunk_uids=["c_a", "c_b"], mode="chunks",
        chunks_detail=[
            {"index": 0, "uid": "c_a", "section": "第一章 总则 › 第1条"},
            {"index": 1, "uid": "c_b", "section": "第二章 审批 › 第5条"},
        ],
    )
    entry = ledger.records("b1")[0]
    by_title = {f"{entry['file'].replace('.md', '')}#{c['index']:03d}": c
                for c in entry["chunks_detail"]}
    assert by_title["制度A#001"]["uid"] == "c_b"
    assert by_title["制度A#001"]["section"] == "第二章 审批 › 第5条"


def test_new_batch_id_is_unique_and_readable():
    ids = {new_batch_id(salt="x") for _ in range(20)}
    assert len(ids) == 20
    assert all(len(b) >= 17 and "T" in b for b in ids)


def test_sha256_file_matches_hashlib(tmp_path):
    import hashlib
    path = tmp_path / "a.md"
    path.write_bytes("内容".encode("utf-8") * 1000)
    assert sha256_file(path) == hashlib.sha256(path.read_bytes()).hexdigest()


def test_missing_ledger_returns_empty(tmp_path):
    ledger = BatchLedger(tmp_path / "nope.jsonl")
    assert ledger.records() == []
    assert ledger.last_batch() == ""


def test_doctor_ledger_check_reports_failures(tmp_path):
    from ops.doctor import check_ledger

    ledger = BatchLedger(tmp_path / "ingest.jsonl")
    ledger.record(batch_id="b1", kb_id="kb", file="a.md", status="uploaded")
    ledger.record(batch_id="b1", kb_id="kb", file="b.md", status="failed", error="400 bad")

    code, messages = check_ledger(tmp_path / "ingest.jsonl")
    assert code == 2
    assert messages and "b.md" in messages[0]

    ledger.record(batch_id="b2", kb_id="kb", file="c.md", status="uploaded")
    code, messages = check_ledger(tmp_path / "ingest.jsonl")
    assert code == 0 and messages == []


def test_doctor_ledger_check_missing_file(tmp_path):
    from ops.doctor import check_ledger

    code, messages = check_ledger(tmp_path / "nope.jsonl")
    assert code == 2 and "台账" in messages[0]

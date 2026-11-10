import json

from pipeline import ingest


def summary_of(out: str) -> dict:
    """从 CLI 输出里取出 JSON：dry-run 会先打印摘要、再打印人话合计。"""
    start = out.index("{")
    data, _ = json.JSONDecoder().raw_decode(out[start:])
    return data


def test_dry_run_on_sample(capsys):
    code = ingest.main(["--dir", "data/sample", "--dry-run"])
    out = capsys.readouterr().out
    summary = summary_of(out)
    assert code == 0
    doc = summary["documents"][0]
    assert summary["totals"]["chunks"] == 8
    assert doc["status"] == "dry_run"
    assert doc["version"] == "V3.2"
    assert doc["clearance"] == "内部"
    assert doc["grade"] == "A"


def write(path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def versioned_doc(version: str, effective: str, body: str = "第1条 规定。") -> str:
    return f"文件编号：HR-1\n版本号：{version}\n生效日期：{effective}\n第一章 总则\n{body}\n"


def test_old_version_is_skipped_by_default(tmp_path, capsys):
    write(tmp_path / "v1.md", versioned_doc("V1.0", "2024-01-01", "第1条 旧规定。"))
    write(tmp_path / "v2.md", versioned_doc("V2.0", "2026-03-01", "第1条 新规定。"))
    code = ingest.main(["--dir", str(tmp_path), "--dry-run", "--as-of", "2026-06-01"])
    summary = summary_of(capsys.readouterr().out)
    docs = {d["file"]: d for d in summary["documents"]}
    assert docs["v1.md"]["status"] == "skipped"
    assert "取代" in docs["v1.md"]["reason"]
    assert docs["v2.md"]["status"] == "dry_run"
    assert code == 0


def test_keep_policy_uploads_all_versions(tmp_path, capsys):
    write(tmp_path / "v1.md", versioned_doc("V1.0", "2024-01-01"))
    write(tmp_path / "v2.md", versioned_doc("V2.0", "2026-03-01"))
    ingest.main(["--dir", str(tmp_path), "--dry-run", "--version-policy", "keep"])
    summary = summary_of(capsys.readouterr().out)
    assert all(d["status"] == "dry_run" for d in summary["documents"])


def test_confidential_doc_is_blocked_by_kb_clearance(tmp_path, capsys):
    write(tmp_path / "薪酬.md",
          "密级：机密\n文件编号：HR-9\n版本号：V1.0\n生效日期：2026-01-01\n"
          "第一章 总则\n第1条 高管薪酬标准。\n")
    code = ingest.main(["--dir", str(tmp_path), "--dry-run"])
    summary = summary_of(capsys.readouterr().out)
    doc = summary["documents"][0]
    assert doc["status"] == "blocked"
    assert doc["clearance"] == "机密"
    assert "专用知识库" in doc["reason"]
    assert code == 3


def test_passthrough_requires_explicit_permission(tmp_path, capsys):
    (tmp_path / "附件.pdf").write_bytes(b"%PDF-1.4 fake")
    code = ingest.main(["--dir", str(tmp_path), "--dry-run"])
    summary = summary_of(capsys.readouterr().out)
    doc = summary["documents"][0]
    assert doc["status"] == "skipped"
    assert "allow-passthrough" in doc["reason"]
    assert code == 0

    ingest.main(["--dir", str(tmp_path), "--dry-run", "--allow-passthrough"])
    summary = summary_of(capsys.readouterr().out)
    assert summary["documents"][0]["status"] == "dry_run"


def test_min_quality_blocks_messy_document(tmp_path, capsys):
    messy = "\n".join(["一段没有条款编号的说明文字，重复又零散。"] * 6)
    write(tmp_path / "乱.md", messy)
    code = ingest.main(["--dir", str(tmp_path), "--dry-run", "--min-quality", "80"])
    summary = summary_of(capsys.readouterr().out)
    doc = summary["documents"][0]
    assert doc["status"] == "blocked"
    assert "体检" in doc["reason"]
    assert code == 3


def test_as_of_selects_historical_version(tmp_path, capsys):
    write(tmp_path / "v1.md", versioned_doc("V1.0", "2024-01-01"))
    write(tmp_path / "v2.md", versioned_doc("V2.0", "2026-03-01"))
    ingest.main(["--dir", str(tmp_path), "--dry-run", "--as-of", "2025-01-01"])
    summary = summary_of(capsys.readouterr().out)
    docs = {d["file"]: d for d in summary["documents"]}
    assert docs["v1.md"]["status"] == "dry_run"
    assert docs["v2.md"]["status"] == "skipped"
    assert "尚未生效" in docs["v2.md"]["reason"]


def test_policies_manifest_is_exported(tmp_path):
    out = tmp_path / "policies.json"
    assert ingest.main(["--dir", "data/sample", "--dry-run",
                        "--policies-out", str(out)]) == 0
    manifest = json.loads(out.read_text(encoding="utf-8"))
    doc = manifest["documents"]["企业差旅与费用管理办法"]
    assert doc["level"] == "内部"
    assert manifest["kb_clearance"] == "内部"


def test_tables_are_exported_from_ingest(tmp_path):
    out = tmp_path / "tables.json"
    assert ingest.main(["--dir", "data/sample", "--dry-run",
                        "--tables-out", str(out)]) == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    tables = payload["documents"]["企业差旅与费用管理办法"]["tables"]
    assert len(tables) == 2
    assert tables[0]["clause"].endswith("第5条")


def test_passthrough_is_always_uploaded_raw(tmp_path):
    """二进制文档没有清洗稿可传：cleaned 模式下也要强制回 raw，不能传空文件。"""
    pdf = tmp_path / "附件.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    plan = ingest.DocPlan(path=pdf, doc_id="附件", rel="附件.pdf", native=False)
    targets = ingest.stage(plan, "cleaned", tmp_path / "staging")
    assert targets == [(pdf, "附件")]


def test_missing_directory_and_empty_directory(tmp_path, capsys):
    assert ingest.main(["--dir", str(tmp_path / "nope"), "--dry-run"]) == 1
    assert ingest.main(["--dir", str(tmp_path), "--dry-run"]) == 1


def test_upload_without_kb_id_is_rejected(tmp_path, capsys):
    write(tmp_path / "a.md", versioned_doc("V1.0", "2024-01-01"))
    assert ingest.main(["--dir", str(tmp_path)]) == 1
    assert "kb-id" in capsys.readouterr().out

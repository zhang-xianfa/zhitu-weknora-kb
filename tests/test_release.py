import json
from pathlib import Path

from ops.release import build_snapshot, main


def test_snapshot_reads_sample():
    snapshot = build_snapshot(Path("data/sample"))
    assert len(snapshot.docs) == 1
    assert len(snapshot.chunks) == 8
    assert snapshot.lowest_score >= 60


def test_sample_passes_release_check(capsys):
    code = main(["--dir", "data/sample"])
    out = capsys.readouterr().out
    assert code == 0
    assert "结论：通过" in out
    assert "指标门禁" in out


def test_low_quality_doc_fails(tmp_path, capsys):
    (tmp_path / "乱.md").write_text("\n".join(["没有条款编号的散乱文字。"] * 6),
                                    encoding="utf-8")
    code = main(["--dir", str(tmp_path), "--min-score", "80", "--skip-offline"])
    out = capsys.readouterr().out
    assert code == 1
    assert "文档体检" in out and "失败" in out


def versioned(version: str, effective: str = "") -> str:
    head = f"文件编号：HR-1\n版本号：{version}\n"
    if effective:
        head += f"生效日期：{effective}\n"
    return head + "第一章 总则\n第1条 规定内容。\n"


def test_version_conflict_fails(tmp_path, capsys):
    (tmp_path / "v1.md").write_text(versioned("V1.0"), encoding="utf-8")
    (tmp_path / "v2.md").write_text(versioned("V2.0"), encoding="utf-8")
    code = main(["--dir", str(tmp_path), "--min-score", "0", "--skip-offline"])
    out = capsys.readouterr().out
    assert code == 1
    assert "版本时效" in out and "同时有效" in out


def test_doc_above_kb_clearance_fails(tmp_path, capsys):
    (tmp_path / "薪酬.md").write_text(
        "密级：机密\n文件编号：HR-9\n版本号：V1.0\n第一章 总则\n第1条 高管薪酬标准。\n",
        encoding="utf-8")
    code = main(["--dir", str(tmp_path), "--min-score", "0", "--skip-offline"])
    out = capsys.readouterr().out
    assert code == 1
    assert "密级边界" in out and "高于知识库" in out


def test_metric_regression_fails(tmp_path, capsys):
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"检索命中率": 1.0, "召回率@3": 1.0, "MRR": 1.0},
                                    ensure_ascii=False), encoding="utf-8")
    code = main(["--dir", "data/sample", "--baseline", str(baseline)])
    out = capsys.readouterr().out
    assert code == 1
    assert "指标门禁" in out and "失败" in out


def test_json_output_and_ignore(tmp_path, capsys):
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"检索命中率": 0.98, "召回率@3": 1.0}, ensure_ascii=False),
                        encoding="utf-8")
    out_json = tmp_path / "release.json"
    code = main(["--dir", "data/sample", "--baseline", str(baseline),
                 "--ignore", "检索命中率", "--json", str(out_json)])
    assert code == 0
    payload = json.loads(out_json.read_text(encoding="utf-8"))
    assert payload["passed"] is True
    assert len(payload["steps"]) == 4


def test_ignore_metric_avoids_false_failure(tmp_path, capsys):
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"检索命中率": 0.98}, ensure_ascii=False), encoding="utf-8")
    assert main(["--dir", "data/sample", "--baseline", str(baseline)]) == 1
    capsys.readouterr()
    assert main(["--dir", "data/sample", "--baseline", str(baseline),
                 "--ignore", "检索命中率"]) == 0

import json

from eval.regression import compare, main


def base() -> dict:
    return {
        "命中率": 0.90,
        "引用命中率": 1.0,
        "误拒率": 0.05,
        "含糊率": 0.10,
        "拒答率(负例)": 0.75,
        "召回率@3": 1.0,
        "延迟P95_ms": 100.0,
    }


def verdicts(findings) -> dict:
    return {f.metric: f.verdict for f in findings}


def test_drop_beyond_threshold_fails():
    current = base()
    current["命中率"] = 0.80                     # -10 个百分点，超过 5
    assert verdicts(compare(base(), current))["命中率"] == "fail"


def test_drop_within_threshold_passes():
    current = base()
    current["命中率"] = 0.87                     # -3 个百分点，在噪声范围内
    assert verdicts(compare(base(), current))["命中率"] == "pass"


def test_rise_in_bad_metric_fails():
    current = base()
    current["误拒率"] = 0.20                     # 误拒率越高越差
    assert verdicts(compare(base(), current))["误拒率"] == "fail"
    current["误拒率"] = 0.07
    assert verdicts(compare(base(), current))["误拒率"] == "pass"


def test_latency_uses_ratio_not_points():
    current = base()
    current["延迟P95_ms"] = 180.0                 # 1.8 倍，超过 2 倍阈值？没超
    assert verdicts(compare(base(), current))["延迟P95_ms"] == "pass"
    current["延迟P95_ms"] = 260.0
    assert verdicts(compare(base(), current))["延迟P95_ms"] == "fail"


def test_recall_prefix_is_gated():
    current = base()
    current["召回率@3"] = 0.70
    assert verdicts(compare(base(), current))["召回率@3"] == "fail"


def test_missing_or_non_numeric_metrics_are_skipped():
    current = {"命中率": 0.9, "金标未召回": ["F-02"]}
    findings = compare({"命中率": 0.9}, current)
    assert [f.metric for f in findings] == ["命中率"]


def test_report_and_exit_code(tmp_path):
    baseline = tmp_path / "baseline.json"
    current = tmp_path / "current.json"
    baseline.write_text(json.dumps(base(), ensure_ascii=False), encoding="utf-8")

    current.write_text(json.dumps(base(), ensure_ascii=False), encoding="utf-8")
    assert main(["--baseline", str(baseline), "--current", str(current)]) == 0

    worse = base()
    worse["命中率"] = 0.5
    current.write_text(json.dumps(worse, ensure_ascii=False), encoding="utf-8")
    assert main(["--baseline", str(baseline), "--current", str(current)]) == 1


def test_update_writes_baseline(tmp_path):
    baseline = tmp_path / "baseline.json"
    current = tmp_path / "current.json"
    current.write_text(json.dumps(base(), ensure_ascii=False), encoding="utf-8")
    assert main(["--baseline", str(baseline), "--current", str(current), "--update"]) == 0
    assert json.loads(baseline.read_text(encoding="utf-8"))["命中率"] == 0.90


def test_missing_files_return_usage_error(tmp_path):
    assert main(["--baseline", str(tmp_path / "no.json"),
                 "--current", str(tmp_path / "also_no.json")]) == 2


def test_ignore_skips_noisy_metric(tmp_path):
    """CI 机器噪声大时忽略延迟指标，只卡正确性。"""
    baseline = tmp_path / "baseline.json"
    current = tmp_path / "current.json"
    baseline.write_text(json.dumps(base(), ensure_ascii=False), encoding="utf-8")
    noisy = base()
    noisy["延迟P95_ms"] = 900.0
    current.write_text(json.dumps(noisy, ensure_ascii=False), encoding="utf-8")

    assert main(["--baseline", str(baseline), "--current", str(current)]) == 1
    assert main(["--baseline", str(baseline), "--current", str(current),
                 "--ignore", "延迟P95_ms"]) == 0

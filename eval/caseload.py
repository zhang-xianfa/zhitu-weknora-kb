# -*- coding: utf-8 -*-
"""用例文件读写：一份用例集要能被三个入口共用（线上评测、离线基线、回归门禁）。

校验放在这里，而不是各入口各写一遍：用例少一个 ``question``、``unanswerable``
写成字符串 "false"，在哪个入口报错都要能指到具体第几条，否则回归门禁一挂，
第一件事是猜"到底是环境问题还是用例写错了"。
"""

from __future__ import annotations

import json
from pathlib import Path


def load_cases(path: Path) -> list[dict]:
    """读用例：支持顶层数组或 {cases: [...]}。

    只做"能不能跑"的校验，不做语义校验（关键词是否合理由写用例的人负责）。
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    cases = data.get("cases", data) if isinstance(data, dict) else data
    if not isinstance(cases, list):
        raise ValueError("用例文件格式不对：应为数组或 {cases: [...]}")
    for i, case in enumerate(cases):
        if not isinstance(case, dict):
            raise ValueError(f"第 {i} 条用例不是对象")
        if not case.get("question"):
            raise ValueError(f"第 {i} 条用例缺少 question 字段")
        if "unanswerable" in case and not isinstance(case["unanswerable"], bool):
            raise ValueError(f"第 {i} 条用例的 unanswerable 必须是布尔值")
        if "must_include" in case and not isinstance(case["must_include"], list):
            raise ValueError(f"第 {i} 条用例的 must_include 必须是数组")
    return cases

# -*- coding: utf-8 -*-
"""文档清洗：把企业制度文档里"对检索没用、对答案有害"的东西去掉。

制度类文档的脏东西很固定：
  页眉页脚（"内部资料，请勿外传"）、页码（"第 3 页 共 12 页"）、
  目录行（"……第 12 页"）、重复的文档标题、全角数字与空格、
  以及"第三条"被排版成"第 三 条"这类编号断裂。

清洗的目标不是"看起来干净"，而是让分块和检索少踩坑：
页码和页眉会被当成正文召回，目录行会和高价值条款抢排名。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# 页眉页脚 / 水印 / 保密标识
_HEADER_FOOTER = [
    re.compile(r"^\s*第\s*\d+\s*页\s*[，,/]?\s*共\s*\d+\s*页\s*$"),
    re.compile(r"^\s*[-—]?\s*\d+\s*[-—]?\s*$"),          # 裸页码：- 3 -
    re.compile(r"^\s*(内部资料|仅供内部|机密|绝密|请勿外传).*$"),
    re.compile(r"^\s*目\s*录\s*$"),
]
#: 文档元信息：从正文里摘出来，单独拼成一块（版本/生效日期恰恰是"时效类"问题的答案）
#: 密级与适用范围不只是"展示信息"：它们决定这条内容能不能进谁的上下文（见 pipeline/security.py）
_META = re.compile(
    r"^\s*(文件编号|文档编号|版本号|版本|生效日期|发布日期|失效日期|发布部门|适用部门|适用范围|密级)"
    r"\s*[:：]\s*(.+?)\s*$"
)
#: 元信息键的规范名：同一份文档常写成"生效日期"或"发布日期"，下游按规范名取
META_ALIASES = {
    "文档编号": "文件编号",
    "版本": "版本号",
    "发布日期": "生效日期",
    "适用部门": "适用范围",
}
#: 签署栏：对问答无用，直接丢
_SIGN = re.compile(r"^\s*(编制|审核|批准|签发)\s*[:：].*$")
# 目录行：正文 + 省略号 + 页码。
# 省略号有 "."（英文点）、"·"（间隔号）、"…"（省略号）三种排版，中文排版常用两个"…"，
# 所以量词用 2 起步——之前依赖 NFKC 把"……"折成"..."才能匹配，归一化规则一改就会漏。
_TOC_LINE = re.compile(r".+[.·…]{2,}\s*\d+\s*$")
# 中文条款编号："第 三 条" → "第三条"
_CLAUSE_GAP = re.compile(r"(第)\s+([一二三四五六七八九十百零〇\d]+)\s+(条|章|节|款)")
# 编号与序号之间的多余空格："1. 1. 1" → "1.1.1"
_NUM_GAP = re.compile(r"(?<=\d)\.\s+(?=\d)")
# 连续空白
_BLANK_RUN = re.compile(r"\n{3,}")


@dataclass
class CleanReport:
    """清洗过程的可观测信息：流程跑完要能说清"去掉了什么"。"""

    total_lines: int = 0
    dropped_header_footer: int = 0
    dropped_toc: int = 0
    dropped_dup_title: int = 0
    metadata: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "总行数": self.total_lines,
            "去页眉页脚": self.dropped_header_footer,
            "去目录行": self.dropped_toc,
            "去重复标题": self.dropped_dup_title,
            "元信息": self.metadata,
        }


def normalize(text: str) -> str:
    """统一换行、去掉零宽字符、把**全角字母数字和空白**折成半角。

    这里刻意**不用整段 NFKC**：NFKC 会把中文全角标点一起改掉
    （"第1条，应当：报销（含税）" → "第1条,应当:报销(含税)"）。标点虽然不进分词，
    但它会让引用给用户看的原文变样、也让"和原文逐字比对"的核对/审计报告失真。

    只折"看起来一样、编码不同"的字符：全角数字、全角字母、全角空格——
    它们才是"第５条"和"第5条"被当成两个词的原因。
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("​", "").replace("﻿", "").replace("‌", "")
    return "".join(_fold_char(ch) for ch in text)


def _fold_char(ch: str) -> str:
    """单字符宽度归一：只处理空白与全角字母数字，中文标点原样保留。"""
    if ch.isascii():
        return ch
    if ch.isspace():                 # 含 U+3000 全角空格
        return " "
    folded = unicodedata.normalize("NFKC", ch)
    return folded if folded.isascii() and folded.isalnum() else ch


def normalize_clause_numbering(text: str) -> str:
    """修复条款编号被排版拆开的问题。"""
    text = _CLAUSE_GAP.sub(r"\1\2\3", text)
    text = _NUM_GAP.sub(".", text)
    return text


def clean(text: str, title: str = "") -> tuple[str, CleanReport]:
    """清洗整篇文档，返回 (正文, 清洗报告)。

    title 用于识别"每页重复出现的文档标题"，为空则不处理。
    """
    report = CleanReport()
    text = normalize(text)
    lines = text.split("\n")
    report.total_lines = len(lines)

    kept: list[str] = []
    seen_title = 0
    for raw in lines:
        line = raw.strip()

        meta = _META.match(line)
        if meta:
            key = META_ALIASES.get(meta.group(1), meta.group(1))
            report.metadata.setdefault(key, meta.group(2))
            continue
        if _SIGN.match(line) or any(p.match(line) for p in _HEADER_FOOTER):
            report.dropped_header_footer += 1
            continue
        if _TOC_LINE.match(line):
            report.dropped_toc += 1
            continue
        # 文档标题允许出现一次，重复出现的是页眉残留
        if title and line == title.strip():
            seen_title += 1
            if seen_title > 1:
                report.dropped_dup_title += 1
                continue

        kept.append(raw.rstrip())

    body = normalize_clause_numbering("\n".join(kept))
    body = _BLANK_RUN.sub("\n\n", body).strip()

    # 元信息单独成块放在最前面：版本/生效日期散落在页眉里，丢了就答不了"哪一版"类问题
    if report.metadata:
        meta_line = "、".join(f"{k}：{v}" for k, v in report.metadata.items())
        body = f"【文档元信息】{meta_line}\n\n{body}"
    return body, report


def is_table_line(line: str) -> bool:
    """Markdown 表格行（以 | 开头或结尾），分块时整表不切。"""
    s = line.strip()
    return s.startswith("|") or (s.endswith("|") and s.count("|") >= 2)

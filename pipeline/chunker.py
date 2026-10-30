# -*- coding: utf-8 -*-
"""条款感知分块：制度类文档的检索单元是"条"，不是"500 字"。

和通用按长度切分的区别，就三条：

1. **层级路径注入**——每块开头带 `第X章 › 第X节 › 第X条`，短块也带上下文，
   否则"职工请假需提前三天申请"这种块，向量检索只能靠正文里那点字去猜它属于哪份文件；
2. **表格整表不切**——权限矩阵、金额档位被切断之后，任何一段都没有完整含义；
3. **不跨条款切**——先按条边界切，超长条再按句子切；宁可块短一点，也不要一条半。

代价也清楚：行数变多、块大小不均、跨条问题（"这个流程前面提到过吗"）召回要靠多跳。
这些取舍写在 docs/02 里，不是没想到。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .cleaner import is_table_line

# ── 标题识别 ──────────────────────────

#: 中文编号标题：第X章 / 第X节 / 第X条
_CN_HEADING = re.compile(
    r"^(第[一二三四五六七八九十百零〇\d]+[章节]|第[一二三四五六七八九十百零〇\d]+条)\s*(.*)$"
)
#: 阿拉伯数字层级：1. / 1.1 / 1.1.1（后面必须跟标题文字，避免把"1.5 倍"当标题）
_NUM_HEADING = re.compile(r"^(\d+(?:\.\d+){0,3})[\.、\s]\s*(\S.{0,40})$")
#: 中文顿号层级：一、 （一）
_CN_LIST = re.compile(r"^([一二三四五六七八九十]+)、\s*(.*)$|^[（(]([一二三四五六七八九十]+)[）)]\s*(.*)$")

_LEVEL_OF_CN = {"章": 1, "节": 2, "条": 3}


@dataclass
class Chunk:
    """一个可入库的块。"""

    doc_id: str
    index: int
    text: str                 # 带层级前缀的正文（入库用这个）
    body: str                 # 不含前缀的正文（调试/评测展示用）
    heading_path: str         # "第3章 › 第2节 › 第5条"
    char_count: int
    has_table: bool = False
    oversized: bool = False   # 整表或超长条款导致的超限块

    def as_dict(self) -> dict:
        return {
            "doc_id": self.doc_id,
            "index": self.index,
            "heading_path": self.heading_path,
            "char_count": self.char_count,
            "has_table": self.has_table,
            "oversized": self.oversized,
            "text": self.text,
        }


@dataclass
class _Unit:
    kind: str          # heading / table / para
    text: str
    level: int = 0
    title: str = ""


def _parse_heading(line: str) -> tuple[int, str, str] | None:
    """返回 (层级, 标题, 同行的正文)。

    「第X条」这种条款标题后面通常直接跟正文，例如「第4条 报销须在15个工作日内提交」。
    标题只取「第4条」，后面的正文要当内容留下来——否则整条内容会被当成标题丢掉。
    """
    s = line.strip()
    if len(s) > 60:          # 太长的行不是标题
        return None

    m = _CN_HEADING.match(s)
    if m:
        mark, rest = m.group(1), m.group(2).strip()
        level = _LEVEL_OF_CN.get(mark[-1], 3)
        if mark.endswith("条"):
            return level, mark, rest
        return level, mark + (" " + rest if rest else ""), ""

    m = _NUM_HEADING.match(s)
    if m and not s.endswith("。") and not s.endswith("；"):
        level = 3 + m.group(1).count(".")
        return level, s, ""

    m = _CN_LIST.match(s)
    if m:
        return 4, s, ""

    return None


def _to_units(text: str) -> list[_Unit]:
    """先把文档拆成标题 / 表格 / 段落三类单元。"""
    units: list[_Unit] = []
    para_buf: list[str] = []
    table_buf: list[str] = []

    def flush_para() -> None:
        if para_buf:
            units.append(_Unit("para", "\n".join(para_buf)))
            para_buf.clear()

    def flush_table() -> None:
        if table_buf:
            units.append(_Unit("table", "\n".join(table_buf)))
            table_buf.clear()

    for line in text.split("\n"):
        if is_table_line(line):
            flush_para()
            table_buf.append(line)
            continue
        flush_table()

        if not line.strip():
            flush_para()
            continue

        heading = _parse_heading(line)
        if heading:
            flush_para()
            level, title, inline = heading
            units.append(_Unit("heading", line.strip(), level=level, title=title))
            if inline:
                units.append(_Unit("para", inline))
            continue

        para_buf.append(line.strip())

    flush_para()
    flush_table()
    return units


def chunk_document(
    doc_id: str,
    text: str,
    max_chars: int = 600,
    overlap: int = 80,
) -> list[Chunk]:
    """把清洗后的正文切成带层级路径的块。"""
    units = _to_units(text)
    chunks: list[Chunk] = []

    stack: list[tuple[int, str]] = []       # (level, title)
    buf: list[str] = []
    buf_len = 0
    buf_has_table = False
    buf_oversized = False

    def current_path() -> str:
        return " › ".join(t for _, t in stack) or "（无标题）"

    def path_prefix() -> str:
        p = current_path()
        return f"【{p}】\n" if stack else ""

    def flush() -> None:
        nonlocal buf, buf_len, buf_has_table, buf_oversized
        body = "\n".join(buf).strip()
        if not body:
            buf, buf_len, buf_has_table, buf_oversized = [], 0, False, False
            return
        prefix = path_prefix()
        full = prefix + body
        chunks.append(Chunk(
            doc_id=doc_id,
            index=len(chunks),
            text=full,
            body=body,
            heading_path=current_path(),
            char_count=len(full),
            has_table=buf_has_table,
            oversized=buf_oversized,
        ))
        buf, buf_len, buf_has_table, buf_oversized = [], 0, False, False

    for unit in units:
        if unit.kind == "heading":
            flush()
            while stack and stack[-1][0] >= unit.level:
                stack.pop()
            stack.append((unit.level, unit.title))
            continue

        if unit.kind == "table":
            # 表格整表进一块；放不下就单独成块，允许超限
            if buf and buf_len + len(unit.text) + 1 > max_chars:
                flush()
            if buf:
                buf.append(unit.text)
                buf_len += len(unit.text) + 1
            else:
                buf = [unit.text]
                buf_len = len(unit.text)
            buf_has_table = True
            if buf_len > max_chars:
                buf_oversized = True
                flush()
            continue

        # 普通段落：超长先按句子拆
        for piece in _split_long(unit.text, max_chars):
            if buf and buf_len + len(piece) + 1 > max_chars:
                prev_tail = buf[-1][-overlap:] if overlap and buf else ""
                flush()
                if prev_tail and len(prev_tail) + len(piece) + 1 <= max_chars:
                    buf = [prev_tail]
                    buf_len = len(prev_tail)
            if not buf:
                buf = [piece]
                buf_len = len(piece)
            else:
                buf.append(piece)
                buf_len += len(piece) + 1

    flush()
    return _merge_short(chunks, max_chars)


def _merge_short(chunks: list[Chunk], max_chars: int, min_chars: int = 120) -> list[Chunk]:
    """合并同章内的碎块。

    条款一句话就是一条，单块常常只有五六十字，向量里信息量太薄。
    把同章相邻的短块合起来（合并后不超过上限），既能补上下文，又不会跨章串味。
    含表格的块不参与合并——表格单独成块更好追踪。
    """
    if not chunks:
        return chunks
    merged: list[Chunk] = [chunks[0]]
    for cur in chunks[1:]:
        prev = merged[-1]
        same_chapter = prev.heading_path.split(" › ")[0] == cur.heading_path.split(" › ")[0]
        short = prev.char_count < min_chars or cur.char_count < min_chars
        fits = prev.char_count + cur.char_count + 1 <= max_chars
        if same_chapter and short and fits and not prev.has_table and not cur.has_table:
            body = prev.body + "\n" + cur.body
            prev.body = body
            prev.text = f"【{prev.heading_path}】\n{body}" if prev.heading_path != "（无标题）" else body
            prev.char_count = len(prev.text)
            prev.oversized = prev.oversized or cur.oversized
        else:
            merged.append(cur)
    for i, c in enumerate(merged):
        c.index = i
    return merged


_SENT_END = re.compile(r"(?<=[。！？；!?;])")


def _split_long(text: str, max_chars: int) -> list[str]:
    """单段超过上限时按句子拆，句子仍超长再硬切。"""
    if len(text) <= max_chars:
        return [text]
    out: list[str] = []
    for sent in _SENT_END.split(text):
        sent = sent.strip()
        if not sent:
            continue
        if len(sent) <= max_chars:
            out.append(sent)
        else:
            out.extend(sent[i:i + max_chars] for i in range(0, len(sent), max_chars))
    return out

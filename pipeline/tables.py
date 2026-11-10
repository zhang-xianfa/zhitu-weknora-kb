# -*- coding: utf-8 -*-
"""表格结构化：让"数值题"走确定性查询，而不是让模型硬算。

"整表入块"解决了**看得见**，没解决**算得对**。像"出差 9 天谁审批""二线城市普通员工
住 5 晚能报多少"这类问题，正确答案是一次区间匹配或乘法，不是语言模型该做的事——
让 LLM 读表算金额，错一次就是一次报销事故。

这里做三件事（全部离线、可复算）：

1. **抽表**：Markdown 表格 → 表头/行 + 所属条款路径（"第三章 差旅标准 › 第8条"）；
2. **导出**：JSON（给下游/前端）与长表 CSV（给业务用 Excel 核对）；
3. **查询**：按列做等值/包含/**区间**匹配——"8 天及以上"能匹配 9，也能匹配 "9 天"。

边界（不硬猜）：

- 只处理 Markdown 表格；docx/pdf 的表要等上游解析器转成 Markdown 之后才能用；
- 合并单元格、多行表头、跨页续表不支持，遇到会给出 warning 并跳过，而不是猜一个结果；
- 查询只做等值/区间/包含，不做聚合、排序和跨表 join——那些应该进数据库，不是塞给 CLI。
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from pipeline.chunker import parse_heading
from pipeline.cleaner import clean, is_table_line

_SEPARATOR = re.compile(r"^:?-{2,}:?$")
_UNIT_TAIL = r"(?:（含）|\(含\))?"
_WS = re.compile(r"\s+")


@dataclass(frozen=True)
class Bounds:
    """一个单元格表达的数值区间；None 表示该侧无界。"""

    low: float | None
    high: float | None
    low_inclusive: bool = True
    high_inclusive: bool = True

    def contains(self, value: float) -> bool:
        if self.low is not None:
            if value < self.low or (value == self.low and not self.low_inclusive):
                return False
        if self.high is not None:
            if value > self.high or (value == self.high and not self.high_inclusive):
                return False
        return True


@dataclass
class Table:
    index: int
    heading_path: str
    headers: list[str]
    rows: list[list[str]]
    warnings: list[str] = field(default_factory=list)

    def records(self) -> list[dict]:
        return [dict(zip(self.headers, row)) for row in self.rows]

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "clause": self.heading_path,
            "headers": list(self.headers),
            "rows": [list(row) for row in self.rows],
        }


def _split_row(line: str) -> list[str]:
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|"):
        text = text[:-1]
    return [cell.strip() for cell in text.split("|")]


def _is_separator(cells: list[str]) -> bool:
    return bool(cells) and all(_SEPARATOR.match(cell) for cell in cells)


def _clean_headers(cells: list[str]) -> list[str]:
    headers: list[str] = []
    seen: dict[str, int] = {}
    for i, cell in enumerate(cells):
        name = cell.strip() or f"列{i + 1}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 1
        headers.append(name)
    return headers


def _normalize_row(cells: list[str], width: int) -> tuple[list[str], bool]:
    if len(cells) < width:
        return cells + [""] * (width - len(cells)), False
    if len(cells) > width:
        return cells[:width], True
    return cells, False


def extract_tables(text: str) -> list[Table]:
    """从清洗后的正文里抽表，并记录每张表所属的条款路径。"""
    tables: list[Table] = []
    stack: list[tuple[int, str]] = []
    heading_path = "（无标题）"
    block: list[str] = []
    block_path = heading_path

    def flush() -> None:
        nonlocal block
        if not block:
            return
        current, block = block, []
        if len(current) < 2:
            return
        headers = _split_row(current[0])
        separator = _split_row(current[1])
        if not _is_separator(separator):
            return                      # 没有分隔行的"伪表格"，宁可不抽
        width = len(headers)
        if width < 2:
            return
        rows: list[list[str]] = []
        warnings: list[str] = []
        for line in current[2:]:
            cells, truncated = _normalize_row(_split_row(line), width)
            if truncated:
                warnings.append("有一行列数多于表头，已截断到表头宽度")
            if any(cell.strip() for cell in cells):
                rows.append(cells)
        tables.append(Table(len(tables) + 1, block_path,
                            _clean_headers(headers), rows, warnings))

    for line in text.split("\n"):
        if is_table_line(line):
            if not block:
                block_path = heading_path
            block.append(line)
            continue
        flush()
        heading = parse_heading(line)
        if heading:
            level, title, _ = heading
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            heading_path = " › ".join(t for _, t in stack) or "（无标题）"
    flush()
    return tables


# ── 数值区间解析 ──────────────────────

_QUALIFIER = re.compile(
    rf"^(\d+(?:\.\d+)?)\s*[^\d]*?(及以上|以上|及以下|以下|以内|之内|内){_UNIT_TAIL}$"
)
_RANGE = re.compile(r"^(\d+(?:\.\d+)?)\s*[-–—~～－至到]\s*(\d+(?:\.\d+)?)")
_EXACT = re.compile(r"^(\d+(?:\.\d+)?)\s*(.*)$")


def _number_text(cell: str) -> str:
    return cell.replace(",", "").replace("，", "").strip()


def parse_bounds(cell: str) -> Bounds | None:
    """把 "1-3 天" / "8 天及以上" / "3 天以内" / "500" 解析成区间。

    解析不出来返回 None（例如"无""见附件"），调用方应当放弃匹配而不是猜 0。
    """
    text = _number_text(cell)
    if not text:
        return None

    match = _QUALIFIER.match(text)
    if match:
        value = float(match.group(1))
        if match.group(2) in ("及以上", "以上"):
            return Bounds(value, None)
        return Bounds(None, value)

    match = _RANGE.match(text)
    if match:
        return Bounds(float(match.group(1)), float(match.group(2)))

    match = _EXACT.match(text)
    if match:
        value = float(match.group(1))
        return Bounds(value, value)
    return None


def _norm(text) -> str:
    return _WS.sub("", str(text or "")).lower()


def match_cell(cell: str, value) -> bool:
    """单元格是否匹配查询值：数值走区间，字符串走等值/包含。"""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        bounds = parse_bounds(cell)
        return bounds.contains(float(value)) if bounds else False
    left, right = _norm(cell), _norm(value)
    if not left or not right:
        return False
    return left == right or right in left or left in right


def query(tables: list[Table], criteria: dict) -> list[tuple[Table, dict]]:
    matches: list[tuple[Table, dict]] = []
    for table in tables:
        for record in table.records():
            if all(match_cell(record.get(column, ""), value)
                   for column, value in criteria.items()):
                matches.append((table, record))
    return matches


def parse_criteria(text: str) -> dict:
    """把 "出差天数=9,城市等级=二线" 解析成 {列: 值}；纯数字转成 int/float。"""
    criteria: dict = {}
    if not text:
        return criteria
    for part in re.split(r"[,，;；]", text):
        part = part.strip()
        if not part:
            continue
        match = re.match(r"^(.+?)[=:：](.+)$", part)
        if not match:
            raise ValueError(f"查询条件格式应为 列=值，收到：{part}")
        column, raw = match.group(1).strip(), match.group(2).strip()
        if re.fullmatch(r"-?\d+(?:\.\d+)?", raw):
            criteria[column] = int(raw) if raw.isdigit() else float(raw)
        else:
            criteria[column] = raw
    return criteria


# ── CLI ───────────────────────────────

def _load_tables(root: Path) -> list[tuple[str, str, Table]]:
    """返回 [(文档名, 文件, 表)]，按文件顺序。"""
    out: list[tuple[str, str, Table]] = []
    for path in sorted(root.rglob("*")):
        if path.suffix.lower() not in (".md", ".txt") or not path.is_file():
            continue
        body, _ = clean(path.read_text(encoding="utf-8", errors="replace"), title=path.stem)
        for table in extract_tables(body):
            out.append((path.stem, str(path.relative_to(root)), table))
    return out


def write_json(tables: list[tuple[str, str, Table]], out: Path) -> None:
    """把 [(文档名, 相对路径, 表)] 写成结构化 JSON（入库流水线也用它）。"""
    documents: dict[str, dict] = {}
    for doc_id, rel, table in tables:
        key = doc_id
        if key in documents and documents[key].get("file") != rel:
            key = rel                      # 不同目录下同名文档：用相对路径区分，别互相覆盖
        entry = documents.setdefault(key, {"file": rel, "tables": []})
        entry["tables"].append(table.as_dict())
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"generated_at": datetime.now().isoformat(timespec="seconds"),
                               "documents": documents}, ensure_ascii=False, indent=2),
                   encoding="utf-8")


def write_csv(tables: list[tuple[str, str, Table]], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["文件", "文档", "表格", "条款", "行号", "列名", "值"])
        for doc_id, rel, table in tables:
            for row_no, record in enumerate(table.records(), 1):
                for column, value in record.items():
                    writer.writerow([rel, doc_id, table.index, table.heading_path,
                                     row_no, column, value])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="表格结构化导出与区间查询")
    parser.add_argument("--dir", required=True, help="文档目录（.md/.txt）")
    parser.add_argument("--json", default="", help="把表格结构写成 JSON")
    parser.add_argument("--csv", default="", help="把表格结构写成常表 CSV（Excel 可看）")
    parser.add_argument("--query", default="", help='查询条件，如 "出差天数=9" 或 "城市等级=二线"')
    parser.add_argument("--select", default="", help="查询结果只显示这些列（逗号分隔）")
    args = parser.parse_args(argv)

    root = Path(args.dir)
    if not root.exists():
        print(f"目录不存在：{root}")
        return 1
    tables = _load_tables(root)
    if not tables:
        print(f"目录下没有可抽取的表格：{root}")
        return 1

    if args.json:
        write_json(tables, Path(args.json))
        print(f"表格结构已写入 {args.json}")
    if args.csv:
        write_csv(tables, Path(args.csv))
        print(f"常表 CSV 已写入 {args.csv}")

    if args.query:
        try:
            criteria = parse_criteria(args.query)
        except ValueError as e:
            print(e)
            return 1
        matches = query([t for _, _, t in tables], criteria)
        if not matches:
            print(f"没有匹配的行：{args.query}")
            return 2
        select = [c.strip() for c in re.split(r"[,，]", args.select) if c.strip()]
        for table, record in matches:
            shown = {k: v for k, v in record.items() if not select or k in select}
            body = "，".join(f"{k}={v}" for k, v in shown.items())
            where = next(rel for _, rel, t in tables if t is table)
            print(f"[{where} · {table.heading_path}] {body}")
        return 0

    for doc_id, rel, table in tables:
        print(f"{rel} · 表{table.index} · {table.heading_path} · "
              f"{len(table.headers)} 列 × {len(table.rows)} 行")
        for warning in table.warnings:
            print(f"  注意：{warning}")
    print(f"\n共 {len(tables)} 张表；用 --query '列=值' 做区间/等值查询。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

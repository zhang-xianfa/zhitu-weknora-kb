# -*- coding: utf-8 -*-
"""版本与时效：制度类知识库最常见的"答错"，不是检索不准，而是**答了一个作废的版本**。

同一份《差旅办法》在共享盘里躺着 V2.1、V3.2 两份，都进了库；员工问"住宿标准多少"，
向量检索不知道哪个版本有效，召回哪份算哪份，于是同一个问题两个人问出两个答案。
这不是模型问题，是库里有两条互相矛盾的事实，而检索层没有"现在有效"的概念。

这里做三件事（都只依赖文档自身元信息，不接文档管理系统）：

1. **识别同一制度的多个版本**：按"文件编号"归组（没有编号时退化为标题），
   这是"同一份制度"的唯一可靠锚点；
2. **判断时点有效性**：生效日期 ≤ 查询时点 < 失效日期。支持 ``as_of``
   回答"今年 1 月的时候标准是多少"这类历史问题；
3. **给出可执行的处置**：``keep`` 当前有效的最新版、``superseded`` 应归档的旧版、
   ``pending`` 尚未生效的预告版、``retired`` 已过失效日期的版本。
   入库流水线据此只上"当前有效"的那份。

代价与边界：元信息不全（没编号、没生效日期）时只能退化为"按版本号取最大"，
这种情况会被显式标成 ``missing_effective`` 而不是假装判断准确；
生效日期填错，整套判断跟着错——所以体检报告里要能看见它。
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from pipeline.cleaner import clean

# ── 解析 ──────────────────────────────

_DATE_NUM = re.compile(r"(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?")
_DATE_COMPACT = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
_VERSION_NUM = re.compile(r"(\d+(?:\.\d+)*)")
_VERSION_CN = re.compile(r"第?\s*([一二三四五六七八九十两]+)\s*[版次稿]")
#: 整个字段就写了个中文数字（"版本号：三"），这种也要认，否则排不出版本先后
_VERSION_CN_BARE = re.compile(r"^[一二三四五六七八九十两]{1,3}$")

_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def parse_date(value) -> date | None:
    """吃下 "2026-03-01" / "2026/3/1" / "2026年3月1日" / "20260301"。"""
    if isinstance(value, date):
        return value
    s = str(value or "").strip()
    if not s:
        return None
    m = _DATE_NUM.search(s) or _DATE_COMPACT.match(s)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:              # 2026-02-30 这种脏数据：宁可不判，也不要崩
        return None


def parse_version(value) -> tuple[int, ...]:
    """把 "V3.2" / "v2" / "第3版" / "三" 解析成可比较的元组。

    用元组而不是字符串比较：字符串排序下 "V10" < "V9"，版本号一多就翻车。
    """
    s = str(value or "").strip()
    m = _VERSION_NUM.search(s)
    if m:
        return tuple(int(x) for x in m.group(1).split("."))
    m = _VERSION_CN.search(s)
    if m:
        return (_cn_to_int(m.group(1)),)
    if _VERSION_CN_BARE.match(s):
        return (_cn_to_int(s),)
    return ()


def _cn_to_int(text: str) -> int:
    if "十" not in text:
        return _CN_DIGITS.get(text, 0) if len(text) == 1 else 0
    head, _, tail = text.partition("十")
    tens = _CN_DIGITS.get(head, 1) if head else 1
    ones = _CN_DIGITS.get(tail, 0) if tail else 0
    return tens * 10 + ones


@dataclass(frozen=True)
class DocVersion:
    """一份文档的"制度身份"：编号、版本、生效窗口。"""

    doc_id: str
    file: str
    title: str = ""
    doc_no: str = ""
    version: str = ""
    effective: date | None = None
    expiry: date | None = None
    source: str = ""                # 源文件相对路径，作为批次里的唯一键

    @property
    def key(self) -> str:
        return self.source or self.file or self.doc_id

    @property
    def group_key(self) -> str:
        """同一制度的归组键：有文件编号用编号，没有就退化为标题/文件名。"""
        return self.doc_no or self.title or self.doc_id

    @property
    def version_key(self) -> tuple[int, ...]:
        return parse_version(self.version)

    def sort_key(self) -> tuple:
        # 版本号优先、生效日期次之：只写了"V3.2"没写日期的文档也能排进去
        return (self.version_key, self.effective or date.min, self.file)

    @property
    def has_window(self) -> bool:
        return self.effective is not None or self.expiry is not None

    def describe(self) -> str:
        eff = self.effective.isoformat() if self.effective else "未标注"
        exp = self.expiry.isoformat() if self.expiry else ""
        ver = self.version or "未标注"
        tail = f"，失效 {exp}" if exp else ""
        return f"{ver}（生效 {eff}{tail}）"


def from_metadata(doc_id: str, file: str, metadata: dict, title: str = "",
                  source: str = "") -> DocVersion:
    return DocVersion(
        doc_id=doc_id,
        file=file,
        title=title or doc_id,
        doc_no=str(metadata.get("文件编号", "")).strip(),
        version=str(metadata.get("版本号", "")).strip(),
        effective=parse_date(metadata.get("生效日期")),
        expiry=parse_date(metadata.get("失效日期")),
        source=source,
    )


def collect(directory: Path, suffixes=(".md", ".txt")) -> list[DocVersion]:
    """扫目录，只读元信息，用来做上线前的版本体检。"""
    out: list[DocVersion] = []
    root = Path(directory)
    for path in sorted(root.rglob("*")):
        if path.suffix.lower() not in suffixes or not path.is_file():
            continue
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        _, report = clean(raw, title=path.stem)
        out.append(from_metadata(path.stem, path.name, report.metadata,
                                 title=path.stem,
                                 source=str(path.relative_to(root))))
    return out


# ── 冲突判定 ──────────────────────────

@dataclass
class Conflict:
    group: str
    kind: str            # active_overlap / superseded / not_effective / missing_effective / title_collision
    severity: str        # high / info
    docs: list[DocVersion] = field(default_factory=list)
    detail: str = ""


@dataclass
class Selection:
    keep: list[DocVersion] = field(default_factory=list)
    superseded: list[DocVersion] = field(default_factory=list)
    pending: list[DocVersion] = field(default_factory=list)
    retired: list[DocVersion] = field(default_factory=list)
    reason: dict[str, str] = field(default_factory=dict)   # DocVersion.key → 人话原因


def _groups(docs: list[DocVersion]) -> dict[str, list[DocVersion]]:
    out: dict[str, list[DocVersion]] = {}
    for d in docs:
        out.setdefault(d.group_key, []).append(d)
    return out


def effective_window(d: DocVersion, group: list[DocVersion]) -> tuple[date | None, date | None]:
    """一份文档的生效窗口 [start, end)。

    关键规则：**没写失效日期时，新版生效日就是旧版的失效日**。
    制度改版几乎没人给旧版补"失效日期"，如果只认显式失效，旧版会永远"有效"，
    于是每一组版本都被判成严重冲突，告警打多了就没人看了。
    真实的业务语义是"新版一生效，旧版自动作废"，这里按这个语义算。
    """
    start, end = d.effective, d.expiry
    if end is None and start is not None:
        later = [x.effective for x in group if x is not d and x.effective and x.effective > start]
        if later:
            end = min(later)
    return start, end


def state_in_group(d: DocVersion, group: list[DocVersion], as_of: date | None = None) -> str:
    """active / pending / expired（按组内窗口判断，不是单看自己）。"""
    day = as_of or date.today()
    start, end = effective_window(d, group)
    if end and day >= end:
        return "expired"
    if start and day < start:
        return "pending"
    return "active"


def _state_lists(items: list[DocVersion], as_of: date | None):
    actives, pending, expired = [], [], []
    for d in items:
        {"active": actives, "pending": pending, "expired": expired}[
            state_in_group(d, items, as_of)].append(d)
    return actives, pending, expired


def _successor(d: DocVersion, group: list[DocVersion]) -> DocVersion | None:
    later = [x for x in group if x is not d and x.effective and d.effective
             and x.effective > d.effective]
    return min(later, key=lambda x: x.effective) if later else None


def find_conflicts(docs: list[DocVersion], as_of: date | None = None) -> list[Conflict]:
    """找出"会让人混答"的组合。high 级别必须在入库前处理掉。"""
    conflicts: list[Conflict] = []
    for group, items in _groups(docs).items():
        if len(items) < 2:
            continue
        actives, pending, expired = _state_lists(items, as_of)
        missing = [d for d in items if not d.has_window]

        if len(actives) > 1:
            why = "（都没有标注生效日期，无法判断谁作废）" if len(missing) > 1 else ""
            conflicts.append(Conflict(
                group, "active_overlap", "high", actives,
                f"同一制度有 {len(actives)} 个版本在查询时点同时有效{why}："
                + "、".join(d.version or d.file for d in actives),
            ))
        elif actives and (expired or pending):
            parts = []
            if expired:
                parts.append(f"{len(expired)} 个旧版应归档")
            if pending:
                parts.append(f"{len(pending)} 个新版尚未生效")
            conflicts.append(Conflict(
                group, "superseded", "info", expired + pending,
                f"当前有效的是 {actives[0].describe()}；" + "，".join(parts),
            ))
        elif not actives:
            conflicts.append(Conflict(
                group, "not_effective", "info", pending + expired,
                "组内没有在查询时点生效的版本："
                + ("新版尚未生效" if pending else "全部已过失效日期"),
            ))
        if missing and len(missing) < len(items):
            conflicts.append(Conflict(
                group, "missing_effective", "info", missing,
                "部分版本缺少生效日期，时效判断只能按版本号兜底",
            ))

    # 同名不同编号：多半是"改了编码重发"，也可能是两个部门的同名制度，人工确认为准
    by_title: dict[str, set[str]] = {}
    for d in docs:
        by_title.setdefault(d.title, set()).add(d.group_key)
    for title, keys in by_title.items():
        if len(keys) > 1:
            conflicts.append(Conflict(
                title, "title_collision", "info",
                [d for d in docs if d.title == title],
                "同名文档存在多个文件编号，确认是「换版重发」还是「两个部门的同名制度」",
            ))
    return conflicts


def select_effective(docs: list[DocVersion], as_of: date | None = None) -> Selection:
    """每个制度只留一份：时点有效的最新版。"""
    sel = Selection()
    for _, items in _groups(docs).items():
        actives, pending, expired = _state_lists(items, as_of)
        if actives:
            keep = max(actives, key=lambda d: d.sort_key())
            sel.keep.append(keep)
            for d in actives:
                if d is not keep:
                    sel.superseded.append(d)
                    sel.reason[d.key] = f"同版本窗口内被 {keep.version or keep.file} 取代（{keep.describe()}）"
        for d in expired:
            successor = _successor(d, items)
            if d.expiry is not None:
                sel.retired.append(d)
                sel.reason[d.key] = f"已过失效日期（{d.expiry}），建议归档，不进当前有效库"
            elif successor is not None:
                sel.superseded.append(d)
                sel.reason[d.key] = (f"被 {successor.version or successor.file} 取代"
                                     f"（{successor.describe()}）")
            else:
                sel.retired.append(d)
                sel.reason[d.key] = "已失效，建议归档"
        for d in pending:
            sel.pending.append(d)
            sel.reason[d.key] = f"尚未生效（生效日期 {d.effective}），等生效后再入当前库"
        if not actives and not pending and not expired:
            # 组内一份都没有（理论上不会走到），兜底保留可读性最好的一份
            sel.keep.append(max(items, key=lambda d: d.sort_key()))
    return sel


def summarize(conflicts: list[Conflict], docs: list[DocVersion]) -> dict:
    return {
        "文档数": len(docs),
        "制度数": len(_groups(docs)),
        "高优先级冲突": sum(1 for c in conflicts if c.severity == "high"),
        "提示": sum(1 for c in conflicts if c.severity != "high"),
        "缺少生效日期": sum(1 for d in docs if not d.has_window),
    }


# ── CLI ───────────────────────────────

def _print_report(docs: list[DocVersion], conflicts: list[Conflict], as_of: date | None) -> None:
    day = as_of or date.today()
    groups = _groups(docs)
    print(f"时点：{day.isoformat()}    文档 {len(docs)} 份")
    print("\n文件 | 编号 | 版本 | 生效 | 失效 | 当前状态")
    state_cn = {"active": "有效", "pending": "尚未生效", "expired": "已失效"}
    for d in sorted(docs, key=lambda x: (x.group_key, x.sort_key())):
        state = state_in_group(d, groups[d.group_key], as_of)
        print(f"{d.file} | {d.doc_no or '-'} | {d.version or '-'} | "
              f"{d.effective or '-'} | {d.expiry or '-'} | {state_cn[state]}")
    if not conflicts:
        print("\n未发现版本冲突。")
        return
    print("\n冲突与提示：")
    for c in conflicts:
        flag = "严重" if c.severity == "high" else "提示"
        print(f" - [{flag}] {c.group}：{c.detail}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="制度文档版本与时效体检")
    parser.add_argument("--dir", required=True, help="待检查的文档目录")
    parser.add_argument("--as-of", default="", help="查询时点 YYYY-MM-DD，默认今天")
    parser.add_argument("--json", default="", help="把结果写成 JSON，便于留档")
    args = parser.parse_args(argv)

    root = Path(args.dir)
    if not root.exists():
        print(f"目录不存在：{root}")
        return 1
    as_of = parse_date(args.as_of) if args.as_of else None
    if args.as_of and not as_of:
        print(f"--as-of 格式不对：{args.as_of}")
        return 1

    docs = collect(root)
    if not docs:
        print(f"目录下没有可检查的文档：{root}")
        return 1
    conflicts = find_conflicts(docs, as_of)
    _print_report(docs, conflicts, as_of)

    if args.json:
        import json
        groups = _groups(docs)
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps({
            "as_of": (as_of or date.today()).isoformat(),
            "summary": summarize(conflicts, docs),
            "documents": [
                {"file": d.file, "doc_no": d.doc_no, "version": d.version,
                 "effective": d.effective.isoformat() if d.effective else None,
                 "expiry": d.expiry.isoformat() if d.expiry else None,
                 "state": state_in_group(d, groups[d.group_key], as_of)}
                for d in docs
            ],
            "conflicts": [
                {"group": c.group, "kind": c.kind, "severity": c.severity,
                 "files": [d.file for d in c.docs], "detail": c.detail}
                for c in conflicts
            ],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n结果已写入 {args.json}")

    return 3 if any(c.severity == "high" for c in conflicts) else 0


if __name__ == "__main__":
    sys.exit(main())

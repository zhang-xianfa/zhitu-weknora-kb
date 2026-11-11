# -*- coding: utf-8 -*-
"""旧块清理计划：文档变短之后，库里那些旧知识条目不会自己消失。

``chunks`` 模式按块上传，标题是"文档名#003"。文档删掉一条、或者改短之后，重新入库只会补新块，
旧块还留在库里——检索时它们仍然会被召回，答案里出现"已经删掉的规定"，这比答错更难查。

这个模块只出**计划**，不删数据：

- 读批次台账里的 ``chunks_detail``（uid / 条款路径 / 序号）；
- 重新切一遍当前文档，按 uid 对齐；
- 输出 unchanged / changed / removed 三类，给出需要处理的知识条目标题。

真正的删除走 WeKnora 管理端或 API。POC 不确认删除接口的幂等语义，不在这里冒险自动删——
"清理脚本删错数据"是比"旧块多召回一次"严重得多的事故。

判断规则：

| 情况 | 判定 | 处理 |
|---|---|---|
| uid 还在新块里 | unchanged | 不用动 |
| 同序号但 uid 变了 | changed | 内容改了，标题会重复；建议先删旧标题再重传，或走更新接口 |
| 序号超出新块数，或 uid 不在新块里 | removed | 删除该知识条目 |

边界：非 ``chunks`` 模式的批次（raw/cleaned）标题由上游解析器决定，这里只能给"哪一块没了"，
给不出可删的标题，会明确提示仅供参考。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from pipeline.chunker import chunk_document
from pipeline.cleaner import clean
from pipeline.ledger import DEFAULT_LEDGER, BatchLedger

_STATUS_CN = {"unchanged": "未变", "changed": "变更", "removed": "已删除"}


@dataclass
class PruneItem:
    doc_id: str
    index: int
    uid: str
    section: str
    status: str          # unchanged / changed / removed
    title: str = ""      # chunks 模式下可直接用于删除的知识条目标题

    def as_dict(self) -> dict:
        return {
            "doc_id": self.doc_id,
            "index": self.index,
            "uid": self.uid,
            "section": self.section,
            "status": self.status,
            "title": self.title,
        }


def load_batch_chunks(ledger_path: Path, batch_id: str = "") -> tuple[dict[str, dict], str]:
    """读某个批次的块明细：{doc_id: {"file": ..., "chunks": [...]}} 和入库模式。"""
    ledger = BatchLedger(ledger_path)
    batch_id = batch_id or ledger.last_batch()
    if not batch_id:
        raise ValueError(f"台账为空或不存在：{ledger_path}")

    records = [r for r in ledger.records(batch_id)
               if r.get("status") in ("uploaded", "skipped")]
    if not records:
        raise ValueError(f"批次 {batch_id} 没有成功入库的记录")

    out: dict[str, dict] = {}
    modes: set[str] = set()
    for record in records:
        file = str(record.get("file") or "")
        doc_id = Path(file).stem
        details = record.get("chunks_detail") or []
        chunks = [
            {"index": int(item.get("index", i)), "uid": str(item.get("uid") or ""),
             "section": str(item.get("section") or "")}
            for i, item in enumerate(details)
            if isinstance(item, dict) and item.get("uid")
        ]
        if not chunks:
            continue
        out[doc_id] = {"file": file, "chunks": chunks}
        if record.get("mode"):
            modes.add(str(record["mode"]))
    return out, (modes.pop() if len(modes) == 1 else "unknown")


def current_chunks(directory: Path, max_chars: int = 600,
                   overlap: int = 80) -> dict[str, list]:
    """按当前文档重新切块：uid 是内容指纹，正好用来对齐。"""
    out: dict[str, list] = {}
    for path in sorted(Path(directory).rglob("*")):
        if path.suffix.lower() not in (".md", ".txt") or not path.is_file():
            continue
        body, _ = clean(path.read_text(encoding="utf-8", errors="replace"), title=path.stem)
        out[path.stem] = chunk_document(path.stem, body, max_chars=max_chars, overlap=overlap)
    return out


def plan_prune(old: dict[str, dict], new: dict[str, list],
               mode: str = "chunks") -> list[PruneItem]:
    items: list[PruneItem] = []
    for doc_id, info in old.items():
        fresh = new.get(doc_id, [])
        fresh_by_index = {chunk.index: chunk for chunk in fresh}
        fresh_uids = {chunk.uid for chunk in fresh}
        for detail in info["chunks"]:
            index = int(detail.get("index", -1))
            uid = str(detail.get("uid", ""))
            if uid in fresh_uids:
                status = "unchanged"
            elif index in fresh_by_index:
                status = "changed"
            else:
                status = "removed"
            items.append(PruneItem(
                doc_id=doc_id, index=index, uid=uid,
                section=str(detail.get("section", "")), status=status,
                title=f"{doc_id}#{index:03d}" if mode == "chunks" else "",
            ))
    return sorted(items, key=lambda x: (x.doc_id, x.index))


def summarize(items: list[PruneItem]) -> dict:
    counts = {status: 0 for status in _STATUS_CN}
    for item in items:
        counts[item.status] = counts.get(item.status, 0) + 1
    return {
        "旧块数": len(items),
        "未变": counts["unchanged"],
        "变更": counts["changed"],
        "已删除": counts["removed"],
        "需要处理": counts["changed"] + counts["removed"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按台账差异生成旧块清理计划（只出清单，不删数据）")
    parser.add_argument("--dir", required=True, help="当前文档目录（用于重新切块）")
    parser.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    parser.add_argument("--batch", default="", help="批次号，默认最近一批")
    parser.add_argument("--max-chars", type=int, default=600)
    parser.add_argument("--overlap", type=int, default=80)
    parser.add_argument("--json", default="", help="把计划写成 JSON")
    parser.add_argument("--fail-on-stale", action="store_true",
                        help="有变更/删除时退出码为 3（发布前检查用）")
    args = parser.parse_args(argv)

    root = Path(args.dir)
    if not root.exists():
        print(f"目录不存在：{root}")
        return 1
    try:
        old, mode = load_batch_chunks(Path(args.ledger), args.batch)
    except (OSError, ValueError) as e:
        print(f"读批次台账失败：{e}")
        return 1
    if not old:
        print("该批次没有块级明细（chunks_detail 为空），无法生成清理计划。")
        return 1

    items = plan_prune(old, current_chunks(root, args.max_chars, args.overlap), mode)
    summary = summarize(items)
    print(f"批次 {args.batch or '最近一批'}    模式 {mode}    旧块 {summary['旧块数']} 个")
    if mode != "chunks":
        print("注意：该批次不是 chunks 模式，引用标题由上游解析器决定，下面的标题仅供参考。")
    for item in items:
        if item.status == "unchanged":
            continue
        target = item.title or item.section or f"{item.doc_id}#{item.index:03d}"
        action = "内容已变化，建议先删旧标题再重传" if item.status == "changed" else "删除该知识条目"
        print(f" - [{_STATUS_CN[item.status]}] {target}（{item.section or '无条款路径'}）→ {action}")
    print(f"合计：未变 {summary['未变']}，变更 {summary['变更']}，已删除 {summary['已删除']}；"
          f"需要处理 {summary['需要处理']} 个。")

    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"batch": args.batch, "mode": mode,
                                   "summary": summary,
                                   "items": [item.as_dict() for item in items]},
                                  ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"计划已写入 {out}")

    if args.fail_on_stale and summary["需要处理"]:
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())

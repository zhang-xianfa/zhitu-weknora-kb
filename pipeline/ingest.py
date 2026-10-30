# -*- coding: utf-8 -*-
"""入库流水线：清洗 → 分块 → 上传。

用法::

    python -m pipeline.ingest --dir data/sample --dry-run     # 只看切块结果，不联网
    python -m pipeline.ingest --dir data/sample --kb-id <id>  # 真正入库

设计要点：
- **dry-run 是必需品**，不是调试开关。分块改一条规则，先肉眼看过 20 个块再入库，
  比事后去线上找"为什么这个问题答不出来"便宜得多；
- **内容指纹去重**：同一文件重跑不会产生重复知识条目（见 client.upload_if_changed）；
- **单文件失败不中断整批**：记录失败清单，最后统一汇报，避免一个坏文件卡住 80 篇。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from client.weknora import WeKnoraClient
from pipeline.chunker import chunk_document
from pipeline.cleaner import clean

SUPPORTED = {".md", ".txt"}


def read_document(path: Path) -> str:
    """读文档。docx/pdf 走外部转换（企业侧由 WeKnora 解析器负责），这里只处理纯文本。"""
    return path.read_text(encoding="utf-8", errors="replace")


def process_file(path: Path, max_chars: int, overlap: int) -> tuple[list, dict]:
    raw = read_document(path)
    body, report = clean(raw, title=path.stem)
    chunks = chunk_document(path.stem, body, max_chars=max_chars, overlap=overlap)
    stats = {
        "file": path.name,
        "clean": report.as_dict(),
        "chunks": len(chunks),
        "avg_chars": round(sum(c.char_count for c in chunks) / len(chunks), 1) if chunks else 0,
        "max_chars": max(c.char_count for c in chunks) if chunks else 0,
        "oversized": sum(1 for c in chunks if c.oversized),
        "tables": sum(1 for c in chunks if c.has_table),
    }
    return chunks, stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="企业文档清洗、分块与入库")
    parser.add_argument("--dir", required=True, help="待处理文档目录")
    parser.add_argument("--kb-id", default="", help="知识库 ID（dry-run 时可省略）")
    parser.add_argument("--max-chars", type=int, default=600)
    parser.add_argument("--overlap", type=int, default=80)
    parser.add_argument("--dry-run", action="store_true", help="只打印切块结果，不上传")
    parser.add_argument("--dump", default="", help="把切好的块写成 jsonl，便于人工检查")
    args = parser.parse_args(argv)

    root = Path(args.dir)
    files = sorted(p for p in root.rglob("*") if p.suffix.lower() in SUPPORTED)
    if not files:
        print(f"目录下没有可处理的文档：{root}")
        return 1
    if not args.dry_run and not args.kb_id:
        print("非 dry-run 模式必须提供 --kb-id")
        return 1

    client = WeKnoraClient() if not args.dry_run else None
    all_chunks, summaries, failures = [], [], []

    for path in files:
        chunks, stats = process_file(path, args.max_chars, args.overlap)
        summaries.append(stats)
        all_chunks.extend(c.as_dict() for c in chunks)

        if args.dry_run:
            continue
        try:
            result = client.upload_if_changed(args.kb_id, path)  # type: ignore[union-attr]
            stats["upload"] = result["status"]
        except Exception as e:                      # 单文件失败不中断整批
            stats["upload"] = "failed"
            stats["error"] = str(e)[:300]
            failures.append(path.name)

    if args.dump:
        out = Path(args.dump)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(json.dumps(c, ensure_ascii=False) for c in all_chunks),
                       encoding="utf-8")
        print(f"切块结果已写入 {out}")

    print(json.dumps(summaries, ensure_ascii=False, indent=2))
    total = sum(s["chunks"] for s in summaries)
    print(f"\n共 {len(files)} 个文档、{total} 个块；超限块 {sum(s['oversized'] for s in summaries)} 个")
    if failures:
        print("入库失败：" + "、".join(failures))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""入库批次台账：让"这条答案是哪一批、哪个文件、哪一块"有账可查。

没有台账时，线上出现一个错答案，排查路径是这样的：翻聊天记录 → 猜是哪份文档 →
去知识库里搜标题 → 找到条目 → 但不知道它是什么时候、被哪次入库带进来的。
如果中间有人重跑过流水线、换过一版分块参数，整个推理就断了。

台账把这条链补上，一条 JSONL 记录一次文件入库：

    batch_id ── 一次运行（同一批参数、同一个知识库）
      └─ file / sha256 / mode / chunks / chunk_uids / status / error / ts

三个直接收益：

1. **可回溯**：用户反馈的答案 → 引用里的块 uid → 台账 → 批次与源文件指纹；
2. **可比对**：两次批次之间，哪些块 uid 变了（内容真变了）哪些只是换序，一目了然；
3. **可门禁**：发布前查"上一批有没有 failed"，比人工问"都传上去了吗"可靠。

代价：多一个需要运维的文件（不要提交、不要随手删）；``chunk_uids`` 会让台账变大，
文档量大时可以只存数量、把 uid 明细落到单独文件。
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

DEFAULT_LEDGER = Path("data/ledger/ingest.jsonl")

#: 同一进程内的批次序号：保证"同一秒跑两次"也拿到不同批次号
_BATCH_SEQ = itertools.count(1)


def new_batch_id(now: datetime | None = None, salt: str = "") -> str:
    """批次号：时间可读 + 随机后缀防同秒撞号（撞号会让两次运行的数据混在一起）。

    后缀掺了进程内自增序号：Windows 上 ``time_ns()`` 的粒度可能粗到毫秒级，
    纯靠时间戳，同一秒内连续跑两次就会拿到同一个批次号。
    """
    ts = (now or datetime.now()).strftime("%Y%m%dT%H%M%S")
    seq = next(_BATCH_SEQ)
    digest = hashlib.sha1(f"{ts}:{salt}:{time.time_ns()}:{seq}".encode("utf-8")).hexdigest()[:4]
    return f"{ts}-{digest}"


class BatchLedger:
    """JSONL 追加写：一行一条，坏一行不影响其他行，也方便直接 grep。"""

    def __init__(self, path: Path | str = DEFAULT_LEDGER) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    # ── 写 ──────────────────────────

    def record(self, *, batch_id: str, kb_id: str, file: str, status: str,
               sha256: str = "", chunks: int = 0, chunk_uids: list[str] | tuple[str, ...] = (),
               mode: str = "", error: str = "", **extra) -> dict:
        entry = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "batch_id": batch_id,
            "kb_id": kb_id,
            "file": file,
            "mode": mode,
            "sha256": sha256,
            "chunks": chunks,
            "chunk_uids": list(chunk_uids),
            "status": status,
        }
        if error:
            entry["error"] = error[:500]
        entry.update(extra)
        self._append(entry)
        return entry

    def _append(self, entry: dict) -> None:
        line = json.dumps(entry, ensure_ascii=False)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()          # 进程被 kill 也能留下已完成的记录；代价是没做 fsync

    # ── 读 ──────────────────────────

    def records(self, batch_id: str | None = None) -> list[dict]:
        if not self.path.exists():
            return []
        out: list[dict] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:          # 半行写入/手工编辑损坏：跳过，不让台账拖垮流水线
                continue
            if batch_id is None or entry.get("batch_id") == batch_id:
                out.append(entry)
        return out

    def batches(self) -> list[str]:
        seen: list[str] = []
        for entry in self.records():
            bid = entry.get("batch_id")
            if bid and bid not in seen:
                seen.append(bid)
        return seen

    def last_batch(self) -> str:
        batches = self.batches()
        return batches[-1] if batches else ""

    def summary(self, batch_id: str | None = None) -> dict:
        batch_id = batch_id or self.last_batch()
        entries = self.records(batch_id) if batch_id else []
        status: dict[str, int] = {}
        for e in entries:
            status[e.get("status", "unknown")] = status.get(e.get("status", "unknown"), 0) + 1
        return {
            "batch_id": batch_id,
            "文件数": len(entries),
            "状态": status,
            "块数": sum(int(e.get("chunks") or 0) for e in entries),
            "kb_id": next((e.get("kb_id") for e in entries if e.get("kb_id")), ""),
            "开始": entries[0]["ts"] if entries else "",
            "结束": entries[-1]["ts"] if entries else "",
            "失败": [e.get("file", "") for e in entries if e.get("status") == "failed"],
        }

    def failures(self, batch_id: str | None = None) -> list[dict]:
        batch_id = batch_id or self.last_batch()
        return [e for e in self.records(batch_id) if e.get("status") == "failed"]

    def changed_uids(self, old_batch: str, new_batch: str) -> dict:
        """两批之间哪些块新增/消失：判断"这次到底改动了什么"的最小依据。"""
        old = {u for e in self.records(old_batch) for u in e.get("chunk_uids", [])}
        new = {u for e in self.records(new_batch) for u in e.get("chunk_uids", [])}
        return {"新增": sorted(new - old), "消失": sorted(old - new),
                "未变": len(old & new)}


def sha256_file(path: Path) -> str:
    """大文件分块读取：制度附件偶尔有几十 MB，一次性读进内存没有必要。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="入库批次台账")
    parser.add_argument("--path", default=str(DEFAULT_LEDGER))
    parser.add_argument("--batch", default="", help="批次号，默认最近一批")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--require-ok", action="store_true",
                        help="该批次有失败文件时退出码为 3（发布前门禁用）")
    args = parser.parse_args(argv)

    ledger = BatchLedger(args.path)
    batch_id = args.batch or ledger.last_batch()
    if not batch_id:
        print(f"台账里没有记录：{ledger.path}")
        return 1
    summary = ledger.summary(batch_id)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"批次 {batch_id}")
        print(f"知识库 {summary['kb_id'] or '-'}    文件 {summary['文件数']}    块 {summary['块数']}")
        print("状态：" + "、".join(f"{k} {v}" for k, v in sorted(summary["状态"].items())))
        if summary["失败"]:
            print("失败文件：" + "、".join(summary["失败"]))
    if args.require_ok and summary["失败"]:
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())

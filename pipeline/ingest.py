# -*- coding: utf-8 -*-
"""入库流水线：清洗 → 分块 → 体检 → 版本/密级准入 → 上传 → 记账。

用法::

    python -m pipeline.ingest --dir data/sample --dry-run       # 只看切块与体检，不联网
    python -m pipeline.ingest --dir data/sample --kb-id <id>   # 真正入库
    python -m pipeline.ingest --dir ... --kb-id ... --mode chunks   # 条款级入库

三个容易被忽略、但决定了"这套东西能不能信"的设计：

1. **dry-run 是必需品，不是调试开关**。分块改一条规则，先肉眼看过 20 个块再入库，
   比事后去线上找"为什么这个问题答不出来"便宜得多；
2. **入库内容必须等于评测内容**。原样上传文件时，清洗与分块只作用于本地评测，
   线上是 WeKnora 自己的解析器在切——评测报告再漂亮，也证明不了线上。所以默认
   ``--mode cleaned``（上传清洗稿），需要条款级引用时用 ``--mode chunks``；
3. **入库前过三道闸**：密级（能不能进这个库）、版本时效（是不是当前有效的那一版）、
   体检分数（文档结构够不够支撑问答）。过不了的不静默上传，只记录原因。

失败处理：单文件失败不中断整批，最后统一汇报，并把原因写进批次台账。
退出码：0 正常、1 用法错误、2 有上传失败、3 有内容被阻断（需人工处理）。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from client.weknora import WeKnoraClient
from pipeline.chunker import chunk_document
from pipeline.cleaner import clean
from pipeline.ledger import BatchLedger, new_batch_id, sha256_file
from pipeline.quality import QualityReport, analyze
from pipeline.security import (
    DocumentPolicy, check_upload_level, policies_manifest, policy_from_metadata,
)
from pipeline.tables import extract_tables, write_json as write_tables_json
from pipeline.versioning import (
    DocVersion, find_conflicts, from_metadata, parse_date, select_effective,
)

#: 能被本地清洗/分块规则处理的格式
NATIVE = {".md", ".txt"}
#: 只能原样交给 WeKnora 解析器的格式（本地读不出密级与元信息）
PASSTHROUGH = {".docx", ".pdf", ".doc"}
SUPPORTED = NATIVE | PASSTHROUGH


@dataclass
class DocPlan:
    """一份文档在这次入库里的全部中间结果，便于最终一次性汇报。"""

    path: Path
    doc_id: str
    rel: str
    native: bool = True
    body: str = ""
    metadata: dict = field(default_factory=dict)
    version: DocVersion | None = None
    policy: DocumentPolicy | None = None
    quality: QualityReport | None = None
    chunks: list = field(default_factory=list)
    status: str = "pending"
    reason: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def text_chars(self) -> int:
        return sum(getattr(c, "char_count", 0) for c in self.chunks)


def read_document(path: Path) -> str:
    """读纯文本文档。docx/pdf 走外部解析（企业侧由 WeKnora 解析器负责）。"""
    return path.read_text(encoding="utf-8", errors="replace")


def discover(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED)


def plan_document(path: Path, root: Path, max_chars: int, overlap: int) -> DocPlan:
    rel = str(path.relative_to(root))
    plan = DocPlan(path=path, doc_id=path.stem, rel=rel,
                   native=path.suffix.lower() in NATIVE)
    if not plan.native:
        plan.notes.append("二进制文档：本地不做清洗与分块，密级未知")
        return plan

    body, report = clean(read_document(path), title=path.stem)
    plan.body = body
    plan.metadata = report.metadata
    plan.policy = policy_from_metadata(path.stem, report.metadata, body)
    plan.version = from_metadata(path.stem, path.name, report.metadata,
                                 title=path.stem, source=rel)
    plan.chunks = chunk_document(
        path.stem, body, max_chars=max_chars, overlap=overlap,
        clearance=plan.policy.level, departments=plan.policy.departments,
    )
    plan.quality = analyze(path.stem, plan.chunks, max_chars=max_chars)
    return plan


def version_skips(plans: list[DocPlan], as_of, policy: str) -> dict[str, str]:
    """按版本时效决定哪些文件这次不入库。``policy=keep`` 时不做拦截。"""
    if policy == "keep":
        return {}
    docs = [p.version for p in plans if p.version]
    if not docs:
        return {}
    sel = select_effective(docs, as_of)
    out: dict[str, str] = {}
    for dv in sel.superseded + sel.pending + sel.retired:
        out[dv.source or dv.file] = sel.reason.get(dv.key, "非当前有效版本")
    return out


def precheck(plan: DocPlan, args, skips: dict[str, str]) -> tuple[str, str]:
    """上传前的三道闸。返回 ("", 说明) 表示可以上传。"""
    if not plan.native:
        if not args.allow_passthrough:
            return "skipped", "二进制文档无法清洗、也无法判定密级；确认可原样上传后加 --allow-passthrough"
        return "", "密级未知，按知识库密级处理（建议在 WeKnora 侧单独设权限）"
    if not plan.chunks:
        return "blocked", "未切出任何块：检查是否加密、扫描件，或清洗规则把内容洗掉了"
    if plan.policy:
        ok, why = check_upload_level(plan.policy.level, args.kb_clearance)
        if not ok:
            return "blocked", why
    if plan.rel in skips:
        return "skipped", skips[plan.rel]
    if args.min_quality and plan.quality and plan.quality.score < args.min_quality:
        return "blocked", f"文档体检 {plan.quality.score} 分，低于门槛 {args.min_quality}，建议整改后再入库"
    return "", ""


def stage(plan: DocPlan, mode: str, staging: Path) -> list[tuple[Path, str]]:
    """把"要上传的内容"落到磁盘，返回 [(路径, 知识条目标题)]。

    落盘而不是直接用内存/流：上传失败要能重跑、要能人工打开看一眼、
    要能用 sha256 判断"这一版到底传没传过"。
    """
    rel = Path(plan.rel)
    if not plan.native or mode == "raw":
        return [(plan.path, plan.doc_id)]
    if mode == "cleaned":
        target = staging / "cleaned" / rel.with_suffix(".md")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(plan.body, encoding="utf-8")
        return [(target, plan.doc_id)]
    if mode == "chunks":
        out_dir = staging / "chunks" / rel.with_suffix("")
        out_dir.mkdir(parents=True, exist_ok=True)
        out: list[tuple[Path, str]] = []
        for c in plan.chunks:
            target = out_dir / f"{c.index:03d}.md"
            target.write_text(c.text, encoding="utf-8")
            out.append((target, f"{plan.doc_id}#{c.index:03d}"))
        return out
    return [(plan.path, plan.doc_id)]


def upload_plan(client: WeKnoraClient, kb_id: str, plan: DocPlan, mode: str,
                staging: Path) -> tuple[str, str]:
    # 二进制文档没有清洗稿/切块可传，无论 --mode 选什么都只能原样上传；
    # 强制回 raw，避免"cleaned 模式把空文件传上去"这种静默损坏。
    effective_mode = mode if plan.native else "raw"
    targets = stage(plan, effective_mode, staging)
    errors: list[str] = []
    uploaded = 0
    for target, title in targets:
        try:
            result = client.upload_if_changed(kb_id, target, mode=effective_mode, title=title)
            if result["status"] == "uploaded":
                uploaded += 1
        except Exception as e:                     # 单文件/单块失败不中断整批
            errors.append(f"{target.name}: {str(e)[:200]}")
    if errors:
        return "failed", "；".join(errors[:3])
    if not uploaded:
        return "skipped", f"{len(targets)} 个知识条目内容未变化"
    return "uploaded", f"{uploaded}/{len(targets)} 个知识条目已上传"


def _summary(plans: list[DocPlan], batch_id: str, args, conflicts) -> dict:
    counts: dict[str, int] = {}
    for p in plans:
        counts[p.status] = counts.get(p.status, 0) + 1
    return {
        "batch_id": batch_id,
        "mode": args.mode,
        "kb_id": args.kb_id,
        "as_of": args.as_of or "",
        "kb_clearance": args.kb_clearance,
        "documents": [
            {
                "file": p.rel,
                "status": p.status,
                "reason": p.reason,
                "doc_no": p.version.doc_no if p.version else "",
                "version": p.version.version if p.version else "",
                "effective": p.version.effective.isoformat() if p.version and p.version.effective else "",
                "clearance": p.policy.level if p.policy else "未知",
                "chunks": len(p.chunks),
                "chars": p.text_chars,
                "avg_chars": p.quality.metrics.get("平均字数", 0) if p.quality else 0,
                "tables": p.quality.metrics.get("表格块", 0) if p.quality else 0,
                "oversized": p.quality.metrics.get("超限块", 0) if p.quality else 0,
                "score": p.quality.score if p.quality else 0,
                "grade": p.quality.grade if p.quality else "-",
                "notes": p.notes,
            }
            for p in plans
        ],
        "totals": {
            "documents": len(plans),
            "chunks": sum(len(p.chunks) for p in plans),
            **counts,
        },
        "conflicts": [
            {"group": c.group, "kind": c.kind, "severity": c.severity, "detail": c.detail}
            for c in conflicts
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="企业文档清洗、分块、体检与入库")
    parser.add_argument("--dir", required=True, help="待处理文档目录")
    parser.add_argument("--kb-id", default="", help="知识库 ID（dry-run 时可省略）")
    parser.add_argument("--mode", choices=["cleaned", "chunks", "raw"], default="cleaned",
                        help="cleaned=上传清洗稿（默认，保证线上与评测同源）；"
                             "chunks=按块上传（条款级引用）；raw=原样上传（交给上游解析器）")
    parser.add_argument("--max-chars", type=int, default=600)
    parser.add_argument("--overlap", type=int, default=80)
    parser.add_argument("--dry-run", action="store_true", help="只打印切块/体检结果，不联网、不落账")
    parser.add_argument("--dump", default="", help="把切好的块写成 jsonl，便于人工检查")
    parser.add_argument("--staging", default="data/staging", help="清洗稿/切块的落盘目录")
    parser.add_argument("--ledger", default="data/ledger/ingest.jsonl", help="批次台账路径")
    parser.add_argument("--no-ledger", action="store_true", help="不写批次台账")
    parser.add_argument("--kb-clearance", default="内部",
                        help="知识库允许的最高密级（公开/内部/秘密/机密/绝密）")
    parser.add_argument("--version-policy", choices=["latest", "keep"], default="latest",
                        help="latest=同一制度只入库当前有效最新版；keep=全部保留")
    parser.add_argument("--as-of", default="", help="按指定时点判断版本有效性（YYYY-MM-DD）")
    parser.add_argument("--min-quality", type=int, default=0, help="体检分数低于该值时阻断入库")
    parser.add_argument("--policies-out", default="",
                        help="把各文档的密级/适用范围写成策略清单（给检索层过滤用）")
    parser.add_argument("--tables-out", default="",
                        help="把文档里的表格结构导出为 JSON（数值题走确定性查询，不让模型硬算）")
    parser.add_argument("--allow-passthrough", action="store_true",
                        help="允许原样上传 docx/pdf（密级未知，需自行确认）")
    args = parser.parse_args(argv)

    root = Path(args.dir)
    if not root.exists():
        print(f"目录不存在：{root}")
        return 1
    files = discover(root)
    if not files:
        print(f"目录下没有可处理的文档：{root}")
        return 1
    if not args.dry_run and not args.kb_id:
        print("非 dry-run 模式必须提供 --kb-id")
        return 1
    as_of = parse_date(args.as_of) if args.as_of else None
    if args.as_of and not as_of:
        print(f"--as-of 格式不对：{args.as_of}")
        return 1

    plans = [plan_document(p, root, args.max_chars, args.overlap) for p in files]
    conflicts = find_conflicts([p.version for p in plans if p.version], as_of)
    skips = version_skips(plans, as_of, args.version_policy)
    batch_id = new_batch_id(salt=f"{root}|{args.mode}")

    client = None if args.dry_run else WeKnoraClient()
    ledger = None if (args.dry_run or args.no_ledger) else BatchLedger(args.ledger)
    staging = Path(args.staging)

    for plan in plans:
        status, reason = precheck(plan, args, skips)
        if status:
            plan.status, plan.reason = status, reason
        elif args.dry_run:
            plan.status, plan.reason = "dry_run", reason
        else:
            plan.status, plan.reason = upload_plan(client, args.kb_id, plan, args.mode, staging)  # type: ignore[arg-type]

        if ledger is not None:
            ledger.record(
                batch_id=batch_id, kb_id=args.kb_id, file=plan.rel, status=plan.status,
                sha256=sha256_file(plan.path), chunks=len(plan.chunks),
                chunk_uids=[c.uid for c in plan.chunks],
                # 引用标题里带的是 "文档名#003"，台账要能把它映射回 uid 与条款路径，
                # 否则"从答案反查到源文件"这条链在标题这一环就断了
                chunks_detail=[{"index": c.index, "uid": c.uid, "section": c.heading_path}
                               for c in plan.chunks],
                mode=args.mode if plan.native else "raw",
                error=plan.reason if plan.status in ("failed", "blocked") else "",
                doc_no=plan.version.doc_no if plan.version else "",
                version=plan.version.version if plan.version else "",
                clearance=plan.policy.level if plan.policy else "未知",
            )

    if args.policies_out:
        # 检索层过滤靠这份清单：线上返回的块没有密级字段，必须按来源反查
        manifest = policies_manifest(
            {p.doc_id: p.policy for p in plans if p.policy},
            {"kb_id": args.kb_id, "kb_clearance": args.kb_clearance},
        )
        out = Path(args.policies_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"策略清单已写入 {out}（{len(manifest['documents'])} 份文档）")

    if args.tables_out:
        # 表格结构单独落一份：数值类问题（"出差 9 天谁审批"）走区间查询，不让 LLM 读表算数
        entries = [(p.doc_id, p.rel, table)
                   for p in plans if p.native and p.body
                   for table in extract_tables(p.body)]
        write_tables_json(entries, Path(args.tables_out))
        print(f"表格结构已写入 {args.tables_out}（{len(entries)} 张表）")

    if args.dump:
        out = Path(args.dump)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(json.dumps(c.as_dict(), ensure_ascii=False)
                                 for p in plans for c in p.chunks), encoding="utf-8")
        print(f"切块结果已写入 {out}")

    summary = _summary(plans, batch_id, args, conflicts)
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    t = summary["totals"]
    print(f"\n批次 {batch_id}（{args.mode}）  文档 {t['documents']}  块 {t['chunks']}  "
          f"上传 {t.get('uploaded', 0)}  跳过 {t.get('skipped', 0)}  "
          f"阻断 {t.get('blocked', 0)}  失败 {t.get('failed', 0)}")
    for c in conflicts:
        if c.severity == "high":
            print(f"[版本冲突] {c.group}：{c.detail}")
    if t.get("failed"):
        return 2
    if t.get("blocked") or any(c.severity == "high" for c in conflicts):
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())

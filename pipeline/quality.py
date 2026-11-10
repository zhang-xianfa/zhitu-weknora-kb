# -*- coding: utf-8 -*-
"""文档体检：把"答不准"的一部分归因到文档本身。

POC 最容易犯的错，是把所有问题都当成技术问题去调参。实际排查下来，很大一部分
问不准的根因在文档侧：条款没有标题、一条被排版拆成八行、表格贴成图片、
同一段话在附录里重复三遍。这些不是模型能救的，也不是工程能改的——
得让业务部门去补文档，而业务部门只认一样东西：**能看懂的问题清单**。

所以这里输出的是"体检报告"而不是"指标"：分数 + 分项 + 具体建议，
每条建议都对应一个可执行的文档修改动作。

边界：分数是经验权重，不是学术指标；它的用途是横向比较同一批文档、跟踪改进，
不是对外承诺"这篇文档 87 分所以答案 87 分会准"。
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from pipeline.chunker import chunk_document
from pipeline.cleaner import clean

#: 块的层级路径里是否真的落到了"条"上
_CLAUSE_IN_PATH = re.compile(r"第[一二三四五六七八九十百零〇\d]+条")

_GRADES = ((85, "A", "可直接上线"), (70, "B", "可用，建议补齐标题"), (55, "C", "需要整改后再上"), (0, "D", "不建议入库"))


def _get(item, name, default=None):
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


@dataclass
class QualityReport:
    doc_id: str
    chunks: int
    score: int
    grade: str
    verdict: str
    metrics: dict
    suggestions: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "doc_id": self.doc_id,
            "chunks": self.chunks,
            "score": self.score,
            "grade": self.grade,
            "verdict": self.verdict,
            "metrics": self.metrics,
            "suggestions": self.suggestions,
        }


def _rate(part: int, whole: int) -> float:
    return round(part / whole, 4) if whole else 0.0


def analyze(doc_id: str, chunks: list, min_chars: int = 120, max_chars: int = 600) -> QualityReport:
    """对一篇文档切出来的块打分。

    评分只看**内容块**：开头那块"【文档元信息】"是流水线自己拼的（版本、生效日期），
    它没有条款路径是设计使然，不该算成文档缺陷——把系统自己造的问题扣到业务部门头上，
    体检报告第一次发出去就没人信了。
    """
    total = len(chunks)
    if not total:
        return QualityReport(doc_id, 0, 0, "D", "没有切出任何块",
                             {}, ["文档未切出内容：检查是否加密、扫描件或清洗过度"])

    bodies = [str(_get(c, "body", "") or "") for c in chunks]
    meta_flags = [b.startswith("【文档元信息】") for b in bodies]
    content = [c for c, is_meta in zip(chunks, meta_flags) if not is_meta]
    content_bodies = [b for b, is_meta in zip(bodies, meta_flags) if not is_meta]
    paths = [str(_get(c, "heading_path", "") or "") for c in content]
    counts = [_get(c, "char_count", 0) or 0 for c in content]
    n = len(content)
    if not n:
        return QualityReport(doc_id, total, 0, "D", "只有元信息块，没有正文",
                             {"块数": total, "元信息块": total},
                             ["正文为空：检查清洗规则是否把内容洗掉了"])

    uids = {_get(c, "uid", None) or f"{p}\x1f{b}"
            for c, p, b in zip(content, paths, content_bodies)}
    untitled = sum(1 for p in paths if p in ("", "（无标题）"))
    clause_less = sum(1 for p in paths if not _CLAUSE_IN_PATH.search(p))
    short = sum(1 for x in counts if x < min_chars)
    # 真正信息量薄的块：又短、又没有条款路径。短但有出处（"第6条 一句话"）不是缺陷——
    # 制度就是会有一句话的条款，硬要合并反而把两条不相干的规定粘在一起。
    naked_short = sum(1 for c, p in zip(content, paths)
                      if (_get(c, "char_count", 0) or 0) < min_chars
                      and not _CLAUSE_IN_PATH.search(p) and p not in ("", "（无标题）"))
    oversized = sum(1 for c in content if _get(c, "oversized", False))
    tables = sum(1 for c in content if _get(c, "has_table", False))
    dup = n - len(uids)

    metrics = {
        "块数": total,
        "元信息块": total - n,
        "平均字数": round(sum(counts) / n, 1),
        "最大字数": max(counts),
        "短块占比": _rate(short, n),
        "裸短块": naked_short,
        "超限块": oversized,
        "无标题块占比": _rate(untitled, n),
        "无条款路径占比": _rate(clause_less, n),
        "重复块": dup,
        "表格块": tables,
    }

    penalty = (
        metrics["无标题块占比"] * 40
        + metrics["无条款路径占比"] * 10
        + _rate(naked_short, n) * 25
        + _rate(oversized, n) * 10
        + _rate(dup, n) * 30
    )
    score = max(0, min(100, round(100 - penalty)))
    grade, verdict = next((g, v) for floor, g, v in _GRADES if score >= floor)

    suggestions: list[str] = []
    if untitled:
        suggestions.append(
            f"{untitled} 个内容块没有章节/条款路径：这些块在向量里几乎没有上下文，"
            "请给对应段落补上「第X章 / 第X条」这类标题"
        )
    if metrics["无条款路径占比"] > 0.5:
        suggestions.append(
            "超过一半的块不在任何条款下：这份文档可能不是制度/流程体裁，"
            "需要单独配一套分块规则，别硬套条款切分"
        )
    if naked_short:
        suggestions.append(
            f"{naked_short} 个块又短又没条款归属（< {min_chars} 字）：这类块召回噪声最大，"
            "优先给它们补标题或并入相邻条款"
        )
    if oversized:
        suggestions.append(
            f"{oversized} 个超限块（整表或超长条款）：它们会挤占上下文预算，"
            "建议把跨页大表拆成「表头+分组」的多个块"
        )
    if dup:
        suggestions.append(f"{dup} 个块内容重复（常见于附录/附件重复粘贴）：重复块会挤掉真正不同的召回结果")
    if not suggestions:
        suggestions.append("结构健康，未发现需要业务侧修改的地方")
    return QualityReport(doc_id, total, score, grade, verdict, metrics, suggestions)


def analyze_directory(directory: Path, max_chars: int = 600, overlap: int = 80,
                      min_chars: int = 120) -> list[QualityReport]:
    reports: list[QualityReport] = []
    for path in sorted(Path(directory).rglob("*")):
        if path.suffix.lower() not in (".md", ".txt") or not path.is_file():
            continue
        body, _ = clean(path.read_text(encoding="utf-8", errors="replace"), title=path.stem)
        chunks = chunk_document(path.stem, body, max_chars=max_chars, overlap=overlap)
        reports.append(analyze(path.stem, chunks, min_chars=min_chars, max_chars=max_chars))
    return reports


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="入库前文档体检（可问答性评分）")
    parser.add_argument("--dir", required=True, help="待体检的文档目录")
    parser.add_argument("--max-chars", type=int, default=600)
    parser.add_argument("--overlap", type=int, default=80)
    parser.add_argument("--min-score", type=int, default=0, help="低于该分数时退出码为 3，可用作入库门禁")
    parser.add_argument("--json", default="", help="把报告写成 JSON")
    args = parser.parse_args(argv)

    root = Path(args.dir)
    if not root.exists():
        print(f"目录不存在：{root}")
        return 1
    reports = analyze_directory(root, args.max_chars, args.overlap)
    if not reports:
        print(f"目录下没有可体检的文档：{root}")
        return 1

    print(f"{'文档':<24} {'块数':>4} {'均长':>6} {'短块':>6} {'无标题':>6} {'超限':>4} {'重复':>4} {'分数':>4} {'等级':>3}")
    for r in reports:
        m = r.metrics
        print(f"{r.doc_id:<24} {r.chunks:>4} {m.get('平均字数', 0):>6} "
              f"{m.get('短块占比', 0):>6.0%} {m.get('无标题块占比', 0):>6.0%} "
              f"{m.get('超限块', 0):>4} {m.get('重复块', 0):>4} {r.score:>4} {r.grade:>3}")
    print("\n建议：")
    for r in reports:
        print(f"\n[{r.doc_id}] {r.score} 分 · {r.grade} · {r.verdict}")
        for s in r.suggestions:
            print(f"  - {s}")

    if args.json:
        import json
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps([r.as_dict() for r in reports],
                                              ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n报告已写入 {args.json}")

    if args.min_score and any(r.score < args.min_score for r in reports):
        print(f"\n有文档低于 {args.min_score} 分，按门禁要求需整改后再入库。")
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())

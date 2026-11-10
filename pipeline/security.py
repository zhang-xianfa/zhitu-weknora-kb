# -*- coding: utf-8 -*-
"""密级与可见范围：企业知识库里，"相关"不等于"该看"。

制度文档天然带层级——薪酬、并购、审计底稿不可能对全员开放。通用 RAG 只优化相关性，
把召回的前 N 块直接拼进 prompt，于是"谁都能问到高管薪酬"这种事故不是模型幻觉，
而是**检索层做对了、权限层根本没做**。

这里的取舍：

1. **权限过滤必须在"检索之后、生成之前"**。放在生成之后去删句子，等于让模型先看过
   机密内容，删不干净（它已经在推理里用过），而且流式输出根本来不及删；
2. **粒度先做到文档级**。WeKnora 的权限模型是库/知识条目级，硬做条款级 ACL
   会和上游冲突；文档级已经能挡住绝大多数事故（一份制度本身密级一致）；
3. **越权时的话术必须和无答案完全一致**。"该文档存在但你没权限"这句话本身就是泄露，
   所以对外统一回 `UNAUTHORIZED_REFUSAL`，真实原因只进审计日志（且不记正文）。

代价：密级靠文档元信息或页眉标识，标注错就防不住；多知识库拆分需要业务侧配合。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

#: 密级由低到高。顺序即权限大小，不要改顺序。
LEVELS = ("公开", "内部", "秘密", "机密", "绝密")
DEFAULT_LEVEL = "内部"

_ALIASES = {
    "public": "公开", "外部": "公开", "公开": "公开",
    "internal": "内部", "内部资料": "内部", "仅内部": "内部", "内部": "内部",
    "confidential": "秘密", "秘密": "秘密", "秘密级": "秘密",
    "secret": "机密", "机密": "机密", "机密级": "机密",
    "topsecret": "绝密", "绝密": "绝密", "绝密级": "绝密",
}

#: 只认"带壳"的密级标识：【机密】/（机密）/ 机密级 / 机密文件。
#: 裸词"秘密"在正文里太常见（"商业秘密""保守秘密"），据此判密会误伤。
_LEVEL_MARK = re.compile(
    r"[【\[（(]\s*(绝密|机密|秘密|内部|公开)\s*[】\]）)]"
    r"|(绝密|机密|秘密)\s*[级文件资料]"
)

#: 越权拒答话术：与"无答案"完全一致，不能让人从话术差异推断出"存在但你没权限"
UNAUTHORIZED_REFUSAL = "资料中没有找到相关内容。"


def normalize_level(level: str | None, default: str = DEFAULT_LEVEL) -> str:
    """把各种写法归一到 LEVELS 之一；认不出来就按默认（保守方向由调用方决定）。"""
    if not level:
        return default
    key = str(level).strip().replace(" ", "").lower()
    if key in _ALIASES:
        return _ALIASES[key]
    for name in LEVELS:                 # "机密（仅限董事会）" 这类带补充说明的写法
        if name in key:
            return name
    return default


def level_rank(level: str | None) -> int:
    return LEVELS.index(normalize_level(level))


def extract_level(metadata: dict, text: str = "") -> str:
    """先看元信息里的"密级："，再看页眉/首行的密级标识。

    元信息优先：页眉标识常被清洗规则当水印删掉，元信息才是可审计的来源。
    """
    if metadata.get("密级"):
        return normalize_level(metadata["密级"])
    head = "\n".join(text.strip().splitlines()[:5])
    m = _LEVEL_MARK.search(head)
    if m:
        return normalize_level(m.group(1) or m.group(2))
    return DEFAULT_LEVEL


def parse_departments(value: str | None) -> tuple[str, ...]:
    """适用范围："人力资源部、财务部" → ("人力资源部", "财务部")。空表示不限部门。"""
    if not value:
        return ()
    parts = re.split(r"[、,，;；/\s]+", str(value).strip())
    return tuple(p for p in (x.strip() for x in parts) if p)


@dataclass(frozen=True)
class Viewer:
    """提问的人。内网里这些字段通常来自 SSO / OA，POC 阶段先由命令行显式传。"""

    name: str = "anonymous"
    clearance: str = DEFAULT_LEVEL
    departments: tuple[str, ...] = ()

    @property
    def rank(self) -> int:
        return level_rank(self.clearance)


@dataclass(frozen=True)
class DocumentPolicy:
    """一份文档的访问策略。"""

    doc_id: str
    level: str = DEFAULT_LEVEL
    departments: tuple[str, ...] = ()

    @property
    def rank(self) -> int:
        return level_rank(self.level)

    def allows(self, viewer: Viewer) -> bool:
        if self.rank > viewer.rank:
            return False
        # 限定了适用部门：提问人必须属于其中之一。没传部门的人一律看不到，
        # 宁可少答，也不能靠"猜他大概是总部的"放行。
        if self.departments:
            return bool(set(self.departments) & set(viewer.departments))
        return True


def policy_from_metadata(doc_id: str, metadata: dict, text: str = "") -> DocumentPolicy:
    return DocumentPolicy(
        doc_id=doc_id,
        level=extract_level(metadata, text),
        departments=parse_departments(metadata.get("适用范围")),
    )


def check_upload_level(doc_level: str, kb_level: str) -> tuple[bool, str]:
    """入库准入：文档密级高于知识库允许的最高密级时拒绝入库。

    这条规则的作用不是"多一层校验"，而是**逼着部署时把知识库拆对**：
    涉密制度应当进独立的知识库、配独立的 Token，而不是和全员制度混在一个库里。
    """
    doc_rank, kb_rank = level_rank(doc_level), level_rank(kb_level)
    if doc_rank <= kb_rank:
        return True, ""
    return False, (
        f"文档密级「{normalize_level(doc_level)}」高于知识库允许的最高密级"
        f"「{normalize_level(kb_level)}」，应改入专用知识库"
    )


def _level_of(item) -> str:
    if isinstance(item, dict):
        return normalize_level(item.get("clearance"))
    return normalize_level(getattr(item, "clearance", DEFAULT_LEVEL))


def _departments_of(item) -> tuple[str, ...]:
    if isinstance(item, dict):
        return tuple(item.get("departments") or ())
    return tuple(getattr(item, "departments", ()) or ())


def filter_chunks(chunks: list, viewer: Viewer) -> tuple[list, list]:
    """按提问人过滤块，返回 (可见, 被拦下)。

    被拦下的块**不返回给生成层**，但计数要留下——"某人经常问到越权内容"
    本身就是需要审计的信号。
    """
    kept, blocked = [], []
    for item in chunks:
        doc_id = item.get("doc_id", "") if isinstance(item, dict) else getattr(item, "doc_id", "")
        policy = DocumentPolicy(doc_id, _level_of(item), _departments_of(item))
        (kept if policy.allows(viewer) else blocked).append(item)
    return kept, blocked


# ── 策略清单：把"哪份文档归谁看"从正文里抽出来给检索层用 ──────

def source_key(source: str) -> str:
    """把引用里的来源名归一成文档 key。

    "企业差旅与费用管理办法#003.md" / "企业差旅与费用管理办法.pdf" / 绝对路径
    都要能对上同一份策略——否则越权过滤会因为"标题多了个 #003"而整条失效。
    """
    base = str(source or "").replace("\\", "/").rsplit("/", 1)[-1]
    # 先剥扩展名再剥 #003：切块上传的标题是"文档名#003.md"，顺序反了就剥不干净
    base = re.sub(r"\.(md|txt|docx?|pdf)$", "", base, flags=re.IGNORECASE)
    base = re.sub(r"#\d+$", "", base)
    return base.strip()


def policies_manifest(policies: dict[str, DocumentPolicy], extra: dict | None = None) -> dict:
    """生成可落盘的策略清单：检索层按它过滤，不必再读文档正文。"""
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        **(extra or {}),
        "documents": {
            doc_id: {
                "level": normalize_level(policy.level),
                "departments": list(policy.departments),
            }
            for doc_id, policy in policies.items()
        },
    }


def policies_from_manifest(data: dict) -> dict[str, DocumentPolicy]:
    """读回策略清单；兼容 {documents: {...}} 和平铺两种写法。"""
    entries = data.get("documents") if isinstance(data.get("documents"), dict) else data
    out: dict[str, DocumentPolicy] = {}
    for doc_id, item in (entries or {}).items():
        if not isinstance(item, dict):
            continue
        level = item.get("level") or item.get("clearance") or item.get("密级")
        departments = item.get("departments")
        if departments is None:
            departments = item.get("scope") or item.get("适用范围")
        out[str(doc_id)] = DocumentPolicy(
            str(doc_id), normalize_level(level),
            tuple(departments) if isinstance(departments, (list, tuple))
            else parse_departments(departments),
        )
    return out


def _source_of(item) -> str:
    for key in ("source", "knowledge_title", "file_name", "title", "doc_name", "doc_id"):
        value = item.get(key) if isinstance(item, dict) else getattr(item, key, None)
        if isinstance(value, str) and value:
            return value
    return ""


def filter_by_source(chunks: list[dict], policies: dict[str, DocumentPolicy],
                     viewer: Viewer) -> tuple[list[dict], list[dict]]:
    """在线检索结果的过滤：按引用来源找到策略，再判断这个人能不能看。

    线上返回的块里没有密级字段（密级在入库时才知道），所以必须靠策略清单反查。
    查不到策略的块按"内部"处理：宁可少给公开库的访客看一条，也不默认放开。
    """
    kept, blocked = [], []
    for chunk in chunks:
        source = _source_of(chunk)
        key = source_key(source)
        policy = policies.get(key) or policies.get(source) \
            or DocumentPolicy(key or "unknown", DEFAULT_LEVEL)
        (kept if policy.allows(viewer) else blocked).append(chunk)
    return kept, blocked


def audit_record(query: str, viewer: Viewer, kept: list, blocked: list) -> dict:
    """审计记录：留下"谁问了、拦了什么"，但不留问题原文与正文。

    问题里可能带个人信息（"张三的工伤怎么算"），制度文档本身涉密，
    所以只存问题哈希与文档 ID，能对账、不能反查内容。
    """
    docs = sorted({
        (b.get("doc_id", "") if isinstance(b, dict) else getattr(b, "doc_id", ""))
        for b in blocked
    } - {""})
    return {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "viewer": viewer.name,
        "clearance": normalize_level(viewer.clearance),
        "query_hash": hashlib.sha1(query.encode("utf-8")).hexdigest()[:12],
        "kept": len(kept),
        "blocked": len(blocked),
        "blocked_docs": docs,
        "decision": "refused" if blocked and not kept else "served",
    }


# ── CLI：上线前按角色做一次可见性检查 ──────────────

def main(argv: list[str] | None = None) -> int:
    """用一份块清单（chunks.jsonl）模拟某个角色，看会漏出多少越权内容。

    这一步不替代在线过滤，但能在上线前回答一个具体问题：
    "内部员工提问时，这个库里会不会有他看不到的块被召回？"
    如果有，说明知识库的密级拆分没做好——检索阶段就把它们捞出来了，
    只在生成后删句子是安全上不成立的。
    """
    parser = argparse.ArgumentParser(description="按角色检查块的可见性（离线策略检查）")
    parser.add_argument("--chunks", required=True, help="pipeline.ingest --dump 产出的 chunks.jsonl")
    parser.add_argument("--viewer", default="anonymous")
    parser.add_argument("--clearance", default=DEFAULT_LEVEL,
                        help="检查对象的密级权限：公开/内部/秘密/机密/绝密")
    parser.add_argument("--department", action="append", default=[], help="所属部门，可重复")
    parser.add_argument("--query", default="（离线可见性检查）", help="审计记录里用的查询（只存哈希）")
    parser.add_argument("--audit", default="", help="把审计记录追加到 JSONL")
    args = parser.parse_args(argv)

    path = Path(args.chunks)
    if not path.exists():
        print(f"块文件不存在：{path}")
        return 1
    chunks = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            chunks.append(json.loads(line))
    if not chunks:
        print(f"块文件为空：{path}")
        return 1

    viewer = Viewer(args.viewer, args.clearance, tuple(args.department))
    kept, blocked = filter_chunks(chunks, viewer)
    record = audit_record(args.query, viewer, kept, blocked)

    print(f"角色：{viewer.name}    密级：{normalize_level(viewer.clearance)}    "
          f"部门：{'、'.join(viewer.departments) or '-'}")
    print(f"块 {len(chunks)} 个：可见 {len(kept)}，拦截 {len(blocked)}")
    if blocked:
        docs = sorted({b.get("doc_id", "") for b in blocked})
        print("被拦截的文档：" + "、".join(docs))
        print("建议：这些内容应放进独立知识库（配独立 Token），而不是和全库混在一起；"
              "越权时对外话术必须与“无答案”一致。")
    if args.audit:
        out = Path(args.audit)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        print(f"审计记录已追加：{out}")
    print(json.dumps(record, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

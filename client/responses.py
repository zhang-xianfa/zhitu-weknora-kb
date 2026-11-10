# -*- coding: utf-8 -*-
"""响应形状适配：把"接口到底返回了什么"收在一个地方。

WeKnora 的不同接口、不同版本包装层不一样：块有时在 ``data.chunks``，有时在
``results``，有时顶层就是数组；答案有时叫 ``answer``，有时叫 ``content``。

在评测器、自检脚本里各写一遍"猜字段"，最难受的后果不是重复代码，而是**两边猜得不一样**：
自检说"召回了"，评测说"没召回"，排查时先花半天争论谁对。所以这里只留一份口径，
`ops` 和 `eval` 都从它取。
"""

from __future__ import annotations

_CHUNK_KEYS = ("chunks", "results", "data", "knowledge", "items")
_TEXT_KEYS = ("content", "text", "chunk_content", "snippet")
_SOURCE_KEYS = ("knowledge_title", "file_name", "title", "source", "doc_name", "document_title")
_ANSWER_KEYS = ("answer", "content", "response", "text")
_CITATION_KEYS = ("references", "citations", "sources", "documents")


def extract_chunks(resp) -> list[dict]:
    """把检索响应里"像块的东西"挑出来，兼容几种常见包装。"""
    if isinstance(resp, list):
        return [x for x in resp if isinstance(x, dict)]
    if not isinstance(resp, dict):
        return []
    for key in _CHUNK_KEYS:
        value = resp.get(key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
        if isinstance(value, dict):
            found = extract_chunks(value)
            if found:
                return found
    return []


def chunk_text(chunk: dict) -> str:
    for key in _TEXT_KEYS:
        value = chunk.get(key) if isinstance(chunk, dict) else None
        if isinstance(value, str) and value:
            return value
    return ""


def chunk_source(chunk: dict) -> str:
    """块来自哪份文档：引用命中率、越权过滤都靠它对齐。"""
    for key in _SOURCE_KEYS:
        value = chunk.get(key) if isinstance(chunk, dict) else None
        if isinstance(value, str) and value:
            return value
    return ""


def answer_text(resp) -> str:
    if isinstance(resp, str):
        return resp
    if not isinstance(resp, dict):
        return ""
    for key in _ANSWER_KEYS:
        value = resp.get(key)
        if isinstance(value, str) and value:
            return value
    data = resp.get("data")
    return answer_text(data) if isinstance(data, (dict, str)) else ""


def answer_citations(resp) -> list[str]:
    out: list[str] = []
    if not isinstance(resp, dict):
        return out
    for key in _CITATION_KEYS:
        value = resp.get(key)
        if not isinstance(value, list):
            continue
        for item in value:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict):
                src = chunk_source(item)
                if src:
                    out.append(src)
    if not out:
        out = [chunk_source(c) for c in extract_chunks(resp) if chunk_source(c)]
    return out


def has_content(resp) -> bool:
    """检索响应里有没有真正的块正文（空召回判定）。"""
    return any(chunk_text(c) for c in extract_chunks(resp))

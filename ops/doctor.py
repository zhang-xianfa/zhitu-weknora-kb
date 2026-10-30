# -*- coding: utf-8 -*-
"""上线自检：出问题先跑这个，别一上来就翻日志。

检查项按"从下往上"排：

1. **连通性**——连不上就不用往下查了（DNS / 端口 / 网关 / 鉴权）；
2. **知识库可见性**——Token 有没有这个库的权限；
3. **入库积压**——知识条目的解析状态里有多少卡在 pending/processing；
4. **空召回**——拿几个已知能答的问题去检索，全空说明分块或索引有问题；
5. **延迟分布**——P50/P95，判断是"慢"还是"错"。

退出码：0 全过，1 有硬失败（连通性/全空召回），2 只有告警（积压、延迟偏高）。
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time

from client.weknora import WeKnoraClient, WeKnoraError

DEFAULT_QUERIES = ["请假流程是什么", "报销需要哪些材料", "审批权限"]
_PENDING_STATES = {"pending", "processing", "parsing", "queued", "running"}


def _parse_state(item: dict) -> str:
    for key in ("parse_status", "status", "state"):
        value = item.get(key)
        if isinstance(value, str):
            return value.lower()
    return ""


def check(client: WeKnoraClient, kb_id: str, queries: list[str]) -> tuple[int, list[str]]:
    problems: list[str] = []
    warnings: list[str] = []

    # 1. 连通性
    started = time.perf_counter()
    try:
        client.health()
    except WeKnoraError as e:
        return 1, [f"连通性失败：{e}"]
    print(f"[1/5] 连通性 OK，{round((time.perf_counter() - started) * 1000)} ms")

    # 2. 知识库
    try:
        kbs = client.list_knowledge_bases()
    except WeKnoraError as e:
        return 1, [f"知识库列表不可用：{e}"]
    ids = {str(kb.get("id", "")) for kb in kbs}
    if kb_id and kb_id not in ids:
        return 1, [f"当前凭据看不到知识库 {kb_id}（共 {len(ids)} 个可见）"]
    print(f"[2/5] 知识库 {len(kbs)} 个，目标库可见")

    # 3. 入库积压
    items = client.list_knowledge(kb_id) if kb_id else []
    pending = [i for i in items if _parse_state(i) in _PENDING_STATES]
    if pending:
        warnings.append(f"有 {len(pending)} 条知识仍处于解析中/排队")
    print(f"[3/5] 知识条目 {len(items)} 条，解析中 {len(pending)} 条")

    # 4. 空召回
    empty = 0
    for q in queries:
        try:
            chunks = client.search(q, [kb_id]) if kb_id else {}
        except WeKnoraError as e:
            warnings.append(f"检索失败「{q}」：{e}")
            continue
        if not _has_content(chunks):
            empty += 1
    if queries and empty == len(queries):
        problems.append("所有样例查询都空召回：优先查分块/清洗是否把内容洗掉、索引是否建完")
    elif empty:
        warnings.append(f"{empty}/{len(queries)} 个样例查询空召回")
    print(f"[4/5] 空召回 {empty}/{len(queries)}")

    # 5. 延迟
    latencies = []
    for q in queries[:3]:
        started = time.perf_counter()
        try:
            if kb_id:
                client.search(q, [kb_id])
        except WeKnoraError:
            continue
        latencies.append((time.perf_counter() - started) * 1000)
    if latencies:
        p95 = max(latencies)
        median = statistics.median(latencies)
        if median > 2000:
            warnings.append(f"检索中位延迟 {median:.0f} ms 偏高")
        print(f"[5/5] 检索延迟 P50 {median:.0f} ms / 最大 {p95:.0f} ms")

    code = 1 if problems else (2 if warnings else 0)
    return code, problems + warnings


def _has_content(resp) -> bool:
    """判断检索响应里有没有实际的块。"""
    from eval.runner import extract_chunks, chunk_text
    return any(chunk_text(c) for c in extract_chunks(resp))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="WeKnora 知识库上线自检")
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--kb-id", default="")
    parser.add_argument("--queries", nargs="*", default=DEFAULT_QUERIES)
    args = parser.parse_args(argv)

    from client.weknora import ClientConfig
    config = ClientConfig.from_env()
    if args.base_url:
        config.base_url = args.base_url
    if args.api_key:
        config.api_key = args.api_key

    client = WeKnoraClient(config)
    code, messages = check(client, args.kb_id, args.queries)

    if messages:
        print("\n输出：")
        for m in messages:
            print(" -", m)
    else:
        print("\n全部检查通过。")
    return code


if __name__ == "__main__":
    sys.exit(main())

from pipeline.chunker import Chunk
from pipeline.quality import analyze


def make_chunk(body: str, path: str = "第一章 总则 › 第1条", *, index: int = 0,
               table: bool = False, oversized: bool = False) -> Chunk:
    text = body if path == "（无标题）" else f"【{path}】\n{body}"
    return Chunk("d", index, text, body, path, len(text), table, oversized)


def test_clean_doc_scores_a():
    chunks = [make_chunk(f"第{i}条 " + "规定内容" * 30, f"第一章 总则 › 第{i}条", index=i)
              for i in range(1, 6)]
    r = analyze("d", chunks)
    assert r.score >= 85 and r.grade == "A"
    assert r.suggestions and "结构健康" in r.suggestions[0]


def test_untitled_blocks_are_penalized_with_actionable_advice():
    chunks = [make_chunk("没有标题的一大段正文" * 20, "（无标题）", index=i) for i in range(4)]
    r = analyze("d", chunks)
    assert r.score < 70
    assert any("没有章节/条款路径" in s for s in r.suggestions)


def test_metadata_block_is_a_feature_not_a_defect():
    meta = make_chunk("【文档元信息】文件编号：X、版本号：V1.0", "（无标题）")
    content = [make_chunk(f"第{i}条 " + "正文" * 60, f"第一章 › 第{i}条", index=i)
               for i in range(1, 4)]
    r = analyze("d", [meta] + content)
    assert r.metrics["元信息块"] == 1
    assert r.score >= 85


def test_duplicate_blocks_are_penalized():
    body = "附录里重复粘贴的一段话" * 12
    chunks = [make_chunk(body, "第五章 附录 › 第20条", index=i) for i in range(3)]
    r = analyze("d", chunks)
    assert r.metrics["重复块"] == 2
    assert any("重复" in s for s in r.suggestions)


def test_short_block_with_clause_path_is_not_penalized():
    """一句话的条款是制度常态，短不是罪；"短且没有出处"才是噪声。"""
    short_with_path = [make_chunk("本办法自发布之日起施行。", f"第{i}章 › 第{i}条", index=i)
                       for i in range(1, 5)]
    r = analyze("d", short_with_path)
    assert r.metrics["裸短块"] == 0
    assert r.score >= 85


def test_oversized_table_block_gets_advice():
    chunks = [make_chunk("| 表 | 内容 |\n| --- | --- |\n| 1 | 2 |" * 20,
                         "第二章 › 第5条", index=0, table=True, oversized=True)]
    r = analyze("d", chunks)
    assert r.metrics["超限块"] == 1
    assert any("超限块" in s for s in r.suggestions)


def test_no_chunks_scores_zero():
    r = analyze("d", [])
    assert r.score == 0 and r.grade == "D"
    assert any("未切出内容" in s for s in r.suggestions)


def test_accepts_plain_dicts_from_chunk_dump():
    chunks = [{"body": "第1条 " + "内容" * 60, "heading_path": "第一章 › 第1条",
               "char_count": 70, "uid": "c_1", "has_table": False, "oversized": False}]
    r = analyze("d", chunks)
    assert r.chunks == 1 and r.score >= 85

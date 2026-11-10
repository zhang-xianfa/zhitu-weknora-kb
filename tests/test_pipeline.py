from pipeline.chunker import _parse_heading, chunk_document
from pipeline.cleaner import clean, normalize

DOC = """内部资料，请勿外传
测试管理办法
文件编号：T-001
版本号：V2.1
第 1 页 共 2 页

目 录
第一章 总则 …… 1
第二章 流程 …… 2

测试管理办法

第一章 总则
第 1 条 本办法用于测试。
第 2 条 本办法自发布之日起施行。

第二章 流程
第 3 条 审批权限如下：

| 天数 | 审批人 |
| --- | --- |
| 1-3 天 | 主管 |
| 4 天以上 | 部门负责人 |

第 4 条 报销须在 15 个工作日内提交，逾期不予受理。
第 2 页 共 2 页
"""


def test_clean_removes_header_footer_and_toc():
    body, report = clean(DOC, title="测试管理办法")
    assert "第 1 页 共 2 页" not in body
    assert "内部资料" not in body
    assert "第一章 总则 …… 1" not in body
    assert report.dropped_header_footer >= 2
    assert report.dropped_toc == 2


def test_clean_keeps_metadata_as_block():
    body, report = clean(DOC, title="测试管理办法")
    assert report.metadata["版本号"] == "V2.1"
    assert "【文档元信息】" in body
    assert "T-001" in body


def test_clean_normalizes_split_clause_numbers():
    text = "第 三 条 报销须在 15 个工作日内提交。"
    body, _ = clean(text)
    assert "第三条" in body


def test_chunk_carries_heading_path():
    body, _ = clean(DOC, title="测试管理办法")
    chunks = chunk_document("测试管理办法", body, max_chars=120, overlap=0)
    paths = [c.heading_path for c in chunks]
    assert any("第一章" in p for p in paths)
    assert any("第二章" in p for p in paths)


def test_table_is_never_split():
    body, _ = clean(DOC, title="测试管理办法")
    chunks = chunk_document("测试管理办法", body, max_chars=60, overlap=0)
    table_chunks = [c for c in chunks if c.has_table]
    assert table_chunks, "应至少有一个含表格的块"
    for c in table_chunks:
        assert "1-3 天" in c.text and "4 天以上" in c.text, "表格被切断了"


def test_chunks_respect_max_chars_unless_oversized():
    body, _ = clean(DOC, title="测试管理办法")
    chunks = chunk_document("测试管理办法", body, max_chars=100, overlap=10)
    for c in chunks:
        if not c.oversized:
            assert c.char_count <= 100 + 40      # 允许层级前缀带来的小幅溢出


def test_short_clauses_are_merged():
    body, _ = clean(DOC, title="测试管理办法")
    chunks = chunk_document("测试管理办法", body, max_chars=600, overlap=0)
    assert any(c.body.count("。") >= 2 and not c.has_table for c in chunks), "同章短条款应被合并"
    assert all(c.char_count <= 600 for c in chunks)


def test_every_chunk_within_short_clause_keeps_prefix():
    body, _ = clean(DOC, title="测试管理办法")
    chunks = chunk_document("测试管理办法", body, max_chars=600, overlap=0)
    assert chunks
    for c in chunks:
        if c.heading_path != "（无标题）":
            assert c.text.startswith("【")


def test_normalize_keeps_chinese_punctuation():
    """只折全角字母数字与空白；中文标点原样保留，引用原文才不会变形。"""
    assert normalize("第５条，应当：报销（含税）；金额≤200元　共 2 项") == \
        "第5条，应当：报销（含税）；金额≤200元 共 2 项"
    assert normalize("ＡＢＣ１２３") == "ABC123"


def test_toc_with_double_ellipsis_is_dropped():
    """中文排版常写"……"，不能只认英文 "..."。"""
    body, report = clean("第一章 总则 …… 1\n第二章 流程 …… 2\n正文", title="x")
    assert report.dropped_toc == 2
    assert "第一章" not in body


def test_value_lines_are_not_mistaken_for_headings():
    """制度里的"1.5 倍""3.5 天"是正文数值，误判成标题会打断整棵层级树。"""
    for text in ("1.5 倍报销", "3.5 天年假", "2.5 小时通勤", "3.2 元每公里"):
        assert _parse_heading(text) is None, text
    assert _parse_heading("1.1 总则") is not None
    assert _parse_heading("2、审批流程") is not None


def test_merge_keeps_later_clause_number_in_text():
    """同章短块合并后，被并入条款的编号要留在正文里，否则条款级引用会串号。"""
    body, _ = clean(DOC, title="测试管理办法")
    chunks = chunk_document("测试管理办法", body, max_chars=600, overlap=0)
    assert any("第2条" in c.text for c in chunks), "被合并的条款号丢了"


def test_chunk_uid_is_stable_and_content_sensitive():
    text = "第一章 总则\n第1条 甲规定。\n第2条 乙规定。"
    first = chunk_document("d1", text, max_chars=600)
    same = chunk_document("d1", text, max_chars=600)
    changed = chunk_document("d1", text.replace("甲规定", "甲修改"), max_chars=600)
    assert [c.uid for c in first] == [c.uid for c in same]
    assert first[0].uid != changed[0].uid
    assert len({c.uid for c in first}) == len(first)

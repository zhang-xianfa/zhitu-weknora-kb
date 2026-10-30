from pipeline.chunker import chunk_document
from pipeline.cleaner import clean

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

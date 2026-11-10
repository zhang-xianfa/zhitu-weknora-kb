from pipeline.security import (
    DocumentPolicy, UNAUTHORIZED_REFUSAL, Viewer, audit_record, check_upload_level,
    extract_level, filter_by_source, filter_chunks, level_rank, main, normalize_level,
    parse_departments, policies_from_manifest, policies_manifest, policy_from_metadata,
    source_key,
)


def test_level_rank_follows_declared_order():
    assert level_rank("公开") < level_rank("内部") < level_rank("秘密") < level_rank("机密") < level_rank("绝密")


def test_normalize_level_accepts_common_aliases():
    assert normalize_level("内部资料") == "内部"
    assert normalize_level("机密（仅限董事会）") == "机密"
    assert normalize_level("secret") == "机密"
    assert normalize_level("") == "内部"
    assert normalize_level("看不懂的写法") == "内部"


def test_extract_level_prefers_metadata_then_header_mark():
    assert extract_level({"密级": "机密"}, "【绝密】正文") == "机密"
    assert extract_level({}, "【机密】薪酬制度\n正文") == "机密"
    # 裸词"秘密"不算密级标识，避免"保守商业秘密"这类正文误判
    assert extract_level({}, "员工应当保守商业秘密") == "内部"


def test_clearance_blocks_higher_level():
    viewer = Viewer("张三", "内部")
    assert DocumentPolicy("d1", "内部").allows(viewer)
    assert not DocumentPolicy("d2", "机密").allows(viewer)
    assert DocumentPolicy("d2", "机密").allows(Viewer("李四", "机密"))


def test_department_scope_requires_membership():
    policy = DocumentPolicy("d1", "内部", ("人力资源部",))
    assert policy.allows(Viewer("a", "内部", ("人力资源部",)))
    assert not policy.allows(Viewer("b", "内部", ("财务部",)))
    # 没传部门的人一律看不到：宁可少答，不靠猜
    assert not policy.allows(Viewer("c", "内部"))


def test_parse_departments_splits_common_separators():
    assert parse_departments("人力资源部、财务部") == ("人力资源部", "财务部")
    assert parse_departments("") == ()


def test_policy_from_metadata_reads_level_and_scope():
    policy = policy_from_metadata("薪酬制度", {"密级": "秘密", "适用范围": "人力资源部"})
    assert policy.level == "秘密"
    assert policy.departments == ("人力资源部",)


def test_upload_gate_rejects_doc_above_kb_level():
    ok, why = check_upload_level("机密", "内部")
    assert not ok and "专用知识库" in why
    assert check_upload_level("内部", "秘密") == (True, "")


def test_filter_chunks_keeps_and_blocks():
    chunks = [
        {"doc_id": "公开制度", "clearance": "公开", "text": "a"},
        {"doc_id": "薪酬制度", "clearance": "机密", "text": "b"},
    ]
    kept, blocked = filter_chunks(chunks, Viewer("员工", "内部"))
    assert [c["doc_id"] for c in kept] == ["公开制度"]
    assert [c["doc_id"] for c in blocked] == ["薪酬制度"]


def test_source_key_normalizes_citation_titles():
    assert source_key("企业差旅与费用管理办法#003.md") == "企业差旅与费用管理办法"
    assert source_key("制度/企业差旅与费用管理办法.pdf") == "企业差旅与费用管理办法"
    assert source_key("企业差旅与费用管理办法") == "企业差旅与费用管理办法"


def test_policies_manifest_roundtrip():
    policies = {"差旅办法": DocumentPolicy("差旅办法", "内部", ("人力资源部",))}
    manifest = policies_manifest(policies, {"kb_id": "kb1"})
    assert manifest["kb_id"] == "kb1"
    back = policies_from_manifest(manifest)
    assert back["差旅办法"].level == "内部"
    assert back["差旅办法"].departments == ("人力资源部",)

    flat = policies_from_manifest({"差旅办法": {"clearance": "机密", "适用范围": "财务部"}})
    assert flat["差旅办法"].level == "机密"
    assert flat["差旅办法"].departments == ("财务部",)


def test_filter_by_source_blocks_unauthorized_and_unknown():
    chunks = [
        {"knowledge_title": "差旅办法#003.md", "content": "公开内容"},
        {"knowledge_title": "高管薪酬", "content": "机密内容"},
    ]
    policies = {
        "差旅办法": DocumentPolicy("差旅办法", "内部"),
        "高管薪酬": DocumentPolicy("高管薪酬", "机密"),
    }
    kept, blocked = filter_by_source(chunks, policies, Viewer("员工", "内部"))
    assert [c["content"] for c in kept] == ["公开内容"]
    assert [c["content"] for c in blocked] == ["机密内容"]

    # 查不到策略的块按"内部"处理：公开库访客看不到
    kept, blocked = filter_by_source([{"knowledge_title": "未登记文档"}], {}, Viewer("访客", "公开"))
    assert kept == [] and len(blocked) == 1


def test_audit_record_does_not_store_question_or_content():
    viewer = Viewer("员工", "内部")
    record = audit_record("张三的工伤怎么赔", viewer, [{"doc_id": "公开制度"}],
                          [{"doc_id": "薪酬制度", "text": "机密内容"}])
    assert record["viewer"] == "员工"
    assert record["blocked_docs"] == ["薪酬制度"]
    assert "question" not in record and "query" not in record
    assert "机密内容" not in str(record)
    assert record["decision"] == "served"      # 还有可见内容时仍然作答


def test_unauthorized_refusal_is_indistinguishable_from_no_answer():
    """越权不能有独立话术：否则用户能推断出"这条存在但你没权限"。"""
    assert UNAUTHORIZED_REFUSAL == "资料中没有找到相关内容。"
    assert "权限" not in UNAUTHORIZED_REFUSAL


def test_cli_reports_blocked_chunks_and_writes_audit(tmp_path, capsys):
    import json

    chunks = tmp_path / "chunks.jsonl"
    rows = [
        {"doc_id": "差旅办法", "clearance": "内部", "departments": [], "text": "a"},
        {"doc_id": "高管薪酬", "clearance": "机密", "departments": [], "text": "b"},
    ]
    chunks.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows),
                      encoding="utf-8")
    audit = tmp_path / "audit.jsonl"

    code = main(["--chunks", str(chunks), "--clearance", "内部", "--audit", str(audit)])
    out = capsys.readouterr().out
    assert code == 0
    assert "可见 1" in out and "拦截 1" in out
    assert "高管薪酬" in out
    record = json.loads(audit.read_text(encoding="utf-8").strip())
    assert record["blocked"] == 1 and record["blocked_docs"] == ["高管薪酬"]
    assert "text" not in record          # 审计不落正文


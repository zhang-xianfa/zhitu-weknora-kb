import pytest

from client.errors import CircuitOpenError, FatalError
from client.weknora import ClientConfig, WeKnoraClient
from tests.mock_server import FakeServer


def make_client(server: FakeServer, **overrides) -> WeKnoraClient:
    options = {"base_url": server.base_url, "max_retries": 3, "backoff_base": 0.01, "qps": 100}
    options.update(overrides)
    return WeKnoraClient(ClientConfig(**options))


def test_retries_on_5xx_then_succeeds():
    with FakeServer() as server:
        server.queue((500, {}, {"error": "boom"}), (503, {}, {"error": "busy"}),
                     (200, {}, {"data": "ok"}))
        client = make_client(server)
        result = client.request("GET", "/health")
        assert result == {"data": "ok"}
        assert client.last_retry_count == 2
        assert len(server.requests) == 3


def test_retries_on_429():
    with FakeServer() as server:
        server.queue((429, {"Retry-After": "0"}, {"error": "slow down"}),
                     (200, {}, {"ok": 1}))
        client = make_client(server)
        assert client.request("GET", "/health") == {"ok": 1}
        assert len(server.requests) == 2


def test_bad_request_fails_fast_without_retry():
    with FakeServer() as server:
        server.queue((400, {}, {"error": "bad param"}))
        client = make_client(server)
        with pytest.raises(FatalError) as exc:
            client.request("POST", "/knowledge-search", json_body={"query": ""})
        assert exc.value.status_code == 400
        assert len(server.requests) == 1        # 参数错误不重试


def test_circuit_breaker_opens_after_threshold():
    with FakeServer() as server:
        server.queue((500, {}, {}), (500, {}, {}))
        client = make_client(server, max_retries=0, breaker_threshold=2)
        for _ in range(2):
            with pytest.raises(Exception):
                client.request("GET", "/health")
        before = len(server.requests)
        with pytest.raises(CircuitOpenError):
            client.request("GET", "/health")
        assert len(server.requests) == before   # 熔断期间不打上游


def test_list_knowledge_bases_unwraps_data():
    with FakeServer() as server:
        server.queue((200, {}, {"data": [{"id": "kb1", "name": "制度库"}]}))
        client = make_client(server)
        assert client.list_knowledge_bases()[0]["id"] == "kb1"


def test_upload_if_changed_skips_same_content(tmp_path):
    doc = tmp_path / "制度.md"
    doc.write_text("第一章 总则", encoding="utf-8")
    state = tmp_path / "state.json"

    with FakeServer() as server:
        server.queue((200, {}, {"id": "k-1"}))
        client = make_client(server)
        client.state_file = state
        first = client.upload_if_changed("kb1", doc)
        second = client.upload_if_changed("kb1", doc)
        assert first["status"] == "uploaded"
        assert second["status"] == "skipped"
        assert len(server.requests) == 1        # 第二次没有再打上游

        doc.write_text("第一章 总则（修订）", encoding="utf-8")
        server.queue((200, {}, {"id": "k-2"}))
        assert client.upload_if_changed("kb1", doc)["status"] == "uploaded"

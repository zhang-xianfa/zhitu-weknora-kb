# -*- coding: utf-8 -*-
"""一个可控的假 WeKnora：指定每次请求返回什么状态码。

用它验证三件事：可重试错误会不会重试、不可重试错误会不会立刻失败、
连续失败会不会打开熔断——这些都没法靠真实服务复现。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _Handler(BaseHTTPRequestHandler):
    #: [(status, headers, body_dict), ...]，按请求顺序消费；用完后全返回 200
    responses: list = []
    requests: list = []
    #: 每次请求的原始 body：上传重试是否把完整文件重发，只能靠它验证
    bodies: list = []

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        type(self).requests.append((self.command, self.path))
        type(self).bodies.append(body)

        if type(self).responses:
            status, headers, payload = type(self).responses.pop(0)
        else:
            status, headers, payload = 200, {}, {"ok": True}

        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_GET = _handle
    do_POST = _handle

    def log_message(self, *args):  # 静音
        pass


class FakeServer:
    def __init__(self) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    def queue(self, *responses) -> None:
        _Handler.responses.extend(responses)
        _Handler.requests.clear()
        _Handler.bodies.clear()

    @property
    def requests(self) -> list:
        return _Handler.requests

    @property
    def bodies(self) -> list:
        return _Handler.bodies

    def __enter__(self) -> "FakeServer":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

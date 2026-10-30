# -*- coding: utf-8 -*-
"""WeKnora API 客户端：把"接口会不会挂"这件事挡在业务代码之外。

四道保护，按重要性排：

1. **超时**：连接 5s、读 30s。内网最常见的事故是"没设超时，一条请求挂住整个线程池"；
2. **重试 + 指数退避 + 抖动**：只重试超时/429/5xx，退避 0.5s→1s→2s 并加随机抖动，
   避免一批客户端同时重试把上游再打挂；
3. **限流**：令牌桶，默认 5 QPS，入库和评测都走同一个桶；
4. **熔断**：连续 5 次可重试失败后打开 30 秒，期间直接失败，给上游恢复时间。

再叠一层**幂等**：文件入库前用 sha256 内容指纹比对本地状态文件，
重跑流水线不会产生重复知识条目。
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

from .errors import CircuitOpenError, FatalError, RetryableError, WeKnoraError

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


@dataclass
class ClientConfig:
    base_url: str = "http://127.0.0.1:8080"
    api_key: str = ""
    connect_timeout: float = 5.0
    read_timeout: float = 30.0
    max_retries: int = 3
    backoff_base: float = 0.5
    qps: float = 5.0
    breaker_threshold: int = 5
    breaker_cooldown: float = 30.0

    @classmethod
    def from_env(cls) -> "ClientConfig":
        return cls(
            base_url=os.getenv("WEKNORA_BASE_URL", "http://127.0.0.1:8080"),
            api_key=os.getenv("WEKNORA_API_KEY", ""),
            qps=float(os.getenv("WEKNORA_QPS", "5")),
        )


class TokenBucket:
    """简单令牌桶：允许小突发，长期速率不超过 qps。"""

    def __init__(self, qps: float, capacity: float | None = None) -> None:
        self.qps = max(qps, 0.1)
        self.capacity = capacity or max(qps, 1.0)
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.qps)
                self._last = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                wait = (1 - self._tokens) / self.qps
            if time.monotonic() + wait > deadline:
                raise WeKnoraError("限流等待超时：请求速率超过 %.1f QPS" % self.qps)
            time.sleep(wait)


class CircuitBreaker:
    """连续失败 N 次打开，冷却期内直接失败。"""

    def __init__(self, threshold: int = 5, cooldown: float = 30.0) -> None:
        self.threshold = threshold
        self.cooldown = cooldown
        self._failures = 0
        self._opened_at = 0.0
        self._lock = threading.Lock()

    @property
    def is_open(self) -> bool:
        with self._lock:
            if self._opened_at == 0:
                return False
            if time.monotonic() - self._opened_at >= self.cooldown:
                self._opened_at = 0
                self._failures = 0
                return False
            return True

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = 0.0

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self.threshold:
                self._opened_at = time.monotonic()


class WeKnoraClient:
    def __init__(self, config: ClientConfig | None = None,
                 state_file: Path | None = None) -> None:
        self.config = config or ClientConfig.from_env()
        self.session = requests.Session()
        if self.config.api_key:
            self.session.headers["Authorization"] = f"Bearer {self.config.api_key}"
        self.session.headers["Content-Type"] = "application/json"
        self.bucket = TokenBucket(self.config.qps)
        self.breaker = CircuitBreaker(self.config.breaker_threshold, self.config.breaker_cooldown)
        self.state_file = state_file or Path(".weknora_state.json")
        #: 记录最近一次请求的重试次数，运维排查时能直接看出上游是否在抖
        self.last_retry_count = 0

    # ── 底层请求 ──────────────────────

    def request(self, method: str, path: str, *, json_body: dict | None = None,
                params: dict | None = None, files: dict | None = None,
                data: dict | None = None) -> dict:
        if self.breaker.is_open:
            raise CircuitOpenError("熔断中：上游连续失败，%.0f 秒后再试" % self.config.breaker_cooldown)

        url = self.config.base_url.rstrip("/") + path
        headers = None
        if files is not None:
            headers = {"Content-Type": None}  # 交给 requests 生成 multipart 边界

        self.last_retry_count = 0
        for attempt in range(self.config.max_retries + 1):
            self.bucket.acquire()
            try:
                resp = self.session.request(
                    method, url, json=json_body, params=params,
                    files=files, data=data, headers=headers,
                    timeout=(self.config.connect_timeout, self.config.read_timeout),
                )
            except (requests.Timeout, requests.ConnectionError) as e:
                self._on_retryable(e, attempt)
                continue

            if resp.status_code in _RETRYABLE_STATUS:
                # 429 优先按 Retry-After 等，没有就用退避
                self._on_retryable(
                    f"HTTP {resp.status_code}: {resp.text[:200]}", attempt,
                    retry_after=_retry_after(resp),
                )
                continue
            if resp.status_code >= 400:
                self.breaker.record_success()   # 参数错误不是上游不健康
                raise FatalError(
                    f"{method} {path} 失败：HTTP {resp.status_code} {resp.text[:300]}",
                    status_code=resp.status_code,
                )

            self.breaker.record_success()
            if not resp.content:
                return {}
            try:
                return resp.json()
            except ValueError:
                return {"raw": resp.text}

        raise RetryableError(f"{method} {path} 重试 {self.config.max_retries} 次仍失败")

    def _on_retryable(self, reason, attempt: int, retry_after: float | None = None) -> None:
        self.breaker.record_failure()
        if self.breaker.is_open:
            raise CircuitOpenError(f"熔断打开：{reason}")
        if attempt >= self.config.max_retries:
            raise RetryableError(f"重试耗尽：{reason}")
        delay = retry_after if retry_after else self.config.backoff_base * (2 ** attempt)
        delay += random.uniform(0, delay * 0.3)      # 抖动，避免重试风暴
        self.last_retry_count = attempt + 1
        time.sleep(delay)

    # ── 业务接口 ──────────────────────

    def health(self) -> dict:
        return self.request("GET", "/health")

    def list_knowledge_bases(self) -> list[dict]:
        data = self.request("GET", "/knowledge-bases")
        return _as_list(data)

    def create_knowledge_base(self, name: str, description: str = "") -> dict:
        return self.request("POST", "/knowledge-bases",
                            json_body={"name": name, "description": description})

    def list_knowledge(self, kb_id: str) -> list[dict]:
        data = self.request("GET", f"/knowledge-bases/{kb_id}/knowledge")
        return _as_list(data)

    def upload_file(self, kb_id: str, path: Path) -> dict:
        """上传一个文档到知识库（multipart）。"""
        with open(path, "rb") as f:
            return self.request(
                "POST", f"/knowledge-bases/{kb_id}/knowledge/file",
                files={"file": (path.name, f)},
                data={"title": path.stem},
            )

    def search(self, query: str, kb_ids: list[str], match_count: int = 5) -> dict:
        return self.request("POST", "/knowledge-search", json_body={
            "query": query,
            "knowledge_base_ids": kb_ids,
            "match_count": match_count,
        })

    def ask(self, session_id: str, query: str) -> dict:
        """带 LLM 总结的问答（需要已建会话）。"""
        return self.request("POST", f"/knowledge-chat/{session_id}", json_body={"query": query})

    # ── 幂等入库 ──────────────────────

    def upload_if_changed(self, kb_id: str, path: Path) -> dict:
        """内容没变就跳过。返回 {"status": "uploaded"/"skipped", ...}。"""
        digest = _sha256(path)
        state = self._load_state()
        key = f"{kb_id}:{path.resolve()}"
        if state.get(key, {}).get("sha256") == digest:
            return {"status": "skipped", "reason": "内容未变化", "sha256": digest}
        resp = self.upload_file(kb_id, path)
        state[key] = {"sha256": digest, "uploaded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                      "knowledge_id": _find_knowledge_id(resp)}
        self._save_state(state)
        return {"status": "uploaded", "response": resp, "sha256": digest}

    def _load_state(self) -> dict:
        if self.state_file.exists():
            try:
                return json.loads(self.state_file.read_text(encoding="utf-8"))
            except ValueError:
                return {}
        return {}

    def _save_state(self, state: dict) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                                   encoding="utf-8")


# ── 工具函数 ──────────────────────────

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _retry_after(resp: requests.Response) -> float | None:
    value = resp.headers.get("Retry-After")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _as_list(data) -> list[dict]:
    """WeKnora 的列表接口偶尔直接返回数组，偶尔包一层 data。"""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("data", "items", "list", "knowledge_bases"):
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []


def _find_knowledge_id(resp: dict) -> str:
    if not isinstance(resp, dict):
        return ""
    for key in ("id", "knowledge_id"):
        if resp.get(key):
            return str(resp[key])
    data = resp.get("data")
    if isinstance(data, dict):
        return _find_knowledge_id(data)
    return ""

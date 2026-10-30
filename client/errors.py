# -*- coding: utf-8 -*-
"""错误分类：决定"重不重试"的只有这一个判断。

企业内网里失败的原因高度集中——超时、429 限流、502/503 网关、偶发 500。
把它们和"参数写错（400）""没权限（401/403）""资源不存在（404）"分开，
前者重试，后者立刻失败并给出人话，避免把参数错误重试三遍浪费时间。
"""

from __future__ import annotations


class WeKnoraError(Exception):
    """所有客户端错误的基类。"""


class RetryableError(WeKnoraError):
    """可以重试：超时、连接失败、429、5xx。"""


class FatalError(WeKnoraError):
    """重试也没用：400/401/403/404 等。"""

    def __init__(self, message: str, status_code: int = 0) -> None:
        super().__init__(message)
        self.status_code = status_code


class CircuitOpenError(WeKnoraError):
    """熔断器打开期间直接失败，不再打上游。"""

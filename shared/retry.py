"""HTTP 重试辅助。"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeVar

import httpx

from shared.logger import get_logger

T = TypeVar("T")
logger = get_logger("daily_report.retry")


class RetryableHTTPError(Exception):
    """可重试的 HTTP 错误（5xx / 429）。"""


def with_retries(
    fn: Callable[[], T],
    *,
    max_attempts: int = 3,
    delay_seconds: float = 5.0,
    retry_on: tuple[type[BaseException], ...] = (
        httpx.TimeoutException,
        httpx.TransportError,
        RetryableHTTPError,
    ),
) -> T:
    last_exc: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            result = fn()
            # 若返回 Response，对 5xx/429 转为可重试异常
            if isinstance(result, httpx.Response):
                if result.status_code == 429 or result.status_code >= 500:
                    raise RetryableHTTPError(
                        f"HTTP {result.status_code}: {result.text[:200]}"
                    )
            return result
        except retry_on as exc:
            last_exc = exc
            logger.error(f"请求失败，第 {attempt}/{max_attempts} 次: {exc}")
            if attempt < max_attempts:
                time.sleep(delay_seconds)
        except httpx.HTTPStatusError as exc:
            # raise_for_status 路径：5xx/429 可重试
            code = exc.response.status_code if exc.response is not None else 0
            if code == 429 or code >= 500:
                last_exc = exc
                logger.error(f"请求失败，第 {attempt}/{max_attempts} 次: {exc}")
                if attempt < max_attempts:
                    time.sleep(delay_seconds)
                    continue
            raise
    assert last_exc is not None
    raise last_exc

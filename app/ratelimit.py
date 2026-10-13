"""Ограничитель частоты запросов к API Яндекс Диска (Token Bucket)."""

import asyncio
import threading
import time


class RateLimiter:
    def __init__(self, qps: int = 4):
        self.qps = max(1, int(qps))
        self.capacity = float(self.qps)
        self.tokens = float(self.qps)
        self.updated_at = time.monotonic()
        self._async_lock = asyncio.Lock()
        self._sync_lock = threading.Lock()

    async def acquire(self, timeout: float = 120.0) -> None:
        deadline = time.monotonic() + timeout
        while True:
            async with self._async_lock:
                now = time.monotonic()
                elapsed = now - self.updated_at
                self.tokens = min(self.capacity, self.tokens + elapsed * self.qps)
                self.updated_at = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                wait_time = (1.0 - self.tokens) / self.qps

            if time.monotonic() + wait_time > deadline:
                raise TimeoutError("Не удалось пройти rate limit за отведённое время")
            await asyncio.sleep(max(0.005, wait_time))

    def acquire_sync(self, timeout: float = 120.0) -> None:
        deadline = time.monotonic() + timeout
        while True:
            with self._sync_lock:
                now = time.monotonic()
                elapsed = now - self.updated_at
                self.tokens = min(self.capacity, self.tokens + elapsed * self.qps)
                self.updated_at = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                wait_time = (1.0 - self.tokens) / self.qps

            if time.monotonic() + wait_time > deadline:
                raise TimeoutError("Не удалось пройти rate limit за отведённое время")
            time.sleep(max(0.005, wait_time))

import asyncio
import time
import pytest
from app.ratelimit import RateLimiter


@pytest.mark.asyncio
async def test_allows_up_to_qps_in_one_window():
    rl = RateLimiter(qps=10)
    t0 = time.monotonic()
    for _ in range(10):
        await rl.acquire(timeout=5)
    t1 = time.monotonic()
    # Первые 10 запросов берутся мгновенно из начальной емкости корзины
    assert t1 - t0 < 0.2


@pytest.mark.asyncio
async def test_throttles_over_qps():
    rl = RateLimiter(qps=4)
    # Исчерпываем начальные токены
    for _ in range(4):
        await rl.acquire(timeout=5)

    # Следующий запрос должен подождать генерации токена (не менее ~0.2с)
    t0 = time.monotonic()
    await rl.acquire(timeout=5)
    t1 = time.monotonic()
    assert t1 - t0 >= 0.15


@pytest.mark.asyncio
async def test_timeout():
    rl = RateLimiter(qps=1)
    await rl.acquire(timeout=5)
    # Токенов больше нет, таймаут 0.05с при скорости 1 токен/сек
    with pytest.raises(TimeoutError):
        await rl.acquire(timeout=0.05)

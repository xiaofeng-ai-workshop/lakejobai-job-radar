# -*- coding: utf-8 -*-
"""core.scrape.BrowserGate 单测（P2 浏览器并发锁 + 频率控制）。

不依赖真实浏览器 / DB：min-interval 与 daily-limit 通过 monkeypatch 控成确定值，
验证「互斥 + 节流 + 每日上限 + 重置」四条核心契约。
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import core.scrape as scrape  # noqa: E402
from core.scrape import (  # noqa: E402
    BrowserBusy,
    BrowserDailyLimit,
    browser_busy,
    browser_session,
    reset_gate,
)


def test_idle_not_busy():
    reset_gate()
    assert browser_busy() is False


def test_enter_sets_busy_and_releases():
    reset_gate()

    async def run():
        async with browser_session("t"):
            assert browser_busy() is True
        assert browser_busy() is False

    asyncio.run(run())


def test_concurrent_session_raises_busy():
    reset_gate()

    async def run():
        async with browser_session("a"):
            # 锁持有期间再次进入 → 应被拒绝（不排队、不阻塞）
            with pytest.raises(BrowserBusy):
                async with browser_session("b"):
                    pass
        assert browser_busy() is False

    asyncio.run(run())


def test_min_interval_enforced(monkeypatch):
    reset_gate()
    monkeypatch.setattr(scrape, "_min_interval", lambda: 0.05)
    monkeypatch.setattr(scrape, "_daily_limit", lambda: 100)
    import time

    async def run():
        t0 = time.time()
        async with browser_session("x"):
            pass
        async with browser_session("y"):
            pass
        return time.time() - t0

    dt = asyncio.run(run())
    # 第二次进入应至少等待 min_interval(0.05s)，证明节流生效
    assert dt >= 0.04, f"两次操作间隔过短({dt:.3f}s)，节流未生效"


def test_daily_limit(monkeypatch):
    reset_gate()
    monkeypatch.setattr(scrape, "_min_interval", lambda: 0.0)
    monkeypatch.setattr(scrape, "_daily_limit", lambda: 3)

    async def run():
        for _ in range(3):
            async with browser_session("t"):
                pass
        with pytest.raises(BrowserDailyLimit):
            async with browser_session("t"):
                pass

    asyncio.run(run())


def test_reset_gate_clears_daily(monkeypatch):
    reset_gate()
    monkeypatch.setattr(scrape, "_min_interval", lambda: 0.0)
    monkeypatch.setattr(scrape, "_daily_limit", lambda: 2)

    async def run():
        for _ in range(2):
            async with browser_session("t"):
                pass
        with pytest.raises(BrowserDailyLimit):
            async with browser_session("t"):
                pass
        reset_gate()
        # 重置后当日计数清零，应可再次进入
        async with browser_session("t"):
            pass

    asyncio.run(run())

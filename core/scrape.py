# -*- coding: utf-8 -*-
"""浏览器操作闸门（BrowserGate）：进程内锁 + 频率控制。

P2 重组核心组件。背景与职责：
- 浏览器（Firefox profile）是**单例互斥**资源，归 Web 控制台（boss_app 的
  `automation` 实例）独占。Web 手动操作与 CLI / 脚本经 API 提交的操作，
  若并发驱动同一 profile，会造成点击错位、状态污染。
- 重组后脚本不再自开浏览器，所有浏览器操作统一经 Web API。多个调用面可能
  同时提交任务，必须串行化 + 节流，否则被 Boss 风控识别为异常 → 封号风险放大。

本模块是「浏览器操作的唯一受控入口」：
- 提供 `browser_session(reason)` 异步上下文管理器，进入即抢占闸门，退出释放；
- 进入时若闸门被占用 → 抛 `BrowserBusy`（调用方返回 409 busy）；
- 进入时若达每日上限 → 抛 `BrowserDailyLimit`（调用方返回 429）；
- 进入时 enforce 最小操作间隔（下限；具体随机间隔仍由采集循环里的 pause() 控制）。

**不持有浏览器实例**——实例由 web 适配器的 `automation` 单例持有。
**不依赖任何适配器**（铁律①）：仅读 `boss_state` 的 settings 表拿配置。
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

# boss_state 仅依赖标准库（sqlite3），可安全在 core 层导入
sys.path.insert(0, str(Path(__file__).parent.parent))
from boss_state import get_setting  # noqa: E402

_lock: asyncio.Lock | None = None
_last_op_ts: float = 0.0
_daily_count_date: str = ""
_daily_count: int = 0


class BrowserBusy(Exception):
    """浏览器正被其他操作占用，本次提交被拒绝。"""


class BrowserDailyLimit(Exception):
    """今日浏览器操作次数已达上限。"""


def _get_lock() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


def browser_busy() -> bool:
    """当前是否有操作正在占用浏览器（供 CLI / 前端轮询判断是否可提交）。"""
    return _get_lock().locked()


def _min_interval() -> float:
    """两次浏览器操作之间的最小间隔（秒），读 settings 表。"""
    try:
        return max(0.0, float(get_setting("browser_min_interval", "2.0")))
    except Exception:
        return 2.0


def _daily_limit() -> int:
    """每日浏览器操作次数上限，读 settings 表。"""
    try:
        return max(0, int(get_setting("browser_daily_limit", "200")))
    except Exception:
        return 200


def _check_daily_reset() -> None:
    global _daily_count_date, _daily_count
    today = time.strftime("%Y-%m-%d")
    if _daily_count_date != today:
        _daily_count_date = today
        _daily_count = 0


def reset_gate() -> None:
    """测试 / 进程重启时重置频率状态（不影响锁对象本身）。"""
    global _last_op_ts, _daily_count_date, _daily_count
    _last_op_ts = 0.0
    _daily_count_date = ""
    _daily_count = 0


class _BrowserSession:
    """`browser_session` 的异步上下文管理器实现。"""

    def __init__(self, reason: str):
        self.reason = reason
        self._lock = _get_lock()

    async def __aenter__(self) -> "_BrowserSession":
        global _last_op_ts, _daily_count
        # 闸门被占用 → 直接拒绝（不排队、不阻塞等待）
        if self._lock.locked():
            raise BrowserBusy(self.reason)
        await self._lock.acquire()
        try:
            # 最小间隔节流：距上次操作不足间隔则等待补足
            now = time.time()
            wait = _min_interval() - (now - _last_op_ts)
            if wait > 0:
                await asyncio.sleep(wait)
            # 每日上限
            _check_daily_reset()
            if _daily_count >= _daily_limit():
                raise BrowserDailyLimit(self.reason)
            _last_op_ts = time.time()
            _daily_count += 1
            return self
        except Exception:
            # 获取阶段异常（含每日上限）→ 释放已占用的锁，向上抛
            self._lock.release()
            raise

    async def __aexit__(self, *exc) -> bool:
        self._lock.release()
        return False


def browser_session(reason: str = ""):
    """异步上下文管理器：进入即抢占浏览器闸门，退出释放。

    进入时若浏览器正忙抛出 `BrowserBusy`；若达每日上限抛出 `BrowserDailyLimit`。
    调用方通常这样用：

        try:
            async with browser_session("search"):
                jobs = await _run_pw(automation.search, kw, city)
        except BrowserBusy:
            raise HTTPException(status_code=409, detail="浏览器正忙，请稍后重试")
        except BrowserDailyLimit:
            raise HTTPException(status_code=429, detail="今日浏览器操作已达上限")
    """
    return _BrowserSession(reason)

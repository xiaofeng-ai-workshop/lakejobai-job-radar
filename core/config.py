"""core/config.py — 配置统一起步（单一真相源之配置侧）。

原则（见 docs/架构重组方案.md ADR-0002 配置统一）：
- 运行时以 `settings` 表为权威；env 仅作为启动注入 / 回退。
- 同一配置项绝不允许 CLI 读 env、Web 读表导致两边值不同。
- 本模块是纯函数 + 懒导入，可被任意调用面安全 import，也便于单测。

后续 P1 把数据层抽到 core/store.py 后，_read_setting 的导入目标随之调整，
本文件对外接口保持不变。
"""

from __future__ import annotations

import os
from typing import Optional

DEFAULT_BASE_URL = "http://127.0.0.1:8010"
BASE_URL_ENV = "LAKEJOB_API"


def resolve_base_url(
    env_value: Optional[str] = None,
    settings_value: Optional[str] = None,
    default: str = DEFAULT_BASE_URL,
) -> str:
    """解析 API 基址的优先级：env(启动注入) > settings 表(运行时权威) > 默认。

    这样本机地址可用 env 在启动时注入，而运行期若改地址应写入 settings 表，
    避免「CLI 读 env、Web 读表」两边不一致。
    """
    if env_value:
        return env_value
    if settings_value:
        return settings_value
    return default


def _read_setting(key: str) -> str:
    """懒读取 settings 表（不破坏 core 层无顶层依赖的约束）。"""
    try:
        from boss_state import get_setting  # 当前数据层；P1 后改为 core.store
        return get_setting(key, "")
    except Exception:
        return ""


def get_runtime_base_url() -> str:
    """运行时 API 基址：env 优先，其次 settings 表，最后默认。"""
    return resolve_base_url(os.getenv(BASE_URL_ENV), _read_setting("api_base_url") or None)


def get_config(key: str, default: str = "") -> str:
    """运行时配置读取：以 settings 表为权威。"""
    val = _read_setting(key)
    return val if val != "" else default

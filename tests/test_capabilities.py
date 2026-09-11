"""P0 验证：核心注册表与配置单测。

验证目标（对应 docs/架构重组方案.md §8 + 任务表 P0）：
- capabilities 可导入、14 条能力可解析、CLI schema 可由其生成、能力矩阵可派生；
- config 配置解析优先级正确（env > settings > default）。
"""

import json
import sys
from pathlib import Path

import pytest

# 让测试能 import 项目根的 core 包
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core import capabilities as cap  # noqa: E402
from core import config as cfg  # noqa: E402


# ── 能力注册表 ──
def test_import_ok():
    assert cap is not None


def test_capability_count_is_14():
    caps = cap.list_capabilities()
    assert len(caps) == 14


def test_capability_keys_unique():
    keys = [c.key for c in cap.list_capabilities()]
    assert len(keys) == len(set(keys))


def test_dead_link_capabilities_flagged():
    dead = [c.key for c in cap.list_capabilities() if c.status == "dead_link"]
    assert set(dead) == {"smart_send", "company_preview"}


def test_capability_matrix_derivable():
    matrix = cap.generate_capability_matrix()
    assert len(matrix) == 14
    assert all({"domain", "key", "title", "surfaces", "requires_browser"} <= set(row) for row in matrix)


# ── 命令注册表 / schema 生成 ──
def test_command_count_is_23():
    cmds = cap.list_commands(include_dead=True)
    # P2 后：原 20 + collect + batch market-report + batch match-report = 23
    assert len(cmds) == 23


def test_dead_commands_excluded_when_requested():
    active = cap.list_commands(include_dead=False)
    # 23 总数含 1 个 dead_link(smart-send)，活跃 = 22
    assert len(active) == 22
    assert all(c.status != "dead_link" for c in active)


def test_schema_generation_valid_json():
    schema = cap.generate_schema_dict(include_dead=True)
    # 可被 JSON 序列化（无不可序列化对象）
    text = json.dumps(schema, ensure_ascii=False)
    parsed = json.loads(text)
    assert parsed["name"] == "lakejob"
    assert parsed["version"] == cap.SCHEMA_VERSION
    assert len(parsed["commands"]) == 23
    # 每个命令有 name + description
    assert all("name" in c and "description" in c for c in parsed["commands"])


def test_schema_excludes_dead_when_requested():
    schema = cap.generate_schema_dict(include_dead=False)
    names = [c["name"] for c in schema["commands"]]
    assert "smart-send" not in names


def test_schema_command_maps_to_known_capability():
    schema = cap.generate_schema_dict(include_dead=True)
    valid_keys = {c.key for c in cap.list_capabilities()} | {""}
    for cmd in schema["commands"]:
        assert cmd["capability_key"] in valid_keys


# ── 配置统一 ──
def test_resolve_base_url_priority_env_over_settings():
    assert cfg.resolve_base_url("http://env", "http://settings") == "http://env"


def test_resolve_base_url_settings_over_default():
    assert cfg.resolve_base_url(None, "http://settings") == "http://settings"


def test_resolve_base_url_default_when_empty():
    assert cfg.resolve_base_url("", "") == cfg.DEFAULT_BASE_URL
    assert cfg.resolve_base_url(None, None) == cfg.DEFAULT_BASE_URL

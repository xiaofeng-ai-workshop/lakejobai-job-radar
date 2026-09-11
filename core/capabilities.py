"""core/capabilities.py — 能力注册表（唯一真相源）。

本模块是 lakejob 全部「能力」与「CLI 命令」的单一来源。
Web / CLI / 文档（README / SKILL / schema）都由它派生，禁止在别处手搓，
否则会出现 README / SKILL / schema / 代码 四处命令数与描述对不上的漂移。

设计约束（见 docs/架构重组方案.md 铁律③）：
- 不 import 任何适配器（web / cli / script），core 层零调用面耦合。
- 纯标准库，可被任意调用面安全 import。
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

# P4 起改为从 pyproject.toml 读取，根治版本号漂移。
SCHEMA_VERSION = "0.1.0"

DEFAULT_SERVER = "http://127.0.0.1:8010"


# ──────────────────────────────────────────────
# 能力（按域分组，对应 §3.1 能力矩阵）
# ──────────────────────────────────────────────
@dataclass(frozen=True)
class Capability:
    domain: str
    key: str
    title: str
    surfaces: List[str]        # 暴露面：web / cli / independent
    requires_browser: bool
    status: str = "active"      # active | dead_link


# 顺序即 §3.1 能力矩阵顺序
CAPABILITIES: List[Capability] = [
    Capability("采集", "collect", "采集岗位", ["web", "cli"], True),
    Capability("分析", "match_analyze", "单 JD 匹配分析", ["web", "cli"], False),
    Capability("分析", "market_report", "市场报告(md)", ["cli"], False),
    Capability("分析", "resume_optimize", "简历优化建议", ["web", "cli"], False),
    Capability("分析", "chat_suggestion", "沟通话术建议", ["web", "cli"], False),
    Capability("行动", "auto_apply", "自动/批量投递", ["web", "cli"], True),
    Capability("行动", "chat_auto_reply", "AI 自动回复+聊天监控", ["web", "cli"], True),
    # 死链：辅助函数有、路由没挂，调过去 404。P3 下架（决策⑤ = A）。
    Capability("行动", "smart_send", "智能投递", [], True, "dead_link"),
    Capability("数据", "shortlist", "候选池管理", ["web", "cli"], False),
    Capability("数据", "company_query", "公司/已投递查询", ["web", "cli"], False),
    Capability("数据", "company_preview", "公司画像预览", [], False, "dead_link"),
    Capability("配置", "settings", "设置(CORS/Key/城市)", ["web"], False),
    Capability("系统", "system", "启停/心跳/重登", ["web", "cli"], True),
    Capability("独立", "interview", "面试刷题子服务", ["independent"], False),
]


# ──────────────────────────────────────────────
# 命令（CLI 命令级注册表，驱动 schema 生成）
# ──────────────────────────────────────────────
@dataclass
class Command:
    name: str
    description: str
    cli: str
    endpoint: str = ""
    params: Dict[str, Any] = field(default_factory=dict)
    capability_key: str = ""     # 关联到 Capability.key；"" 表示元命令（version/schema）
    status: str = "active"       # active | dead_link


# 真实 CLI 命令面（lakejob_cli/cli.py 的 click 命令），共 20 个。
# company-preview 在老 schema.json 里被误列为命令，但实际无对应 click 命令，
# 故不在此列出（它是 smart-send 内部调用的端点）。smart-send 标 dead_link。
COMMANDS: List[Command] = [
    Command("version", "查看版本号。", "lakejob version", capability_key=""),
    Command("schema", "输出工具描述（供 AI Agent 调用）。", "lakejob schema", capability_key=""),
    Command(
        "search", "搜索BOSS直聘岗位。返回岗位列表含薪资/公司/城市/经验要求等。",
        "lakejob search <keyword> --city <城市> [--welfare <福利>] [--count <数量>]",
        endpoint="POST /api/jobs/search",
        params={
            "keyword": {"type": "string", "required": True, "description": "搜索关键词如 AI Agent / Golang 等"},
            "city": {"type": "string", "required": False, "description": "城市名如 北京/广州/全国，不传则用 default_city 设置"},
            "welfare": {"type": "string", "required": False, "description": "福利筛选 逗号分隔 如 双休,五险一金"},
            "count": {"type": "integer", "required": False, "default": 60, "description": "返回数量上限"},
        },
        capability_key="collect",
    ),
    Command("status", "查看浏览器状态和今日统计。", "lakejob status", endpoint="GET /api/status", capability_key="system"),
    Command("stats", "投递转化漏斗统计：搜索→待投递→已投递→HR回复→面试。", "lakejob stats",
            endpoint="GET /api/stats", capability_key="auto_apply"),
    Command(
        "jobs", "列出本地数据库中的岗位。", "lakejob jobs [--status pending|applied|replied] [--limit 50]",
        endpoint="GET /api/jobs",
        params={
            "status": {"type": "string", "required": False, "enum": ["pending", "applied", "replied"], "description": "岗位状态筛选"},
            "limit": {"type": "integer", "required": False, "default": 50},
        },
        capability_key="collect",
    ),
    Command("apply", "投递单个岗位。", "lakejob apply <job_url>", endpoint="POST /api/jobs/apply",
            params={"job_url": {"type": "string", "required": True, "description": "岗位URL"}}, capability_key="auto_apply"),
    Command("apply-batch", "批量投递待投递岗位。", "lakejob apply-batch [--status pending]",
            endpoint="POST /api/jobs/apply-batch", capability_key="auto_apply"),
    Command("scan", "扫描当前BOSS搜索结果页，提取所有可见岗位保存到数据库。用于手动调筛后捕获过滤结果。",
            "lakejob scan", endpoint="POST /api/jobs/scan", capability_key="collect"),
    Command("scan-apply", "扫描当前页面全部岗位 → 一键批量投递。相当于 scan + apply-batch 合一步完成。",
            "lakejob scan-apply", endpoint="POST /api/jobs/scan-and-apply", capability_key="auto_apply"),
    Command("conversations", "列出所有HR会话。", "lakejob conversations", endpoint="GET /api/conversations",
            capability_key="chat_auto_reply"),
    Command("chat", "查看与某HR的聊天记录。", "lakejob chat <conv_id>", endpoint="GET /api/conversations/{conv_id}/messages",
            params={"conv_id": {"type": "integer", "required": True, "description": "会话ID"}}, capability_key="chat_auto_reply"),
    Command("send", "向HR手动发送消息。", "lakejob send <conv_id> --msg <消息内容>",
            endpoint="POST /api/conversations/{conv_id}/send",
            params={"conv_id": {"type": "integer", "required": True}, "msg": {"type": "string", "required": True, "description": "消息内容"}},
            capability_key="chat_auto_reply"),
    Command("doctor", "诊断环境：Python版本、浏览器状态、登录态、AI配置等。", "lakejob doctor",
            endpoint="GET /api/doctor", capability_key="system"),
    Command("login", "重新扫码登录BOSS直聘。", "lakejob login", endpoint="POST /api/system/relogin", capability_key="system"),
    Command("analyze", "AI 分析单个岗位与简历的匹配度。", "lakejob analyze <job_url> [--title <名称>] [--company <公司>] [--desc <JD>]",
            endpoint="POST /api/jobs/analyze",
            params={
                "job_url": {"type": "string", "required": True, "description": "岗位URL"},
                "title": {"type": "string", "required": False, "description": "岗位名称"},
                "company": {"type": "string", "required": False, "description": "公司名"},
                "desc": {"type": "string", "required": False, "description": "JD描述"},
            },
            capability_key="match_analyze"),
    Command("shortlist", "候选池管理：list / add / remove。",
            "lakejob shortlist <list|add|remove> [--job-url <URL>] [--title <名称>] [--company <公司>] [--id <ID>]",
            endpoint="GET /api/shortlists",
            params={
                "action": {"type": "string", "required": True, "enum": ["list", "add", "remove"]},
                "job_url": {"type": "string", "required": False, "description": "岗位URL"},
                "title": {"type": "string", "required": False, "description": "岗位名称"},
                "company": {"type": "string", "required": False, "description": "公司名"},
                "id": {"type": "integer", "required": False, "description": "shortlist ID"},
            },
            capability_key="shortlist"),
    Command("server", "管理后台服务（启动/停止/状态）。", "lakejob server [--start|--stop] [--port 8010]",
            params={"start": {"type": "boolean", "required": False}, "stop": {"type": "boolean", "required": False},
                    "port": {"type": "integer", "required": False, "default": 8010}}, capability_key="system"),
    Command("restart", "杀旧进程并重启后台服务。", "lakejob restart [--port 8010]",
            params={"port": {"type": "integer", "required": False, "default": 8010}}, capability_key="system"),
    # 死链：内部调 /api/companies/preview + /api/companies/smart-send，后端无路由 → 404。P3 下架。
    Command(
        "smart-send", "智能投递：搜索→按公司分组→挑最高HR→批量投递（死链，P3 下架）。",
        "lakejob smart-send --keyword <关键词> [--city <城市>] [--greeting <招呼语>] [--yes]",
        endpoint="POST /api/companies/smart-send",
        params={
            "keyword": {"type": "string", "required": False},
            "city": {"type": "string", "required": False},
            "greeting": {"type": "string", "required": False, "description": "自定义招呼语"},
            "yes": {"type": "boolean", "required": False, "default": False, "description": "跳过确认直接投递"},
        },
        capability_key="smart_send", status="dead_link",
    ),
]


# ──────────────────────────────────────────────
# 派生函数（供 Web / CLI / 文档 调用）
# ──────────────────────────────────────────────
def list_capabilities() -> List[Capability]:
    return list(CAPABILITIES)


def get_capability(key: str) -> Optional[Capability]:
    for c in CAPABILITIES:
        if c.key == key:
            return c
    return None


def list_commands(include_dead: bool = True) -> List[Command]:
    if include_dead:
        return list(COMMANDS)
    return [c for c in COMMANDS if c.status != "dead_link"]


def generate_schema_dict(include_dead: bool = True) -> Dict[str, Any]:
    """生成 CLI 工具 schema（供 `lakejob schema` 与 schema.json 使用）。

    include_dead=True（默认）保留死链命令，保证 P0 阶段实行为止与老 schema 兼容；
    P3 下架时改为 False 即可让死链从 CLI 消失。
    """
    commands = [asdict(c) for c in list_commands(include_dead)]
    return {
        "name": "lakejob",
        "version": SCHEMA_VERSION,
        "description": "BOSS直聘求职自动化工具 - 搜索/投递/AI聊天/微信交换",
        "server": DEFAULT_SERVER,
        "commands": commands,
        "output_format": {
            "type": "JSON envelope",
            "fields": {
                "ok": "boolean - 操作是否成功",
                "command": "string - 执行的命令名",
                "data": "object|array|null - 返回数据",
                "error": "string|null - 错误信息",
                "pagination": "object - 分页信息 (搜索等)",
                "hints": "object - 后续操作提示",
            },
        },
        "agent_instructions": (
            "当用户要求搜索职位、投递岗位、查看聊天等BOSS直聘操作时："
            "1. 先运行 lakejob status 检查状态；2. 如需登录运行 lakejob login；"
            "3. 根据意图调用对应命令；4. 解析返回JSON中的 ok 字段判断成败。"
            "推荐工作流：search → 用户手动在BOSS调筛 → scan → scan-apply（或 apply-batch）。"
        ),
    }


def generate_capability_matrix() -> List[Dict[str, Any]]:
    """生成 §3.1 能力矩阵行（供文档由注册表派生，不再手搓）。"""
    return [asdict(c) for c in CAPABILITIES]

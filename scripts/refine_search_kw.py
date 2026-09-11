#!/usr/bin/env python3
"""一次性脚本: 按 job_title 重写 search_kw, 修正 --backfill-kw 留下的错位。

用法:
    python scripts/refine_search_kw.py            # 预览(默认, 不改 DB)
    python scripts/refine_search_kw.py --apply    # 写库

优先级: FDE > AI测试 > AI项目经理 > 项目经理 > 留空
- FDE 词根最明确, 优先命中不被打到 PM 组
- AI 测试排除 "测试开发/开发测试"
- AI 项目经理: 含 AI 且含 PM 相关词根
- 纯项目经理: 只含 PM 词根, 无 AI
- 黑名单关键词(销售/投资/合伙人/制作/编导 等)→ 标 None, 不进任何报告
"""
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from boss_state import get_db

NOISE_WORDS = (
    "销售", "投资", "合伙人", "招商", "加盟", "猎头", "讲师",
    "主播", "带货", "制作师", "编导", "漫剧", "城市主管",
    "城市合伙人", "广告标识", "淘宝", "天使投资",
)
AI_TOKENS = ("AI", "人工智能", "智能体", "AIGC", "ai")
PM_WORDS = (
    "项目经理", "项目主管", "项目助理", "项目总负责", "项目总监",
    "项目管理", "PM", "pm", "PMO", "pmo",
    "产品经理", "售前", "总经理", "事业部",
    "交付", "商务", "渠道经理", "KA经理", "技术经理",
    "研发经理", "供应链经理", "制作经理", "总监", "主管", "经理",
)


def classify(title: str) -> str | None:
    t = (title or "").strip()
    if not t:
        return None
    # 1. FDE 最优先
    if "FDE" in t or "前沿部署" in t or "AI效能顾问" in t:
        return "FDE"
    # 2. AI 测试
    if "测试" in t and not re.search(r"测试开发|开发测试", t):
        return "AI测试"
    # 3. 黑名单 (销售/投资...不进 PM)
    if any(w in t for w in NOISE_WORDS):
        return None
    # 4. AI + PM 类
    if any(x in t for x in AI_TOKENS):
        if any(w in t for w in PM_WORDS):
            return "AI项目经理"
        return None
    # 5. 纯 PM
    if any(w in t for w in ("项目经理", "项目主管", "项目助理", "项目管理", "PM", "pm", "PMO", "pmo")):
        return "项目经理"
    return None


def main() -> None:
    apply = "--apply" in sys.argv
    db = get_db()
    rows = db.execute(
        "SELECT id, job_title, search_kw FROM applications ORDER BY id"
    ).fetchall()

    old_dist = Counter(r["search_kw"] or "" for r in rows)
    new_dist: Counter = Counter()
    changes: list[tuple[int, str, str, str | None]] = []  # id, title, old, new

    for r in rows:
        new_kw = classify(r["job_title"])
        new_dist[new_kw or ""] += 1
        if new_kw != r["search_kw"]:
            changes.append((r["id"], r["job_title"], r["search_kw"] or "", new_kw))

    print(f"=== {'即将写库' if apply else '预览(不写库), 加 --apply 落地'} ===\n")
    print(f"总 {len(rows)} 条, 拟改 {len(changes)} 条\n")

    print("--- 老 search_kw 分布 ---")
    for k, v in old_dist.most_common():
        print(f"  {k or '(空)':15s} {v}")
    print("\n--- 新 search_kw 分布 ---")
    for k, v in new_dist.most_common():
        print(f"  {k or '(未追溯)':15s} {v}")

    print(f"\n--- 改动明细 ({len(changes)} 条) ---")
    for rid, t, old, new in changes:
        mark = "→ 删" if new is None else f"→ {new}"
        print(f"  #{rid:3d} {old:12s} {mark:12s} {t}")

    if not apply:
        print("\n[预览模式] 未写库, 加 --apply 落地")
        return

    for rid, _t, _o, new in changes:
        db.execute("UPDATE applications SET search_kw=? WHERE id=?", (new or "", rid))
    db.commit()
    print(f"\n[OK] 已写库, 改动 {len(changes)} 条")


if __name__ == "__main__":
    main()

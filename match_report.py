# -*- coding: utf-8 -*-
"""
简历-JD 匹配报告生成器
====================

流程: 登录BOSS(复用 boss_firefox 登录态) -> 按关键词搜索 -> 逐个采集JD全文 -> 入库
      -> 与 resume_summary 比对打分 -> 按匹配度排序输出 Markdown 报告 + 关键词缺口建议

用法(在项目根目录):
  # 0. 首次: 扫码登录(只需一次)
  python boss_firefox.py --login

  # 1. (可选) 导入简历文本到设置页 resume_summary
  python match_report.py --resume-file 我的简历.txt

  # 2. 采集 + 分析 + 报告
  python match_report.py --keywords "AI Agent,Linux运维" --city 广州 --max-jobs 20

  # 只分析库中已有 JD, 不再采集
  python match_report.py --report-only

  # 只刷新关键词缺口(不调LLM, 零成本)
  python match_report.py --report-only --keyword-only

说明:
  - 有 DeepSeek API Key(设置页配置)时逐岗打分; 没有则自动降级为关键词覆盖率打分
  - LLM 结果缓存在 .boss_profile/match_cache.json, 简历变更后自动重新分析
"""

import argparse
import hashlib
import json
import random
import re
import sys
import time
from collections import Counter
from datetime import date
from pathlib import Path

# Windows 控制台中文输出
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "interview"))

from boss_firefox import BossScraper, parse_skills, pause  # noqa: E402
from boss_state import (  # noqa: E402
    get_setting,
    set_setting,
    get_db,
    add_application,
    get_application_by_url,
    update_application_from_job,
)

CACHE_FILE = ROOT / ".boss_profile" / "match_cache.json"


# ── 工具 ──────────────────────────────────────────────

def _norm_url(u: str) -> str:
    return (u or "").split("?")[0].rstrip("/")


def _cache_key(url: str, resume: str) -> str:
    return hashlib.sha1((url + "|" + resume[:500]).encode("utf-8")).hexdigest()


def _load_cache() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cache(cache: dict):
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")


def _resolve_city_code(name: str) -> str:
    """城市名 -> BOSS city code, 失败回退全国。"""
    name = (name or "").strip()
    if not name or name in ("全国", "all"):
        return "100010000"
    try:
        from boss_geo import resolve_city_code
        code = resolve_city_code(name)
        if code:
            return code
    except Exception:
        pass
    print(f"  [提示] 未识别城市「{name}」, 使用全国")
    return "100010000"


# ── 采集 ──────────────────────────────────────────────

def collect(keywords, cities, per_query, max_total, headless, refresh):
    """搜索 + 详情采集, 结果入库。每个 城市×关键词 组合各采 per_query 条。"""
    sc = BossScraper(headless=headless)
    sc.start()

    # 登录态校验：Cookie 在持久化 profile 中(Web控制台扫码 / --login 均可),
    # 不依赖 STATE_FILE; 实际打开页面验证
    try:
        sc.page.goto("https://www.zhipin.com/web/geek/job", wait_until="domcontentloaded", timeout=30000)
        pause(2, 3)
        if sc._login_prompt_visible():
            print("登录态无效或已过期。请先扫码登录:")
            print("  方式一: Web控制台 设置页 → 启动浏览器 → 扫码")
            print("  方式二: python boss_firefox.py --login")
            sys.exit(1)
        print("[OK] 登录态有效")
    except SystemExit:
        raise
    except Exception as e:
        print(f"登录检查失败: {e}")
        sys.exit(1)

    def _total_done():
        return bool(max_total) and n_saved >= max_total

    n_saved = 0
    try:
        seen = set()
        for city in cities:
            if _total_done():
                break
            city_code = _resolve_city_code(city)
            for kw in keywords:
                if _total_done():
                    break
                print(f"\n[搜索] {kw} @ {city or '全国'} (目标 {per_query} 条)")
                try:
                    jobs = sc.search(kw, city_code)
                except Exception as e:
                    print(f"  搜索失败: {e}")
                    continue
                combo = 0
                for j in jobs:
                    if combo >= per_query or _total_done():
                        break
                    url = _norm_url(j.get("url", ""))
                    key = j.get("title", "") + j.get("salary", "") + j.get("company", "")
                    if not url and key in seen:
                        continue
                    seen.add(key)
                    # 已入库且已有JD全文 → 跳过详情采集(省时间)
                    existing = get_application_by_url(url) if url else None
                    if existing and (existing.get("description") or "").strip() and not refresh:
                        continue
                    # 详情采集
                    if url:
                        try:
                            d = sc.fetch_detail(url)
                            if d.get("description"):
                                j["description"] = d["description"]
                            j["hr_name"] = d.get("hr_name", "")
                            j["hr_title"] = d.get("hr_title", "")
                        except Exception as e:
                            print(f"  详情失败: {e}")
                        pause(1.5, 3.0)
                    j["url"] = url
                    if existing:
                        update_application_from_job(existing["id"], j)
                    else:
                        add_application(j)
                    combo += 1
                    n_saved += 1
                    mark = "✅" if (j.get("description") or "").strip() else "⚠️无JD"
                    print(f"  {mark} [本组合 {combo}/{per_query} · 累计 {n_saved}] {j.get('title','')[:30]}")
                print(f"  [组合完成] {kw} @ {city}: 采集 {combo} 条")
        print(f"\n[采集完成] 新增/刷新 {n_saved} 条")
    finally:
        sc.close()
    return n_saved


# ── 分析 ──────────────────────────────────────────────

def _llm_available() -> bool:
    try:
        from llm_client import _load_ai_config
        cfg = _load_ai_config()
        return bool(cfg.get("api_key"))
    except Exception:
        return False


def _analyze_llm(job, resume, cache):
    """单岗 LLM 打分, 带缓存。失败返回 None。"""
    from llm_client import llm_chat_deepseek, parse_json_from_llm

    key = _cache_key(job["job_url"], resume)
    if key in cache:
        return cache[key]

    prompt = f"""你是求职辅导专家。分析以下岗位JD，对比求职者简历，输出JSON。

## 求职者简历
{resume[:2000]}

## 岗位信息
- 公司: {job.get('company','')}
- 职位: {job.get('job_title','')}
- 薪资: {job.get('salary','')}
- JD: {(job.get('description') or '')[:2000]}

## 输出格式（严格JSON，不要多余文字）
{{
  "match_score": 85,
  "decision": "建议投递",
  "key_skills": ["Python", "RAG"],
  "gap": "缺少K8s部署经验",
  "advice": "建议强调Agent开发经验",
  "summary": "一两句总结"
}}"""
    try:
        raw = llm_chat_deepseek(
            [{"role": "user", "content": prompt}],
            system_prompt="你是求职辅导专家，输出严格JSON。",
            temperature=0.2,
        )
        data = parse_json_from_llm(raw)
        if not data or "match_score" not in data:
            return None
        cache[key] = data
        return data
    except Exception as e:
        print(f"  LLM分析失败({job.get('job_title','')[:20]}): {e}")
        return None


def _keyword_score(job, resume):
    """无API Key时的降级打分: JD技能词 vs 简历文本覆盖率的粗估。"""
    text = (job.get("description") or "") + " " + (job.get("job_title") or "")
    skills = []
    for _, ss in parse_skills(text).items():
        skills.extend(ss)
    if not skills:
        return None
    r = (resume or "").lower()
    have = [s for s in skills if s.lower() in r]
    score = int(round(40 + 60.0 * len(set(have)) / len(set(skills))))
    return {
        "match_score": score,
        "decision": "建议投递" if score >= 75 else "可以尝试" if score >= 55 else "谨慎",
        "key_skills": sorted(set(skills), key=lambda x: -len(x))[:8],
        "gap": "、".join(sorted(set(skills) - set(have))[:6]),
        "advice": "(关键词模式, 建议配置AI Key获得更准的分析)",
        "summary": f"JD共识别{len(set(skills))}个技能词, 简历覆盖{len(set(have))}个",
    }


def _has_resume() -> bool:
    """resume_summary 是否为有效简历（排除短文本/空模板占位）。"""
    r = (get_setting("resume_summary") or "").strip()
    return len(r) >= 60


def _fetch_jobs_with_jd(limit):
    rows = get_db().execute(
        """SELECT * FROM applications
           WHERE description IS NOT NULL AND length(description) > 50
           ORDER BY id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def analyze_market(limit):
    """市场模式：不比对简历，统计 JD 中的技能词频（市场需求热词）。"""
    jobs = _fetch_jobs_with_jd(limit)
    if not jobs:
        print("库中没有含JD全文的岗位, 请先采集(--keywords ...)")
        sys.exit(1)
    freq = Counter()
    cats = {}  # 技能小写 -> 类别
    for job in jobs:
        text = (job.get("description") or "") + " " + (job.get("job_title") or "")
        found = set()
        for cat, ss in parse_skills(text).items():
            for s in ss:
                found.add(s.lower())
                cats.setdefault(s.lower(), cat)
        for s in found:
            freq[s] += 1
    print(f"[市场分析] {len(jobs)} 个岗位, 提取到 {len(freq)} 个技能词")
    return jobs, freq.most_common(), cats


def analyze_match(limit, keyword_only):
    """匹配模式：与 resume_summary 比对打分排序。"""
    resume = (get_setting("resume_summary") or "").strip()
    if not _has_resume():
        print("简历摘要无效(过短或为空模板)! 请用 --resume-file 导入, 或使用市场模式 --market")
        sys.exit(1)
    jobs = _fetch_jobs_with_jd(limit)
    if not jobs:
        print("库中没有含JD全文的岗位, 请先采集(--keywords ...) 或在Web控制台搜索扫描")
        sys.exit(1)

    use_llm = (not keyword_only) and _llm_available()
    mode = "LLM智能分析" if use_llm else "关键词覆盖分析(未配置AI Key或指定--keyword-only)"
    print(f"[分析] {len(jobs)} 个岗位 · 模式: {mode}")

    cache = _load_cache() if use_llm else {}
    results, failed = [], 0
    for i, job in enumerate(jobs, 1):
        r = _analyze_llm(job, resume, cache) if use_llm else None
        if r is None:
            r = _keyword_score(job, resume)
            if r is None:
                failed += 1
                continue
        results.append((job, r))
        print(f"  [{i}/{len(jobs)}] {r['match_score']}分 {job['job_title'][:25]}")
    if use_llm:
        _save_cache(cache)
    if failed:
        print(f"  [跳过] {failed} 个岗位无法解析")

    results.sort(key=lambda x: -int(x[1].get("match_score") or 0))
    return results, resume, mode


# ── 关键词缺口聚合 ────────────────────────────────────

def keyword_gap(results, resume):
    """统计所有JD中的技能词频次, 区分简历已覆盖/缺失。"""
    r = (resume or "").lower()
    have, miss = Counter(), Counter()
    for job, _ in results:
        text = (job.get("description") or "") + " " + (job.get("job_title") or "")
        found = set()
        for _, ss in parse_skills(text).items():
            for s in ss:
                found.add(s.lower())
        for s in found:
            (have if s in r else miss)[s] += 1
    return have, miss


# ── 报告 ──────────────────────────────────────────────

def build_report(results, resume, mode):
    today = date.today().isoformat()
    scores = [int(r.get("match_score") or 0) for _, r in results]
    lines = [f"# 简历-JD 匹配报告 · {today}", ""]
    lines.append(f"> 分析模式: **{mode}** · 岗位数: **{len(results)}** · 平均分: **{sum(scores)//max(len(scores),1)}**")
    lines.append(f"> 简历摘要(前100字): {resume[:100]}...")
    lines.append("")
    lines.append("## 一、匹配度排名(高→低)")
    lines.append("")
    lines.append("| # | 分数 | 结论 | 岗位 | 公司 | 薪资 | 主要差距 |")
    lines.append("|---|------|------|------|------|------|----------|")
    for i, (job, r) in enumerate(results, 1):
        gap = (r.get("gap") or "-").replace("|", "/")[:40]
        lines.append(
            f"| {i} | {r.get('match_score','-')} | {r.get('decision','')} "
            f"| {job['job_title'][:24]} | {(job.get('company') or '')[:14]} "
            f"| {job.get('salary') or ''} | {gap} |"
        )
    lines.append("")
    lines.append("## 二、岗位详情")
    lines.append("")
    for i, (job, r) in enumerate(results, 1):
        lines.append(f"### {i}. {job['job_title']} · {job.get('company') or ''} ({r.get('match_score','-')}分)")
        lines.append(f"- **结论**: {r.get('decision','')} — {r.get('summary','')}")
        if r.get("key_skills"):
            lines.append(f"- **关键技能**: {', '.join(r['key_skills'])}")
        if r.get("gap"):
            lines.append(f"- **差距**: {r['gap']}")
        if r.get("advice"):
            lines.append(f"- **建议**: {r['advice']}")
        if job.get("job_url"):
            lines.append(f"- 链接: {job['job_url']}")
        lines.append("")

    have, miss = keyword_gap(results, resume)
    lines.append("## 三、关键词缺口分析(全部JD统计)")
    lines.append("")
    if have:
        lines.append("**简历已覆盖**(招聘方要求且你已具备):")
        lines.append("")
        for s, n in have.most_common(15):
            lines.append(f"- {s} ({n}个岗位)")
        lines.append("")
    if miss:
        lines.append("**建议补充**(JD高频出现但简历未提及 —— 写进简历技能/项目经历可提升匹配度):")
        lines.append("")
        for s, n in miss.most_common(25):
            p = "🔴" if n >= 10 else "🟡" if n >= 5 else "🟢"
            lines.append(f"- {p} **{s}** ({n}个岗位)")
        lines.append("")
    lines.append("---")
    lines.append(f"*生成于 {today} · lakejobai-job-radar match_report.py · 前提: 简历内容真实, 不建议堆砌未掌握的关键词*")
    return "\n".join(lines), today


def build_market_report(jobs, freq, cats):
    """市场模式报告：JD 热词统计，不涉及简历。"""
    today = date.today().isoformat()
    total = len(jobs)
    lines = [f"# 市场热词报告 · {today}", ""]
    lines.append(f"> 样本: **{total} 个岗位**(JD全文) · 提取技能词 **{len(freq)}** 个 · 市场模式(未导入简历, 仅统计市场需求)")
    lines.append("")
    lines.append("## 一、热门技能词 TOP 30")
    lines.append("")
    lines.append("| # | 技能 | 出现岗位数 | 占比 | 类别 |")
    lines.append("|---|------|-----------|------|------|")
    for i, (s, n) in enumerate(freq[:30], 1):
        lines.append(f"| {i} | {s} | {n} | {n * 100 // total}% | {cats.get(s, '')} |")
    lines.append("")

    by_cat = {}
    for s, n in freq:
        if n * 100 // total >= 20:  # 只列出现于≥20%岗位的
            by_cat.setdefault(cats.get(s, "其他"), []).append((s, n))
    if by_cat:
        lines.append("## 二、分类视图(出现于≥20%岗位的技能)")
        lines.append("")
        for cat in sorted(by_cat, key=lambda c: -max(n for _, n in by_cat[c])):
            items = "、".join(f"**{s}**({n})" for s, n in sorted(by_cat[cat], key=lambda x: -x[1]))
            lines.append(f"- **{cat}**: {items}")
        lines.append("")

    lines.append("## 三、岗位样本")
    lines.append("")
    for i, j in enumerate(jobs[:40], 1):
        lines.append(f"{i}. {j['job_title']} · {j.get('company') or ''} · {j.get('salary') or ''}")
    lines.append("")
    lines.append("---")
    lines.append(f"*生成于 {today} · 市场模式 · 后续用 --resume-file 导入简历后再次运行, 即自动切换为逐岗匹配排序*")
    return "\n".join(lines), today


def main():
    ap = argparse.ArgumentParser(description="简历-JD匹配报告")
    ap.add_argument("--keywords", default="AI项目经理", help="搜索关键词, 逗号分隔可多个; 默认 AI项目经理")
    ap.add_argument("--city", default="武汉", help="城市名, 逗号分隔可多个, 如 \"武汉,深圳,广州\"; 默认武汉")
    ap.add_argument("--per-query", type=int, default=20, help="每个 城市×关键词 组合采集条数(默认20)")
    ap.add_argument("--max-total", type=int, default=50, help="全局采集上限(默认50, 0=不限)")
    ap.add_argument("--headless", action="store_true", help="无头模式运行浏览器")
    ap.add_argument("--refresh", action="store_true", help="强制重新采集已有岗位的JD")
    ap.add_argument("--report-only", action="store_true", help="跳过采集, 只分析库中已有数据")
    ap.add_argument("--limit", type=int, default=500, help="分析最近N条(默认500)")
    ap.add_argument("--keyword-only", action="store_true", help="不调LLM, 只做关键词分析(零成本)")
    ap.add_argument("--market", action="store_true", help="市场模式: 不比对简历, 只统计JD热词(无简历时自动进入)")
    ap.add_argument("--resume-file", help="从文本文件导入简历到设置页resume_summary")
    args = ap.parse_args()

    if args.resume_file:
        p = Path(args.resume_file)
        if not p.exists():
            print(f"文件不存在: {p}")
            sys.exit(1)
        text = p.read_text(encoding="utf-8").strip()
        if len(text) <= 5:
            print("简历文件内容过短")
            sys.exit(1)
        set_setting("resume_summary", text[:3000])
        print(f"[OK] 已导入简历({len(text)}字, 截取前3000字)到 resume_summary")

    if not args.report_only:
        kws = [k.strip() for k in args.keywords.split(",") if k.strip()]
        cities = [c.strip() for c in args.city.split(",") if c.strip()] or ["武汉"]
        n_combo = len(cities) * len(kws)
        print(f"[计划] {n_combo} 个组合 × 每个{args.per_query}条, 预计约 {n_combo * args.per_query * 4 // 60 + 1} 分钟")
        collect(kws, cities, args.per_query, args.max_total, args.headless, args.refresh)
        pause(1, 2)

    market = args.market or not _has_resume()
    # 报告文件名: xx年xx月xx日-xx岗位（xx城市）.md, 输出到项目 reports/ 目录
    kws_used = [k.strip() for k in (args.keywords or "AI项目经理").split(",") if k.strip()]
    cities_used = [c.strip() for c in (args.city or "武汉").split(",") if c.strip()] or ["武汉"]
    kw_part = "+".join(kws_used)[:30]
    city_part = "+".join(cities_used)[:20]
    y, m, d = date.today().strftime("%Y-%m-%d").split("-")
    report_title = f"{y}年{m}月{d}日-{kw_part}（{city_part}）"

    if market:
        if not args.market:
            print("[提示] 未检测到有效简历, 自动进入市场模式(仅统计JD热词); 导入简历后自动切换为匹配模式")
        jobs, freq, cats = analyze_market(args.limit)
        report, today = build_market_report(jobs, freq, cats)
        report = report.replace(f"# 市场热词报告 · {today}", f"# 市场热词报告 · {report_title}", 1)
        summary = f"共{len(jobs)}个岗位" + (f", 热词TOP1: {freq[0][0]}({freq[0][1]}个岗位)" if freq else "")
    else:
        results, resume, mode = analyze_match(args.limit, args.keyword_only)
        if not results:
            print("没有可分析的结果")
            sys.exit(1)
        report, today = build_report(results, resume, mode)
        report = report.replace(f"# 简历-JD 匹配报告 · {today}", f"# 简历-JD 匹配报告 · {report_title}", 1)
        summary = f"共{len(results)}个岗位, Top1: {results[0][0]['job_title']}({results[0][1].get('match_score')}分)"

    # 输出到项目 reports/ 目录; 同名文件(同日同岗位同城市)则追加合并
    out_dir = ROOT / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{report_title}.md"
    if out.exists():
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"\n\n---\n\n<!-- 追加于 {date.today().strftime('%Y-%m-%d %H:%M')} -->\n\n")
            f.write(report)
        print(f"\n[完成] 报告已合并追加到: {out}")
    else:
        out.write_text(report, encoding="utf-8")
        print(f"\n[完成] 报告已生成: {out}")
    print(f"        {summary}")


if __name__ == "__main__":
    main()

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

from boss_firefox import BossScraper, pause  # noqa: E402
from boss_state import (  # noqa: E402
    get_setting,
    set_setting,
    get_db,
    add_application,
    get_application_by_url,
    update_application_from_job,
)

CACHE_FILE = ROOT / ".boss_profile" / "match_cache.json"


# ── 技能词典 v2：全品类 + 词边界匹配 ──────────────────
# 设计: 一套综合词典覆盖所有岗位族(管理/产品/技术/商务), 词频只计入JD中真实出现的词,
# 所以采集什么岗位, 报告自然突出什么类别的词; 分类视图可以看出岗位偏管理还是偏技术。

SKILL_MAP_V2 = {
    "项目管理": {
        "项目管理", "PMP", "PgMP", "PRINCE2", "敏捷", "Scrum", "看板", "Kanban",
        "需求分析", "需求管理", "需求调研", "跨部门协作", "跨部门沟通", "跨团队协作",
        "风险管控", "风险管理", "交付管理", "项目交付", "项目落地", "项目推进",
        "干系人", "OKR", "KPI", "甘特图", "里程碑", "立项", "结项", "排期",
        "资源协调", "进度管理", "变更管理", "项目集", "项目群", "WBS", "复盘",
    },
    "产品管理": {
        "产品规划", "产品设计", "产品迭代", "产品生命周期", "用户研究", "用户体验",
        "用户调研", "原型设计", "PRD", "MRD", "竞品分析", "竞品调研", "用户增长",
        "用户画像", "MVP", "埋点", "A/B测试", "产品思维", "商业化", "变现",
    },
    "协作/办公工具": {
        "Jira", "Confluence", "飞书", "钉钉", "企业微信", "Teambition", "TAPD",
        "禅道", "Notion", "Axure", "Figma", "Visio", "Xmind", "Excel", "PPT",
        "Office", "墨刀", "ProcessOn", "n8n", "Zapier",
    },
    "AI/大模型": {
        "大模型", "LLM", "Agent", "智能体", "RAG", "微调", "SFT", "Prompt",
        "提示词", "Function Calling", "Tool Calling", "Embedding", "AIGC",
        "知识库", "智能客服", "数字人", "Copilot", "多模态", "LangChain",
        "LangGraph", "Dify", "Coze", "MCP", "机器学习", "深度学习", "NLP",
        "计算机视觉", "语音识别", "OCR", "ChatGPT", "DeepSeek",
    },
    "技术-开发": {
        "Python", "Java", "Go", "Golang", "C++", "C#", "C语言", "PHP",
        "TypeScript", "JavaScript", "Node.js", "React", "Vue", "FastAPI",
        "Flask", "Django", "Spring", "微服务", "API", "爬虫", "自动化脚本", "SQL",
    },
    "技术-数据": {
        "MySQL", "PostgreSQL", "Redis", "MongoDB", "Elasticsearch", "Kafka",
        "数据分析", "数据治理", "数据可视化", "BI", "数仓", "数据仓库", "大数据",
        "Hadoop", "Spark", "Flink", "Hive",
    },
    "技术-部署/架构": {
        "Docker", "Kubernetes", "K8s", "Linux", "CI/CD", "Nginx", "GPU", "CUDA",
        "架构设计", "系统设计", "高并发", "分布式",
    },
    "商务/行业": {
        "售前", "解决方案", "方案编写", "商务谈判", "客户沟通", "客户成功", "大客户",
        "B端", "G端", "C端", "政务", "国企", "央企", "医疗", "金融", "教育",
        "制造", "工业", "能源", "汽车", "车载", "电商", "供应链", "SaaS",
    },
    "软技能/领导力": {
        "团队管理", "人员管理", "目标管理", "沟通协调", "汇报", "演讲", "谈判",
        "领导力", "执行力", "出差", "驻场", "文档编写", "逻辑思维", "抗压",
    },
}

# 词边界匹配缓存(修复旧版 substring 匹配把 "c" 从任意英文单词里抠出来的 bug)
_TERM_RE = {}


def _term_hit(term: str, text: str) -> bool:
    """ASCII 技能词用词边界匹配(防止单字母/短词误命中), 中文词用子串匹配。"""
    if re.fullmatch(r"[A-Za-z0-9+#./ -]+", term):
        if term not in _TERM_RE:
            _TERM_RE[term] = re.compile(
                r"(?<![A-Za-z0-9+#])" + re.escape(term) + r"(?![A-Za-z0-9+#])", re.I
            )
        return bool(_TERM_RE[term].search(text))
    return term.lower() in text.lower()


def parse_skills2(text: str) -> dict:
    r = {}
    for cat, skills in SKILL_MAP_V2.items():
        hits = [s for s in skills if _term_hit(s, text)]
        if hits:
            r[cat] = hits
    return r


# ── 噪音过滤与结构化解析 ──────────────────────────────

# 默认标题黑名单: 明显与求职方向无关的岗位(可通过 settings.noise_filter_keywords 追加)
DEFAULT_NOISE_WORDS = (
    "销售", "投资", "合伙人", "猎头", "讲师", "加盟", "招商", "渠道",
    "保险", "房产", "中介", "客服", "催收", "地推",
)


def _load_noise_words() -> list:
    custom = (get_setting("noise_filter_keywords") or "").strip()
    extra = [w.strip() for w in custom.split(",") if w.strip()]
    return list(DEFAULT_NOISE_WORDS) + extra


def _is_noise(title: str, noise_words) -> bool:
    return any(w in (title or "") for w in noise_words)


def _parse_salary_k(s: str):
    """'23-29K·24薪' -> (23, 29); '361-461元/天'等日薪/异常格式 -> None"""
    m = re.search(r"(\d+)-(\d+)K", s or "")
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        if 1 <= lo <= 200 and lo < hi <= 300:
            return lo, hi
    return None


def _salary_stats(jobs):
    """薪资统计: 可解析岗位数/中位数/分档分布"""
    pairs = [(j, _parse_salary_k(j.get("salary") or "")) for j in jobs]
    ok = [(j, p) for j, p in pairs if p]
    if not ok:
        return None
    los = sorted(p[0] for _, p in ok)
    his = sorted(p[1] for _, p in ok)
    med = lambda a: a[len(a) // 2] if len(a) % 2 else (a[len(a) // 2 - 1] + a[len(a) // 2]) / 2
    brackets = [("≤15K", 0, 15), ("15-25K", 15, 25), ("25-35K", 25, 35), (">35K", 35, 999)]
    dist = {name: 0 for name, _, _ in brackets}
    for _, (lo, hi) in ok:
        mid = (lo + hi) / 2
        for name, a, b in brackets:
            if a <= mid < b or (name == ">35K" and mid >= 35):
                dist[name] += 1
                break
    return {
        "n": len(ok), "n_total": len(jobs),
        "lo_med": med(los), "hi_med": med(his),
        "dist": dist,
    }


def _dist_table(jobs, col, order=None):
    """经验/学历等字段的分布统计"""
    c = Counter((j.get(col) or "").strip() for j in jobs if (j.get(col) or "").strip())
    if order:
        items = [(k, c[k]) for k in order if k in c] + [(k, n) for k, n in c.most_common() if k not in order]
    else:
        items = c.most_common()
    return items


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
    """搜索 + 详情采集, 结果入库。每个 城市×关键词 组合各采 per_query 条。
    返回 (n_saved, search_urls): search_urls = [{'kw','city','url'}], 供报告记录采集方法。"""
    sc = BossScraper(headless=headless)
    sc.start()
    search_urls = []

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
                # 记录实际搜索URL(与 BOSS 页面原生格式一致: /jobs, city在前)
                from urllib.parse import quote_plus as _qp
                search_urls.append({
                    "kw": kw, "city": city or "全国",
                    "url": f"https://www.zhipin.com/web/geek/jobs?city={city_code}&query={_qp(kw)}",
                })
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
    return n_saved, search_urls


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
    for _, ss in parse_skills2(text).items():
        skills.extend(ss)
    if not skills:
        return None
    r = (resume or "").lower()
    have = [s for s in skills if _term_hit(s, r) or s.lower() in r]
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


def _is_relevant(title: str, kws) -> bool:
    """标题相关性: 搜索关键词的核心词根须出现在标题中。

    规则: 把关键词按"AI/智能/人工智能"等修饰词拆出核心词根(如"AI项目经理"→"项目经理"),
    标题至少包含一个核心词根(或其常见变体, 如"项目经理"≈"项目管理")才算相关。
    """
    MODIFIERS = ("ai", "人工智能", "智能")
    # 常见等价变体: 命中任一即算词根出现
    VARIANTS = {"项目经理": ("项目经理", "项目管理", "项目主管"), "产品经理": ("产品经理", "产品管理")}

    def _hit(core: str, title: str) -> bool:
        for k, vs in VARIANTS.items():
            if k in core or core in k:
                return any(v in title for v in vs)
        return core in title

    title = (title or "").lower()
    for kw in kws:
        core = kw.lower()
        for m in MODIFIERS:
            core = core.replace(m, "").strip()
        if not core:
            continue
        for part in [p for p in re.split(r"[/\s]+", core) if len(p) >= 2]:
            if _hit(part, title):
                return True
    return False


def _split_jobs(jobs_all, kws):
    """三路分流: 相关岗位 / 弱相关(标题不含关键词词根) / 噪音(黑名单)。
    弱相关和噪音都不进统计, 在报告中单列可复核。"""
    noise_words = _load_noise_words()
    jobs, weak_jobs, noise_jobs = [], [], []
    for j in jobs_all:
        t = j.get("job_title") or ""
        if _is_noise(t, noise_words):
            noise_jobs.append(j)
        elif not _is_relevant(t, kws):
            weak_jobs.append(j)
        else:
            jobs.append(j)
    return jobs, weak_jobs, noise_jobs


def analyze_market(limit, kws=None):
    """市场模式：不比对简历。相关性过滤 + 噪音过滤 + JD技能词频 + 薪资/经验/学历统计。"""
    jobs_all = _fetch_jobs_with_jd(limit)
    if not jobs_all:
        print("库中没有含JD全文的岗位, 请先采集(--keywords ...)")
        sys.exit(1)
    kws = kws or ["项目经理"]
    jobs, weak_jobs, noise_jobs = _split_jobs(jobs_all, kws)

    freq = Counter()
    cats = {}  # 技能小写 -> 类别
    for job in jobs:
        text = (job.get("description") or "") + " " + (job.get("job_title") or "")
        found = set()
        for cat, ss in parse_skills2(text).items():
            for s in ss:
                found.add(s.lower())
                cats.setdefault(s.lower(), cat)
        for s in found:
            freq[s] += 1
    print(
        f"[市场分析] 强相关{len(jobs)}个 · 剔除弱相关{len(weak_jobs)}个(标题不含关键词词根) · "
        f"剔除噪音{len(noise_jobs)}个 · 提取到 {len(freq)} 个技能词"
    )
    return jobs, weak_jobs, noise_jobs, freq.most_common(), cats


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
        for _, ss in parse_skills2(text).items():
            for s in ss:
                found.add(s.lower())
        for s in found:
            (have if (_term_hit(s, r) or s in r) else miss)[s] += 1
    return have, miss


# ── 报告 ──────────────────────────────────────────────

def _market_sections(jobs):
    """薪资/经验/学历市场统计(市场报告与匹配报告共用)。返回 md 行列表, 三级标题。"""
    out = []
    s = _salary_stats(jobs)
    if s:
        out.append("### 薪资分析")
        out.append("")
        out.append(
            f"可解析薪资的岗位 **{s['n']}/{s['n_total']}** · 区间中位数: "
            f"**下限 {s['lo_med']:g}K / 上限 {s['hi_med']:g}K**"
        )
        out.append("")
        out.append("| 薪资档(取区间中点) | 岗位数 | 占比 |")
        out.append("|---|---|---|")
        for name, n in s["dist"].items():
            out.append(f"| {name} | {n} | {n * 100 // max(s['n'], 1)}% |")
        out.append("")
    exp = _dist_table(jobs, "experience", order=["经验不限", "应届", "1年内", "1-3年", "3-5年", "5-10年", "10年以上"])
    edu = _dist_table(jobs, "education", order=["学历不限", "大专", "本科", "硕士", "博士"])
    if exp or edu:
        out.append("### 经验 / 学历要求分布")
        out.append("")
        if exp:
            ne = sum(n for _, n in exp)
            out.append(f"经验要求（{ne}/{len(jobs)} 个岗位标注了该项）：")
            out.append("")
            out.append("| 经验要求 | 岗位数 | 占比 |")
            out.append("|---|---|---|")
            for k, n in exp:
                out.append(f"| {k} | {n} | {n * 100 // ne}% |")
            out.append("")
        if edu:
            nd = sum(n for _, n in edu)
            out.append(f"学历要求（{nd}/{len(jobs)} 个岗位标注了该项）：")
            out.append("")
            out.append("| 学历要求 | 岗位数 | 占比 |")
            out.append("|---|---|---|")
            for k, n in edu:
                out.append(f"| {k} | {n} | {n * 100 // nd}% |")
            out.append("")
    return out


def _hr_sort_key(j):
    d = j.get("hr_active_days")
    if d is None or d == "" or d == -1:
        return (999, 0)
    return (int(d), 0)


def _is_campus_job(j) -> bool:
    """应届/校招岗: 标题或经验要求含相关词"""
    probe = (j.get("job_title") or "") + " " + (j.get("experience") or "")
    return any(w in probe for w in ("校招", "应届", "校方", "校园"))


def _job_sample_lines(jobs, top=40):
    """岗位样本行: 应届/校招岗排最后, 其余按HR活跃度; JD全文折叠块缩进到列表项内"""
    ordered = sorted(jobs, key=_hr_sort_key)
    ordered = [j for j in ordered if not _is_campus_job(j)] + [j for j in ordered if _is_campus_job(j)]
    out = []
    for i, j in enumerate(ordered[:top], 1):
        title = j["job_title"]
        url = (j.get("job_url") or "").strip()
        head = f"[{title}]({url})" if url else title
        if _is_campus_job(j):
            head += "　**🎓应届/校招**"
        active = (j.get("hr_active_label") or "").strip() or "活跃度未知"
        out.append(f"{i}. {head} · {j.get('company') or ''} · {j.get('salary') or ''} · HR:{active}")
        desc = (j.get("description") or "").strip()
        if desc:
            # 缩进宽度 = 序号位数 + 2 (如"1."→3格, "10."→4格), 保证嵌套进列表项
            ind = " " * (len(str(i)) + 2)
            out.append("")
            out.append(f"{ind}<details><summary>展开JD全文（{len(desc)}字）</summary>")
            out.append("")
            for ln in desc.splitlines():
                out.append((ind + "  " + ln).rstrip())
            out.append("")
            out.append(f"{ind}</details>")
            out.append("")
    return out


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
    # 市场背景: 薪资/经验/学历分布 + 高匹配岗位薪资对比
    jobs_only = [j for j, _ in results]
    ms = _market_sections(jobs_only)
    if ms:
        lines.append("## 二、市场背景（薪资 / 经验 / 学历）")
        lines.append("")
        lines.extend(ms)
        strong = [j for j, r in results if int(r.get("match_score") or 0) >= 70]
        s_all = _salary_stats(jobs_only)
        s_strong = _salary_stats(strong) if strong else None
        if s_all and s_strong:
            lines.append(
                f"> 💡 与你匹配度≥70分的 {len(strong)} 个岗位薪资中位数为 "
                f"**{s_strong['lo_med']:g}-{s_strong['hi_med']:g}K**, 全部样本为 "
                f"**{s_all['lo_med']:g}-{s_all['hi_med']:g}K** —— 前者可视为\"你目前够得着的薪资带\"。"
            )
            lines.append("")
    lines.append("## 三、岗位详情")
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
    lines.append("## 四、关键词缺口分析(全部JD统计)")
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


def build_market_report(jobs, weak_jobs, noise_jobs, freq, cats):
    """市场模式报告：市场画像（薪资/经验/学历 + 热词 + 分类 + 样本 + 剔除清单），不涉及简历。"""
    today = date.today().isoformat()
    total = len(jobs)
    lines = [f"# 市场热词报告 · {today}", ""]
    lines.append(
        f"> 强相关样本: **{total} 个岗位**(JD全文) · 剔除弱相关 **{len(weak_jobs)}** 个(标题不含关键词词根) · "
        f"剔除噪音 **{len(noise_jobs)}** 个(标题黑名单) · "
        f"提取技能词 **{len(freq)}** 个 · 市场模式(未导入简历, 仅统计市场需求)"
    )
    lines.append("")
    lines.append("## 一、市场画像（薪资 / 经验 / 学历）")
    lines.append("")
    ms = _market_sections(jobs)
    if ms:
        lines.extend(ms)
    else:
        lines.append("*样本数据不足以统计薪资/经验/学历分布*")
        lines.append("")

    lines.append("## 二、热门技能词 TOP 30")
    lines.append("")
    lines.append("| # | 技能 | 出现岗位数 | 占比 | 类别 |")
    lines.append("|---|------|-----------|------|------|")
    for i, (s, n) in enumerate(freq[:30], 1):
        lines.append(f"| {i} | {s} | {n} | {n * 100 // max(total, 1)}% | {cats.get(s, '')} |")
    lines.append("")

    by_cat = {}
    for s, n in freq:
        if n * 100 // max(total, 1) >= 20:  # 只列出现于≥20%岗位的
            by_cat.setdefault(cats.get(s, "其他"), []).append((s, n))
    if by_cat:
        lines.append("## 三、分类视图(出现于≥20%岗位的技能)")
        lines.append("")
        for cat in sorted(by_cat, key=lambda c: -max(n for _, n in by_cat[c])):
            items = "、".join(f"**{s}**({n})" for s, n in sorted(by_cat[cat], key=lambda x: -x[1]))
            lines.append(f"- **{cat}**: {items}")
        lines.append("")

    lines.append("## 四、岗位样本(应届/校招岗排最后, 其余按HR活跃度)")
    lines.append("")
    lines.extend(_job_sample_lines(jobs))
    lines.append("")

    if weak_jobs:
        lines.append("## 五、已剔除的弱相关岗位(标题不含关键词词根, 未参与统计)")
        lines.append("")
        for j in weak_jobs[:20]:
            lines.append(f"- {j.get('job_title') or ''} · {j.get('company') or ''} · {j.get('salary') or ''}")
        lines.append("")
        lines.append("> 判定规则：岗位标题须包含搜索关键词的核心词根（去掉 AI/智能/人工智能 等修饰词），BOSS 相关性召回的其他岗位在此剔除。")
        lines.append("")

    if noise_jobs:
        lines.append("## 六、已过滤的疑似无关岗位(标题命中黑名单, 未参与统计)")
        lines.append("")
        for j in noise_jobs[:20]:
            lines.append(f"- {j.get('job_title') or ''} · {j.get('company') or ''} · {j.get('salary') or ''}")
        lines.append("")
        lines.append("> 黑名单默认: 销售/投资/合伙人/猎头/讲师/加盟/招商/渠道/保险/房产等; 可在 Web 设置页 `noise_filter_keywords` 追加自定义词(逗号分隔)。")
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

    search_urls = []
    if not args.report_only:
        kws = [k.strip() for k in args.keywords.split(",") if k.strip()]
        cities = [c.strip() for c in args.city.split(",") if c.strip()] or ["武汉"]
        n_combo = len(cities) * len(kws)
        print(f"[计划] {n_combo} 个组合 × 每个{args.per_query}条, 预计约 {n_combo * args.per_query * 4 // 60 + 1} 分钟")
        _, search_urls = collect(kws, cities, args.per_query, args.max_total, args.headless, args.refresh)
        pause(1, 2)

    market = args.market or not _has_resume()
    # 报告文件名: xx年xx月xx日-xx岗位（xx城市）.md, 输出到项目 reports/ 目录
    kws_used = [k.strip() for k in (args.keywords or "AI项目经理").split(",") if k.strip()]
    cities_used = [c.strip() for c in (args.city or "武汉").split(",") if c.strip()] or ["武汉"]
    kw_part = "+".join(kws_used)[:30]
    city_part = "+".join(cities_used)[:20]
    y, m, d = date.today().strftime("%Y-%m-%d").split("-")
    report_title = f"{y}年{m}月{d}日-{kw_part}（{city_part}）"

    # 采集方法章节: 记录每次实际搜索的URL, BOSS召回的相关岗位可溯源
    method_lines = ["## 采集方法", ""]
    if search_urls:
        for su in search_urls:
            method_lines.append(f"- 关键词「{su['kw']}」@ {su['city']}：[{su['url']}]({su['url']})")
        method_lines.append("")
        method_lines.append(
            "> 搜索词直接交给 BOSS 直聘搜索引擎做相关性召回（非标题精确匹配），"
            "结果会包含平台认为相关的岗位（如产品经理/售前/总经理类）；"
            "报告已按黑名单过滤明显无关岗位，弱相关岗位保留在样本中、可在上方岗位样本列表复核。"
        )
    else:
        method_lines.append(f"- 本次为 --report-only 模式（未重新采集），报告基于库中已有数据，关键词：{'、'.join(kws_used)}")
    method_lines.append("")

    if market:
        if not args.market:
            print("[提示] 未检测到有效简历, 自动进入市场模式(仅统计JD热词); 导入简历后自动切换为匹配模式")
        jobs, weak_jobs, noise_jobs, freq, cats = analyze_market(args.limit, kws_used)
        report, today = build_market_report(jobs, weak_jobs, noise_jobs, freq, cats)
        report = report.replace(f"# 市场热词报告 · {today}", f"# 市场热词报告 · {report_title}", 1)
        report = report.replace("## 一、市场画像", "\n".join(method_lines) + "## 一、市场画像", 1)
        summary = (
            f"强相关{len(jobs)}个岗位(剔除弱相关{len(weak_jobs)}个、噪音{len(noise_jobs)}个)"
            + (f", 热词TOP1: {freq[0][0]}({freq[0][1]}个岗位)" if freq else "")
        )
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

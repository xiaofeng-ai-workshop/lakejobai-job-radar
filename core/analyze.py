# -*- coding: utf-8 -*-
"""核心分析层（core/analyze）

统一「单 JD 匹配分析」与「批量市场/匹配分析」两份逻辑的唯一真相源：
- `analyze_single_jd`  ← 原 boss_app.analyze_jd（单 JD，请求体传 JD，LLM 出 reasons/risks/suggested_questions）
- `analyze_market` / `analyze_match` ← 原 match_report.py 批量逻辑（从 DB 取 + 过滤 + 打分）

本模块只依赖 core 层（boss_state）与共享 LLM 客户端（interview/llm_client），
不反向依赖任何适配器（web/cli/脚本）。
"""

import hashlib
import json
import re
import sys
from collections import Counter
from datetime import date
from pathlib import Path

ROOT = Path(__file__).parent.parent
CACHE_FILE = ROOT / ".boss_profile" / "match_cache.json"

# 让本模块能引入共享 LLM 客户端（interview/llm_client.py，主项目与刷题子模块共用）
_INTERVIEW_DIR = ROOT / "interview"
if str(_INTERVIEW_DIR) not in sys.path:
    sys.path.insert(0, str(_INTERVIEW_DIR))

from boss_state import get_setting, get_db  # noqa: E402

__all__ = [
    "analyze_single_jd", "analyze_market", "analyze_match", "keyword_gap",
    "analyze_match", "split_jobs", "expand_search_kws", "direction_filter",
    "is_tech_job", "is_relevant", "all_search_kws", "fetch_jobs_with_jd",
]


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


# 软件向领域词: 标题 / JD前500字 / 公司名 含其中任一词, 才算"软件相关岗位"(默认开启)
# 增补: 测试 / QA / 质量 —— 让"AI测试"等岗位在软件向过滤中不被误伤
TECH_WORDS = (
    "软件", "互联网", "IT", "信息化", "数字化", "SaaS", "计算机",
    "程序员", "开发", "算法", "大数据", "人工智能", "AI", "ai",
    "系统集成", "智能", "数据", "系统", "App", "app", "平台",
    "Java", "Python", "研发", "产品", "云计算", "云", "科技",
    "测试", "QA", "质量",
)

# 非软件行业词: **只在标题 / 公司名生效**(不在 JD 文本生效),
# 避免"服务建筑/金融行业"的软件 PM 因 JD 里写了客户行业被误伤。
TITLE_NON_TECH_WORDS = (
    "建筑", "地产", "施工", "弱电", "强电", "电气", "冶金", "酒店", "物业",
    "风电", "光伏", "电力工程", "防腐", "保温", "人资", "门店", "零售", "餐饮",
    "物流", "仓储", "工厂" , "产线", "化工", "医药代表", "护理", "护士", "家教",
    "教练", "保安", "保洁", "司机", "快递员", "外卖", "房产", "中介", "团餐",
    "铁路", "路桥", "市政", "门窗", "锂电池", "机加工", "汽车零部件", "锅炉",
)


def is_tech_job(j) -> bool:
    """软件向判定(默认开启):

    - 标题或公司名命中非软件行业词 → 直接判非软件(行业词只在标题/公司名生效)
    - 标题 / JD前500字 / 公司名 命中任一软件领域词 → 软件向
    """
    t = (j.get("job_title") or "")
    company = (j.get("company") or "")
    if any(w in t or w in company for w in TITLE_NON_TECH_WORDS):
        return False
    probe = t + " " + (j.get("description") or "")[:500] + " " + company
    return any(w in probe for w in TECH_WORDS)


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


# 关键词的"宽词 ⊃ 窄词"方向性包含: 修饰词(AI/人工智能/智能/数字化)只是给同一岗位加限定,
# 所以窄词(AI项目经理)的 JD 同时也属于宽词(项目经理)的分析范围, 反之不成立。
_KW_MODIFIERS = ("人工智能", "数字化", "智能", "ai")


def _kw_core(kw: str) -> str:
    """去掉 AI/智能等修饰词后的核心词根。"""
    c = (kw or "").lower()
    for m in _KW_MODIFIERS:
        c = c.replace(m, "")
    return c.strip()


def all_search_kws():
    """库里出现过的全部非空 search_kw 值。"""
    rows = get_db().execute(
        "SELECT DISTINCT search_kw FROM applications WHERE search_kw IS NOT NULL AND search_kw != ''"
    ).fetchall()
    return [r["search_kw"] for r in rows]


def expand_search_kws(kw, all_kws=None):
    """把分析方向 kw 展开成"应纳入的 search_kw 集合"。宽词包含窄词, **不可反向**。

    例: kw="项目经理"   → {"项目经理", "AI项目经理"}    AI 的 JD 也是 PM 的 JD
        kw="AI项目经理"  → {"AI项目经理"}                不倒吞宽词, 否则污染
        kw="FDE"        → {"FDE"}                        平行词互不交叉
    """
    broad = (kw or "").strip()
    if not broad:
        return set()
    bl, bc = broad.lower(), _kw_core(broad)
    out = {broad}
    for k in (all_kws if all_kws is not None else all_search_kws()):
        k = (k or "").strip()
        if not k or k == broad:
            continue
        kl, kc = k.lower(), _kw_core(k)
        # 窄词判定: 原串里含宽词(AI项目经理 ⊃ 项目经理);
        # 或去修饰后核心更长且含宽词核心(测试开发 ⊃ 测试)。
        # 只有"更具体"的方向才包含, 保证不反向。
        if bl in kl or (bc and len(kc) > len(bc) and bc in kc):
            out.add(k)
    return out


def fetch_jobs_with_jd(limit, since_minutes=None, search_kws=None):
    """取含 JD 全文的岗位。

    search_kws=None      → 不过滤(全部岗位);
    search_kws=集合/列表 → 只取 search_kw 在其中的岗位(单 kw 报告隔离, 支持宽词含窄词的 IN 展开)。
    since_minutes=N      → 只取最近 N 分钟内入库(配合 --only-new)。
    """
    if since_minutes:
        base_sql = """SELECT * FROM applications
               WHERE description IS NOT NULL AND length(description) > 50
                 AND created_at >= datetime('now', ?)"""
    else:
        base_sql = """SELECT * FROM applications
               WHERE description IS NOT NULL AND length(description) > 50"""
    prefix = ((f"-{int(since_minutes)} minutes",) if since_minutes else ())
    if search_kws:
        kws_list = [k for k in search_kws if k]
        if not kws_list:
            return []
        placeholders = ",".join("?" * len(kws_list))
        rows = get_db().execute(
            base_sql + f" AND search_kw IN ({placeholders}) ORDER BY id DESC LIMIT ?",
            prefix + tuple(kws_list) + (limit,),
        ).fetchall()
    else:
        rows = get_db().execute(
            base_sql + " ORDER BY id DESC LIMIT ?", prefix + (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def is_relevant(title: str, kws) -> bool:
    """标题相关性: 搜索关键词的核心词根须出现在标题中。

    规则: 把关键词按"AI/智能/人工智能"等修饰词拆出核心词根(如"AI项目经理"→"项目经理"),
    标题至少包含一个核心词根(或其常见变体, 如"项目经理"≈"项目管理")才算相关。
    """
    MODIFIERS = ("ai", "人工智能", "智能")
    # 常见等价变体: 命中任一即算词根出现
    VARIANTS = {
        "项目经理": ("项目经理", "项目管理", "项目主管", "项目助理"),
        "产品经理": ("产品经理", "产品管理"),
    }

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


def _backfill_search_kw(jobs_all):
    """给 jobs_all 里 search_kw 为空的岗位, 用 search_url_history + 时间窗反推归属 kw。

    历史条目形如 {kw, city, url, ts}; 给定岗位 created_at, 找 ts 离它最近且 ts <= created_at
    的那次采集, 把那次 kw 当作它的 search_kw。**只补内存里的 job dict, 不回写 DB**——
    旧数据若从未被新采集刷新过, 它的 search_kw 列保持空, 下次反推时还会再补一次;
    用户后续真正入库的新数据自带 search_kw, 不依赖此函数。
    """
    try:
        history = json.loads(get_setting("search_url_history") or "[]")
    except Exception:
        return
    # 必须有 ts 才能做时间窗反推; 老条目(无 ts)直接跳过
    h_with_ts = [h for h in history if h.get("ts") and h.get("kw")]
    if not h_with_ts:
        return
    h_with_ts.sort(key=lambda h: h["ts"])

    from datetime import datetime
    for j in jobs_all:
        if (j.get("search_kw") or "").strip():
            continue
        created = (j.get("created_at") or "")[:19]
        try:
            ct = datetime.strptime(created, "%Y-%m-%d %H:%M:%S").timestamp()
        except Exception:
            continue
        # 找 ts <= ct 中离 ct 最近的(已排序, 取最后一个满足条件的)
        best = None
        for h in h_with_ts:
            if h["ts"] <= ct:
                best = h
            else:
                break
        if best:
            j["search_kw"] = best["kw"]
            j["_kw_backfilled"] = True  # 标记是反推的, 报告里说清楚


# ── 方向级 require / exclude 规则 ─────────────────────
# 解决"词根太粗"导致弱关联岗位混进强相关的问题(如"AI项目经理"词根=项目经理,
# 于是 AI产品经理/售前项目经理 都被判强相关)。
#
# 判定顺序(对"已过 kw/标题相关性"的候选岗):
#   1. keep 例外优先 —— 命中即强相关(覆盖下面的 exclude, 用于救回被 exclude 误伤的合法岗)
#   2. exclude 命中  —— 弱相关(直接剔除)
#   3. require 命中  —— 强相关
#   4. 都不命中      —— 弱相关(未命中本方向必要词)
#
# 行业词只在标题/公司名生效(见 is_tech_job), 本规则只看标题。
KW_FILTER_RULES = {
    "AI测试": {
        "require": [r"测试|QA|质量|质检"],
        # 漫剧/剧本/真人生成/训练师/硬件/产品经理/管培生/信息化管理/销售/助理 等混入测试召回的无关岗
        "exclude": [r"漫剧|剧本|真人生成|训练师|硬件|产品经理|管培生|信息化管理|销售|助理"],
        "keep": [],
    },
    "FDE": {
        # 含 Agent 开发(大小写敏感关闭) / 实施 / 交付 / 部署 / 效能 等 FDE 真实职责
        "require": [r"(?i)FDE|前沿部署|效能顾问|效能工程师|实施工程师|交付工程师|部署工程师|应用工程师|智能体开发|Agent.*开发|开发.*Agent|Agent.*工程师"],
        # 助理/管培生/算法/软件测试(路由器·WiFi)/销售 混入 FDE 召回的无关岗
        "exclude": [r"助理|管培生|算法工程师|软件测试|测试.*路由器|测试.*WiFi|销售顾问|销售经理"],
        # 用户确认保留: Agent后端开发 / AI agent开发 / Agent评测工程师 / FDE售前工程师(靠 require 已命中, 这里兜底)
        "keep": [r"(?i)Agent.*开发|开发.*Agent|Agent.*工程师|Agent.*评测|FDE.*售前"],
    },
    "AI项目经理": {
        # AI + 项目经理/PM/项目主管/助理(双向邻接, 容差8字)
        "require": [r"(?:AI|人工智能|智能|大模型|Agent|AIGC|ai).{0,8}(?:项目经理|项目管理|PM|pm|项目主管|项目助理)|(?:项目经理|项目管理|PM|pm|项目主管|项目助理).{0,8}(?:AI|人工智能|智能|大模型|Agent|AIGC|ai)"],
        # 产品经理/售前/总经理/内容生态/远程交付/供应链/硬件/成本产品/脚本开发 混入 AI PM 召回的无关岗
        "exclude": [r"产品经理|售前|总经理|BG总经理|事业部总经理|内容生态|远程交付|销售经理|供应链经理|硬件|成本产品|脚本开发"],
        # 用户确认保留(被 exclude 命中但确属 AI PM 方向):
        # 车载AI产品经理 / AI交付领域经理 / AI研发技术经理 / 以及同类 AI 技术/研发/交付领域经理
        "keep": [r"车载AI产品经理", r"AI交付领域经理", r"AI研发技术经理",
                 r"AI.{0,8}(?:技术经理|研发经理|研发.{0,8}经理|交付领域经理)", r"车载.{0,4}AI.{0,4}产品经理"],
    },
    "项目经理": {
        "require": [r"项目经理|项目管理|PM|pm|项目主管|项目助理"],
        # 新增"远程交付"(用户确认项目经理方向也剔除); 其余为混入 PM 召回的各行业/职能无关岗
        "exclude": [r"AI|人工智能|AIGC|大模型|Agent|产品经理|售前|总经理|BG总经理|事业部总经理|弱电|门店|酒店|物业|风电|光伏|冶金|人资|施工|电力工程|防腐|保温|业务架构师|成本产品|团餐|铁路|路桥|建筑|市政|门窗|锂电池|机加工|汽车零部件|远程交付"],
        "keep": [],
    },
}


def direction_filter(title: str, direction: str):
    """方向级 require/exclude 过滤。返回 (ok: bool, reason: str)。

    direction 为空或没有对应规则 → (True, '无方向规则')。
    语义: 该报告方向(如 AI项目经理)要求岗位标题满足必要词、且不命中排除词。
    """
    rule = KW_FILTER_RULES.get((direction or "").strip())
    if not rule:
        return True, "无方向规则"
    t = title or ""
    for p in rule.get("keep", []):
        if re.search(p, t):
            return True, "命中方向保留例外"
    for p in rule.get("exclude", []):
        if re.search(p, t):
            return False, "命中方向排除词"
    for p in rule.get("require", []):
        if re.search(p, t):
            return True, "命中方向必要词"
    return False, "未命中方向必要词"


def split_jobs(jobs_all, kws, kw_scope=None, direction=None):
    """三路分流: 强相关 / 弱相关 / 噪音。
    按每个岗位**自己入库时的 search_kw**判定相关性, 不再让 FDE 去考 PM 试卷。

    kw_scope → 宽词报告应纳入的 search_kw 集合(expand_search_kws 展开结果),
               比 kws 更宽(如跑"项目经理"时含"AI项目经理")。None 时退化为 kws 本身。

    判定逻辑:
      1. 标题命中黑名单 → 噪音(独立)
      2. search_kw ∈ kw_scope  → 强相关候选
      3. 标题含当前 kws 词根   → 强相关候选(兜底, 处理"同 kw 召回相邻岗"的旧 is_relevant 语义)
      4. 候选再走方向级过滤(KW_FILTER_RULES, 由 direction 决定规则):
         命中 require(且未命中 exclude/keep 优先) → 强相关; 否则 → 弱相关(方向不符)
      5. 其它(非候选):
         - search_kw 非空 → 弱相关(属其他 kw 报告)
         - search_kw 空 + 反推得到 kw + 该 kw 在 kw_scope 里 → 上面已归到强相关
         - search_kw 空(反推也得不到) → 弱相关, 标"未追溯到入库 kw"

    返回 (jobs, weak_jobs, noise_jobs, dir_weak_jobs):
      dir_weak_jobs = 已过相关性、但被方向 require/exclude 判为弱相关的岗位。
    """
    noise_words = _load_noise_words()
    _backfill_search_kw(jobs_all)
    kws_set = {k.strip() for k in kws if k.strip()}
    scope_set = {k.strip() for k in (kw_scope or ()) if k.strip()} | kws_set
    jobs, weak_jobs, noise_jobs, dir_weak_jobs = [], [], [], []
    for j in jobs_all:
        t = j.get("job_title") or ""
        if _is_noise(t, noise_words):
            noise_jobs.append(j)
            continue
        job_kw = (j.get("search_kw") or "").strip()
        kw_ok = bool(job_kw and job_kw in scope_set)
        title_ok = is_relevant(t, kws)
        if kw_ok or title_ok:
            # 方向级过滤: 已过相关性, 但不满足本方向 require/exclude → 弱相关(方向不符)
            dir_ok, dir_reason = direction_filter(t, direction)
            if not dir_ok:
                j["_kw_status"] = dir_reason
                dir_weak_jobs.append(j)
                continue
            j["_kw_status"] = job_kw if kw_ok else f"同源相邻召回(无 search_kw, 标题含词根)"
            jobs.append(j)
        else:
            if not job_kw:
                j["_kw_status"] = "未追溯到入库 kw"
            else:
                j["_kw_status"] = f"属其他 kw 报告({job_kw})"
            weak_jobs.append(j)
    return jobs, weak_jobs, noise_jobs, dir_weak_jobs


def analyze_market(limit, kws=None, tech_only=True, since_minutes=None, search_kws=None, direction=None):
    """市场模式：不比对简历。相关性过滤 + 方向过滤 + 噪音过滤 + 可选软件向过滤 + JD技能词频 + 薪资/经验/学历统计。

    tech_only=True → 在相关性过滤后, 进一步用 is_tech_job 排除非软件/互联网岗位,
                     这些岗位进 non_tech_jobs 单独列出(可复核), 不参与统计。**默认开启**。
    since_minutes  → 只取最近 N 分钟入库(配合 --only-new)。
    search_kws     → 只分析入库 search_kw 在该集合内的岗位(单 kw 报告隔离); None=全部。
    direction       → 报告方向(如 "AI项目经理"), 用于选 KW_FILTER_RULES; None=不做方向过滤。
    """
    from collections import Counter
    jobs_all = fetch_jobs_with_jd(limit, since_minutes=since_minutes, search_kws=search_kws)
    if not jobs_all:
        raise RuntimeError("库中没有含JD全文的岗位, 请先采集(--keywords ...)")
    kws = kws or ["项目经理"]
    jobs, weak_jobs, noise_jobs, dir_weak_jobs = split_jobs(
        jobs_all, kws, kw_scope=search_kws, direction=direction
    )

    non_tech_jobs = []
    if tech_only:
        kept, dropped = [], []
        for j in jobs:
            (kept if is_tech_job(j) else dropped).append(j)
        non_tech_jobs = dropped
        jobs = kept
        if not jobs:
            print("[警告] 软件向过滤后没有剩余岗位; 想看全部(含建筑/制造/金融/政务)请加 --include-non-tech")

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
        f"剔除方向不符{len(dir_weak_jobs)}个(require/exclude) · "
        f"剔除噪音{len(noise_jobs)}个(标题黑名单)"
        + (f" · 剔除非软件{len(non_tech_jobs)}个(软件向过滤)" if tech_only else "")
        + f" · 提取到 {len(freq)} 个技能词"
    )
    return jobs, weak_jobs, noise_jobs, dir_weak_jobs, non_tech_jobs, freq.most_common(), cats


def analyze_match(limit, keyword_only, tech_only=True, since_minutes=None, search_kws=None, direction=None):
    """匹配模式：与 resume_summary 比对打分排序。

    tech_only=True  → 在评分前先过滤掉非软件/互联网岗位(用 is_tech_job),
                     被过滤的进 non_tech_jobs 单独列出(报告里单节, 可复核)。**默认开启**。
    since_minutes   → 只取最近 N 分钟入库(配合 --only-new)。
    search_kws      → 只分析入库 search_kw 在该集合内的岗位(单 kw 报告隔离); None=全部。
    direction       → 报告方向(如 "AI项目经理"), 用于选 KW_FILTER_RULES; None=不做方向过滤。
    """
    resume = (get_setting("resume_summary") or "").strip()
    if not _has_resume():
        raise RuntimeError("简历摘要无效(过短或为空模板)! 请用 --resume-file 导入, 或使用市场模式 --market")
    jobs = fetch_jobs_with_jd(limit, since_minutes=since_minutes, search_kws=search_kws)
    if not jobs:
        raise RuntimeError("库中没有含JD全文的岗位, 请先采集(--keywords ...) 或在Web控制台搜索扫描")

    non_tech_jobs = []
    if tech_only:
        kept, dropped = [], []
        for j in jobs:
            (kept if is_tech_job(j) else dropped).append(j)
        non_tech_jobs = dropped
        jobs = kept
        if not jobs:
            print("[警告] 软件向过滤后没有剩余岗位; 想看全部请加 --include-non-tech")

    # 方向级过滤: 已过软件向, 再按本方向 require/exclude 剔除弱关联(如 AI PM 方向剔除 ai产品经理/售前)
    dir_weak_jobs = []
    if direction:
        kept_dir, dropped_dir = [], []
        for j in jobs:
            ok, reason = direction_filter(j.get("job_title") or "", direction)
            if ok:
                kept_dir.append(j)
            else:
                j["_kw_status"] = reason
                dropped_dir.append(j)
        dir_weak_jobs = dropped_dir
        jobs = kept_dir
        if not jobs:
            print(f"[警告] 方向过滤({direction})后没有剩余岗位; 若误伤请用 --include-non-tech 或调整规则")

    use_llm = (not keyword_only) and _llm_available()
    mode = "LLM智能分析" if use_llm else "关键词覆盖分析(未配置AI Key或指定--keyword-only)"
    scope = []
    if since_minutes:
        scope.append(f"近{int(since_minutes)}分钟入库")
    if tech_only:
        scope.append("软件向")
    scope_str = f" · 范围: {'/'.join(scope)}" if scope else ""
    print(f"[分析] {len(jobs)} 个岗位 · 模式: {mode}{scope_str}")

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
    return results, resume, mode, non_tech_jobs, dir_weak_jobs


# ── 单 JD 匹配分析（原 boss_app.analyze_jd 迁移，契约不变） ──

def analyze_single_jd(description: str, job_title: str = "", company: str = "", resume: str = ""):
    """AI分析单个岗位JD，返回匹配度、关键技能、差距、建议。

    契约保持与原 /api/jobs/analyze 完全一致：
    - 有简历时：输出含 reasons / risks / suggested_questions
    - 无简历时：基于 JD 难度预估 match_score，summary 写岗位核心要求
    返回 dict（FastAPI 直接序列化）；失败返回 {"error":..., "match_score":0, ...}
    """
    desc = description or ""
    title = job_title or ""
    comp = company or ""

    if resume and len(resume.strip()) > 5:
        prompt = f"""你是求职辅导专家。分析以下岗位JD，对比求职者简历，输出JSON。

## 求职者简历
{resume}

## 岗位信息
- 公司: {comp}
- 职位: {title}
- JD: {desc[:2000]}

## 输出格式（严格JSON）
{{
  "match_score": 85,
  "decision": "建议投递",
  "key_skills": ["Python", "LangChain", "RAG"],
  "gap": "缺少K8s部署经验",
  "advice": "建议强调Agent开发经验，问对方技术栈",
  "summary": "整体匹配度较高，注意补充部署相关经验",
  "reasons": ["匹配理由1", "匹配理由2"],
  "risks": ["风险点1"],
  "suggested_questions": ["建议追问1"]
}}"""
    else:
        prompt = f"""你是求职辅导专家。分析以下岗位JD，提取关键信息，输出JSON。

## 岗位信息
- 公司: {comp}
- 职位: {title}
- JD: {desc[:2000]}

## 输出格式（严格JSON）
{{
  "match_score": 70,
  "decision": "可以尝试",
  "key_skills": ["Python", "LangChain", "RAG"],
  "gap": "",
  "advice": "",
  "summary": "该岗位的核心要求是...",
  "reasons": ["理由1"],
  "risks": ["风险1"],
  "suggested_questions": ["追问1"]
}}

注意：match_score 基于 JD 难度和市场需求预估即可，不必对比简历。summary 用一两句总结这个岗位的核心要求。"""

    try:
        from llm_client import llm_chat_deepseek
        raw = llm_chat_deepseek(
            [{"role": "user", "content": prompt}],
            system_prompt="你是求职辅导专家，输出严格JSON。",
            temperature=0.3,
        )
        return json.loads(raw.strip().strip("`").strip("json").strip())
    except Exception as e:
        return {"error": f"AI分析失败: {e}", "match_score": 0, "summary": "请检查AI配置"}

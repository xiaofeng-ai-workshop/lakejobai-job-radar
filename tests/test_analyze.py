# -*- coding: utf-8 -*-
"""core.analyze 回归测试：抽层后行为不变 + 输出字段契约不变。

- 纯逻辑（方向过滤 / 宽词展开 / 软件向 / 词边界 / 薪资）不依赖 DB、不依赖 LLM。
- 单 JD 分析 + 批量匹配：注入假 llm_client（sys.modules）使输出确定，验证字段契约。
- 批量：用临时 SQLite 验证 fetch/过滤/排序，关键字模式不触发真实 LLM。
"""

import sys
import json
import types
import pytest


# ── 注入假 llm_client，避免真实网络 / API Key ──────────
_fake = types.ModuleType("llm_client")


def _fake_llm(messages, system_prompt=None, temperature=0.2):
    return json.dumps({
        "match_score": 82, "decision": "建议投递",
        "key_skills": ["Python"], "gap": "缺K8s", "advice": "强调Agent",
        "summary": "匹配度较高", "reasons": ["r1"], "risks": ["x"],
        "suggested_questions": ["q"],
    })


def _fake_parse(raw):
    try:
        return json.loads(raw)
    except Exception:
        return {"match_score": 0}


_fake.llm_chat_deepseek = _fake_llm
_fake.parse_json_from_llm = _fake_parse
_fake._load_ai_config = lambda: {"api_key": "x"}
sys.modules["llm_client"] = _fake

import core.analyze as A  # noqa: E402


# ── 纯逻辑（隔离 get_setting，避免触碰 DB） ─────────────
def _isolate_settings():
    A.get_setting = lambda k, d="": ""


def test_direction_filter_require_exclude():
    _isolate_settings()
    assert A.direction_filter("AI项目经理", "AI项目经理")[0] is True
    # AI产品经理 被 AI项目经理 方向 exclude 判定为弱相关
    assert A.direction_filter("AI产品经理", "AI项目经理")[0] is False
    # keep 例外救回合法岗
    assert A.direction_filter("车载AI产品经理", "AI项目经理")[0] is True
    assert A.direction_filter("FDE", "FDE")[0] is True
    # 无方向规则 → 通过
    assert A.direction_filter("随便什么", "不存在方向")[0] is True


def test_expand_search_kws_direction():
    _isolate_settings()
    # 宽词 ⊃ 窄词（单向）
    assert A.expand_search_kws("项目经理", ["项目经理", "AI项目经理", "FDE"]) == {"项目经理", "AI项目经理"}
    # 窄词不倒吞宽词
    assert A.expand_search_kws("AI项目经理", ["项目经理", "AI项目经理"]) == {"AI项目经理"}
    # 平行词互不交叉
    assert A.expand_search_kws("FDE", ["FDE", "AI测试"]) == {"FDE"}


def test_is_tech_job():
    _isolate_settings()
    assert A.is_tech_job({"job_title": "AI项目经理", "company": "某科技", "description": "Python 开发"}) is True
    assert A.is_tech_job({"job_title": "建筑项目经理", "company": "某地产", "description": "施工管理"}) is False


def test_term_hit_word_boundary():
    _isolate_settings()
    assert A._term_hit("Python", "Python 开发") is True
    assert A._term_hit("Go", "google") is False  # 词边界防止子串误命中


def test_salary_stats():
    _isolate_settings()
    s = A._salary_stats([{"salary": "23-29K"}, {"salary": "15-20K"}])
    assert s and s["n"] == 2
    # 异常格式应被忽略
    assert A._salary_stats([{"salary": "361-461元/天"}]) is None


# ── 单 JD 分析字段契约（与原 /api/jobs/analyze 一致） ──
def test_analyze_single_jd_keys_with_resume():
    r = A.analyze_single_jd("JD内容", "AI项目经理", "某公司", "我有Python与Agent经验")
    for k in ("match_score", "decision", "key_skills", "gap", "advice",
              "summary", "reasons", "risks", "suggested_questions"):
        assert k in r, f"缺少字段 {k}"


def test_analyze_single_jd_keys_no_resume():
    r = A.analyze_single_jd("JD内容", "AI项目经理", "某公司", "")
    for k in ("match_score", "decision", "key_skills", "summary",
              "reasons", "risks", "suggested_questions"):
        assert k in r


# ── 批量：临时 DB + 关键字模式（确定性，不调真实 LLM） ─
@pytest.fixture
def tempdb(tmp_path, monkeypatch):
    import boss_state
    boss_state.DB_PATH = tmp_path / "t.db"
    boss_state._local.conn = None
    boss_state.init_db()
    # 让 core.analyze 读真实临时库（恢复被纯逻辑测试改写的 get_setting）
    monkeypatch.setattr(A, "get_setting", boss_state.get_setting)
    boss_state.set_setting("resume_summary", RESUME)
    return boss_state


def _insert(db, rows):
    for r in rows:
        db.get_db().execute(
            "INSERT INTO applications (job_title, company, salary, job_url, description, search_kw) "
            "VALUES (?,?,?,?,?,?)",
            (r["job_title"], r["company"], r["salary"], r["job_url"], r["description"], r["search_kw"]),
        )
    db.get_db().commit()


SAMPLE = [
    {"job_title": "AI项目经理", "company": "甲科技", "salary": "23-29K",
     "job_url": "u1",
     "description": "负责AI产品经理方向的项目管理工作，使用Python和Agent技术栈推动敏捷交付，"
                    "协调跨团队需求，把控项目进度与质量，沉淀方法论并赋能团队，定期复盘迭代。",
     "search_kw": "AI项目经理"},
    {"job_title": "AI测试工程师", "company": "乙软件", "salary": "15-20K",
     "job_url": "u2",
     "description": "负责AI系统的功能测试与质量保障工作，编写自动化测试脚本，搭建CI流水线，"
                    "覆盖接口与性能测试，输出测试报告并推动缺陷闭环，保障发布质量稳定。",
     "search_kw": "AI测试"},
    {"job_title": "FDE前沿部署工程师", "company": "丙科技", "salary": "20-30K",
     "job_url": "u3",
     "description": "负责AI产品的FDE前沿部署与交付工作，对接客户实施，编写部署文档，"
                    "支持效能团队完成Agent落地，处理线上问题并沉淀最佳实践。",
     "search_kw": "FDE"},
]

RESUME = ("我有八年Python后端与项目管理经验，主导过多个Agent开发项目，熟悉敏捷与DevOps流程，"
          "带过十人团队，擅长跨部门协作与需求拆解，能独立推动从0到1的产品落地与交付。")


def test_analyze_match_keyword_only(tempdb):
    _insert(tempdb, SAMPLE)
    results, resume, mode, non_tech, dir_weak = A.analyze_match(
        500, keyword_only=True, tech_only=False, search_kws={"AI项目经理"}
    )
    # 只纳入 search_kw=AI项目经理 的岗位（u1），其余按 search_kw 隔离排除
    titles = {j["job_title"] for j, _ in results}
    assert titles == {"AI项目经理"}
    assert "AI测试工程师" not in titles
    assert "FDE前沿部署工程师" not in titles
    # 按分数降序
    scores = [int(r.get("match_score") or 0) for _, r in results]
    assert scores == sorted(scores, reverse=True)
    assert mode == "关键词覆盖分析(未配置AI Key或指定--keyword-only)"


def test_analyze_market_isolation(tempdb):
    _insert(tempdb, SAMPLE)
    # search_kws=None 时全量取，由 split_jobs 按入库 kw 判强/弱相关
    jobs, weak, noise, dir_weak, non_tech, freq, cats = A.analyze_market(
        500, kws=["AI项目经理"], tech_only=False, search_kws=None
    )
    # 强相关：标题含「项目经理」词根 → u1；u2(AI测试)/u3(FDE) 属其他入库 kw → 弱相关
    assert {j["job_title"] for j in jobs} == {"AI项目经理"}
    weak_titles = {j["job_title"] for j in weak}
    assert "AI测试工程师" in weak_titles
    assert "FDE前沿部署工程师" in weak_titles
    assert isinstance(freq, list) and all(isinstance(x, tuple) for x in freq)

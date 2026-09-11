# -*- coding: utf-8 -*-
"""core.report 回归测试：抽层后 markdown 产出章节完备且确定性（同输入同输出）。

不依赖 DB / LLM，全部用合成岗位数据。
"""

from core.report import build_report, build_market_report


def _fake_job(title, company="某司", salary="20-30K", url="http://x/1", desc="Python 项目管理 Agent",
              hr_label="活跃", hr_days=3, search_kw="AI项目经理", kw_status=""):
    return {
        "job_title": title, "company": company, "salary": salary, "job_url": url,
        "description": desc, "hr_active_label": hr_label, "hr_active_days": hr_days,
        "search_kw": search_kw, "_kw_status": kw_status,
    }


def _fake_result(job, score=80):
    return job, {
        "match_score": score, "decision": "建议投递", "summary": "匹配度较高",
        "key_skills": ["Python", "Agent"], "gap": "缺K8s", "advice": "强调Agent经验",
    }


def test_build_report_sections():
    results = [_fake_result(_fake_job("AI项目经理")), _fake_result(_fake_job("AI产品经理"), 60)]
    md, _ = build_report(results, "我有Python与项目管理经验", "关键词覆盖分析")
    assert "# 简历-JD 匹配报告" in md
    assert "## 一、匹配度排名" in md
    assert "## 二、市场背景" in md
    assert "## 三、岗位详情" in md
    assert "## 四、关键词缺口分析" in md
    # 排序：80 分在前
    assert md.index("AI项目经理") < md.index("AI产品经理")


def test_build_report_deterministic():
    results = [_fake_result(_fake_job("AI项目经理")), _fake_result(_fake_job("AI测试工程师"))]
    md1, _ = build_report(results, "简历文本", "关键词覆盖分析")
    md2, _ = build_report(results, "简历文本", "关键词覆盖分析")
    assert md1 == md2


def test_build_market_report_sections():
    jobs = [_fake_job("AI项目经理"), _fake_job("AI测试工程师")]
    weak = [_fake_job("房产销售经理", search_kw="AI项目经理", kw_status="属其他 kw 报告")]
    md, _ = build_market_report(jobs, weak, noise_jobs=[], dir_weak_jobs=[], non_tech_jobs=[],
                                freq=[("Python", 2), ("Agent", 2)], cats={"python": "技术-开发", "agent": "AI/大模型"})
    assert "# 市场热词报告" in md
    assert "## 一、市场画像" in md
    assert "## 二、热门技能词 TOP 30" in md
    assert "## 四、岗位样本" in md
    assert "## 五、已剔除的弱相关岗位" in md
    # 弱相关清单含房产销售经理
    assert "房产销售经理" in md


def test_build_market_report_deterministic():
    jobs = [_fake_job("AI项目经理"), _fake_job("AI测试工程师")]
    md1, _ = build_market_report(jobs, [], [], [], [], [("Python", 2)], {"python": "技术-开发"})
    md2, _ = build_market_report(jobs, [], [], [], [], [("Python", 2)], {"python": "技术-开发"})
    assert md1 == md2

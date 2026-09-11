# -*- coding: utf-8 -*-
"""核心报告层（core/report）

统一「匹配报告」与「市场报告」两份 markdown 生成逻辑的唯一真相源：
- build_report       ← 原 match_report.build_report（逐岗匹配排序报告）
- build_market_report ← 原 match_report.build_market_report（市场热词画像报告）

本模块只依赖 core.analyze（keyword_gap / is_tech_job），不反向依赖适配器。
输出 markdown 章节与现状逐字一致。
"""

from collections import Counter
from datetime import date

from core.analyze import keyword_gap, is_tech_job, _salary_stats

import re  # noqa: E402


# ── 市场统计章节（两种报告共用） ──────────────────────

def _market_sections(jobs):
    """薪资/经验/学历市场统计(市场报告与匹配报告共用)。返回 md 行列表, 三级标题。"""
    from core.analyze import _dist_table
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


def _job_sample_lines(jobs, top=None):
    """岗位样本行: 应届/校招岗排最后, 其余按HR活跃度; JD全文折叠块缩进到列表项内。

    top=None → 显示全部进入统计的岗位(默认行为, 不再丢岗位);
    top=N   → 报告过大时手动限前 N 个。
    """
    ordered = sorted(jobs, key=_hr_sort_key)
    ordered = [j for j in ordered if not _is_campus_job(j)] + [j for j in ordered if _is_campus_job(j)]
    if top is not None:
        ordered = ordered[:top]
    out = []
    for i, j in enumerate(ordered[:top], 1):
        title = j["job_title"]
        url = (j.get("job_url") or "").strip()
        head = f"[{title}]({url})" if url else title
        if _is_campus_job(j):
            head += "　**🎓应届/校招**"
        active = (j.get("hr_active_label") or "").strip() or "活跃度未知"
        out.append(f"{i}. {head} · **{j.get('company') or ''}** · {j.get('salary') or ''} · HR:{active}")
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


def _skill_contexts(jobs, top_skills, per_skill=3):
    """Top技能在JD中的原文要求摘录: 抽含该技能词的句子, 按公司分组(组内去重)。

    返回 {skill: [(company, [sent, ...]), ...]} —— 每个公司一个组, 组内是该公司的相关句子,
    便于对比不同公司对同一技能的要求差异。
    """
    from core.analyze import _term_hit
    result = {}
    for skill, _n in top_skills:
        by_company = {}
        for j in jobs:
            company = (j.get("company") or "未知公司").strip()
            sents = []
            seen = set()
            text = (j.get("description") or "") + "\n" + (j.get("job_title") or "")
            for sent in re.split(r"[。；;！？\n]", text):
                sent = sent.strip()
                if not (8 <= len(sent) <= 100):
                    continue
                if not _term_hit(skill, sent):
                    continue
                key = sent[:30]
                if key in seen:
                    continue
                seen.add(key)
                sents.append(sent)
            if sents:
                # 同名公司(不同岗位)合并
                if company in by_company:
                    by_company[company].extend(sents)
                else:
                    by_company[company] = sents
        groups = list(by_company.items())
        if per_skill and sum(len(s) for _, s in groups) > per_skill:
            # 限量模式: 按公司顺序截断
            kept, total = [], 0
            for c, sents in groups:
                if total >= per_skill:
                    break
                kept.append((c, sents))
                total += len(sents)
            groups = kept
        if groups:
            result[skill] = groups
    return result


# ── 匹配报告 ──────────────────────────────────────────

def build_report(results, resume, mode, non_tech_jobs=None, dir_weak_jobs=None):
    today = date.today().isoformat()
    scores = [int(r.get("match_score") or 0) for _, r in results]
    lines = [f"# 简历-JD 匹配报告 · {today}", ""]
    non_tech_jobs = non_tech_jobs or []
    extra = " · 软件向" if (results and is_tech_job(results[0][0])) and False else ""  # 头部由 main 的 suffix 控制, 这里不强加
    scope_note = ""
    # 软件向过滤生效且存在过滤时, 头部明确
    if non_tech_jobs:
        scope_note = f" · 剔除非软件 {len(non_tech_jobs)} 个(软件向过滤)"
    lines.append(f"> 分析模式: **{mode}** · 岗位数: **{len(results)}** · 平均分: **{sum(scores)//max(len(scores),1)}**{scope_note}")
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

    if dir_weak_jobs:
        lines.append("## 五、已剔除的方向弱相关岗位(命中本方向 require/exclude, 未参与打分) · 全部列出")
        lines.append("")
        for i, j in enumerate(dir_weak_jobs, 1):
            title = j.get("job_title") or ""
            url = (j.get("job_url") or "").strip()
            head = f"[{title}]({url})" if url else title
            reason = j.get("_kw_status") or "—"
            lines.append(f"{i}. {head} · {j.get('company') or ''} · {j.get('salary') or ''} · 命中规则: {reason}")
        lines.append("")
        lines.append("> 判定规则：已过软件向过滤，但被本方向 `require/exclude` 判为弱相关（如 AI 项目经理方向剔除 AI产品经理/AI售前工程师）。")
        lines.append("")

    if non_tech_jobs:
        lines.append("## 六、已过滤的非软件岗位(软件向过滤, 未参与打分) · 全部列出")
        lines.append("")
        for i, j in enumerate(non_tech_jobs, 1):
            title = j.get("job_title") or ""
            url = (j.get("job_url") or "").strip()
            head = f"[{title}]({url})" if url else title
            lines.append(f"{i}. {head} · {j.get('company') or ''} · {j.get('salary') or ''}")
        lines.append("")
        lines.append("> 判定规则：标题或JD前500字命中软件/互联网/IT/信息化/数字化/SaaS/AI/系统集成/研发/产品/云计算/科技等领域词才算软件向；建筑/制造/金融/政务等行业词只在标题/公司名生效。")
        lines.append("> 软件向过滤默认开启：建筑/制造/金融/政务等行业的 PM 岗不参与打分，单独列在此节可复核；想纳入全部请加 `--include-non-tech`。")
        lines.append("")
        lines.append("---")
    else:
        lines.append("---")
    lines.append(f"*生成于 {today} · lakejobai-job-radar core/report · 前提: 简历内容真实, 不建议堆砌未掌握的关键词*")
    return "\n".join(lines), today


def build_market_report(jobs, weak_jobs, noise_jobs, dir_weak_jobs, non_tech_jobs, freq, cats):
    """市场模式报告：市场画像（薪资/经验/学历 + 热词 + 分类 + 样本 + 剔除清单），不涉及简历。

    所有"剔除/过滤"清单均完整展示(不截断), 方便用户复核。
    """
    from core.analyze import _salary_stats
    today = date.today().isoformat()
    total = len(jobs)
    lines = [f"# 市场热词报告 · {today}", ""]
    has_non_tech = bool(non_tech_jobs)
    extra = " · 软件向" if has_non_tech else ""
    lines.append(
        f"> 强相关样本: **{total} 个岗位**(JD全文) · 剔除弱相关 **{len(weak_jobs)}** 个(标题不含关键词词根) · "
        f"剔除方向不符 **{len(dir_weak_jobs)}** 个(require/exclude)"
        + (f" · 剔除噪音 **{len(noise_jobs)}** 个(标题黑名单)" if noise_jobs else "")
        + (f" · 剔除非软件 **{len(non_tech_jobs)}** 个(软件向过滤)" if has_non_tech else "")
        + f" · 提取技能词 **{len(freq)}** 个 · 市场模式(未导入简历, 仅统计市场需求){extra}"
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

    # 全部技能的JD原文要求(按公司分组, 后续可接LLM做智能归纳)
    ctx = _skill_contexts(jobs, freq[:30], per_skill=0)

    lines.append("## 二、热门技能词 TOP 30（含 JD 原文要求，按公司分组，点击展开）")
    lines.append("")
    lines.append("| # | 技能 | 出现岗位数 | 占比 | 类别 | JD 原文要求 |")
    lines.append("|---|------|-----------|------|------|-------------|")
    for i, (s, n) in enumerate(freq[:30], 1):
        groups = ctx.get(s) or []
        if groups:
            n_all = sum(len(sents) for _, sents in groups)
            parts = []
            for gi, (c, sents) in enumerate(groups):
                items = "".join(f"<br>{qi}. {sent}" for qi, sent in enumerate(sents, 1))
                # 公司标题前也换行(第一组除外, 其后与上一组隔开), 标题后接第一句前有<br>
                lead = "<br>" if gi > 0 else ""
                parts.append(f"{lead}<b>【{c}】</b>{items}")
            jd_cell = f"<details><summary>展开 {len(groups)} 家公司 / {n_all} 条</summary>" + "".join(parts) + "</details>"
        else:
            jd_cell = "—"
        lines.append(f"| {i} | {s} | {n} | {n * 100 // max(total, 1)}% | {cats.get(s, '')} | {jd_cell} |")
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
        lines.append("## 五、已剔除的弱相关岗位(标题不含关键词词根, 未参与统计) · 全部列出")
        lines.append("")
        lines.append("| # | 岗位 | 公司 | 薪资 | 入库 kw | 说明 |")
        lines.append("|---|------|------|------|---------|------|")
        for i, j in enumerate(weak_jobs, 1):
            title = j.get("job_title") or ""
            url = (j.get("job_url") or "").strip()
            head = f"[{title}]({url})" if url else title
            kw = (j.get("search_kw") or "").strip() or "—"
            if j.get("_kw_backfilled"):
                kw = f"{kw} *(反推)*"
            reason = j.get("_kw_status") or "—"
            lines.append(f"| {i} | {head} | {j.get('company') or ''} | {j.get('salary') or ''} | {kw} | {reason} |")
        lines.append("")
        lines.append("> 判定规则：每个岗位按**入库时记录的 search_kw**核对当前报告关键词；不在其中且标题也不含词根则视为弱相关，常见两种情况：")
        lines.append("> 1. **属其他 kw 报告**：该岗位是按其它关键词搜进来的，应在另一份报告里分析；")
        lines.append("> 2. **同 kw 召回的相邻岗**：BOSS 搜索引擎把相邻岗位（如搜『AI 项目经理』召回『产品经理』）也带了回来。")
        lines.append("> 旧数据（search_kw 为空）按采集时间窗 + `search_url_history` 反推；标 *(反推)* 表示。反推也得不到的标 `未追溯到入库 kw`。")
        lines.append("")

    if dir_weak_jobs:
        lines.append("## 六、已剔除的方向弱相关岗位(命中本方向 require/exclude, 未参与统计) · 全部列出")
        lines.append("")
        lines.append("| # | 岗位 | 公司 | 薪资 | 入库 kw | 命中规则 |")
        lines.append("|---|------|------|------|---------|----------|")
        for i, j in enumerate(dir_weak_jobs, 1):
            title = j.get("job_title") or ""
            url = (j.get("job_url") or "").strip()
            head = f"[{title}]({url})" if url else title
            kw = (j.get("search_kw") or "").strip() or "—"
            if j.get("_kw_backfilled"):
                kw = f"{kw} *(反推)*"
            reason = j.get("_kw_status") or "—"
            lines.append(f"| {i} | {head} | {j.get('company') or ''} | {j.get('salary') or ''} | {kw} | {reason} |")
        lines.append("")
        lines.append("> 判定规则：该岗位已过『标题含关键词词根 / 属本方向 search_kw』的相关性初筛，但被本方向的 `require/exclude` 规则判为弱相关。")
        lines.append("> 典型来源：BOSS 把相邻岗位（如搜『AI 项目经理』召回『AI 产品经理』『AI 售前工程师』）一并带回；规则只认『方向必要词 + 不命中排除词』。")
        lines.append("> `keep` 例外可救回被 exclude 误伤的合法岗（如 车载AI产品经理 / AI交付领域经理 / AI研发技术经理）。")
        lines.append("")

    if noise_jobs:
        lines.append("## 七、已过滤的疑似无关岗位(标题命中黑名单, 未参与统计) · 全部列出")
        lines.append("")
        for i, j in enumerate(noise_jobs, 1):
            title = j.get("job_title") or ""
            url = (j.get("job_url") or "").strip()
            head = f"[{title}]({url})" if url else title
            lines.append(f"{i}. {head} · {j.get('company') or ''} · {j.get('salary') or ''}")
        lines.append("")
        lines.append("> 黑名单默认: 销售/投资/合伙人/猎头/讲师/加盟/招商/渠道/保险/房产等; 可在 Web 设置页 `noise_filter_keywords` 追加自定义词(逗号分隔)。")
        lines.append("")

    if non_tech_jobs:
        lines.append("## 八、已过滤的非软件岗位(软件向过滤, 未参与统计) · 全部列出")
        lines.append("")
        for i, j in enumerate(non_tech_jobs, 1):
            title = j.get("job_title") or ""
            url = (j.get("job_url") or "").strip()
            head = f"[{title}]({url})" if url else title
            lines.append(f"{i}. {head} · {j.get('company') or ''} · {j.get('salary') or ''}")
        lines.append("")
        lines.append("> 判定规则：标题或 JD 前 500 字命中 软件/互联网/IT/信息化/数字化/SaaS/AI/系统集成/研发/产品/云计算/科技 等领域词才算软件向。")
        lines.append("> 软件向过滤默认开启：建筑/制造/金融/政务等行业的 PM 岗不参与统计，单独列在此节可复核；想纳入全部请加 `--include-non-tech`。")
        lines.append("")

    lines.append("---")
    lines.append(f"*生成于 {today} · 市场模式 · 后续用 --resume-file 导入简历后再次运行, 即自动切换为逐岗匹配排序*")
    return "\n".join(lines), today

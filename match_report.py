# -*- coding: utf-8 -*-
"""
简历-JD 匹配报告生成器（编排层）
==========================

本文件只负责「采集 + 命令行编排」。分析/报告逻辑已下沉到 core：
- 分析/过滤：core.analyze（analyze_market / analyze_match / 方向过滤 / 软件向 / 宽词展开）
- 报告生成：core.report（build_report / build_market_report）

用法(在项目根目录):
  # 0. 首次: 扫码登录(只需一次)
  python boss_firefox.py --login

  # 1. (可选) 导入简历文本到设置页 resume_summary
  python match_report.py --resume-file 我的简历.txt

  # 2. 采集 + 分析 + 报告
  python match_report.py --keywords "AI Agent,Linux运维" --city 广州 --per-query 20 --max-total 50

  # 只分析库中已有 JD, 不再采集
  python match_report.py --report-only

  # 只刷新关键词缺口(不调LLM, 零成本)
  python match_report.py --report-only --keyword-only

  # 软件向过滤: 默认开启(无需加参数), 想看全部才加 --include-non-tech
  python match_report.py --keywords "项目经理" --city 武汉           # 默认只统计软件/互联网 PM
  python match_report.py --keywords "项目经理" --city 武汉 --include-non-tech   # 含建筑/制造/金融/政务 PM

  # 只看本次入库的岗位(默认近 60 分钟, 配合 --report-only 复盘单次采集)
  python match_report.py --report-only --only-new --new-since-minutes 60

说明:
  - 有 DeepSeek API Key(设置页配置)时逐岗打分; 没有则自动降级为关键词覆盖率打分
  - LLM 结果缓存在 .boss_profile/match_cache.json, 简历变更后自动重新分析
  - ⚠️ 本脚本仍直接开浏览器采集；与 Web 控制台共用同一 Firefox profile，互斥。
    决策③：本脚本将于 P2 废弃，采集改走 `lakejob collect`（CLI 脚本模式 → Web API）。
"""

import argparse
import json
import sys
import time
import urllib.request
import urllib.error
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

from boss_firefox import BossScraper, pause  # noqa: E402
from boss_state import (  # noqa: E402
    get_setting,
    set_setting,
    get_db,
    add_application,
    get_application_by_url,
    update_application_from_job,
)
# 分析 / 报告逻辑统一到 core（单一真相源），本文件不再重复实现
from core.analyze import (  # noqa: E402
    analyze_market,
    analyze_match,
    expand_search_kws as _expand_search_kws,
    all_search_kws as _all_search_kws,
    _has_resume,
)
from core.report import build_report, build_market_report  # noqa: E402


def _norm_url(u: str) -> str:
    return (u or "").split("?")[0].rstrip("/")


def _check_web_console_conflict(port: int = 8010):
    """采集前检查 Web 控制台是否正占用同一个 Firefox profile。

    - 8010 端口无响应 → 静默通过（控制台没跑）
    - 端口有响应但 browser_running=False → 通过（控制台在但没启浏览器, 不冲突）
    - 端口有响应且 browser_running=True → 报错退出, 给出明确操作步骤
    """
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/status", method="GET")
        with urllib.request.urlopen(req, timeout=1) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, ConnectionRefusedError, OSError, json.JSONDecodeError, ValueError):
        return  # 控制台没跑 / 不响应 / 返回非 JSON, 都视为不冲突
    if not data.get("browser_running"):
        return
    print("=" * 64, file=sys.stderr)
    print("❌ 检测到 Web 控制台 (boss_app.py) 正在运行且浏览器已启动", file=sys.stderr)
    print("=" * 64, file=sys.stderr)
    print("match_report.py 和 boss_app.py 共用同一个 Firefox profile (.boss_profile/),", file=sys.stderr)
    print("同时启 Firefox 会报 'Failed to launch the browser process'。", file=sys.stderr)
    print("", file=sys.stderr)
    print("请按以下任一方式解决:", file=sys.stderr)
    print("  1) Web 控制台「设置」页 → 点击「停止浏览器」(只关浏览器, 服务还在)", file=sys.stderr)
    print("  2) Ctrl+C 关闭 boss_app 服务(连浏览器一起关)", file=sys.stderr)
    print("  3) 改用 Web 控制台完成本次扫描投递, 不要跑这个脚本", file=sys.stderr)
    print("", file=sys.stderr)
    print("确认 Web 控制台浏览器已关后, 重新跑本命令。", file=sys.stderr)
    print("=" * 64, file=sys.stderr)
    sys.exit(1)


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


def collect(keywords, cities, per_query, max_total, headless, refresh):
    """搜索 + 详情采集, 结果入库。每个 城市×关键词 组合各采 per_query 条。
    返回 (n_saved, search_urls): search_urls = [{'kw','city','url'}], 供报告记录采集方法。"""
    _check_web_console_conflict()  # 启 Firefox 前先确认没和 Web 控制台撞 profile
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
                # 记录实际搜索URL(与 BOSS 页面原生格式一致: /jobs, city在前), 持久化到DB
                from urllib.parse import quote_plus as _qp
                su = {
                    "kw": kw, "city": city or "全国",
                    "url": f"https://www.zhipin.com/web/geek/jobs?city={city_code}&query={_qp(kw)}",
                    "ts": time.time(),  # 旧数据反推 search_kw 的唯一时间锚
                }
                search_urls.append(su)
                try:
                    import json as _json
                    history = _json.loads(get_setting("search_url_history") or "[]")
                except Exception:
                    history = []
                # 历史里可能存在不带 ts 的老条目, 直接按 (kw, city, url) 判重
                if not any(h.get("kw") == su["kw"] and h.get("city") == su["city"]
                           and h.get("url") == su["url"] for h in history):
                    history.append(su)
                set_setting("search_url_history", _json.dumps(history, ensure_ascii=False))
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
                            j["hr_active_label"] = d.get("hr_active_label", "")
                            j["hr_active_days"] = d.get("hr_active_days", -1)
                        except Exception as e:
                            print(f"  详情失败: {e}")
                        pause(1.5, 3.0)
                    j["url"] = url
                    # 记录"该岗位用哪个 kw 搜出来的", 报告层按 kw 隔离分析
                    j["search_kw"] = kw
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


def main():
    ap = argparse.ArgumentParser(description="简历-JD匹配报告")
    ap.add_argument("--keywords", default="AI项目经理", help="搜索关键词, 逗号分隔可多个; 默认 AI项目经理")
    ap.add_argument("--search-kw", default=None,
                    help="报告的分析方向(决定拉哪些 search_kw 的岗位). "
                         "默认从 --keywords 第一个取值; 显式传 'none' 表示不隔离(分析所有岗位). "
                         "宽词自动包含窄词, 例如 项目经理 ⊃ AI项目经理(反向不成立).")
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
    ap.add_argument("--include-non-tech", action="store_true",
                    help="把非软件/互联网岗位(建筑/制造/金融/政务等)也纳入统计. "
                         "**默认只统计软件向岗位, 无需加任何参数**; 加此开关才会包含全部.")
    ap.add_argument("--only-new", action="store_true",
                    help="只分析最近 --new-since-minutes 分钟内入库的岗位(配合 --report-only 复盘单次采集结果).")
    ap.add_argument("--new-since-minutes", type=int, default=60,
                    help="--only-new 的时间窗口(分钟), 默认 60.")
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
    # 软件向过滤默认开启(无需参数), 加 --include-non-tech 才关掉
    tech_only = not args.include_non_tech
    # 单 kw 报告隔离: 默认取 --keywords 第一个; 显式 'none' 表示不隔离
    if args.search_kw is None:
        search_kw_iso = kws_used[0] if kws_used else None
    elif args.search_kw.lower() == "none":
        search_kw_iso = None
    else:
        search_kw_iso = args.search_kw.strip()
    # 宽词 ⊃ 窄词: 展开出本次报告应纳入的 search_kw 集合(如 项目经理 ⊃ AI项目经理)
    search_kws_iso = _expand_search_kws(search_kw_iso, _all_search_kws()) if search_kw_iso else None
    if search_kws_iso and len(search_kws_iso) > 1:
        print(f"[范围] 「{search_kw_iso}」为宽词, 纳入 {sorted(search_kws_iso)} 的岗位")
    # 文件名: 隔离模式下只用该 kw, 不用 + 连接, 避免误导
    kw_part = search_kw_iso if search_kw_iso else "+".join(kws_used)[:30]
    city_part = "+".join(cities_used)[:20]
    y, m, d = date.today().strftime("%Y-%m-%d").split("-")
    suffix_parts = []
    if tech_only:
        suffix_parts.append("软件向")
    scope_label = None
    if args.report_only and args.only_new:
        scope_label = f"本次{int(args.new_since_minutes)}分钟"
        suffix_parts.append(scope_label)
    suffix_str = "·" + "·".join(suffix_parts) if suffix_parts else ""
    report_title = f"{y}年{m}月{d}日-{kw_part}（{city_part}）{suffix_str}"

    # 采集方法章节: 本次采集的实时URL优先; --report-only 时读DB持久化的历史搜索URL
    if not search_urls:
        try:
            history = json.loads(get_setting("search_url_history") or "[]")
        except Exception:
            history = []
        # 与本次报告相关的关键词/城市优先, 其他历史组合也列出(库是累计的)
        rel = [h for h in history if any(k in h.get("kw", "") or h.get("kw", "") in k for k in kws_used)]
        other = [h for h in history if h not in rel]
        search_urls = rel + other
    method_lines = ["## 采集方法", ""]
    if search_urls:
        for su in search_urls:
            method_lines.append(f"- 关键词「{su['kw']}」@ {su['city']}：[{su['url']}]({su['url']})")
        method_lines.append("")
        method_lines.append(
            "> 搜索词直接交给 BOSS 直聘搜索引擎做相关性召回（非标题精确匹配），"
            "结果会包含平台认为相关的岗位（如产品经理/售前/总经理类）；"
            "报告已按相关性+黑名单双重过滤，被剔除岗位在各清单单列可复核。"
            "链接需在已登录 BOSS 的浏览器中打开。"
        )
    else:
        method_lines.append(f"- 数据采集于早期版本（未记录搜索URL），本次报告关键词：{'、'.join(kws_used)}")
    method_lines.append("")

    if market:
        if not args.market:
            print("[提示] 未检测到有效简历, 自动进入市场模式(仅统计JD热词); 导入简历后自动切换为匹配模式")
        since = int(args.new_since_minutes) if (args.report_only and args.only_new) else None
        jobs, weak_jobs, noise_jobs, dir_weak_jobs, non_tech_jobs, freq, cats = analyze_market(
            args.limit, kws_used, tech_only=tech_only, since_minutes=since,
            search_kws=search_kws_iso, direction=search_kw_iso,
        )
        report, today = build_market_report(jobs, weak_jobs, noise_jobs, dir_weak_jobs, non_tech_jobs, freq, cats)
        report = report.replace(f"# 市场热词报告 · {today}", f"# 市场热词报告 · {report_title}", 1)
        report = report.replace("## 一、市场画像", "\n".join(method_lines) + "## 一、市场画像", 1)
        summary = (
            f"强相关{len(jobs)}个岗位(剔除弱相关{len(weak_jobs)}个、方向不符{len(dir_weak_jobs)}个、噪音{len(noise_jobs)}个"
            + (f"、非软件{len(non_tech_jobs)}个" if non_tech_jobs else "")
            + ")"
            + (f", 热词TOP1: {freq[0][0]}({freq[0][1]}个岗位)" if freq else "")
        )
    else:
        since = int(args.new_since_minutes) if (args.report_only and args.only_new) else None
        results, resume, mode, non_tech_jobs, dir_weak_jobs = analyze_match(
            args.limit, args.keyword_only, tech_only=tech_only, since_minutes=since,
            search_kws=search_kws_iso, direction=search_kw_iso,
        )
        if not results:
            print("没有可分析的结果")
            sys.exit(1)
        report, today = build_report(results, resume, mode, non_tech_jobs, dir_weak_jobs)
        report = report.replace(f"# 简历-JD 匹配报告 · {today}", f"# 简历-JD 匹配报告 · {report_title}", 1)
        summary = f"共{len(results)}个岗位, Top1: {results[0][0]['job_title']}({results[0][1].get('match_score')}分)"

    # 输出到项目 reports/ 目录; 同名文件直接覆盖(不追加)
    out_dir = ROOT / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{report_title}.md"
    out.write_text(report, encoding="utf-8")
    print(f"\n[完成] 报告已生成(覆盖写): {out}")
    print(f"        {summary}")


if __name__ == "__main__":
    main()

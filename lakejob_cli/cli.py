"""lakejob CLI — BOSS直聘岗位雷达命令行工具."""

import json
import os
import sys
import click

from . import client, output

# schema 由能力注册表（唯一真相源 core/capabilities.py）生成，不再手搓
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.capabilities import generate_schema_dict


@click.group()
def main():
    """lakejob — BOSS直聘岗位雷达 v0.1.0

    命令返回结构化 JSON 到 stdout，Agent 友好。
    """


# ── 版本 ──
@main.command("version")
def version_cmd():
    output.emit(output.ok("version", data={"version": "0.1.0"}))


# ── Schema：AI Agent 工具描述 ──
@main.command("schema")
def schema_cmd():
    # schema 由 core/capabilities.py（唯一真相源）实时生成
    schema = generate_schema_dict(include_dead=True)
    output.emit(output.ok("schema", data=schema))


# ── 搜索 ──
@main.command("search")
@click.argument("keyword")
@click.option("--city", default="", help="城市名（空则使用设置中的默认城市）")
@click.option("--welfare", default=None, help="福利筛选 如 双休,五险一金")
@click.option("--count", type=int, default=60, help="返回条数上限")
def search_cmd(keyword, city, welfare, count):
    """搜索BOSS直聘岗位。"""
    payload = {"keyword": keyword, "city": city or "", "limit": count}
    if welfare:
        payload["welfare"] = welfare
    resp = client.search(keyword, city, count)
    result = output.ok_or_fail(resp, "search")
    output.emit(result)


# ── 状态 ──
@main.command("status")
def status_cmd():
    resp = client.status()
    result = output.ok_or_fail(resp, "status")
    output.emit(result)


# ── 投递漏斗 ──
@main.command("stats")
def stats_cmd():
    resp = client.stats()
    result = output.ok_or_fail(resp, "stats")
    output.emit(result)


# ── 岗位列表 ──
@main.command("jobs")
@click.option("--status", "filter_status", default=None, help="pending / applied / replied")
@click.option("--limit", type=int, default=50)
def jobs_cmd(filter_status, limit):
    resp = client.jobs(filter_status, limit)
    result = output.ok_or_fail(resp, "jobs")
    output.emit(result)


# ── 投递单个 ──
@main.command("apply")
@click.argument("job_url")
def apply_cmd(job_url):
    resp = client.apply_one(job_url)
    result = output.ok_or_fail(resp, "apply")
    output.emit(result)


# ── 批量投递 ──
@main.command("apply-batch")
@click.option("--status", "filter_status", default="pending", help="pending 等状态")
def apply_batch_cmd(filter_status):
    r = client.jobs(filter_status, limit=200)
    if r.is_error:
        output.emit(output.fail("apply-batch", f"fetch jobs failed: {r.status_code}"))
        return
    jobs_list = r.json().get("jobs", [])
    urls = [j["job_url"] for j in jobs_list if j.get("job_url")]
    if not urls:
        output.emit(output.fail("apply-batch", "no job_urls found"))
        return
    resp = client.apply_batch(urls)
    result = output.ok_or_fail(resp, "apply-batch")
    output.emit(result)


# ── 扫描当前页面 ──
@main.command("scan")
def scan_cmd():
    """扫描当前BOSS搜索结果页，提取所有可见岗位。"""
    resp = client.scan()
    result = output.ok_or_fail(resp, "scan")
    output.emit(result)


# ── 扫描并一键投递 ──
@main.command("scan-apply")
def scan_apply_cmd():
    """扫描当前页面全部岗位并一键批量投递。"""
    resp = client.scan_and_apply()
    result = output.ok_or_fail(resp, "scan-apply")
    output.emit(result)


# ── 会话列表 ──
@main.command("conversations")
def conversations_cmd():
    resp = client.conversations()
    result = output.ok_or_fail(resp, "conversations")
    output.emit(result)


# ── 聊天记录 ──
@main.command("chat")
@click.argument("conv_id", type=int)
def chat_cmd(conv_id):
    resp = client.chat_messages(conv_id)
    result = output.ok_or_fail(resp, "chat")
    output.emit(result)


# ── 手动发消息 ──
@main.command("send")
@click.argument("conv_id", type=int)
@click.option("--msg", required=True, help="消息内容")
def send_cmd(conv_id, msg):
    resp = client.send_message(conv_id, msg)
    result = output.ok_or_fail(resp, "send")
    output.emit(result)


# ── 诊断 ──
@main.command("doctor")
def doctor_cmd():
    resp = client.doctor()
    result = output.ok_or_fail(resp, "doctor")
    output.emit(result)


# ── 扫码登录 ──
@main.command("login")
def login_cmd():
    resp = client.relogin()
    result = output.ok_or_fail(resp, "login")
    output.emit(result)


# ── AI JD分析 ──
@main.command("analyze")
@click.argument("job_url")
@click.option("--title", default="", help="岗位名称")
@click.option("--company", default="", help="公司名")
@click.option("--desc", default="", help="JD描述")
def analyze_cmd(job_url, title, company, desc):
    resp = client.analyze(job_url, title, company, desc)
    output.emit(output.ok_or_fail(resp, "analyze"))


# ── 候选池 ──
@main.command("shortlist")
@click.argument("action", type=click.Choice(["list", "add", "remove"]))
@click.option("--job-url", help="岗位URL")
@click.option("--title", default="", help="岗位名称")
@click.option("--company", default="", help="公司名")
@click.option("--id", "sid", type=int, help="shortlist ID")
def shortlist_cmd(action, job_url, title, company, sid):
    if action == "list":
        resp = client.get_shortlists()
        output.emit(output.ok_or_fail(resp, "shortlist"))
    elif action == "add":
        if not job_url:
            output.emit(output.fail("shortlist", "--job-url required"))
            return
        resp = client.add_shortlist(job_url, title, company)
        output.emit(output.ok_or_fail(resp, "shortlist"))
    elif action == "remove":
        if not sid:
            output.emit(output.fail("shortlist", "--id required"))
            return
        resp = client.remove_shortlist(sid)
        output.emit(output.ok_or_fail(resp, "shortlist"))


# ── 服务管理 ──
@main.command("server")
@click.option("--start", is_flag=True, help="启动后台服务")
@click.option("--stop", is_flag=True, help="停止后台服务（精确杀 boss_app 进程，不动其他 python）")
@click.option("--port", type=int, default=8010, help="服务端口")
def server_cmd(start, stop, port):
    import subprocess, os

    project_dir = os.path.dirname(os.path.dirname(__file__))
    if not os.path.exists(os.path.join(project_dir, "boss_app.py")):
        project_dir = os.environ.get("LAKEJOB_PROJECT", r"D:\lake\jiaoben\job\lakejobai-job-radar-main")

    if start:
        cmd = ["python", os.path.join(project_dir, "boss_app.py"), "--port", str(port)]
        subprocess.Popen(cmd, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000), cwd=project_dir)
        output.emit(output.ok("server", data={"status": "started", "url": f"http://127.0.0.1:{port}"}))
    elif stop:
        killed = _kill_boss_app()
        output.emit(output.ok("server", data={"status": "stopped", "killed": killed}))
    else:
        resp = client.status()
        if resp.is_error:
            output.emit(output.ok("server", data={"status": "not running"}))
        else:
            output.emit(output.ok("server", data={"status": "running"}))


@main.command("restart")
@click.option("--port", type=int, default=8010, help="端口号")
def restart_cmd(port):
    """杀旧进程 + 起新服务。Windows 用 wmic 精确杀，不动其他 python。"""
    import subprocess, os, time, urllib.request

    project_dir = os.path.dirname(os.path.dirname(__file__))
    if not os.path.exists(os.path.join(project_dir, "boss_app.py")):
        project_dir = os.environ.get("LAKEJOB_PROJECT", r"D:\lake\jiaoben\job\lakejobai-job-radar-main")

    boss_py = os.path.join(project_dir, "boss_app.py")
    if not os.path.exists(boss_py):
        output.emit(output.fail("restart", f"找不到 boss_app.py"))
        return

    killed = _kill_boss_app()
    if killed:
        click.echo(f"  killed {killed} process(es)")
    time.sleep(2)

    log_path = os.path.join(os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp", "boss_app.log")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    with open(log_path, "w", encoding="utf-8") as lf:
        subprocess.Popen(
            [sys.executable, boss_py, "--port", str(port)],
            stdout=lf,
            stderr=subprocess.STDOUT,
            cwd=project_dir,
            creationflags=flags,
        )

    time.sleep(5)
    try:
        urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/api/health"), timeout=3)
        output.emit(output.ok("restart", data={"port": port, "url": f"http://127.0.0.1:{port}"}))
    except Exception:
        output.emit(output.ok("restart", data={"port": port, "url": f"http://127.0.0.1:{port}", "note": "稍等再试"}))


def _kill_boss_app():
    """精确杀死所有 boss_app.py 主进程（python 解释器执行 boss_app.py 的）。
    只匹配 cmdline 中包含 'boss_app.py' 的 python 进程，避免误杀任何包含 'boss_app' 字样的 shell。
    返回杀死数。
    """
    killed = 0
    try:
        import psutil  # type: ignore
    except Exception:
        psutil = None

    if psutil is not None:
        my_pid = os.getpid()
        for p in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                pid = p.info.get("pid")
                if pid == my_pid:
                    continue
                cmd = p.info.get("cmdline") or []
                if not isinstance(cmd, list):
                    continue
                name = (p.info.get("name") or "").lower()
                if "python" not in name and not any("python" in (a or "").lower() for a in cmd[:1]):
                    continue
                if not any("boss_app.py" in (a or "") for a in cmd):
                    continue
                p.kill()
                killed += 1
            except Exception:
                continue
        return killed

    # 兜底：用 wmic（旧 Windows 系统），但限定 commandline 必须含 boss_app.py
    import subprocess

    try:
        r = subprocess.run(
            "wmic process where \"name='python.exe' and commandline like '%%boss_app.py%%'\" get processid",
            capture_output=True,
            shell=True,
            text=True,
            timeout=10,
        )
        for line in r.stdout.split("\n"):
            line = line.strip()
            if line.isdigit() and int(line) != os.getpid():
                try:
                    subprocess.run(f"taskkill /F /PID {line}", capture_output=True, shell=True, timeout=5)
                    killed += 1
                except Exception:
                    pass
    except Exception:
        pass
    return killed


# ── 智能投递 ──
@main.command("smart-send")
@click.option("--keyword", default="", help="搜索关键词")
@click.option("--city", default="", help="城市")
@click.option("--greeting", default="", help="自定义招呼语")
@click.option("--yes", "-y", is_flag=True, help="跳过确认")
@click.option("--districts", default="", help="多区 code 列表，逗号分隔，如 440118,440113")
@click.option("--company-size", default="", help="多规模 code 列表，逗号分隔，如 302,303")
def smart_send_cmd(keyword, city, greeting, yes, districts, company_size):
    """智能投递：搜索→按公司分组→挑最高HR→批量投递。"""
    if not keyword:
        output.emit(output.fail("smart-send", "--keyword 必填"))
        return
    ds_list = [x.strip() for x in districts.split(",") if x.strip()] or None
    cs_list = [x.strip() for x in company_size.split(",") if x.strip()] or None
    try:
        resp = client.company_preview(
            keyword=keyword,
            city=city,
            districts=ds_list,
            company_size=cs_list,
        )
        data = resp.json() if not resp.is_error else None
    except Exception as e:
        output.emit(output.fail("smart-send", f"preview 失败: {e}"))
        return
    if resp.is_error or not data or not data.get("ok"):
        output.emit(output.fail("smart-send", f"preview 失败: {(data or {}).get('message', '')}"))
        return

    companies = data.get("companies") or []
    output.emit(output.ok("smart-send-preview", data={"total_companies": len(companies), "keyword": keyword}))

    targets = []
    for c in companies[:20]:
        if c.get("already_applied"):
            continue
        tj = c.get("target_job") or {}
        if not tj.get("url"):
            continue
        top = c.get("top_hr") or {}
        targets.append(
            {
                "company": c["company"],
                "job_url": tj["url"],
                "hr_name": top.get("name", ""),
                "hr_title": top.get("title", ""),
                "is_boss": top.get("is_boss", False),
                "boss_confidence": top.get("boss_confidence", ""),
            }
        )

    if not targets:
        output.emit(output.fail("smart-send", "没有可投递的公司"))
        return

    if not yes:
        click.echo(f"\n  共 {len(targets)} 家公司待投递：")
        for t in targets:
            boss_tag = ""
            if t.get("is_boss"):
                conf = {"high": "★老板", "medium": "疑似老板", "low": "可能老板?"}.get(
                    t.get("boss_confidence", ""), "疑似老板"
                )
                boss_tag = f"  [{conf}]"
            click.echo(f"    {t['company']}  →  {t.get('hr_name') or 'HR'} ({t.get('hr_title', '')}){boss_tag}")
        click.echo("\n  确认？[y/N] ", nl=False)
        try:
            ans = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = "n"
        if ans not in ("y", "yes"):
            output.emit(output.ok("smart-send", data={"cancelled": True}))
            return

    resp2 = client.smart_send(company="", job_url="", targets=targets, confirm=True)
    result = output.ok_or_fail(resp2, "smart-send")
    try:
        payload = resp2.json()
        if isinstance(result.get("data"), dict) and isinstance(payload, dict):
            result["data"].update(payload)
    except Exception:
        pass
    output.emit(result)


# ── 采集（CLI 脚本模式 → Web API，不自开浏览器） ──
@main.command("collect")
@click.argument("keywords")
@click.option("--city", default="武汉", help="城市名，逗号分隔可多个")
@click.option("--count", type=int, default=60, help="每个 城市×关键词 组合采集条数")
@click.option("--no-details", is_flag=True, help="只采集岗位卡片(不含JD全文)，不补齐详情")
def collect_cmd(keywords, city, count, no_details):
    """采集岗位（CLI 脚本模式 → Web API，不自开浏览器）。搜索后自动补齐 JD 全文。"""
    kws = [k.strip() for k in keywords.split(",") if k.strip()]
    cities = [c.strip() for c in city.split(",") if c.strip()] or ["武汉"]
    saved_total = 0
    for c in cities:
        for kw in kws:
            resp = client.search(kw, c, count)
            if resp.is_error:
                output.emit(output.fail("collect", f"search {kw}@{c} 失败: HTTP {resp.status_code} {resp.text[:120]}"))
                return
            body = resp.json()
            saved = body.get("saved", 0) if isinstance(body, dict) else 0
            saved_total += saved
            click.echo(f"  [搜索] {kw} @ {c}: 入库 {saved}", err=True)
    if not no_details:
        resp = client.fetch_details(mode="empty", limit=max(200, count * len(kws) * len(cities) * 4))
        if resp.is_error:
            output.emit(output.fail("collect", f"fetch-details 失败: HTTP {resp.status_code}"))
            return
        d = resp.json()
        updated = d.get("updated", 0) if isinstance(d, dict) else 0
        click.echo(f"  [补齐JD] {updated} 个岗位", err=True)
    output.emit(output.ok("collect", data={"keywords": kws, "cities": cities, "saved_total": saved_total}))


# ── 批量报告（CLI 脚本模式，读 SQLite，不操作浏览器） ──
@main.group("batch")
def batch_group():
    """批量报告（读 SQLite 生成 md，不操作浏览器）。"""


def _report_title_and_method(kws_used, cities_used, tech_only, search_kw_iso, only_new, new_since_minutes):
    """复刻 match_report.main 的「文件名 + 采集方法章节」逻辑（已迁至此作为唯一来源）。"""
    from datetime import date
    from boss_state import get_setting
    import json as _json

    y, m, d = date.today().strftime("%Y-%m-%d").split("-")
    suffix_parts = []
    if tech_only:
        suffix_parts.append("软件向")
    if only_new:
        suffix_parts.append(f"本次{int(new_since_minutes)}分钟")
    suffix_str = "·" + "·".join(suffix_parts) if suffix_parts else ""
    kw_part = search_kw_iso if search_kw_iso else "+".join(kws_used)[:30]
    city_part = "+".join(cities_used)[:20] if cities_used else ""
    report_title = f"{y}年{m}月{d}日-{kw_part}（{city_part}）{suffix_str}"

    try:
        history = _json.loads(get_setting("search_url_history") or "[]")
    except Exception:
        history = []
    rel = [h for h in history if any(k in (h.get("kw", "") or "") for k in kws_used)]
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
    return report_title, method_lines


@batch_group.command("market-report")
@click.option("--keywords", default="", help="报告分析方向关键词(逗号分隔); 空则用全库")
@click.option("--city", default="", help="城市(仅用于报告文件名)")
@click.option("--limit", type=int, default=500)
@click.option("--include-non-tech", is_flag=True, help="纳入非软件向岗位(默认只统计软件向)")
@click.option("--only-new", is_flag=True, help="只看最近 --new-since-minutes 内入库的")
@click.option("--new-since-minutes", type=int, default=60)
@click.option("--search-kw", default=None, help="报告分析方向(单kw隔离); 默认取 --keywords 第一个; 'none'=不隔离")
@click.option("--out", default=None, help="输出文件(默认 reports/ 下按命名规则)")
def batch_market_report_cmd(keywords, city, limit, include_non_tech, only_new, new_since_minutes, search_kw, out):
    """生成市场热词报告（无简历，仅统计 JD 热词；读 SQLite，不操作浏览器）。"""
    from core.analyze import analyze_market, expand_search_kws, all_search_kws
    from core.report import build_market_report
    from pathlib import Path

    kws_used = [k.strip() for k in (keywords or "").split(",") if k.strip()]
    cities_used = [c.strip() for c in (city or "").split(",") if c.strip()]
    tech_only = not include_non_tech
    if search_kw is None:
        search_kw_iso = kws_used[0] if kws_used else None
    elif search_kw.lower() == "none":
        search_kw_iso = None
    else:
        search_kw_iso = search_kw.strip()
    search_kws_iso = expand_search_kws(search_kw_iso, all_search_kws()) if search_kw_iso else None
    if search_kws_iso and len(search_kws_iso) > 1:
        click.echo(f"[范围] 「{search_kw_iso}」为宽词, 纳入 {sorted(search_kws_iso)} 的岗位", err=True)

    since = int(new_since_minutes) if only_new else None
    jobs, weak_jobs, noise_jobs, dir_weak_jobs, non_tech_jobs, freq, cats = analyze_market(
        limit, kws_used or None, tech_only=tech_only, since_minutes=since,
        search_kws=search_kws_iso, direction=search_kw_iso,
    )
    report, today = build_market_report(jobs, weak_jobs, noise_jobs, dir_weak_jobs, non_tech_jobs, freq, cats)
    report_title, method_lines = _report_title_and_method(
        kws_used or ["全库"], cities_used, tech_only, search_kw_iso, only_new, new_since_minutes,
    )
    report = report.replace(f"# 市场热词报告 · {today}", f"# 市场热词报告 · {report_title}", 1)
    report = report.replace("## 一、市场画像", "\n".join(method_lines) + "## 一、市场画像", 1)
    summary = (
        f"强相关{len(jobs)}个岗位(剔除弱相关{len(weak_jobs)}个、方向不符{len(dir_weak_jobs)}个、噪音{len(noise_jobs)}个"
        + (f"、非软件{len(non_tech_jobs)}个" if non_tech_jobs else "")
        + (f", 热词TOP1: {freq[0][0]}({freq[0][1]}个岗位)" if freq else "")
    )
    out_path = Path(out) if out else (Path(__file__).parent.parent / "reports" / f"{report_title}.md")
    if not out:
        out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")
    click.echo(f"[完成] 报告已生成: {out_path}\n        {summary}", err=True)
    output.emit(output.ok("batch market-report", data={
        "report": str(out_path), "summary": summary, "strong": len(jobs), "weak": len(weak_jobs),
    }))


@batch_group.command("match-report")
@click.option("--limit", type=int, default=500)
@click.option("--keyword-only", is_flag=True, help="不调LLM, 只做关键词分析(零成本)")
@click.option("--include-non-tech", is_flag=True, help="纳入非软件向岗位(默认只统计软件向)")
@click.option("--only-new", is_flag=True, help="只看最近 --new-since-minutes 内入库的")
@click.option("--new-since-minutes", type=int, default=60)
@click.option("--search-kw", default=None, help="报告分析方向(单kw隔离); 'none'=不隔离")
@click.option("--out", default=None, help="输出文件(默认 reports/ 下按命名规则)")
def batch_match_report_cmd(limit, keyword_only, include_non_tech, only_new, new_since_minutes, search_kw, out):
    """生成简历-JD 匹配报告（读 SQLite + 可选 LLM；不操作浏览器）。"""
    from core.analyze import analyze_match, expand_search_kws, all_search_kws
    from core.report import build_report
    from pathlib import Path

    tech_only = not include_non_tech
    if search_kw is None or search_kw.lower() == "none":
        search_kw_iso = None
    else:
        search_kw_iso = search_kw.strip()
    search_kws_iso = expand_search_kws(search_kw_iso, all_search_kws()) if search_kw_iso else None
    since = int(new_since_minutes) if only_new else None
    results, resume, mode, non_tech_jobs, dir_weak_jobs = analyze_match(
        limit, keyword_only, tech_only=tech_only, since_minutes=since,
        search_kws=search_kws_iso, direction=search_kw_iso,
    )
    if not results:
        output.emit(output.fail("batch match-report", "没有可分析的结果（可能缺少简历或库为空）"))
        return
    report, today = build_report(results, resume, mode, non_tech_jobs, dir_weak_jobs)
    kws_used = [search_kw_iso] if search_kw_iso else ["全库"]
    report_title, method_lines = _report_title_and_method(
        kws_used, [], tech_only, search_kw_iso, only_new, new_since_minutes,
    )
    report = report.replace(f"# 简历-JD 匹配报告 · {today}", f"# 简历-JD 匹配报告 · {report_title}", 1)
    out_path = Path(out) if out else (Path(__file__).parent.parent / "reports" / f"{report_title}.md")
    if not out:
        out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")
    click.echo(f"[完成] 报告已生成: {out_path}", err=True)
    output.emit(output.ok("batch match-report", data={"report": str(out_path), "count": len(results)}))


if __name__ == "__main__":
    main()

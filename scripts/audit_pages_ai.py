#!/usr/bin/env python3
"""AI 页面审计收集器：渲染线上 Pages（JS 解析）→ 扫描条目准确性与可读性。

部署后由 Deploy Pages workflow 的 ai-audit job 调用：
  1. headless Chromium 打开 https://sim.jiucai.eu.org（三视图逐一渲染）
  2. 从 DOM 提取渲染后的表格行（JSON 化）
  3. 与 filtered.json/manifest.json 对比，扫描：
     - 渲染完整性（各视图行数 vs 数据行数）
     - 单元格异常（NaN/undefined/[object Object]/Infinity/负值）
     - 乱码（�/锟斤拷/GBK 残留）
     - 月租准确性（总价未折算残留 >1000、折算值一致性）
     - 宽带列结构（9 列、带宽格式、接入方式值域、计费周期完整性）
     - 重复条目（同 source+report_no）
     - 名称/备注可读性（超长、空备注比例）
     - 抽样 20 条：页面显示 vs 数据文件逐字段比对
  4. 输出 audit JSON 报告 + AI 审查 prompt（供 opencode CLI 审查）

用法：
  python scripts/audit_pages_ai.py --output report.json --prompt prompt.md [--base URL]
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from pathlib import Path
from typing import Any

# 注意：sim.jiucai.eu.org 当前仅 HTTP 可访问（HTTPS 证书对自定义域名无效——
# 2026-08-11 实测 ERR_CERT_COMMON_NAME_INVALID，与 verify_pages_ui.py 保持一致用 http）。
# 站点启用有效证书前不得改 https。
BASE = "http://sim.jiucai.eu.org"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"

BAD_CELL = re.compile(r"NaN|undefined|null|[oO]bject\s*Object|Infinity|-\d+(?:\.\d+)?$")
GARBAGE = re.compile(r"[\ufffd]|锟斤拷|â€|Ã[\x80-\xbf]")


def fetch_json(url: str, timeout: int = 30):
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def render_rows(page, view: str) -> list[list[str]]:
    """切到指定视图并 dump 每行 td 文本。"""
    if view != "default":
        page.evaluate(
            """(label) => {
                const b = [...document.querySelectorAll('.view-toggle button')]
                          .find(x => x.innerText.trim() === label);
                if (b) b.click();
            }""",
            view,
        )
        page.wait_for_timeout(4000)
    rows = page.evaluate(
        """() => [...document.querySelectorAll('#tbody tr')].map(tr =>
            [...tr.querySelectorAll('td')].map(td => td.innerText.trim()))"""
    )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="pages-audit-report.json")
    parser.add_argument("--prompt", default="pages-audit-prompt.md")
    parser.add_argument("--base", default=BASE)
    parser.add_argument("--sample", type=int, default=20)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    report: dict[str, Any] = {"base": args.base, "checks": {}, "issues": [], "samples": []}

    # ── data layer ────────────────────────────────────────────────
    try:
        manifest = fetch_json(f"{args.base}/data/manifest.json")
        report["manifest"] = {k: manifest.get(k) for k in ("updatedAt", "rowCount", "shownCount", "sources")}
    except Exception as exc:
        report["issues"].append({"severity": "error", "check": "data", "msg": f"manifest.json 无法加载: {exc}"})
        manifest = {}
    try:
        filtered = fetch_json(f"{args.base}/data/filtered.json")
        report["data_row_count"] = len(filtered)
    except Exception as exc:
        report["issues"].append({"severity": "error", "check": "data", "msg": f"filtered.json 无法加载: {exc}"})
        filtered = []

    # ── render layer ──────────────────────────────────────────────
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        report["issues"].append({"severity": "error", "check": "ui", "msg": "playwright 未安装"})
        return _finish(report, args)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context(user_agent=UA, viewport={"width": 1440, "height": 900}, locale="zh-CN")
        page = ctx.new_page()
        try:
            page.goto(args.base + "/", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(12000)
            # wait for rows
            for _ in range(20):
                cnt = page.evaluate("() => document.querySelectorAll('#tbody tr').length")
                if cnt > 0:
                    break
                page.wait_for_timeout(2000)
            report["page_title"] = page.title()
            tabs = page.evaluate("""() => [...document.querySelectorAll('.view-toggle button')].map(b => b.innerText.trim())""")
            report["tabs"] = tabs

            default_rows = render_rows(page, "default")
            bb_rows = render_rows(page, "宽带套餐")
            all_rows = render_rows(page, "全部资费")
            report["render_counts"] = {
                "default": len(default_rows),
                "broadband": len(bb_rows),
                "all": len(all_rows),
            }
            report["checks"]["all_view_matches_data"] = len(all_rows) == len(filtered)
            if len(all_rows) != len(filtered):
                report["issues"].append({"severity": "error", "check": "render", "msg": f"全部视图渲染 {len(all_rows)} 行 ≠ 数据 {len(filtered)} 行（缺行/多行）"})
            # 各视图行数 vs 数据层期望（默认推荐=default_show 行、宽带=is_broadband 行）
            expect_default = sum(1 for r in filtered if r.get("default_show"))
            expect_bb = sum(1 for r in filtered if r.get("is_broadband"))
            report["checks"]["expected_rows"] = {"default": expect_default, "broadband": expect_bb}
            if len(default_rows) != expect_default:
                report["issues"].append({"severity": "warning", "check": "render", "msg": f"默认推荐视图渲染 {len(default_rows)} 行 ≠ 数据 {expect_default} 行"})
            if len(bb_rows) != expect_bb:
                report["issues"].append({"severity": "warning", "check": "render", "msg": f"宽带视图渲染 {len(bb_rows)} 行 ≠ 数据 {expect_bb} 行"})

            # ── cell anomaly scan ──────────────────────────────────
            anomaly: list[str] = []
            for view, rows in (("default", default_rows), ("broadband", bb_rows), ("all", all_rows)):
                for i, row in enumerate(rows):
                    for j, cell in enumerate(row):
                        if BAD_CELL.search(cell) or GARBAGE.search(cell):
                            anomaly.append(f"{view}[{i}][{j}]={cell[:40]!r}")
            report["checks"]["cell_anomaly_count"] = len(anomaly)
            if anomaly:
                report["issues"].append({"severity": "error", "check": "render", "msg": f"单元格异常 {len(anomaly)} 处", "detail": anomaly[:20]})

            # ── broadband structure ────────────────────────────────
            # 宽带视图 td 顺序：0 source / 1 plan_name / 2 region / 3 monthly_fee /
            # 4 billing_period / 5 general_traffic / 6 broadband_mbps / 7 access / 8 note
            bb_cols = len(bb_rows[0]) if bb_rows else 0
            report["checks"]["broadband_cols"] = bb_cols
            if bb_cols != 9:
                report["issues"].append({"severity": "error", "check": "render", "msg": f"宽带视图列数 {bb_cols} ≠ 9"})
            # 逐行校验列数（首行列数正常但后续行异常会漏检）
            bad_col_rows = [i for i, row in enumerate(bb_rows) if len(row) != bb_cols]
            report["checks"]["broadband_bad_col_rows"] = len(bad_col_rows)
            if bad_col_rows:
                report["issues"].append({"severity": "warning", "check": "render", "msg": f"宽带视图 {len(bad_col_rows)} 行列数异常", "detail": bad_col_rows[:10]})
            bb_bw_bad = 0
            bb_bw_none = 0
            bb_access_bad: set[str] = set()
            bb_no_period: list[str] = []
            for row in bb_rows:
                if len(row) < 8:
                    continue
                bw = row[6]
                if bw == "-":
                    bb_bw_none += 1
                elif not re.fullmatch(r"\d+[MG]", bw):
                    bb_bw_bad += 1
                access = row[7]
                if access != "-" and access not in ("光纤", "FTTR", "同轴", "无线", "ADSL", "无线(FWA)", "同轴(HFC)"):
                    bb_access_bad.add(access)
                # 计费周期列 (col 4)：total_period 数据应有周期；扫描所有行周期为 - 的宽带行
                if row[4] == "-":
                    bb_no_period.append(row[1].splitlines()[0][:30])
            report["checks"]["bb_bw_none"] = bb_bw_none
            report["checks"]["bb_bw_format_bad"] = bb_bw_bad
            report["checks"]["bb_access_values"] = sorted(bb_access_bad)[:10]
            if bb_bw_bad:
                report["issues"].append({"severity": "warning", "check": "render", "msg": f"带宽格式异常 {bb_bw_bad} 处"})
            if bb_access_bad:
                report["issues"].append({"severity": "info", "check": "render", "msg": f"接入方式非标准值: {sorted(bb_access_bad)[:8]}"})
            if bb_no_period:
                report["issues"].append({"severity": "info", "check": "data", "msg": f"宽带行计费周期显示 - 共 {len(bb_no_period)} 条（样本: {bb_no_period[:3]}）"})

            # ── fee accuracy scan（渲染 vs 数据）───────────────────
            over1000 = [r for r in filtered if (r.get("monthly_fee") or 0) > 1000]
            if over1000:
                report["issues"].append({"severity": "error", "check": "data", "msg": f"filtered 含月租>1000 共 {len(over1000)} 条（总价未折算残留）", "detail": [r.get("plan_name", "")[:40] for r in over1000[:5]]})
            # 折算套餐：校验 monthly_fee == round(original_fee / period_months)
            conv_bad: list[str] = []
            for r in filtered:
                if r.get("fee_type") == "total_period" and r.get("original_fee") and r.get("billing_period"):
                    bp = str(r["billing_period"])
                    m = re.search(r"(\d+)\s*个?月", bp)
                    if not m:
                        m = re.search(r"([1-5])\s*年", bp)
                        months = int(m.group(1)) * 12 if m else None
                    else:
                        months = int(m.group(1))
                    if months:
                        expect = round(r["original_fee"] / months, 1)
                        if r.get("monthly_fee") != expect:
                            conv_bad.append(f"{r.get('plan_name','')[:30]} fee={r.get('monthly_fee')} expect={expect}")
            report["checks"]["conversion_mismatch"] = len(conv_bad)
            if conv_bad:
                report["issues"].append({"severity": "error", "check": "data", "msg": f"折算值不一致 {len(conv_bad)} 条", "detail": conv_bad[:5]})

            # ── duplicates ─────────────────────────────────────────
            seen: dict[tuple, int] = {}
            dup: list[str] = []
            for r in filtered:
                key = (r.get("source"), r.get("report_no"))
                if key[1]:
                    seen[key] = seen.get(key, 0) + 1
            for (src, no), n in seen.items():
                if n > 1:
                    dup.append(f"{src}/{no} x{n}")
            report["checks"]["duplicate_report_no"] = len(dup)
            if dup:
                report["issues"].append({"severity": "warning", "check": "data", "msg": f"同 source+report_no 重复 {len(dup)} 组", "detail": dup[:8]})

            # ── readability ────────────────────────────────────────
            long_names = [r.get("plan_name", "") for r in filtered if len(str(r.get("plan_name", ""))) > 60]
            report["checks"]["long_names"] = len(long_names)
            if long_names:
                report["issues"].append({"severity": "info", "check": "readability", "msg": f"名称超 60 字 {len(long_names)} 条", "detail": long_names[:3]})
            no_note = [r for r in filtered if not str(r.get("service_content") or "") and not r.get("broadband") and not r.get("contract_desc")]
            report["checks"]["empty_note_ratio"] = round(len(no_note) / max(len(filtered), 1), 3)
            if report["checks"]["empty_note_ratio"] > 0.3:
                report["issues"].append({"severity": "warning", "check": "readability", "msg": f"备注为空比例 {report['checks']['empty_note_ratio']:.0%} 过高"})

            # ── sample consistency（渲染 vs 数据）──────────────────
            def num_eq(a, b) -> bool:
                """数值比较，容忍单位后缀（渲染流量 '35G' vs 数据 35.0）"""
                def to_num(v):
                    if v is None:
                        return None
                    s = str(v).strip().rstrip("GgMm元").strip()
                    try:
                        return float(s)
                    except ValueError:
                        return None
                x, y = to_num(a), to_num(b)
                if x is None and y is None:
                    return True
                if x is None or y is None:
                    return False
                return abs(x - y) < 0.05

            rng = random.Random(args.seed)
            pool = [r for r in filtered if r.get("plan_name")]
            sample = rng.sample(pool, min(args.sample, len(pool)))
            # 全部资费视图（流量结构）td 顺序：0 source / 1 plan_name / 2 region /
            # 3 plan_type / 4 monthly_fee / 5 traffic / 6 orient / 7 voice / 8 note
            # plan_name td 的 innerText 第一行即套餐名（下方是标签行），用它做 key
            all_by_name: dict[str, list[str]] = {}
            name_dup: set[str] = set()
            for row in all_rows:
                if len(row) >= 6:
                    key = row[1].splitlines()[0].strip()
                    if key in all_by_name:
                        name_dup.add(key)  # 同名多行（不同 report_no/折扣档）——比对会歧义
                    all_by_name.setdefault(key, row)
            mismatches: list[str] = []
            missing: list[str] = []
            for r in sample:
                name = str(r.get("plan_name", "")).strip()
                rendered = all_by_name.get(name)
                if rendered and name not in name_dup:
                    # 核心展示字段比对（流量视图列序）：0=source 2=region 3=plan_type
                    # 4=fee 5=traffic（orient/voice/note 前端有格式拼接，不比）
                    src_ok = str(r.get("source") or "") == (rendered[0] if len(rendered) > 0 else "")
                    fee_ok = num_eq(r.get("monthly_fee"), rendered[4]) if len(rendered) > 4 else False
                    region_ok = str(r.get("region") or "") == (rendered[2] if len(rendered) > 2 else "")
                    traffic_ok = num_eq(r.get("general_traffic_gb"), rendered[5]) if len(rendered) > 5 else False
                    ptype_ok = str(r.get("plan_type") or "") == (rendered[3] if len(rendered) > 3 else "")
                    bad = []
                    if not src_ok:
                        bad.append(f"source data={r.get('source')} render={rendered[0] if len(rendered)>0 else '?'}")
                    if not fee_ok:
                        bad.append(f"fee data={r.get('monthly_fee')} render={rendered[4] if len(rendered)>4 else '?'}")
                    if not region_ok:
                        bad.append(f"region data={r.get('region')} render={rendered[2] if len(rendered)>2 else '?'}")
                    if not traffic_ok:
                        bad.append(f"traffic data={r.get('general_traffic_gb')} render={rendered[5] if len(rendered)>5 else '?'}")
                    if not ptype_ok:
                        bad.append(f"ptype data={r.get('plan_type')} render={rendered[3] if len(rendered)>3 else '?'}")
                    if bad:
                        mismatches.append(f"{name[:24]}: {'; '.join(bad)}")
                elif not rendered and name not in name_dup:
                    missing.append(name[:40])
                report["samples"].append({
                    "plan_name": name,
                    "data_fee": r.get("monthly_fee"),
                    "render_fee": rendered[4] if rendered and len(rendered) > 4 else None,
                    "matched": bool(rendered),
                    "ambiguous": name in name_dup,
                })
            report["checks"]["sample_ambiguous"] = len(name_dup)
            report["checks"]["sample_mismatch"] = len(mismatches)
            report["checks"]["sample_missing"] = len(missing)
            if mismatches:
                report["issues"].append({"severity": "warning", "check": "accuracy", "msg": f"抽样 {len(sample)} 条中 {len(mismatches)} 条字段渲染不一致", "detail": mismatches[:5]})
            if missing:
                report["issues"].append({"severity": "warning", "check": "accuracy", "msg": f"抽样 {len(sample)} 条中 {len(missing)} 条未在页面找到（疑似缺行）", "detail": missing[:5]})

            # ── broadband-specific sample（带宽/接入方式/计费周期）────
            def bw_eq(data_mbps, render) -> bool:
                if data_mbps is None:
                    return render in ("-", None)
                if render is None or render == "-":
                    return False
                s = str(render).strip()
                try:
                    if s.endswith("G"):  # 前端 fmtBw: 1000M -> "1G"
                        return abs(float(s[:-1]) * 1000 - float(data_mbps)) < 1
                    return abs(float(s.rstrip("M")) - float(data_mbps)) < 1
                except ValueError:
                    return False

            bb_pool = [r for r in filtered if r.get("is_broadband") and r.get("plan_name")]
            bb_sample = rng.sample(bb_pool, min(args.sample, len(bb_pool)))
            bb_by_name: dict[str, list[str]] = {}
            for row in bb_rows:
                if len(row) >= 8:
                    bb_by_name.setdefault(row[1].splitlines()[0].strip(), row)
            bb_mismatch: list[str] = []
            for r in bb_sample:
                name = str(r.get("plan_name", "")).strip()
                rendered = bb_by_name.get(name)
                if not rendered:
                    continue  # 宽带视图行数已全局校验，同名歧义不重复报
                bad = []
                if not bw_eq(r.get("broadband_mbps"), rendered[6]):
                    bad.append(f"bw data={r.get('broadband_mbps')} render={rendered[6]}")
                if str(r.get("access_method") or "-") != (rendered[7] or "-"):
                    bad.append(f"access data={r.get('access_method')} render={rendered[7]}")
                if str(r.get("billing_period") or "-") != (rendered[4] or "-"):
                    bad.append(f"period data={r.get('billing_period')} render={rendered[4]}")
                if bad:
                    bb_mismatch.append(f"{name[:24]}: {'; '.join(bad)}")
            report["checks"]["bb_sample_count"] = len(bb_sample)
            report["checks"]["bb_sample_mismatch"] = len(bb_mismatch)
            if bb_mismatch:
                report["issues"].append({"severity": "warning", "check": "accuracy", "msg": f"宽带抽样 {len(bb_sample)} 条中 {len(bb_mismatch)} 条字段不一致", "detail": bb_mismatch[:5]})
            report["sample_hit"] = sum(1 for s in report["samples"] if s["matched"])

        except Exception as exc:
            report["issues"].append({"severity": "error", "check": "ui", "msg": f"页面渲染/扫描异常: {type(exc).__name__}: {str(exc)[:200]}"})
        finally:
            browser.close()

    return _finish(report, args)


def _finish(report: dict[str, Any], args: argparse.Namespace) -> int:
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    # 生成 AI 审查 prompt
    prompt_path = Path(args.prompt)
    issues_txt = "\n".join(
        f"- [{i.get('severity')}] {i.get('check')}: {i.get('msg')}"
        + (f" | {i['detail']}" if i.get("detail") else "")
        for i in report.get("issues", [])
    ) or "（无自动扫描问题）"
    samples_txt = "\n".join(
        f"- {s.get('plan_name','')[:50]} | data_fee={s.get('data_fee')} render_fee={s.get('render_fee')} matched={s.get('matched')} ambiguous={s.get('ambiguous')}"
        for s in report.get("samples", [])
    )
    # 完整报告 JSON 一并嵌入（截断至 60KB），AI 可核对摘要之外的检查结果
    full_report = json.dumps(report, ensure_ascii=False)[:60000]
    prompt = f"""你是 crawl-sim 页面发布质量审查员。以下是一次 Pages 部署后的自动审计报告
（headless 渲染线上页面 JS 后的真实扫描结果 + 数据层对比）。

审计统计:
- 数据行数: {report.get('data_row_count')}
- 渲染行数: {report.get('render_counts', {})}
- 期望行数(default_show/is_broadband): {report.get('checks', {}).get('expected_rows')}
- 全部视图行数与数据一致: {report.get('checks', {}).get('all_view_matches_data')}
- 抽样命中: {report.get('sample_hit')}/{len(report.get('samples', []))}
- 宽带视图列数: {report.get('checks', {}).get('broadband_cols')}

自动扫描发现的问题:
{issues_txt}

抽样条目（数据 vs 渲染）:
{samples_txt}

完整审计报告 JSON（含全部 checks/明细，可核对摘要）:
{full_report}

请审查：
1. 上述自动问题中哪些是真实发布缺陷（严重度分级 error/warning/info），哪些可接受。
2. 抽样中字段渲染不一致/未命中的条目，判断是数据源问题还是前端问题。
3. 可读性整体评价（名称、备注、单位、排序默认态）。
4. 给出明确的结论：本次 Pages 发布是否存在需要人工介入的缺陷？如有，列出 TOP 问题及修复建议。

输出格式（严格 JSON）:
{{"conclusion": "OK|NEEDS_ATTENTION|BROKEN", "real_issues": [{{"severity": "...", "item": "...", "reason": "...", "suggestion": "..."}}], "summary": "一段中文总结"}}
"""
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(prompt, encoding="utf-8")
    print(f"audit report -> {out}")
    print(f"ai prompt -> {prompt_path}")
    for i in report.get("issues", []):
        print(f"  [{i['severity']}] {i['check']}: {i['msg']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

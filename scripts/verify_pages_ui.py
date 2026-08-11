#!/usr/bin/env python3
"""Verify the deployed sim.jiucai.eu.org page and data meet expectations.

Runs after Deploy Pages: opens the live site in headless Chromium, exercises
the three views (默认推荐 / 宽带套餐 / 全部资费), and validates the data
payloads. Exits non-zero with a JSON report when anything is off, so the
workflow can trigger AI self-repair.

Checks:
  1. manifest.json + filtered.json load; >= 3 sources present.
  2. default-shown rows satisfy: monthly fee <= 69 and traffic >= 20G (套餐),
     or the data-pack rule; no >512G unexplained traffic; no phone-contract rows.
  3. broadband view has rows (is_broadband > 0).
  4. UI: page renders, three tabs exist, each view fills tbody.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

BASE = "http://sim.jiucai.eu.org"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"


def fetch_json(url: str, timeout: int = 30) -> dict | list:
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def check_data(report: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    try:
        manifest = fetch_json(f"{BASE}/data/manifest.json")
        report["manifest"] = manifest
    except Exception as exc:
        errors.append(f"manifest.json 无法加载: {exc}")
        manifest = None
    try:
        rows = fetch_json(f"{BASE}/data/filtered.json")
    except Exception as exc:
        errors.append(f"filtered.json 无法加载: {exc}")
        return errors
    report["row_count"] = len(rows)
    report["sources"] = sorted({r.get("source", "") for r in rows})

    if len(rows) < 50:
        errors.append(f"filtered.json 仅 {len(rows)} 行 (< 50)")
    if len(report["sources"]) < 3:
        errors.append(f"数据源不足: {report['sources']} (< 3)")

    # 月租>200 高端套餐不得出现在发布数据（非富即贵过滤）
    high_fee = [r.get("plan_name", "") for r in rows if (r.get("monthly_fee") or 0) > 200]
    report["high_fee_rows"] = len(high_fee)
    if high_fee:
        errors.append(f"发布数据含月租>200 元套餐 {len(high_fee)} 条: {high_fee[:2]}")

    shown = [r for r in rows if r.get("default_show")]
    report["default_show_count"] = len(shown)
    if len(shown) < 5:
        errors.append(f"默认推荐仅 {len(shown)} 条 (< 5)")

    # default-show rows must satisfy the publication rules
    rule_broken = 0
    for r in shown:
        if r.get("excluded_phone_contract") or r.get("restricted"):
            rule_broken += 1
            continue
        fee = r.get("monthly_fee")
        traffic = r.get("general_traffic_gb")
        if r.get("plan_type") == "流量包":
            if not (fee is not None and 0 < fee <= 30 and traffic is not None and traffic >= 10):
                rule_broken += 1
        else:
            if not (fee is not None and 0 < fee <= 69 and traffic is not None and traffic >= 20):
                rule_broken += 1
    if rule_broken:
        errors.append(f"默认推荐有 {rule_broken} 条不满足发布规则")

    # implausible traffic (>512G) without explicit text support
    suspect = [
        r.get("plan_name", "")
        for r in rows
        if (r.get("general_traffic_gb") or 0) > 512
        and not re.search(r"(?:[5-9]\d{2,}|\d{4,})\s*(?:GB|G|TB|T)\b",
                          f"{r.get('plan_name','')} {r.get('service_content','')[:200]}")
    ]
    if suspect:
        errors.append(f"{len(suspect)} 条流量异常(>512G 无文本佐证): {suspect[:3]}")

    bb = [r for r in rows if r.get("is_broadband")]
    report["broadband_count"] = len(bb)
    if not bb:
        errors.append("无宽带套餐数据")
    # 校园专属宽带（限制高校区域）不得进入公开 Pages
    campus = [r for r in rows if r.get("excluded_campus")]
    report["campus_excluded_count"] = len(campus)
    if campus:
        names = "、".join(str(r.get("plan_name", ""))[:18] for r in campus[:3])
        errors.append(f"发布数据含 {len(campus)} 条校园专属宽带（不应进入 Pages）: {names}")
    # 宽带字段：过渡期旧 release 数据可能尚未带 broadband_mbps（字段缺失仅报告不报错）；
    # 字段存在但全部为空视为带宽提取失效。
    has_bw_field = any("broadband_mbps" in r for r in bb)
    report["broadband_mbps_field"] = has_bw_field
    if has_bw_field:
        with_bw = [r for r in bb if r.get("broadband_mbps")]
        report["broadband_with_bw_count"] = len(with_bw)
        report["broadband_access_count"] = len([r for r in bb if r.get("access_method")])
        if not with_bw:
            errors.append("宽带行均无 broadband_mbps（带宽提取失效）")
    else:
        report["broadband_with_bw_count"] = None
    return errors


def check_ui(report: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        errors.append("playwright 未安装")
        return errors
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context(user_agent=UA, viewport={"width": 1440, "height": 900}, locale="zh-CN")
        page = ctx.new_page()
        try:
            page.goto(BASE + "/", wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(12000)
            title = page.title()
            report["page_title"] = title
            if "资费" not in title and "套餐" not in title:
                errors.append(f"页面标题异常: {title[:60]}")

            tabs = page.evaluate(
                """() => [...document.querySelectorAll('.view-toggle button')].map(b => b.innerText.trim())"""
            )
            report["tabs"] = tabs
            for expect in ("默认推荐", "宽带套餐", "全部资费"):
                if expect not in tabs:
                    errors.append(f"缺少视图 tab: {expect}")

            # wait for data rows
            for _ in range(15):
                cnt = page.evaluate("() => document.querySelectorAll('#tbody tr').length")
                if cnt > 0:
                    break
                page.wait_for_timeout(2000)
            report["default_view_rows"] = cnt
            if cnt < 5:
                errors.append(f"默认推荐视图仅渲染 {cnt} 行 (< 5)")

            # broadband view
            page.evaluate(
                """() => { const b = [...document.querySelectorAll('.view-toggle button')]
                           .find(x => x.innerText.trim() === '宽带套餐'); if (b) b.click(); }"""
            )
            page.wait_for_timeout(4000)
            bb_cnt = page.evaluate("() => document.querySelectorAll('#tbody tr').length")
            report["broadband_view_rows"] = bb_cnt
            if bb_cnt < 1:
                errors.append("宽带套餐视图无数据行")
            # 宽带视图表头必须含带宽/接入方式列
            bb_head = page.evaluate(
                """() => {
                  const tr = document.getElementById('th-broadband');
                  return tr ? [...tr.querySelectorAll('th')].map(x => x.innerText.trim()) : [];
                }"""
            )
            report["broadband_head"] = bb_head
            head_text = " ".join(bb_head)
            for expect in ("带宽", "接入方式"):
                if expect not in head_text:
                    errors.append(f"宽带视图缺少表头: {expect}")
            if bb_cnt > 0:
                bb_cols = page.evaluate(
                    "() => { const row = document.querySelector('#tbody tr'); return row ? row.querySelectorAll('td').length : 0; }"
                )
                report["broadband_cols"] = bb_cols
                if bb_cols != 9:
                    errors.append(f"宽带视图行 {bb_cols} 列 (期望 9)")

            # all view
            page.evaluate(
                """() => { const b = [...document.querySelectorAll('.view-toggle button')]
                           .find(x => x.innerText.trim() === '全部资费'); if (b) b.click(); }"""
            )
            page.wait_for_timeout(4000)
            all_cnt = page.evaluate("() => document.querySelectorAll('#tbody tr').length")
            report["all_view_rows"] = all_cnt
            if all_cnt < 50:
                errors.append(f"全部资费视图仅渲染 {all_cnt} 行 (< 50)")
        finally:
            browser.close()
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("pages-verify-report.json"))
    args = parser.parse_args()

    report: dict[str, Any] = {"ok": False, "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    errors = check_data(report)
    ui_errors = check_ui(report)
    errors.extend(ui_errors)

    report["errors"] = errors
    report["ok"] = not errors
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())

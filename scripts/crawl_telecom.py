#!/usr/bin/env python3
"""Crawl China Telecom (中国电信) tariff data from 网厅资费专区.

https://www.189.cn/tariffZone/ is protected by 瑞数 (Riversafe) anti-bot JS.
The challenge is only passable with the playwright chromium build that matches
version 1.61.0 (chromium 1208). Pin `playwright==1.61.0` in the workflow and
parse the rendered DOM (API responses carry a per-request dynamic token).

Tabs: 北京资费 (default) / 集团资费公示; categories: 套餐/加装包/营销活动/...
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SOURCE = "中国电信"
SOURCE_SHORT = "电信"
URL = "https://www.189.cn/tariffZone/"

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"

# category labels to click: (tab, category) -> plan_type
CATEGORIES = [
    ("套餐", "套餐"),
    ("加装包", "流量包"),
]


def new_browser(playwright):
    return playwright.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage"],
    )


def wait_for_content(page, timeout_s: int = 150) -> bool:
    """Wait until the tariff list rendered (ruishu challenge solved, may navigate)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            text = page.evaluate("() => document.body ? document.body.innerText.slice(0, 500) : ''")
        except Exception:
            # navigation during ruishu challenge -> keep waiting
            page.wait_for_timeout(2000)
            continue
        if "方案编号" in text and "资费" in text:
            return True
        page.wait_for_timeout(2000)
    return False


EXTRACT_JS = """() => {
        const out = [];
        const seen = new Set();
        const all = [...document.querySelectorAll('div,section,li')];
        for (const el of all) {
            const t = (el.innerText || '').trim();
            if (!t.includes('方案编号')) continue;
            if ((t.match(/方案编号/g) || []).length !== 1) continue;
            if (t.length < 100 || t.length > 6000) continue;
            let parent = el.parentElement;
            let captured = false;
            for (let i = 0; i < 5 && parent; i++) {
                const pt = (parent.innerText || '');
                if (seen.has(pt)) { captured = true; break; }
                parent = parent.parentElement;
            }
            if (captured) continue;
            if (!seen.has(t)) { seen.add(t); out.push(t); }
        }
        return out;
    }"""


def extract_cards(page) -> list[str]:
    """Grab per-tariff cards from DOM (kept for API compatibility)."""
    return page.evaluate(EXTRACT_JS)


def parse_card(text: str) -> dict[str, Any] | None:
    def grab(pattern: str) -> str:
        m = re.search(pattern, text, re.S)
        return m.group(1).strip() if m else ""

    name = grab(r"^([^\n]+)")
    report_no = grab(r"方案编号：\s*([^\n]+)")
    plan_type_label = grab(r"资费类型：\s*([^\n]+)")
    fee_text = grab(r"资费标准：\s*([^\n]+)")
    use_scope = grab(r"适用范围：\s*([^\n]+)")
    sale_channel = grab(r"销售渠道：\s*([^\n]+)")
    online_offline = grab(r"上下线时间：\s*([^\n]+)")
    valid = grab(r"有效期限：\s*([^\n]+)")
    stay = grab(r"在网要求：\s*([^\n]+)")
    breach = grab(r"违约责任：\s*([^\n]+)")
    other_content = grab(r"其他服务内容：\s*([^\n]+)")

    fee = None
    m = re.search(r"(\d+(?:\.\d+)?)\s*元/(月|次|年|1月)", fee_text)
    if m:
        fee = float(m.group(1))

    # service table: 语音/通用流量/定向流量 columns, e.g. "100.00分钟\t50GB\t0MB"
    traffic = None
    orient = None
    voice = None
    table_match = re.search(r"服务内容\s*(语音|通用流量|定向流量|国内流量).{0,200}?", text, re.S)
    if table_match:
        seg = text[table_match.start():]
        # find the value line (first line after headers containing numbers+units)
        for line in seg.split("\n")[1:12]:
            line = line.strip()
            if not line:
                continue
            if "：" in line or "其他" in line or "语音\t" in line or "通用流量" in line or "定向流量" in line:
                continue
            if re.match(r"^[\d.]+\s*(GB|MB|分钟)", line):
                cols = re.split(r"[\s\t]+", line)
                col_idx = 0
                for col in cols:
                    num = re.match(r"^([\d.]+)\s*(GB|MB|分钟)", col)
                    if not num:
                        col_idx += 1
                        continue
                    value, unit = num.group(1), num.group(2)
                    value = float(value)
                    if unit == "分钟":
                        voice = value
                    elif unit == "GB":
                        if col_idx == 0:
                            traffic = value
                        else:
                            orient = value
                    elif unit == "MB":
                        if col_idx == 0:
                            traffic = round(value / 1024, 2)
                        else:
                            orient = round(value / 1024, 2)
                    col_idx += 1
                break

    if not name:
        return None
    service_content = other_content
    return {
        "source": SOURCE,
        "atomic_source_names": [SOURCE_SHORT],
        "plan_name": name,
        "report_no": report_no,
        "region": "北京",
        "plan_type": "流量包" if "加装" in plan_type_label else "套餐",
        "monthly_fee": fee,
        "fee_text": fee_text,
        "general_traffic_gb": traffic,
        "orient_traffic_gb": orient,
        "voice_minutes": voice,
        "sms": None,
        "contract": is_contract(name, service_content + " " + fee_text),
        "contract_desc": contract_desc(name, service_content + " " + fee_text),
        "valid_period": valid,
        "sale_channel": sale_channel,
        "use_scope": use_scope,
        "overage": "",
        "service_content": service_content,
        "online_date": online_offline,
        "offline_date": "",
        "raw_url": URL,
        "crawled_at": datetime.now(timezone.utc).isoformat(),
    }


def is_contract(name: str, content: str) -> bool:
    text = f"{name} {content}"
    strong = ("合约" in text or "预存" in text) and (
        "手机" in text or "终端" in text or "购机" in text or "电子券" in text or "话费" in text
    )
    return bool(strong)


def contract_desc(name: str, content: str) -> str:
    text = f"{name} {content}"
    for key in ("预存", "合约", "赠送"):
        idx = text.find(key)
        if idx != -1:
            return text[idx : idx + 120]
    return ""


def _proxy_server() -> str | None:
    """Playwright does not read HTTP_PROXY env vars; pass the mihomo proxy explicitly."""
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        val = (os.environ.get(key) or "").strip()
        if val and not val.lower().startswith("socks"):
            return val
    return None


def run(playwright) -> list[dict[str, Any]]:
    browser = new_browser(playwright)
    try:
        ctx = browser.new_context(
            user_agent=UA,
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            **({"proxy": {"server": _proxy_server()}} if _proxy_server() else {}),
        )
        page = ctx.new_page()
        # telecom via airport nodes can return ERR_EMPTY_RESPONSE on some nodes;
        # retry a few times (mihomo round-robin rotates node between attempts)
        page_content = None
        for attempt in range(4):
            try:
                page.goto(URL, wait_until="domcontentloaded", timeout=60000)
                page_content = True
                break
            except Exception as exc:
                print(f"  goto attempt {attempt + 1} failed: {type(exc).__name__}: {str(exc)[:120]}")
                page.wait_for_timeout(8000)
        if not page_content:
            raise RuntimeError("telecom page unreachable via proxy after retries")
        if not wait_for_content(page):
            raise RuntimeError("telecom ruishu challenge not solved after 150s")

        def safe_eval(script, arg=None):
            """Run an evaluate, tolerating transient navigation during challenge."""
            for _ in range(5):
                try:
                    if arg is None:
                        return page.evaluate(script)
                    return page.evaluate(script, arg)
                except Exception:
                    page.wait_for_timeout(2000)
            return None

        results: list[dict[str, Any]] = []
        seen_cards: set[str] = set()

        for tab, plan_type in CATEGORIES:
            # click category tab
            try:
                safe_eval(
                    """(label) => {
                        const els = [...document.querySelectorAll('div,li,span,a')];
                        const t = els.find(e => (e.innerText||'').trim() === label &&
                            e.children.length <= 1 && (e.innerText||'').trim().length <= 12);
                        if (t) t.click();
                    }""",
                    tab,
                )
                page.wait_for_timeout(6000)
            except Exception as exc:
                print(f"  click {tab} failed: {exc}")
            # scroll to bottom in steps, collecting cards
            collected: list[str] = []
            prev_len = 0
            for _ in range(25):
                cards = safe_eval(extract_cards.__doc__ and EXTRACT_JS) or []
                for c in cards:
                    if c not in collected:
                        collected.append(c)
                page.mouse.wheel(0, 2200)
                page.wait_for_timeout(1200)
                cur_len = len(collected)
                if cur_len == prev_len and not cards:
                    break
                prev_len = cur_len
            print(f"  {tab}: {len(collected)} cards")
            for text in collected:
                if text in seen_cards:
                    continue
                seen_cards.add(text)
                card = parse_card(text)
                if card:
                    card["plan_type"] = plan_type
                    results.append(card)
        return results
    finally:
        browser.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="data/telecom.json")
    parser.add_argument("--min-records", type=int, default=20)
    args = parser.parse_args()

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        rows = run(p)
    if len(rows) < args.min_records:
        print(f"FAIL: only {len(rows)} rows (< {args.min_records})", file=sys.stderr)
        return 2
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"OK: {len(rows)} rows -> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

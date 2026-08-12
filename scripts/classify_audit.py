#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pages 持续分类审计：真实访问线上 Pages、解析 JS/DOM 获取用户实际看到的数据，
再调用 AI 大模型逐条审查分类是否正确（宽带/流量、套餐/流量包、兆 M 单位语义）。

用户要求（2026-08-12）：
- 持续推进 action 访问 Pages、解析 JS 获取数据后调用 AI 扫描数据。
- 重点判断“每个条目的分类对不对”，尤其分清“兆 M”在不同语境是数量还是速率。

设计原则：
- AI 调用必须经 Agent 工具 CLI（opencode run），禁止 Python 直连大模型 API。
- 采集分两层：
  1. Playwright/headless 打开线上 Pages，执行 JS 并分别切换到默认/宽带/全部视图，
     解析 DOM 得到“用户实际看到”的表格文本（含宽带视图才会出现的列）。
  2. 同时拉取 manifest/filtered.json 做结构对照（字段分类、单位语义、缺列/错列）。
- 分类判定以“用户可见数据 + 字段对照”联合给出：
  - DOM 行映射到数据条目后，用原始字段做本地一致性检查；
  - AI 按批次对每条条目输出宽带/类型/单位语义判断；
  - 两者不一致或单位语义混淆才进入 misclassifications。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ai_providers import apply_proxy_env as _apply_proxy_env
from ai_providers import available as _available_providers

BASE = "http://sim.jiucai.eu.org"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# 移网速率语义名称（流量条目 m_unit=rate 时豁免——5G/升级包/速率/Mbps/王卡 等）
# "升级包/升级功能包" 精确匹配，避免裸"升级"误豁免"升级版流量包"等非速率语义
MOBILE_RATE_NAME_RE = re.compile(r"5G|升级包|升级功能包|速率|峰值|Mbps|王卡|千兆|百兆")

_PLAN_NAME_RE = re.compile(r"^\s*plan_name\s*=\s*(?P<q>['\"])(?P<name>.*?)(?P=q)\s*\|\s*broadband_field=")


def _safe_float(value: Any) -> float | None:
    """安全转 float：None/空/非数值返回 None（不抛异常）。"""
    try:
        if value is None or str(value).strip() in ("", "-"):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def fetch_json(url: str, timeout: int = 60):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            # 防 CDN/浏览器缓存拿到旧数据（Pages max-age=600）
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def normalize_name(name: str) -> str:
    return re.sub(r"\s+", "", (name or "").strip().lower())


def row_key(name: str, region: str) -> str:
    """DOM↔数据映射键：plan_name + region（同名不同地区是不同条目）。"""
    n = normalize_name(name)
    if not n:
        return ""
    return f"{n}|{normalize_name(region)}"


def build_row_index(rows: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    unique: dict[str, dict[str, Any]] = {}
    dup: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        key = row_key(str(r.get("plan_name") or ""), str(r.get("region") or ""))
        if not key:
            continue
        if key in unique:
            dup.setdefault(key, [unique[key]]).append(r)
            unique.pop(key, None)
        elif key not in dup:
            unique[key] = r
    return unique, dup


def build_batch_prompt(batch: list[dict[str, Any]], dom_by_name: dict[str, dict[str, Any]] | None = None) -> str:
    lines = []
    dom_by_name = dom_by_name or {}
    for i, r in enumerate(batch, 1):
        name = str(r.get("plan_name") or "")
        broadband = str(r.get("broadband") or "")
        content = str(r.get("service_content") or "")[:120]
        use_scope = str(r.get("use_scope") or "")[:60]
        bw = r.get("broadband_mbps")
        traffic = r.get("general_traffic_gb")
        orient = r.get("orient_traffic_gb")
        dom = dom_by_name.get(row_key(name, str(r.get("region") or ""))) or {}
        dom_text = ""
        if dom:
            dom_text = (
                f" | dom_view={dom.get('view', '')!r}"
                f" | dom_plan={dom.get('plan_name', '')!r}"
                f" | dom_plan_type={dom.get('plan_type', '')!r}"
                f" | dom_bandwidth={dom.get('bandwidth', '')!r}"
                f" | dom_billing={dom.get('billing_period', '')!r}"
                f" | dom_traffic={dom.get('traffic', '')!r}"
                f" | dom_access={dom.get('access_method', '')!r}"
            )
        lines.append(
            f"{i}. plan_name={name!r} | broadband_field={broadband!r} | "
            f"content={content!r} | use_scope={use_scope!r} | "
            f"data_broadband_mbps={bw!r} | data_traffic_gb={traffic!r} | data_orient_gb={orient!r}"
            f"{dom_text}"
        )
    return (
        "你是资费套餐分类审查员。现在看到的是线上 Pages 真实渲染后的条目（含 DOM 可见文本），"
        "请逐条判断分类与单位语义是否正确。\n"
        "判定规则：\n"
        "- broadband（宽带）: 是否包含宽带线路——独立宽带套餐、或融合套餐含宽带。\n"
        "- 提速包/加装包（提速/加装类）不是宽带套餐（不含宽带线路本身）。\n"
        "- 营销文案里出现'宽带'字样（功能描述/否定语境如'不可同时办理含宽带'）不算宽带。\n"
        "- 校园限定（校园/高校/学生 限定区域）不算普通宽带套餐。\n"
        "- plan_type: 套餐（含月费） vs 流量包（流量包/加量包/加油包/年包/日租包/权益包等）。\n"
        "- **单位语义（重点）**：'M/兆' 在不同语境是不同语义——\n"
        "  ① 速率（rate）：宽带带宽/下行速率（如 300M=300Mbps 宽带速率、千兆=1000Mbps），常见 50~10000；\n"
        "  ② 数量（quantity）：流量/存储量（如 30M=30MB 流量、2TB 云盘），流量常见 0~2048GB。\n"
        "  m_unit 判断该条目中 M/兆 数值的主要语义（rate/quantity/mixed/none）。\n"
        "  unit_confusion=true 表示数据字段语义错位：如 data_broadband_mbps 实际是流量数量、"
        "  data_traffic_gb 实际是速率值、或宽带套餐的带宽被填成了流量数值。\n"
        "- 如果同一 plan_name 在 DOM 中同时出现宽带视图与非宽带视图，按实际页面语境分别判断，"
        "  但最终仍只输出一条判定。\n"
        "对每条输出严格 JSON 数组（不要 markdown，不要其它文字）：\n"
        '[{"idx": 1, "judged_broadband": true/false, "judged_plan_type": "套餐|流量包", '
        '"m_unit": "rate|quantity|mixed|none", "unit_confusion": false, "reason": "一句话理由"}]\n'
        "\n--- 记录开始 ---\n"
        + "\n".join(lines)
        + "\n--- 记录结束 ---"
    )


def parse_verdicts(text: str) -> list[dict[str, Any]]:
    """鲁棒提取 JSON 数组（容忍垃圾文本/fence/think 块）。"""
    dec = json.JSONDecoder()
    text = re.sub(r"```(?:json)?", "", text)
    starts = [m.start() for m in re.finditer(r"\[", text)]
    for i in starts:
        try:
            v, _ = dec.raw_decode(text, i)
            if isinstance(v, list) and v and isinstance(v[0], dict) and "idx" in v[0]:
                return v
        except Exception:
            continue
    for m in re.finditer(r"\{", text):
        try:
            v, _ = dec.raw_decode(text, m.start())
            if isinstance(v, dict) and "idx" in v:
                return [v]
        except Exception:
            continue
    return []


def ai_scan_batch(batch: list[dict[str, Any]], batch_no: int, providers: list[dict[str, Any]], proxy: str,
                  dom_by_name: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """经 opencode CLI（Agent 工具）调用 AI 审查一批，返回逐条判定。"""
    prompt = build_batch_prompt(batch, dom_by_name=dom_by_name)
    read_only = {
        "*": "deny", "read": "allow", "edit": "deny", "bash": "deny",
        "webfetch": "deny", "task": "deny", "question": "deny", "external_directory": "deny",
    }
    config = {
        "provider": {
            p["name"]: {
                "npm": "@ai-sdk/openai-compatible",
                "name": p["name"],
                "options": {"baseURL": p["base"], "apiKey": "{env:%s}" % p["key_env"]},
                "models": {p["model"]: {"limit": {"context": 1000000, "output": 16000}}},
            }
            for p in providers
        },
        "agent": {"plan": {"permission": read_only}},
        "permission": read_only,
    }
    base_env = dict(os.environ)
    base_env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config, ensure_ascii=False)
    base_env["OPENCODE_DISABLE_AUTOUPDATE"] = "1"
    base_env["OPENCODE_DISABLE_TELEMETRY"] = "1"
    opencode_bin = os.environ.get("OPENCODE_BIN", "opencode")
    last_err = ""
    tmpdir = tempfile.mkdtemp(prefix="sim-classify-")
    try:
        prompt_file = Path(tmpdir) / "prompt.md"
        prompt_file.write_text(prompt, encoding="utf-8")
        for p in providers:
            cmd = [
                opencode_bin, "run", "--pure", "--agent", "plan",
                "--model", f"{p['name']}/{p['model']}",
                "--format", "default", "--dir", tmpdir,
                "Output ONLY the requested JSON array, no markdown, no extra text.",
                "--file", str(prompt_file),
            ]
            run_env = _apply_proxy_env(base_env, p, proxy)
            try:
                completed = subprocess.run(cmd, capture_output=True, text=True, timeout=900, env=run_env)
            except (OSError, subprocess.TimeoutExpired) as exc:
                last_err = f"{p['name']} {type(exc).__name__}"
                print(f"  batch {batch_no} {p['name']} failed ({type(exc).__name__}); next", file=sys.stderr)
                continue
            if completed.returncode != 0:
                last_err = f"{p['name']} exit {completed.returncode}"
                print(f"  batch {batch_no} {p['name']} exit {completed.returncode}; next", file=sys.stderr)
                continue
            verdicts = parse_verdicts(completed.stdout or "")
            if not verdicts:
                last_err = f"{p['name']} unparseable output"
                print(f"  batch {batch_no} {p['name']} unparseable; next", file=sys.stderr)
                continue
            print(f"  batch {batch_no} OK via {p['name']}/{p['model']} ({len(verdicts)} verdicts)", file=sys.stderr)
            return verdicts
        raise RuntimeError(f"batch {batch_no} all providers failed: {last_err}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def collect_dom_rows(base: str) -> dict[str, Any]:
    """真实访问 Pages 并解析 JS/DOM，返回三个视图的用户可见表格行。

    说明：
    - 仅在安装了 playwright 时执行；失败时返回 degraded 标记。
    - 行结构按真实页面解析，不把未知字段硬编码死；只补充 plan_name 归一化键。
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # pragma: no cover - 依赖环境
        return {
            "ok": False,
            "reason": f"playwright 未安装: {exc}",
            "manifest": {},
            "views": {},
            "stats": {"dom_rows_total": 0, "matched": 0, "unmatched": 0, "duplicate_names": 0},
        }

    report: dict[str, Any] = {"ok": True, "manifest": {}, "views": {}, "stats": {}, "manifest_error": False}
    stamp = int(time.time())
    try:
        manifest = fetch_json(f"{base}/data/manifest.json?t={stamp}")
        report["manifest"] = {k: manifest.get(k) for k in ("updatedAt", "rowCount", "shownCount", "sources")}
    except Exception as exc:
        # manifest 失败不中断 DOM 渲染（DOM 反映页面真实数据），但标记为采集异常供上报
        report["manifest_error"] = True
        report["manifest_error_msg"] = str(exc)
        report["issues"] = [{"severity": "error", "check": "data", "msg": f"manifest.json 无法加载: {exc}"}]

    try:
        _collect_dom_views(report, base, stamp)
    except Exception as exc:  # noqa: BLE001 - 浏览器启动/goto/解析/点击任一异常都记采集失败
        report["ok"] = False
        report["reason"] = f"DOM 渲染/解析异常: {type(exc).__name__}: {exc}"
        report["stats"] = report.get("stats") or {"dom_rows_total": 0, "matched": 0, "unmatched": 0, "duplicate_names": 0}
        report["views"] = report.get("views") or {}

    return _build_dom_index(report)


def _collect_dom_views(report: dict[str, Any], base: str, stamp: int) -> None:
    """渲染三视图并填充 report['views']（异常向上抛给 collect_dom_rows）。"""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 1400, "height": 1000})
        # 拦截 data/*.json 请求加 cache-busting 时间戳（页面 app.js 内部 fetch 的 manifest/filtered.json
        # 未带版本参数，可能命中 CDN max-age=600 缓存拿到旧数据，与 Python 拉取的版本错配）
        def _bust(route, request):
            url = request.url
            if "/data/" in url and request.resource_type == "fetch":
                sep = "&" if "?" in url else "?"
                url = f"{url}{sep}t={stamp}"
            route.continue_(url=url)

        ctx.route("**/data/*", _bust)
        page = ctx.new_page()
        # 防 CDN/浏览器缓存拿到旧数据（Pages max-age=600）
        page.goto(f"{base}/?t={stamp}", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(12000)

        def extract_rows() -> list[dict[str, str]]:
            return page.evaluate(
                """() => Array.from(document.querySelectorAll('#tbody tr')).map(tr => {
                    const cells = Array.from(tr.querySelectorAll('td')).map(td => ({
                        text: td.textContent.trim(),
                        title: td.getAttribute('title') || ''
                    }));
                    const firstTitle = cells.length > 1 ? cells[1].title : '';
                    // 名称在 <b> 元素内（td 里还有 <br> 后的标签行，textContent 会粘连标签文本）
                    const bEl = tr.querySelectorAll('td').length > 1
                        ? tr.querySelectorAll('td')[1].querySelector('b') : null;
                    const nameFromB = bEl ? bEl.textContent.trim() : '';
                    const nameCell = cells.length > 1 ? cells[1].text : '';
                    const firstLine = (nameCell.split(String.fromCharCode(10))[0] || '').trim();
                    return {
                        plan_name: (nameFromB || firstLine).trim(),
                        region: (cells.length > 2 ? cells[2].text : '').trim(),
                        cells: cells.map(c => c.text),
                        raw_title: firstTitle,
                        col_count: cells.length
                    };
                })"""
            )

        views = {"default": "btn-default", "broadband": "btn-broadband", "all": "btn-all"}
        for view, btn in views.items():
            page.evaluate(f"document.getElementById('{btn}')?.click()")
            page.wait_for_timeout(4000)
            rows = extract_rows()
            report["views"][view] = {
                "count": len(rows),
                "rows": rows,
            }
        browser.close()


def _build_dom_index(report: dict[str, Any]) -> dict[str, Any]:
    """把渲染后的 DOM 视图行汇总为唯一 plan_name+region 索引。"""
    # 视图优先级：broadband 视图信息最全（带宽/计费周期列），all/default 补充
    unique_names: dict[str, dict[str, Any]] = {}
    dup_names: set[str] = set()
    matched_rows = 0
    total_rows = 0
    view_rank = {"broadband": 0, "default": 1, "all": 2}
    seen_in_view: set[tuple[str, str]] = set()
    for view, data in report["views"].items():
        for row in data.get("rows", []):
            total_rows += 1
            key = row_key(row.get("plan_name", ""), row.get("region", ""))
            if not key:
                continue
            if (key, view) in seen_in_view:
                # 同一视图内同名同地区多条 = 真重复，映射不可唯一
                dup_names.add(key)
                unique_names.pop(key, None)
                continue
            seen_in_view.add((key, view))
            if key in dup_names:
                # 已确认同视图重复，任何视图后续出现都不再纳入映射
                continue
            cells = row.get("cells", [])
            entry: dict[str, Any] = {
                "view": view,
                "plan_name": row.get("plan_name", ""),
                "region": row.get("region", ""),
                "raw_title": row.get("raw_title", ""),
                "col_count": row.get("col_count", 0),
            }
            # 宽带视图列：运营商/套餐名称/地区/月租/计费周期/通用流量/带宽/接入方式/备注
            if view == "broadband" and len(cells) >= 8:
                entry.update({
                    "billing_period": cells[4],
                    "traffic": cells[5],
                    "bandwidth": cells[6],
                    "access_method": cells[7],
                })
            elif len(cells) >= 4:
                entry.update({"plan_type": cells[3]})
            existing = unique_names.get(key)
            if existing is None or view_rank.get(view, 9) < view_rank.get(existing.get("view", ""), 9):
                unique_names[key] = entry
            matched_rows += 1

    report["stats"] = {
        "dom_rows_total": total_rows,
        "matched": matched_rows,
        "unmatched": total_rows - matched_rows,
        "duplicate_names": len(dup_names),
    }
    report["dom_index"] = unique_names
    report["dom_keys"] = sorted(unique_names.keys())
    report["duplicate_names"] = sorted(dup_names)
    return report
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="classify-audit-report.json")
    parser.add_argument("--batch", type=int, default=80)
    parser.add_argument("--limit", type=int, default=0, help="0=全量")
    parser.add_argument("--max-batches", type=int, default=0, help="0=不限")
    parser.add_argument("--base", default=BASE)
    parser.add_argument("--skip-dom", action="store_true", help="仅拉 filtered.json，不渲染 Pages DOM")
    args = parser.parse_args()

    providers = _available_providers()
    if not providers:
        print("FAIL: no AI provider key configured", file=sys.stderr)
        return 2
    proxy = os.environ.get("DMIT_PROXY_URL", "").strip()

    # filtered.json 请求带 cache-busting 时间戳（防 CDN/浏览器缓存旧数据与 DOM 错配）
    stamp = int(time.time())
    rows = fetch_json(f"{args.base}/data/filtered.json?t={stamp}")
    if not isinstance(rows, list) or not rows:
        print(f"FAIL: filtered.json 为空或非列表（{type(rows).__name__}），不执行扫描（防止空扫自动关 issue）", file=sys.stderr)
        return 3
    if len(rows) < 50:
        print(f"FAIL: filtered.json 仅 {len(rows)} 行 (<50)，数据异常，不执行扫描", file=sys.stderr)
        return 3
    if args.limit > 0:
        rows = rows[: args.limit]
    print(f"rows: {len(rows)}")

    dom_report = {"ok": False, "reason": "skip-dom", "views": {}, "stats": {}}
    dom_by_name: dict[str, dict[str, Any]] = {}
    if not args.skip_dom:
        dom_report = collect_dom_rows(args.base)
        dom_by_name = dom_report.get("dom_index") or {}
        print(
            "dom: ok=%s total=%s matched=%s unmatched=%s dup=%s"
            % (
                dom_report.get("ok"),
                dom_report.get("stats", {}).get("dom_rows_total", 0),
                dom_report.get("stats", {}).get("matched", 0),
                dom_report.get("stats", {}).get("unmatched", 0),
                dom_report.get("stats", {}).get("duplicate_names", 0),
            )
        )

    unique_by_name, dup_by_name = build_row_index(rows)

    report: dict[str, Any] = {
        "total": len(rows),
        "batches": 0,
        "scanned": 0,
        "ai_judged": 0,
        "misclassifications": [],
        "batch_failures": [],
        "summary": {},
        "dom": {
            "ok": dom_report.get("ok", False),
            "reason": dom_report.get("reason", ""),
            "manifest_error": dom_report.get("manifest_error", False),
            "manifest_error_msg": dom_report.get("manifest_error_msg", ""),
            "dom_only_count": 0,
            "stats": dom_report.get("stats", {}),
            "views": {k: {"count": v.get("count", 0)} for k, v in (dom_report.get("views") or {}).items()},
        },
    }
    mismatches: list[dict[str, Any]] = []
    scanned = 0
    ai_judged = 0

    # DOM 双向集合校验：DOM-only 行（页面渲染了但 filtered.json 没有 = 前端展示不存在的数据）
    # 仅全量扫描时执行（--limit 截断模式下数据子集必然产生大量伪 DOM-only）
    if dom_report.get("ok") and args.limit <= 0:
        data_keys = {
            row_key(str(r.get("plan_name") or ""), str(r.get("region") or "")) for r in rows
        } - {""}
        dom_keys = set(dom_report.get("dom_keys") or [])
        dom_only = sorted(dom_keys - data_keys)
        report["dom"]["dom_only_count"] = len(dom_only)
        report["dom"]["dom_only_samples"] = dom_only[:20]
        for k in dom_only[:50]:
            entry = dom_by_name.get(k) or {}
            mismatches.append({
                "plan_name": str(entry.get("plan_name") or k)[:60],
                "source": None,
                "is_broadband": None,
                "plan_type": None,
                "ai_broadband": None,
                "ai_plan_type": None,
                "m_unit": None,
                "unit_confusion": False,
                "dom_view": entry.get("view"),
                "reason": f"Pages DOM 渲染了数据集中不存在的条目（{entry.get('view', '?')} 视图，DOM-only）",
            })

    # manifest 加载失败 = 采集异常（数据版本无法核对），上报并标 incomplete
    if dom_report.get("manifest_error"):
        mismatches.append({
            "plan_name": "(manifest)",
            "source": None,
            "is_broadband": None,
            "plan_type": None,
            "ai_broadband": None,
            "ai_plan_type": None,
            "m_unit": None,
            "unit_confusion": False,
            "dom_view": None,
            "reason": f"manifest.json 加载失败，无法核对数据版本: {dom_report.get('manifest_error_msg', '')[:100]}",
        })

    # DOM 采集本身失败（playwright 未装/渲染异常）且非 --skip-dom → 采集异常，标 incomplete
    if not args.skip_dom and not dom_report.get("ok"):
        mismatches.append({
            "plan_name": "(dom-collection)",
            "source": None,
            "is_broadband": None,
            "plan_type": None,
            "ai_broadband": None,
            "ai_plan_type": None,
            "m_unit": None,
            "unit_confusion": False,
            "dom_view": None,
            "reason": f"Pages DOM 采集失败（{dom_report.get('reason', '未知')}），DOM 审计未执行",
        })

    for bi in range(0, len(rows), args.batch):
        if args.max_batches and (bi // args.batch) >= args.max_batches:
            break
        batch = rows[bi : bi + args.batch]
        report["batches"] += 1
        try:
            verdicts = ai_scan_batch(batch, report["batches"], providers, proxy, dom_by_name=dom_by_name)
        except RuntimeError as exc:
            report["batch_failures"].append(str(exc))
            print(f"FAIL batch {report['batches']}: {exc}", file=sys.stderr)
            continue
        valid: dict[int, dict[str, Any]] = {}
        invalid = 0
        for v in verdicts:
            # type(idx) is int：bool 是 int 子类，true 会被当索引 1
            idx = v.get("idx")
            if type(idx) is not int or idx < 1 or idx > len(batch) or idx in valid:
                invalid += 1
                continue
            if not isinstance(v.get("judged_broadband"), bool):
                invalid += 1
                continue
            if str(v.get("judged_plan_type") or "") not in ("套餐", "流量包"):
                invalid += 1
                continue
            if not isinstance(v.get("unit_confusion"), bool):
                invalid += 1
                continue
            if str(v.get("m_unit") or "") not in ("rate", "quantity", "mixed", "none"):
                invalid += 1
                continue
            # reason 必须是字符串（null/数字会触发 .get(...)[:80] 抛异常）
            if not isinstance(v.get("reason"), str):
                invalid += 1
                continue
            valid[idx] = v
        if invalid or len(valid) != len(batch):
            report["batch_failures"].append(
                f"batch {report['batches']}: verdict 覆盖不完整 (valid={len(valid)}/{len(batch)} invalid={invalid})"
            )
            print(
                f"WARN batch {report['batches']}: verdict 覆盖 {len(valid)}/{len(batch)} invalid={invalid}",
                file=sys.stderr,
            )
        by_idx = valid
        for offset, row in enumerate(batch, 1):
            scanned += 1
            v = by_idx.get(offset)
            reasons: list[str] = []
            raw_bb = row.get("is_broadband")
            if not isinstance(raw_bb, bool):
                reasons.append(f"源数据 is_broadband 非布尔: {raw_bb!r}")
            data_bb = bool(raw_bb)
            raw_pt = row.get("plan_type")
            if raw_pt is None or str(raw_pt) not in ("套餐", "流量包"):
                reasons.append(f"源数据 plan_type 缺失/非法: {raw_pt!r}（schema 异常）")
            data_pt = str(raw_pt) if raw_pt is not None else ""
            name_key = row_key(str(row.get("plan_name") or ""), str(row.get("region") or ""))
            dom = dom_by_name.get(name_key)

            # 本地校验独立于 AI verdict（AI 缺失时仍执行本地兜底检查）
            bw = row.get("broadband_mbps")
            bw_f = _safe_float(bw)
            if bw is not None and bw_f is None:
                reasons.append(f"带宽字段非数值: {bw!r}")
            elif bw_f is not None and not (1 <= bw_f <= 100000):
                reasons.append(f"带宽值异常(非速率范围): {bw}")
            traffic = row.get("general_traffic_gb")
            tr_f = _safe_float(traffic)
            if traffic is not None and tr_f is None:
                reasons.append(f"流量字段非数值: {traffic!r}")
            elif tr_f is not None and not (0 <= tr_f <= 10000):
                reasons.append(f"流量值异常(非数量范围): {traffic}")
            if data_bb and bw is None:
                reasons.append("宽带套餐缺带宽字段")
            if not data_bb and bw is not None:
                reasons.append(f"非宽带却有带宽值 {bw}（疑似单位/分类错位）")

            # DOM 对照（用户实际看到的行）
            if dom_report.get("ok"):
                dom_dup = set(dom_report.get("duplicate_names") or [])
                if name_key in dup_by_name:
                    reasons.append("源数据 plan_name+region 重复，DOM 行无法唯一映射")
                elif name_key in dom_dup:
                    reasons.append("Pages 同视图渲染同名同地区多条（DOM 重复）")
                elif dom is None:
                    reasons.append("数据条目存在但 Pages DOM 未渲染该行")
                elif dom is not None:
                    dom_view = str(dom.get("view") or "")
                    if data_bb and dom_view != "broadband":
                        reasons.append(f"宽带套餐未出现在宽带视图（实际在 {dom_view or 'unknown'}）")
                    if not data_bb and dom_view == "broadband":
                        reasons.append("非宽带条目出现在宽带视图")
                    dom_pt = str(dom.get("plan_type") or "")
                    if not data_bb and dom_pt and dom_pt != data_pt:
                        reasons.append(f"DOM 类型列与数据不一致: 数据={data_pt} 页面={dom_pt}")
                    if data_bb:
                        dom_bw = str(dom.get("bandwidth") or "").strip()
                        if dom_bw in ("", "-"):
                            reasons.append("宽带视图带宽列显示空/占位符")
                        dom_billing = str(dom.get("billing_period") or "").strip()
                        if dom_billing in ("", "-"):
                            reasons.append("宽带视图计费周期列显示空/占位符")

            judged_bb = None
            judged_pt = None
            if v is not None:
                ai_judged += 1
                judged_bb = bool(v.get("judged_broadband"))
                judged_pt = str(v.get("judged_plan_type") or "")
                if judged_bb != data_bb:
                    reasons.append(f"宽带判定: 数据={data_bb} AI={judged_bb} ({v.get('reason', '')[:80]})")
                if judged_pt and data_pt and judged_pt != data_pt:
                    reasons.append(f"类型判定: 数据={data_pt} AI={judged_pt}")
                if v.get("unit_confusion"):
                    reasons.append(f"单位语义混淆: {v.get('reason', '')[:100]}")
                # m_unit 校验：宽带行有带宽值却判 quantity/none = 语义错位；
                # 流量条目判 rate 仅当名称含移网速率语义（5G/升级/速率/Mbps/王卡）时豁免
                # ——2026-08-12 实测 27 条流量包名含 5G 速率属正常，其余仍报警防真实单位错误
                ai_mu = str(v.get("m_unit") or "")
                if data_bb and bw_f is not None and ai_mu not in ("rate", "mixed"):
                    reasons.append(f"M单位语义可疑: 宽带套餐有带宽值但 AI 判 m_unit={ai_mu} ({v.get('reason', '')[:60]})")
                if (
                    not data_bb
                    and tr_f is not None
                    and ai_mu == "rate"
                    and not MOBILE_RATE_NAME_RE.search(str(row.get("plan_name") or ""))
                ):
                    reasons.append(f"M单位语义可疑: 流量条目但 AI 判 m_unit=rate ({v.get('reason', '')[:60]})")
            if reasons:
                mismatches.append({
                    "plan_name": str(row.get("plan_name") or "")[:60],
                    "source": row.get("source"),
                    "is_broadband": data_bb,
                    "plan_type": data_pt,
                    "ai_broadband": judged_bb,
                    "ai_plan_type": judged_pt,
                    "m_unit": v.get("m_unit") if v else None,
                    "unit_confusion": bool(v.get("unit_confusion")) if v else False,
                    "dom_view": (dom or {}).get("view"),
                    "reason": "；".join(reasons),
                })
        time.sleep(2)

    report["scanned"] = scanned
    report["ai_judged"] = ai_judged
    report["incomplete"] = (
        scanned < len(rows)
        or bool(report["batch_failures"])
        or bool(dom_report.get("manifest_error"))
        or (not args.skip_dom and not dom_report.get("ok"))
    )
    report["misclassifications"] = mismatches
    report["summary"] = {
        "scanned": scanned,
        "total": len(rows),
        "ai_judged": ai_judged,
        "incomplete": report["incomplete"],
        "mismatch_count": len(mismatches),
        "broadband_mismatch": sum(1 for m in mismatches if "宽带判定" in m["reason"]),
        "type_mismatch": sum(1 for m in mismatches if "类型判定" in m["reason"]),
        "unit_confusion": sum(1 for m in mismatches if m.get("unit_confusion")),
        "bandwidth_missing": sum(1 for m in mismatches if "缺带宽字段" in m["reason"]),
        "bandwidth_on_nonbb": sum(1 for m in mismatches if "非宽带却有带宽" in m["reason"]),
        "dom_mismatch": sum(1 for m in mismatches if "DOM" in m["reason"] or "视图" in m["reason"]),
        # 非 AI 行异常（DOM-only / manifest 缺失）独立计数，不计入 scanned
        "dom_anomaly_count": (
            report.get("dom", {}).get("dom_only_count", 0) + (1 if dom_report.get("manifest_error") else 0)
        ),
        "batch_failures": len(report["batch_failures"]),
    }
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"report -> {args.output}")
    print(
        f"scanned={scanned} ai_judged={ai_judged} mismatches={len(mismatches)} "
        f"(bb={report['summary']['broadband_mismatch']} type={report['summary']['type_mismatch']} "
        f"unit={report['summary']['unit_confusion']} dom={report['summary']['dom_mismatch']} "
        f"bw_missing={report['summary']['bandwidth_missing']} bw_on_nonbb={report['summary']['bandwidth_on_nonbb']})"
    )
    for m in mismatches[:20]:
        print(
            f"  [{m['source']}] {m['plan_name'][:40]} | bb={m['is_broadband']}->{m['ai_broadband']} | {m['reason'][:90]}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

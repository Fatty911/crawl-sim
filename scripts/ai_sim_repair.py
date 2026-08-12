#!/usr/bin/env python3
"""Generate and deterministically validate a narrowly-scoped crawl-sim AI patch.

The model only returns text. This program controls paths, validation, and
later application of the patch in GitHub Actions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml

ALLOWED_FILES = {
    "scripts/crawl_unicom.py",
    "scripts/crawl_broadnet.py",
    "scripts/crawl_mobile.py",
    "scripts/crawl_telecom.py",
    "scripts/merge_data.py",
    "scripts/verify_pages_ui.py",
    "scripts/crawl_runtime.py",
    ".github/workflows/crawl-unicom.yml",
    ".github/workflows/crawl-broadnet.yml",
    ".github/workflows/crawl-mobile.yml",
    ".github/workflows/crawl-telecom.yml",
    ".github/workflows/merge-and-filter.yml",
    ".github/workflows/deploy-pages.yml",
    ".github/workflows/ci.yml",
    "docs/index.html",
    "docs/app.js",
    "docs/style.css",
    "config/filter_conditions.json",
    "tests/test_data_quality.py",
}
FORBIDDEN_PATCH_HEADERS = ("diff --git a/.github/workflows/AI", "diff --git a/.git")
DIFF_HEADER = re.compile(r"^diff --git a/(.+?) b/(.+?)$")

# 保证 scripts/ 在 sys.path（无论以 python scripts/ai_sim_repair.py 还是其它方式调用）
sys.path.insert(0, str(Path(__file__).resolve().parent))

# 端点池唯一事实源：scripts/ai_providers.py（免费→单家Plan→聚合Plan→按量付费，自动切换）
from ai_providers import PROVIDERS as _ALL_PROVIDERS
from ai_providers import apply_proxy_env as _apply_proxy_env
from ai_providers import available as _available_providers

# 仅保留 key 已配置的端点（secrets 缺失的跳过，避免空 key 白试一轮）；全缺时兜底用完整池
GENERATOR_PROVIDERS = _available_providers() or _ALL_PROVIDERS


def fail(message: str) -> None:
    raise SystemExit(f"FAIL: {message}")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def opencode_generate(prompt: str, *, effort: str = "high", max_tokens: int = 20000) -> str:
    from ai_providers import build_opencode_config

    config = build_opencode_config(GENERATOR_PROVIDERS, max_tokens=max_tokens)
    base_env = dict(os.environ)
    base_env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config, ensure_ascii=False)
    base_env["OPENCODE_DISABLE_AUTOUPDATE"] = "1"
    base_env["OPENCODE_DISABLE_TELEMETRY"] = "1"
    proxy = os.environ.get("DMIT_PROXY_URL", "").strip()
    opencode_bin = os.environ.get("OPENCODE_BIN", "opencode")
    with tempfile.TemporaryDirectory(prefix="sim-gen-") as tmpdir:
        prompt_file = Path(tmpdir) / "prompt.md"
        prompt_file.write_text(prompt, encoding="utf-8")
        # 注意：message 必须在 --file 之前（yargs 会把 --file 后的位置参数当文件）；
        # --file 按进程 cwd 解析，必须传绝对路径（--dir 不影响 --file 解析）。
        last_err = ""
        for p in GENERATOR_PROVIDERS:
            cmd = [
                opencode_bin, "run", "--pure", "--agent", "plan",
                "--model", f"{p['name']}/{p['model']}",
                "--format", "default", "--dir", tmpdir,
                "Answer the attached prompt directly. Do not call tools or modify files. Return only the requested unified diff.",
                "--file", str(prompt_file),
            ]
            run_env = _apply_proxy_env(base_env, p, proxy)
            try:
                completed = subprocess.run(cmd, capture_output=True, text=True, timeout=2400, env=run_env)
            except (OSError, subprocess.TimeoutExpired) as exc:
                last_err = f"provider={p['name']} {type(exc).__name__}: {str(exc)[:150]}"
                print(f"generator {p['name']} failed ({type(exc).__name__}); trying next", file=sys.stderr)
                continue
            if completed.returncode != 0:
                tail = ((completed.stderr or "") + (completed.stdout or ""))[:300]
                last_err = f"provider={p['name']} exit {completed.returncode}: {tail}"
                print(f"generator {p['name']} exit {completed.returncode}; trying next", file=sys.stderr)
                continue
            out_text = (completed.stdout or "").strip()
            if out_text:
                print(f"generator OK via provider={p['name']} model={p['model']}", file=sys.stderr)
                return out_text
            last_err = f"provider={p['name']} empty output"
        print(f"generator all providers failed ({last_err}); treating as empty", file=sys.stderr)
        return ""


def extract_unified_diff(response: str) -> str:
    text = response.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:diff|patch)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
    start = text.find("diff --git ")
    if start < 0:
        fail("model response contains no unified git diff")
    return text[start:].strip() + "\n"


def patch_paths(patch: str) -> set[str]:
    paths: set[str] = set()
    active: str | None = None
    for line in patch.splitlines():
        match = DIFF_HEADER.match(line)
        if match:
            left, right = match.groups()
            if left != right or not left or left.startswith("/") or ".." in Path(left).parts:
                fail(f"unsafe diff path: {line}")
            if left not in ALLOWED_FILES:
                fail(f"path outside allowlist: {left}")
            paths.add(left)
            active = left
            continue
        if line.startswith(FORBIDDEN_PATCH_HEADERS):
            fail(f"unsafe diff header: {line}")
        if line.startswith("new file mode ") and line != "new file mode 100644":
            fail("new files must be regular non-executable mode 100644")
        if line.startswith("+++ /dev/null"):
            fail("patch may not delete files")
        if active and line.startswith("+++ b/"):
            if line[6:] != active:
                fail(f"inconsistent diff target: {line}")
        if active and line.startswith("--- a/"):
            if line[6:] != active:
                fail(f"inconsistent diff source: {line}")
    if not paths:
        fail("patch touches no files")
    return paths


def build_prompt(repo: Path, failure_log: str, trigger: str) -> str:
    crawler_contract = """
Crawler contract:
- output must be a JSON list of records with fields: source, atomic_source_names, plan_name,
  report_no, region, plan_type, monthly_fee, general_traffic_gb, orient_traffic_gb,
  voice_minutes, contract, service_content, crawled_at.
- Never fabricate data: rank/order must be the source's official order; never hard-code products.
- Keep network parsing separate from deterministic merge logic.
- A crawler artifact with fewer than its min-records must fail.
"""
    publish_rule = """
Publication invariants (do NOT weaken):
- Exclude phone-contract plans (充话费送手机 / 购机合约 / 预存得券) via excluded_phone_contract.
- Exclude campus-restricted broadband (校园/高校/学生 限定, e.g. 校园宽带/沃派校园专属) via
  excluded_campus: only for is_broadband rows; such rows must never appear in the published
  filtered.json payload.
- default_show: 套餐 with 通用流量>=20G and 月租<=69元; 流量包 with >=10G, <=30元, <=1元/GB.
- A rejected raw record must never become a published record via the UI.
- Broadband plans carry broadband_mbps (downlink Mbps) and access_method
  (光纤/FTTR/同轴(HFC)/无线(FWA)/ADSL). is_broadband must NOT be triggered by marketing text
  mentioning 宽带 (negation like 不可办理含宽带, function description like 宽带上网安全,
  exclusive listing like 与…宽带类…互斥); extract bandwidth only from broadband field,
  plan name, or bandwidth-context content; mobile-network peak rates (5G-A 下行3Gbps,
  移动上网速率/峰值速率/网络最高) are NOT broadband bandwidth.
- 广电 yearly plans named "一年…XX元档" (e.g. 靓号宽带双享包48元档) use tier price / 12 as
  monthly fee; the API productPrice is unreliable for tier products.
"""
    return f"""You are fixing a failing operator-tariff crawler/pipeline in the crawl-sim repo.

{crawler_contract}
{publish_rule}

Constraints:
- Only produce a unified git diff touching these allowlisted files:
  {", ".join(sorted(ALLOWED_FILES))}
- Do not add secrets, change workflow permissions, add exec bits, or delete files.
- Keep changes narrowly scoped to fix the actual failure.

<FAILURE_TRIGGER>
{trigger}
</FAILURE_TRIGGER>

<FAILURE_LOG>
{failure_log[-20000:]}
</FAILURE_LOG>

Return ONLY the unified diff (starting with "diff --git a/..."). No prose."""


def run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, cwd=str(cwd) if cwd else None, timeout=300)


def cmd_generate(args: argparse.Namespace) -> int:
    repo = Path(args.repo)
    failure_log = args.failure.read_text(encoding="utf-8", errors="replace")
    prompt = build_prompt(repo, failure_log, args.trigger)
    response = opencode_generate(prompt, effort=args.effort)
    if not response:
        print("generate: empty response from model", file=sys.stderr)
        return 1
    patch = extract_unified_diff(response)
    paths = patch_paths(patch)
    args.patch_out.write_text(patch, encoding="utf-8")
    print(f"generate: patch written to {args.patch_out} ({len(patch)} bytes), paths={sorted(paths)}")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    repo = Path(args.repo)
    patch = args.patch.read_text(encoding="utf-8")
    patch_sha = sha256_text(patch)
    paths = patch_paths(patch)
    checks: list[str] = ["patch_scope"]

    # apply to a clean checkout
    check = run(["git", "stash", "list"], repo)
    apply_result = run(["git", "apply", "--whitespace=error", str(args.patch)], repo)
    if apply_result.returncode != 0:
        fail(f"git apply failed: {(apply_result.stderr or '').strip()[:300]}")
    if run(["git", "diff", "--check"], repo).returncode != 0:
        fail("git diff --check failed")
    checks.append("merge_workflow")

    # deterministic checks
    if run(["python", "-m", "py_compile"] + sorted(str(repo / p) for p in paths if p.endswith(".py")), repo).returncode != 0:
        fail("py_compile failed")
    checks.append("py_compile")

    # pytest if present
    test_paths = [p for p in paths if p.startswith("tests/")]
    if test_paths or Path(repo / "tests").exists():
        if run(["python", "-m", "pytest", "tests/", "-q"], repo).returncode != 0:
            fail("pytest failed")
    checks.append("pytest")

    # docs copy check
    for d in ("docs/index.html", "docs/app.js", "docs/style.css"):
        if not (repo / d).exists():
            fail(f"missing docs file: {d}")
    checks.append("docs_copy")

    report = {
        "base_sha": run(["git", "rev-parse", "HEAD"], repo).stdout.strip(),
        "patch_sha256": patch_sha,
        "paths": sorted(paths),
        "checks": sorted(checks),
    }
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"validate: OK -> {args.report}")
    return 0


def cmd_verify_tree(args: argparse.Namespace) -> int:
    repo = Path(args.repo)
    patch = args.patch.read_text(encoding="utf-8")
    base = run(["git", "rev-parse", "HEAD"], repo).stdout.strip()
    if base != args.base_sha:
        fail(f"tree drifted: base {base} != expected {args.base_sha}")
    # verify patch applies cleanly (tree == HEAD + patch assumed applied)
    check = run(["git", "apply", "--check", "--whitespace=error", str(args.patch)], repo)
    if check.returncode != 0:
        fail("verify-tree: patch no longer applies cleanly (tree modified after apply?)")
    print(f"verify-tree: OK base={base}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    g = sub.add_parser("generate")
    g.add_argument("--repo", type=Path, default=".")
    g.add_argument("--failure", type=Path, required=True)
    g.add_argument("--trigger", default="workflow_dispatch")
    g.add_argument("--patch-out", type=Path, required=True)
    g.add_argument("--effort", default="high")
    g.set_defaults(func=cmd_generate)

    v = sub.add_parser("validate")
    v.add_argument("--repo", type=Path, default=".")
    v.add_argument("--patch", type=Path, required=True)
    v.add_argument("--report", type=Path, required=True)
    v.set_defaults(func=cmd_validate)

    t = sub.add_parser("verify-tree")
    t.add_argument("--repo", type=Path, default=".")
    t.add_argument("--patch", type=Path, required=True)
    t.add_argument("--base-sha", required=True)
    t.set_defaults(func=cmd_verify_tree)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

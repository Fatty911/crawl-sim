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


def opencode_generate(prompt: str, *, repo: Path, effort: str = "high", max_tokens: int = 20000) -> str:
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
        # --dir 必须是仓库根（模型需读取真实源码才能产出准确 diff；tmpdir 只有 prompt.md
        # 会让模型拒绝伪造补丁——2026-08-12 实测定性：'无法生成可用的 unified diff：
        # 仓库源文件不在可读范围内'）。prompt 文件仍用绝对路径 --file。
        # 注意：message 必须在 --file 之前（yargs 会把 --file 后的位置参数当文件）。
        last_err = ""
        for p in GENERATOR_PROVIDERS:
            cmd = [
                opencode_bin, "run", "--pure", "--agent", "plan",
                "--model", f"{p['name']}/{p['model']}",
                "--format", "default", "--dir", str(repo),
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
                if "diff --git " in out_text:
                    print(f"generator OK via provider={p['name']} model={p['model']}", file=sys.stderr)
                    return out_text
                # 端点返回非空但无 unified diff（NIM 免费层输出退化实测：返回分析文字/垃圾文本）：
                # 不 break，继续切下一端点（火山 coding plan 更稳定）
                last_err = f"provider={p['name']} no-diff-in-output"
                print(f"generator {p['name']} returned no unified diff ({len(out_text)} chars); trying next", file=sys.stderr)
                continue
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
    diff_text = text[start:]
    # 剥结尾 markdown fence 及 fence 外的模型说明文字。模型输出结构实测：
    # ```diff\n + diff + \n```\n + 'Note: ...'——用独立 fence 行（\n```\n 或结尾 \n```）匹配，
    # 避免 diff 正文内联 ```（如代码注释）被误截。
    fence = re.search(r"\n```\s*(?:\n|$)", diff_text)
    if fence:
        diff_text = diff_text[: fence.start()]
    # 规范化行尾：模型输出可能带 CRLF（实测定性：hunk 行含 \r 导致 git apply 匹配失败），
    # git apply 需要 LF 行尾的 patch。
    diff_text = diff_text.replace("\r\n", "\n").replace("\r", "\n")
    return diff_text.strip() + "\n"


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
- Speed-boost/bolt-on products (宽带提速包/千兆提速小合约/上行提速包/FTTR 提速包/加装包) do NOT
  contain a broadband line themselves → is_broadband must be False even when 宽带/提速/千兆/1000M
  appear in the name (1000M there is a rate, not a line); same for pure-traffic bolt-ons
  (FWA 通用流量加装包: 200GB is a quantity, not a line).
- plan_type=流量包 only for true data packs (流量包/加量包/加油包/年包/日租包/权益流量包);
  权益会员/场景包/功能包/服务包/公网IP 等非流量包不得标 plan_type=流量包.
- M/兆 semantics: broadband_mbps is a RATE (50~100000); general_traffic_gb/orient_traffic_gb is a
  QUANTITY (0~10000 GB, 1T=1024GB not 1.0GB); never store a rate value in a quantity field.
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


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


def recount_hunks(patch_text: str) -> str:
    """重算每个 hunk 的行数并按旧起始行号排序。

    实测（2026-08-12）：模型生成的 diff hunk header 行数全错（git apply corrupt），
    且文件内 hunk 顺序可能颠倒（GNU patch 报 misordered hunks）。
    行数按 hunk 实际内容重算；起始行号保留模型声明（GNU patch --fuzz 可容忍偏移）。
    """
    lines = patch_text.splitlines(keepends=True)
    out: list[str] = []
    file_blocks: list[tuple[str, list[str]]] = []
    current_header: str | None = None
    current_body: list[str] = []
    for line in lines:
        if line.startswith("diff --git "):
            if current_header is not None:
                file_blocks.append((current_header, current_body))
            current_header = line
            current_body = []
        else:
            current_body.append(line)
    if current_header is not None:
        file_blocks.append((current_header, current_body))

    for header, body in file_blocks:
        out.append(header)
        hunks: list[tuple[int, int, str, list[str]]] = []
        j = 0
        while j < len(body):
            stripped = body[j].rstrip("\r\n")
            m = _HUNK_RE.match(stripped)
            if not m:
                out.append(body[j])
                j += 1
                continue
            old_start = int(m.group(1))
            new_start = int(m.group(3))
            tail = m.group(5)
            k = j + 1
            while k < len(body):
                s = body[k].rstrip("\r\n")
                # hunk 终止：@@（下一 hunk）或 diff --git（下一文件，模型幻觉场景防御）
                if s.startswith("@@") or s.startswith("diff --git "):
                    break
                k += 1
            hunks.append((old_start, new_start, tail, body[j:k]))
            j = k
        # python sort 稳定：同 old_start 保持原序；键含 new_start 防交叉修改错序
        hunks.sort(key=lambda h: (h[0], h[1]))
        for old_start, new_start, tail, body_lines in hunks:
            ctx = dels = adds = 0
            kept: list[str] = []
            for bl in body_lines:
                s = bl.rstrip("\r\n")
                if not s:
                    # 纯空行（无空格前缀）在 unified diff 中非法：剔除该行 + 告警
                    # （行数偏小会让应用失败并反馈模型，安全）
                    print("recount: 模型输出纯空行（无空格前缀）已剔除——格式不规范", file=sys.stderr)
                    continue
                c = s[0]
                if c == "\\":
                    kept.append(bl)  # "\ No newline at end of file" 等标记行原样保留
                elif c == "+":
                    adds += 1
                    kept.append(bl)
                elif c == "-":
                    dels += 1
                    kept.append(bl)
                elif c == " ":
                    ctx += 1
                    kept.append(bl)
                # 其它（如原 @@ 行）：丢弃（新 header 已写入）
            old_total = ctx + dels
            new_total = ctx + adds
            out.append(f"@@ -{old_start},{old_total} +{new_start},{new_total} @@{tail}\n")
            out.extend(kept)
    return "".join(out)


def cmd_generate(args: argparse.Namespace) -> int:
    repo = Path(args.repo)
    failure_log = args.failure.read_text(encoding="utf-8", errors="replace")
    prompt = build_prompt(repo, failure_log, args.trigger)
    response = opencode_generate(prompt, repo=repo, effort=args.effort)
    if not response:
        print("generate: empty response from model", file=sys.stderr)
        return 1
    patch = extract_unified_diff(response)
    patch = recount_hunks(patch)  # 行数/顺序规整，validate 直接用重算版
    paths = patch_paths(patch)
    args.patch_out.write_text(patch, encoding="utf-8", newline="\n")
    print(f"generate: patch written to {args.patch_out} ({len(patch)} bytes), paths={sorted(paths)}")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    repo = Path(args.repo)
    patch_text = args.patch.read_text(encoding="utf-8")
    # 备份模型原始 patch 供追溯（重算版用于应用/校验/artifact）
    original_patch = args.patch.with_name(args.patch.name + ".original")
    original_patch.write_text(patch_text, encoding="utf-8", newline="\n")
    # 模型 diff 行数/顺序常错：重算 hunk 行数 + 排序后写回（sim-validation artifact 用重算版）
    patch_text = recount_hunks(patch_text)
    args.patch.write_text(patch_text, encoding="utf-8", newline="\n")
    patch_sha = sha256_text(patch_text)
    paths = patch_paths(patch_text)
    checks: list[str] = ["patch_scope"]

    # apply to a clean checkout
    check = run(["git", "stash", "list"], repo)
    # 优先 git apply --recount（行数重算后应可直接应用）；若 git 上下文匹配仍失败
    # （2026-08-12 实测：模型上下文行与文件字节一致但 git xdiff 仍拒绝），回退 GNU patch --fuzz
    apply_result = run(["git", "apply", "--recount", "--whitespace=error", str(args.patch)], repo)
    if apply_result.returncode != 0:
        print(f"validate: git apply failed, falling back to GNU patch --fuzz=3: {(apply_result.stderr or '').strip()[:200]}",
              file=sys.stderr)
        # 回退前清理（git apply 原子失败不留痕；保险起见统一模式）
        run(["git", "restore", "--worktree", "."], repo)
        run(["git", "clean", "-fd"], repo)
        patch_result = run(["patch", "-p1", "--fuzz=3", "--dry-run", "-i", str(args.patch)], repo)
        if patch_result.returncode != 0:
            run(["git", "restore", "--worktree", "."], repo)
            run(["git", "clean", "-fd"], repo)
            fail(f"patch --dry-run failed: {(patch_result.stderr or '').strip()[:300]}")
        patch_result = run(["patch", "-p1", "--fuzz=3", "-i", str(args.patch)], repo)
        if patch_result.returncode != 0:
            run(["git", "restore", "--worktree", "."], repo)
            run(["git", "clean", "-fd"], repo)
            fail(f"patch apply failed: {(patch_result.stderr or '').strip()[:300]}")
        # 校验 patch 完整应用：工作树改动文件数 >= patch 实际含 hunk 的文件数
        # （patch_paths 可能含声明但无 hunk 的文件——模型幻觉，不应误报 missing）
        stat = run(["git", "diff", "--name-only"], repo)
        changed = set(p for p in stat.stdout.splitlines() if p)
        hunky_paths = set(re.findall(r"^diff --git a/(.+?) b/", patch_text, re.M))
        missing = hunky_paths - changed
        if missing:
            fail(f"patch applied but missing changes in: {sorted(missing)}")
    if run(["git", "diff", "--check"], repo).returncode != 0:
        fail("git diff --check failed")
    checks.append("merge_workflow")

    # deterministic checks
    pyc = run(["python", "-m", "py_compile"] + sorted(str(repo / p) for p in paths if p.endswith(".py")), repo)
    if pyc.returncode != 0:
        combined = (pyc.stderr or "") + (pyc.stdout or "")
        print(f"py_compile output:\n{combined[-2000:]}", file=sys.stderr)
        fail("py_compile failed")
    checks.append("py_compile")

    # pytest if present（失败时打印完整输出——validate.log → validation issue → attempt=2 模型针对性修复）
    test_paths = [p for p in paths if p.startswith("tests/")]
    if test_paths or Path(repo / "tests").exists():
        pt = run(["python", "-m", "pytest", "tests/", "-q"], repo)
        if pt.returncode != 0:
            print(f"pytest stdout:\n{(pt.stdout or '')[-3000:]}", file=sys.stderr)
            print(f"pytest stderr:\n{(pt.stderr or '')[-2000:]}", file=sys.stderr)
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

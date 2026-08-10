"""
通用多Provider工作流错误自动修复系统

规则（融合全库优点）：
- XXXX_API_KEY 存在 → 启用该Provider
- XXXX_BASE_URL 可覆盖或补充 OpenAI-compatible API 地址
- XXXX_MODEL_LIST 存在 → 使用用户显式配置的模型
- XXXX_PROXY_URL 存在 → 该 Provider 请求走指定代理
- 保留完整的上下文解析和命令自动修复执行功能
"""

import os
import sys
import json
import subprocess
import requests
import re
import tempfile
from typing import Optional, Dict, List

PROVIDER_BASE_URLS = {
    "NVIDIA_NIM": "https://integrate.api.nvidia.com/v1",
    "MODELSCOPE": "https://api-inference.modelscope.cn/v1",
    "OPENROUTER": "https://openrouter.ai/api/v1",
    "ZEN": "https://opencode.ai/zen/v1",
    "CLOUDFLARE": "https://api.cloudflare.com/client/v4/accounts/b3becce2da2399953658ed2a053e7c08/ai/v1",
    "MODAL": "https://api.us-west-2.modal.direct/v1",
    "ATOMGIT": "https://api-ai.gitcode.com/v1",
    "DEEPSEEK": "https://api.deepseek.com/v1",
    "MINIMAX": "https://api.minimax.io/v1",
    "MOONSHOT": "https://api.moonshot.cn/v1",
    "ZHIPU": "https://open.bigmodel.cn/api/paas/v4",
    "XAI": "https://api.x.ai/v1",
    "OPENAI": "https://api.openai.com/v1",
}

# 模型默认列表——免费优先，普通 API 兜底；Plan 凭证由专用 Agent 处理
PROVIDER_DEFAULT_MODELS = {
    # 免费端点（优先尝试）
    "NVIDIA_NIM": ["nvidia/nemotron-3-ultra-550b-a55b:free", "nvidia/nemotron-3-super-120b-a12b:free"],
    "MODELSCOPE": ["MiniMax/MiniMax-M3"],
    "OPENROUTER": ["nvidia/nemotron-3-ultra-550b-a55b:free"],
    "ZEN": ["nemotron-3-ultra-free", "deepseek-v4-flash-free"],
    "ATOMGIT": ["zai-org/GLM-5.1", "deepseek-ai/DeepSeek-V4-Flash"],
    "CLOUDFLARE": ["@cf/zai-org/glm-5.2"],
    # 普通按量 API（免费全部不可用时兜底）
    "DEEPSEEK": ["deepseek-v4-flash"],
}

FREE_PREFIXES = {
    "NVIDIA_NIM",
    "OPENROUTER",
    "ZEN",
    "ATOMGIT",
    "MODELSCOPE",
    "CLOUDFLARE",
}

PLAN_PREFIX_MARKERS = (
    "_CODINGPLAN",
    "_CODING_PLAN",
    "_AGENTPLAN",
    "_AGENT_PLAN",
    "_TOKENPLAN",
    "_TOKEN_PLAN",
    "_PLAN",
)

PLAN_PREFIX_PATTERN = re.compile(
    r"(?:^|_)(?:CODINGPLAN|CODING_PLAN|AGENTPLAN|AGENT_PLAN|TOKENPLAN|TOKEN_PLAN|PLAN)(?:_|$)",
    flags=re.IGNORECASE,
)


def is_plan_prefix(prefix: str) -> bool:
    """Plan credentials are reserved for an explicit Agent process."""
    normalized = prefix.strip("_").upper()
    if PLAN_PREFIX_PATTERN.search(normalized):
        return True
    return any(normalized.endswith(marker.lstrip("_")) for marker in PLAN_PREFIX_MARKERS)

class WorkflowErrorFixer:
    def __init__(self, mode: str = "free-only", free_status: str = ""):
        if mode not in {"free-only", "paid-only"}:
            raise ValueError(f"unsupported routing mode: {mode}")
        if mode == "paid-only" and free_status != "all_free_429":
            raise ValueError("paid-only requires a preceding all_free_429 proof")
        self.mode = mode
        self.free_status = free_status
        self.route_status = "not_started"
        self._attempt_kinds: List[str] = []
        self._last_call_kind = "availability_error"
        self.providers = self._discover_providers()
        print(f"\n发现 {len(self.providers)} 个已配置 API_KEY 的 Provider。")

    def _discover_providers(self) -> List[Dict]:
        providers = []
        env = dict(os.environ)

        for key, value in env.items():
            if not key.endswith("_API_KEY") or not value or len(value.strip()) < 10:
                continue

            prefix = key[:-8]
            if is_plan_prefix(prefix):
                print(f"跳过 {prefix}: Plan 凭证只能由显式 Agent 工具调用")
                continue
            if self.mode == "free-only" and prefix not in FREE_PREFIXES:
                continue
            if self.mode == "paid-only" and prefix in FREE_PREFIXES:
                continue
            name = prefix.replace("_", " ").title()
            base_url = env.get(f"{prefix}_BASE_URL", "").strip() or PROVIDER_BASE_URLS.get(prefix)
            if not base_url:
                print(f"跳过 {prefix}: 未配置 {prefix}_BASE_URL，且没有内置 OpenAI-compatible 地址")
                continue

            model_list = self._parse_model_list(env.get(f"{prefix}_MODEL_LIST", ""))
            if not model_list:
                model_list = PROVIDER_DEFAULT_MODELS.get(prefix, [])
            if not model_list:
                print(f"跳过 {prefix}: 未配置 {prefix}_MODEL_LIST，且没有可靠默认模型")
                continue

            proxy_url = env.get(f"{prefix}_PROXY_URL", "").strip()

            providers.append({
                "prefix": prefix,
                "name": name,
                "api_key": value.strip(),
                "base_url": base_url,
                "model_list": model_list,
                "proxies": {"http": proxy_url, "https": proxy_url} if proxy_url else None,
            })

        # 排序：免费模型优先（ZEN、NVIDIA NIM），AtomGit 作为小 AGENT 降级
        def sort_key(p):
            prefix = p["prefix"]
            # 免费 Provider 优先（AtomGit 降为小 AGENT，排在其他免费之后）
            if prefix in ["ZEN", "NVIDIA_NIM"]:
                return (0, 0)
            # AtomGit 小 AGENT（免费但能力/上下文有限）
            if prefix == "ATOMGIT":
                return (0, 5)
            # OpenRouter 排在免费之后
            if prefix == "OPENROUTER":
                return (1, 0)
            # 有 model_list 的排中间
            if p["model_list"]:
                return (2, 0)
            # 其他排最后
            return (3, 0)

        providers.sort(key=sort_key)
        return providers

    def _parse_model_list(self, value: str) -> List[str]:
        return [m.strip() for m in re.split(r"[\s,;]+", value.strip()) if m.strip()]

    def _fetch_top_models(self) -> List[str]:
        print("\n=== 实时获取最新排行榜 ===")
        try:
            r = requests.get("https://artificialanalysis.ai/leaderboards/models", timeout=15, headers={"User-Agent": "AutoFix/2.0"})
            if r.status_code != 200: return []
            
            text = r.text.lower()
            mapping = {
                "claude": "anthropic/claude-opus-4.6",
                "gemini": "google/gemini-3.1-pro-preview",
                "gpt": "openai/gpt-5.4",
                "deepseek": "deepseek/deepseek-r1",
                "qwen": "qwen/qwen3.5-397b-a17b",
                "minimax": "minimax/minimax-m2.7",
                "mimo": "xiaomi/mimo-v2-pro"
            }
            
            found = []
            for kw in ["gemini", "gpt", "claude", "glm", "minimax", "grok", "mimo", "qwen", "deepseek"]:
                if kw in text and kw in mapping:
                    found.append(mapping[kw])
            
            print(f"映射结果: {found}")
            return found[:10]
        except Exception as e:
            print(f"抓取排行榜失败: {e}")
            return []

    def _resolve_models(self, provider: Dict) -> List[str]:
        if False:  # disabled: dynamic model fetch is fragile
            top = self._fetch_top_models()
            if top:
                return list(dict.fromkeys(top + provider["model_list"]))
        return provider["model_list"]

    def fix_error(self, error_output: str, script_name: str = "") -> bool:
        context = self._collect_context(script_name)
        if not self.providers:
            self.route_status = "no_credentials"
            print("没有可用的提供商")
            return False

        for provider in self.providers:
            print(f"\n尝试 Provider: {provider['name']}")
            
            import json
            try:
                with open(".ai_model_scores.json", "r") as f:
                    scores = json.load(f)
            except Exception:
                scores = {}
            
            models = sorted(self._resolve_models(provider), key=lambda m: scores.get(m, 0), reverse=True)
            
            for model in models[:5]:
                print(f"  → 使用模型: {model}")
                result = self._call_model(provider, model, error_output, context)
                self._attempt_kinds.append(self._last_call_kind)
                if result:
                    if self._apply_fix(result, provider["name"], model):
                        self.route_status = "success"
                        return True
                    else:
                        self._attempt_kinds[-1] = "protocol_error"
                        scores[model] = scores.get(model, 0) - 2
                        with open(".ai_model_scores.json", "w") as f:
                            json.dump(scores, f, indent=2)
                        print(f"    [Penalty] 模型 {model} 修复失败或产生幻觉，扣分")
        if self._attempt_kinds and all(kind == "rate_limited" for kind in self._attempt_kinds):
            self.route_status = "all_free_429" if self.mode == "free-only" else "free_unavailable"
        elif any(kind == "auth_error" for kind in self._attempt_kinds):
            self.route_status = "auth_error"
        elif any(kind == "request_error" for kind in self._attempt_kinds):
            self.route_status = "request_error"
        else:
            self.route_status = "free_unavailable" if self.mode == "free-only" else "unavailable"
        return False

    def _call_model(self, provider: Dict, model: str, error_info: str, context: str) -> Optional[str]:
        prompt = f"分析以下GitHub Actions错误:\n{error_info}\n\n仓库上下文:\n{context}\n\n【AI防幻觉与打分机制】请在修复前确保逻辑正确，不可凭空假设API和类库，务必联网检索确定。若产生幻觉将在下次被扣分。\n用JSON回复(包含 files_to_modify, commands, reasoning, confidence)。格式严格。"
        # 通过 OpenCode CLI（Agent 工具）调用模型，禁止直连模型 API。
        # The provider key is consumed only by the OpenCode process.
        read_only = {
            "*": "deny",
            "read": "allow",
            "edit": "deny",
            "bash": "deny",
            "webfetch": "deny",
            "task": "deny",
            "question": "deny",
            "external_directory": "deny",
        }
        provider_label = re.sub(r"[^A-Za-z0-9_-]", "-", str(provider["prefix"]).lower())[:60]
        base_url = str(provider["base_url"]).rstrip("/")
        config = {
            "provider": {
                provider_label: {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": provider_label,
                    "options": {"baseURL": base_url, "apiKey": provider["api_key"]},
                    "models": {model: {"limit": {"context": 131072, "output": 8192}}},
                }
            },
            "agent": {"plan": {"permission": read_only}},
            "permission": read_only,
        }
        env = dict(os.environ)
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config, ensure_ascii=False)
        env["OPENCODE_DISABLE_AUTOUPDATE"] = "1"
        env["OPENCODE_DISABLE_TELEMETRY"] = "1"
        if provider.get("proxies"):
            proxy = provider["proxies"].get("https") or provider["proxies"].get("http")
            if proxy:
                env["NODE_USE_ENV_PROXY"] = "1"
                env["HTTPS_PROXY"] = proxy
                env["HTTP_PROXY"] = proxy
        opencode_bin = os.environ.get("OPENCODE_BIN", "opencode")
        try:
            with tempfile.TemporaryDirectory(prefix="autofix-") as tmpdir:
                with open(os.path.join(tmpdir, "prompt.md"), "w", encoding="utf-8") as handle:
                    handle.write(prompt)
                cmd = [
                    opencode_bin, "run", "--pure", "--agent", "plan",
                    "--model", f"{provider_label}/{model}",
                    "--format", "default",
                    "--dir", tmpdir,
                    "--file", "prompt.md",
                    "Answer the attached prompt directly. Do not call tools or modify files. Return only the requested JSON.",
                ]
                completed = subprocess.run(cmd, capture_output=True, text=True, timeout=300, env=env)
        except FileNotFoundError:
            self._last_call_kind = "availability_error"
            print("    ✗ opencode CLI 不可用")
            return None
        except Exception as e:
            self._last_call_kind = "availability_error"
            print(f"    ✗ 异常: {e}")
            return None
        if completed.returncode != 0:
            combined = (completed.stderr or "") + (completed.stdout or "")
            if re.search(r"\b429\b|rate.?limit|quota", combined, re.I):
                self._last_call_kind = "rate_limited"
            elif re.search(r"\b401\b|\b403\b|unauthorized|invalid api key", combined, re.I):
                self._last_call_kind = "auth_error"
            else:
                self._last_call_kind = "availability_error"
            print(f"    ✗ opencode exit {completed.returncode}")
            return None
        content = (completed.stdout or "").strip()
        if content:
            self._last_call_kind = "success"
            return content
        self._last_call_kind = "protocol_error"
        return None

    def _apply_fix(self, response: str, provider: str, model: str) -> bool:
        fix = self._parse_json(response)
        if not fix or fix.get("confidence", 0) < 0.6: return False
        print(f"    ★ {provider} ({model}) 成功返回方案。")
        
        for fi in fix.get("files_to_modify", []):
            try:
                with open(fi["path"], "w", encoding="utf-8") as f:
                    f.write(fi["content"])
                print(f"    ✓ 修改文件: {fi['path']}")
            except Exception as e:
                print(f"    ✗ 文件修改失败: {e}")
                
        for cmd in fix.get("commands", []):
            print(f"    执行: {cmd}")
            subprocess.run(cmd, shell=True, timeout=300)

        repo = os.environ.get("GITHUB_REPOSITORY", "")
        token = os.environ.get("ACTION_PAT", "")
        if repo and token:
            subprocess.run('git config --local user.email "xuerui911@gmail.com" && git config --local user.name "github-actions[bot]"', shell=True)

            status_result = subprocess.run(
                ["git", "status", "--porcelain"],
                capture_output=True, text=True
            )
            if not status_result.stdout.strip():
                print("    ⚠️ AI 未产生任何文件改动，拒绝触发后续流程")
                return False

            # === 推送前语法校验 ===
            print("    🔍 执行语法校验...")
            validate_result = subprocess.run(
                ["python", "custom_scripts/validate_syntax.py"],
                capture_output=True, text=True, timeout=60
            )
            if validate_result.returncode != 0:
                print(f"    ✗ 语法校验失败:\n{validate_result.stdout}\n{validate_result.stderr}")
                print("    ⚠️ 回滚修改，拒绝推送")
                subprocess.run("git checkout -- .", shell=True)
                return False
            print("    ✓ 语法校验通过")
            
            try:
                import json
                scores = {}
                try:
                    with open(".ai_model_scores.json", "r") as f:
                        scores = json.load(f)
                except Exception:
                    pass
                scores[model] = scores.get(model, 0) + 1
                with open(".ai_model_scores.json", "w") as f:
                    json.dump(scores, f, indent=2)
            except Exception:
                pass

            subprocess.run("git add -A", shell=True)
            if subprocess.run("git diff --staged --quiet", shell=True).returncode == 0:
                print("    ⚠️ 未产生可提交的修复改动，拒绝触发后续流程")
                return False

            msg = f"Auto-fix by {provider}/{model}"
            commit_result = subprocess.run(f'git commit -m "{msg}"', shell=True)
            if commit_result.returncode != 0:
                print("    ✗ 提交失败")
                return False

            # 安全检查：拒绝推送删除超过 50 行的提交（防止 AI 误删代码）
            diff_stat = subprocess.run(
                "git diff --stat HEAD~1 HEAD", shell=True, capture_output=True, text=True
            ).stdout
            deleted_lines = 0
            for line in diff_stat.split("\n"):
                if "deletion" in line or "-" in line:
                    import re as _re
                    m = _re.search(r'(\d+) deletion', line)
                    if m:
                        deleted_lines += int(m.group(1))
            if deleted_lines > 50:
                print(f"    ✗ 安全拦截：本次修复删除了 {deleted_lines} 行代码（超过50行阈值），拒绝推送")
                subprocess.run("git reset HEAD~1 --soft", shell=True)
                return False

            token = os.environ.get("GITHUB_TOKEN", "")
            repo = os.environ.get("GITHUB_REPOSITORY", "")
            push_cmd = "git push https://x-access-token:" + token + "@github.com/" + repo + ".git"
            push_result = subprocess.run(push_cmd, shell=True)
            if push_result.returncode != 0:
                print("    ✗ 推送失败")
                return False
            print("    ✓ 推送成功")
        return True

    def _parse_json(self, text: str) -> Optional[Dict]:
        try:
            text = re.sub(r"^```json?|```$", "", text.strip(), flags=re.MULTILINE)
            return json.loads(text.strip())
        except (json.JSONDecodeError, ValueError):
            return None

    def _collect_context(self, script_name: str) -> str:
        parts = []
        if script_name and os.path.exists(script_name):
            with open(script_name, "r") as f: parts.append(f.read()[:5000])
        return "\n".join(parts) or "无额外上下文"

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Bounded workflow auto-fixer with explicit free/paid routing")
    parser.add_argument("error_log", nargs="?", default="")
    parser.add_argument("script_name", nargs="?")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--free-only", action="store_true")
    mode_group.add_argument("--paid-only", action="store_true")
    parser.add_argument("--free-status", default=os.environ.get("FREE_ROUTE_STATUS", ""))
    parser.add_argument("--github-output")
    cli_args = parser.parse_args()
    mode = "free-only" if cli_args.free_only else "paid-only" if cli_args.paid_only else "free-only"
    error_text = cli_args.error_log
    if os.path.isfile(error_text):
        with open(error_text, "r", encoding="utf-8") as f: error_text = f.read()
    fixer = WorkflowErrorFixer(mode=mode, free_status=cli_args.free_status)
    fixed = fixer.fix_error(error_text, cli_args.script_name or "")
    if cli_args.github_output:
        with open(cli_args.github_output, "a", encoding="utf-8") as output:
            output.write(f"free_status={fixer.route_status}\n")
    sys.exit(0 if fixed else 1)

#!/usr/bin/env python3
"""Merge crawler outputs from the four operators and apply publication rules.

Publication rules (config/filter_conditions.json):
  - exclude phone-contract plans (充话费送手机 / 购机合约套餐)
  - default display: general (non-directed) traffic >= 20 GB and monthly fee <= 69 CNY
  - also show cost-effective data add-on packs (流量包): >= 10 GB and <= 1 CNY/GB and fee <= 30
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SOURCE_FILES = {
    "unicom": "unicom.json",
    "broadnet": "broadnet.json",
    "mobile": "mobile.json",
    "telecom": "telecom.json",
}


def load_records(path: Path | str) -> list[dict[str, Any]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("items", "records", "data"):
            if isinstance(data.get(key), list):
                return data[key]
    raise ValueError(f"cannot load records from {path}")


def load_sources(data_dir: Path, source_files: dict[str, str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key, rel in source_files.items():
        path = data_dir / rel
        if not path.exists():
            print(f"  missing source: {path} (skip)")
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            print(f"  invalid source (not a list): {path}")
            continue
        rows.extend(data)
        print(f"  {key}: {len(data)} rows")
    return rows


def dedupe(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        key = (str(row.get("source") or ""), str(row.get("report_no") or row.get("plan_name") or ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


CONTRACT_STRONG_PATTERNS = [
    r"预存.*(?:得|送|返).*(?:券|话费|元)",
    r"(?:送|赠|送一|赠一).{0,6}手机",
    r"0元购机",
    r"购机(?:款|补贴|优惠|直降)",
    r"手机(?:0元|直降|补贴|分期)",
    r"终端(?:补贴|直降|优惠)",
    r"合约机",
    r"得电子券",
    r"存费送机",
    r"充.{0,4}送.{0,4}手机",
]


def has_strong_contract(row: dict[str, Any]) -> bool:
    """True if this plan is a phone-contract plan (充话费送手机/购机合约)."""
    if row.get("contract"):
        return True
    name = str(row.get("plan_name") or "")
    content = str(row.get("service_content") or "") + str(row.get("contract_desc") or "")
    text = f"{name} {content}"
    for pattern in CONTRACT_STRONG_PATTERNS:
        if re.search(pattern, text):
            return True
    # plan name marks like 合约（北京）
    if "合约" in name and ("手机" in name or "预存" in name or "电子券" in name or "购机" in name):
        return True
    return False


RESTRICTED_PATTERNS = [
    r"成长计划",
    r"档及以上",
    r"及以上档位",
    r"以上档位用户",
    r"限[^，。；（）]{0,10}(?:用户|客户|套餐|档)",
    r"专属",
    r"专享",
    r"需[^，。；]{0,12}套餐",
    r"校园",
    r"学生",
    r"特定用户",
    r"副卡办理",
    r"(?:仅限|限于|限定)[^，。；（）]{0,8}(?:用户|客户)",
]


def is_restricted(row: dict[str, Any]) -> bool:
    """True if the plan has an ordering threshold (main-plan tier / campus / exclusive).

    Only the plan name and use_scope are inspected: service_content contains
    marketing prose ("仅限新入网用户", "可办理副卡") that is NOT an ordering gate.
    """
    name = str(row.get("plan_name") or "")
    use_scope = str(row.get("use_scope") or row.get("适用范围") or "")
    text = f"{name} {use_scope}"
    for pattern in RESTRICTED_PATTERNS:
        if re.search(pattern, text):
            return True
    return False


def fix_region(row: dict[str, Any]) -> dict[str, Any]:
    """Refine region from the plan name's province mark (e.g. 畅越…（北京）)."""
    out = dict(row)
    name = str(out.get("plan_name") or "")
    m = re.search(r"（(北京|上海|广东|深圳|天津|重庆)）", name)
    if m and out.get("region") == "全国":
        out["region"] = m.group(1)
    return out


# ── multi-period fee normalization ──────────────────────────────────────
# Many broadband plans are advertised as a one-off total price for N months
# (e.g. "5340元五年期", "3204元/24个月", "2376元趸交合约-24月").
# The crawler stores the raw number in monthly_fee, which mixes totals with
# true monthly fees.  normalize_monthly_fee() detects multi-period totals and
# converts them to an equivalent monthly rate, preserving the original as
# original_fee and recording the billing period for UI display.

_CN_YEAR_MONTHS = {"一": 12, "两": 24, "二": 24, "三": 36, "四": 48, "五": 60}
_CN_DIGIT_MONTHS = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12}


def _extract_period_months(name: str, valid_period: str) -> int | None:
    """Extract the contract/billing period in months from the plan name or
    valid_period text.

    Handles patterns:
      名称: "X元/36个月", "X元三年期", "X元两年期", "趸交…24月"
      valid_period: "36个月", "24个月", "五年", "三年", "12个月"
      Chinese months: "两个月" (=2), "三个月" (=3)
    """
    text = f"{name} {valid_period}"
    # strip full dates first: "2029年12月31日" must not yield "12月" as the period
    text = re.sub(r"\d{4}年\d{1,2}月\d{1,2}日", " ", text)
    # yearly/half-year packs: "流量年包" (=12), "流量半年包" (=6) — check
    # 半年包 before 年包 since it contains the substring
    if re.search(r"半年包", name):
        return 6
    if re.search(r"年包", name):
        return 12
    # digit months: "36个月", "24月", "/24个月"
    m = re.search(r"/?(\d+)\s*个?月", text)
    if m:
        return int(m.group(1))
    # Arabic-digit years: "3年期", "3200元3年期" — limited to 1-5 years to
    # avoid matching dates like "2029年12月31日"
    m = re.search(r"([1-5])\s*年期?", text)
    if m:
        return int(m.group(1)) * 12
    # Chinese year: "三年期", "五年", "两年" — must be followed by 年
    # (not just 月) to avoid false match on "两个月"
    m = re.search(r"(一|两|二|三|四|五)\s*年(?:期|)?", text)
    if m:
        return _CN_YEAR_MONTHS.get(m.group(1))
    # Chinese months: "两个月", "三个月" (value from _CN_DIGIT_MONTHS)
    m = re.search(r"(一|两|二|三|四|五|六|七|八|九|十|十一|十二)\s*个月", text)
    if m:
        return _CN_DIGIT_MONTHS.get(m.group(1))
    return None


def normalize_monthly_fee(row: dict[str, Any]) -> dict[str, Any]:
    """Convert multi-period total fees to equivalent monthly rates.

    Returns the row with added fields:
      - monthly_fee: the effective monthly fee (折算 or original)
      - original_fee: the raw fee before normalization (None if unchanged)
      - fee_type: "monthly" | "total_period" | "prepaid_monthly_return"
      - billing_period: str for UI display, e.g. "五年期", "36个月", "24月"
    """
    out = dict(row)
    name = str(out.get("plan_name") or "")
    valid_period = str(out.get("valid_period") or "")
    raw_fee = out.get("monthly_fee")
    fee_text = str(out.get("fee_text") or "")

    out.setdefault("original_fee", None)
    out.setdefault("fee_type", "monthly")
    out.setdefault("billing_period", None)

    if raw_fee is None or not isinstance(raw_fee, (int, float)) or raw_fee <= 0:
        return out

    # ── Priority 1: "月费由X优惠至Y元" → Y is the true monthly fee ──
    m = re.search(r"月费由[\d.]+(?:元)?(?:优惠至?|降至|降为)(\d+(?:\.\d+)?)元?", name)
    if m:
        monthly = float(m.group(1))
        out["original_fee"] = raw_fee if monthly != raw_fee else None
        out["fee_type"] = "monthly"
        out["monthly_fee"] = monthly
        return out

    # ── Priority 2: "预存X元月返Y元" → Y is the effective monthly fee ──
    m = re.search(r"月返(\d+(?:\.\d+)?)元", name)
    if m:
        monthly = float(m.group(1))
        out["original_fee"] = raw_fee
        out["fee_type"] = "prepaid_monthly_return"
        out["monthly_fee"] = monthly
        # billing period from name suffix like "-24月" or valid_period
        mp = re.search(r"-(\d+)\s*月", name)
        if mp:
            out["billing_period"] = f"{mp.group(1)}个月"
        else:
            months = _extract_period_months(name, valid_period)
            if months:
                out["billing_period"] = f"{months}个月"
        return out

    # ── Priority 3: total price ÷ period months ──
    # Only apply when the plan name has strong multi-period总价 signals to
    # avoid converting a genuine monthly fee that happens to contain digit
    # periods (e.g. a 159元/月 plan mentioned "24个月" in valid_period).
    months = _extract_period_months(name, valid_period)
    is_total = (
        "趸交" in name
        or re.search(r"\d+(?:\.\d+)?元\s*/?\s*\d+\s*个?月", name) is not None
        # 年期/年/两年 etc. — name only, valid_period may carry "两年。到期…"
        # for plain monthly plans, so never use valid_period for this signal
        or re.search(r"(?:一|两|二|三|四|五|[1-5])\s*年(?:期|合约)?", name) is not None
        or re.search(r"年包|半年包", name) is not None
    )
    if months and months > 0 and raw_fee > 100 and is_total:
        monthly = round(raw_fee / months, 1)
        # build billing_period label
        # prefer explicit "X元/N个月" or "X元N年期" from the name
        m = re.search(r"(\d+(?:\.\d+)?)元\s*/?\s*(\d+)\s*个?月", name)
        if m:
            out["billing_period"] = f"{m.group(2)}个月"
        else:
            m = re.search(r"(一|两|二|三|四|五|[1-5])\s*年期", name)
            if m:
                out["billing_period"] = f"{m.group(1)}年期"
            elif months >= 12 and months % 12 == 0:
                yrs = months // 12
                out["billing_period"] = f"{yrs}年" if yrs > 1 else "1年"
            else:
                out["billing_period"] = f"{months}个月"
        out["original_fee"] = raw_fee
        out["fee_type"] = "total_period"
        out["monthly_fee"] = monthly
        return out

    # ── No normalization needed ──
    return out


def quality_flags(row: dict[str, Any]) -> list[str]:
    """Deterministic data-quality checks. Flagged records never default-show.

    Catches parser bugs the reviewer model cannot see by reading a diff:
    e.g. a traffic value of 1024/4096 means the source unit was MB, not GB.
    """
    flags: list[str] = []
    traffic = row.get("general_traffic_gb")
    fee = row.get("monthly_fee")
    name = str(row.get("plan_name") or "")
    content = str(row.get("service_content") or "")
    text = f"{name} {content}"

    if traffic is not None and traffic > 512:
        # >512 GB monthly is implausible unless the text explicitly says so
        if not re.search(r"(?:[5-9]\d{2,}|\d{4,})\s*(?:GB|G|TB|T)\b", text):
            flags.append("traffic_unit_suspect")
    if fee is not None and fee > 1000:
        # After normalize_monthly_fee, monthly_fee is the effective monthly
        # rate.  Only flag if no normalization happened (original_fee is None
        # means the fee was already monthly).
        orig = row.get("original_fee")
        if orig is None and fee > 1000:
            flags.append("fee_outlier")
    return flags


BROADBAND_KEYWORDS = ["宽带", "光纤", "FTTH", "全光", "单宽", "方宽", "长宽"]

# 提速/加装/子产品/附加服务不含宽带线路本体（2026-08-12 分类审计 26 条实锤）：
# - 电信提速包/提速小合约/上行提速/提速到（千兆提速包、200M提速到300M宽带提速包）：仅速率
# - 加装包/流量补充/补充月包（FWA通用流量加装包、臻宽带…20GB流量补充月包）：流量加装
# - 公网IP（固网宽带-开通公网IP服务）：宽带附加服务
# - 移网主卡（联通智家…套餐5G移网主卡）：融合套餐的子产品，本身是移网卡，宽带仅营销词
BOLT_ON_OR_SUBPRODUCT_RE = re.compile(
    r"提速包|提速小合约|上行提速包|加装包|公网IP|流量补充|补充月包|移网主卡"
)

# plan_type 语义（2026-08-12 分类审计 7 条实锤：Mini会员/奔马权益会员/5G-A场景包/5G升级功能包/
# 云智手机服务/公网IP/提速包 被源站标"流量包"——会员/场景包/功能包/服务/附加件不是数据包）
# DATA_PACK 用"流量年包"而非裸"年包"（防"宽带年包/宽带包年"等宽带年付误判为流量包）；
# 补充月包是流量补充包（归流量包），不在 NON_DATA_PACK
DATA_PACK_NAME_RE = re.compile(r"流量年包|流量包|加量包|加油包|日租包|补充月包|权益流量包")
NON_DATA_PACK_NAME_RE = re.compile(r"会员|场景包|功能包|服务包|服务高配|公网IP|提速包|提速小合约")

# service_content 中"宽带"字样不一定是宽带产品：营销文案常见功能描述/否定语境
# （"亲情守护-宽带上网安全"、"不可同时办理含宽带的营销案"）。
# content 命中规则：速率+宽带、宽带+产品词、含/加装等实体前缀；并排除否定前缀窗口。
BROADBAND_CONTENT_PATTERN = re.compile(
    r"\d+(?:\.\d+)?\s*M\s*宽带"
    r"|宽带(?:融合|套餐|提速|包年|包月|一年|两年|单月|光网|千兆|速率|光纤)"
    r"|(?:含|加装|叠加|绑定|融合|赠送|送|提供|可办理|可叠加|开通|办理|新增|增配|第二条|含一条)[^，。；（）]{0,10}宽带"
    r"|光纤|FTTH|FTTR|全光|单宽|方宽|长宽"
)
BROADBAND_NEGATION_PREFIX = (
    "不可", "不支持", "不含", "不能", "无需", "不得", "互斥", "禁止", "排除", "不适用", "无法", "不提供",
)


def _content_is_broadband(content: str) -> bool:
    """service_content 是否表达宽带产品实体（排除否定与功能描述语境）。"""
    for m in BROADBAND_CONTENT_PATTERN.finditer(content):
        # 否定词检查覆盖匹配点前后（"…与FTTR类、宽带类…互斥"的互斥在后方）
        window = content[max(0, m.start() - 20) : min(len(content), m.end() + 25)]
        if any(n in window for n in BROADBAND_NEGATION_PREFIX):
            continue
        return True
    return False


def is_broadband(row: dict[str, Any]) -> bool:
    """True if the plan includes broadband (standalone or bundled with mobile)."""
    name = str(row.get("plan_name") or "")
    broadband = str(row.get("broadband") or "")
    content = str(row.get("service_content") or "")[:200]
    # 提速包/加装包/公网IP/移网主卡等加装件与子产品不含宽带线路本体。
    # 仅匹配产品名称（26 条审计实锤全部在名称命中；content/broadband 不参与，
    # 防真实融合套餐的附加权益描述（含"提速/流量补充"字样）被误伤）。
    if BOLT_ON_OR_SUBPRODUCT_RE.search(name):
        return False
    if broadband and broadband not in ("无", "0", "-"):
        return True
    if any(kw in name for kw in BROADBAND_KEYWORDS):
        return True
    return _content_is_broadband(content)


CAMPUS_KEYWORDS = ("校园", "高校", "学生")


def is_campus_broadband(row: dict[str, Any]) -> bool:
    """校园专属宽带（限制高校区域/校园内使用），不应进入公开 Pages。

    依据运营商 use_scope 实测：\"校园用户可办理\"、\"北京校园沃派用户且宽带校园内使用\"、
    \"北京联通WiFi新融合进线的高校学生用户\"——均为高校区域限定，普通用户不可办。
    仅对宽带行生效；校园流量卡等非宽带产品不在本次排除范围。
    """
    if not row.get("is_broadband"):
        return False
    text = " ".join(
        str(row.get(field) or "")
        for field in ("plan_name", "use_scope", "service_content", "broadband")
    )[:300]
    return any(kw in text for kw in CAMPUS_KEYWORDS)


ACCESS_METHOD_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("FTTR", re.compile(r"FTTR|全屋光|光纤到房间|光组网|光网WiFi", re.IGNORECASE)),
    ("光纤", re.compile(r"光纤|FTTH|全光|光网|光猫|GPON|XG-?PON|10G-?PON", re.IGNORECASE)),
    ("同轴(HFC)", re.compile(r"HFC|同轴|Cable", re.IGNORECASE)),
    ("无线(FWA)", re.compile(r"FWA|无线宽带|CPE|5G路由器|4G路由器", re.IGNORECASE)),
    ("ADSL", re.compile(r"ADSL|铜缆|电话线", re.IGNORECASE)),
]

# 移网速率语境（5G-A 峰值速率 / 移动上网速率 / 网络最高下行等），命中则跳过该带宽候选，
# 避免把移网速率（下行3Gbps）误当宽带带宽。裸"网络"不算（HFC网络/FTTH网络是宽带接入网）。
MOBILE_SPEED_MARKERS = re.compile(r"移动|5G|移网|上网|峰值速率|网络最高")


def _is_mobile_speed_context(text: str, match_start: int) -> bool:
    """True if the 15 chars before match_start suggest a mobile-network speed phrase."""
    pre = text[max(0, match_start - 15) : match_start]
    return bool(MOBILE_SPEED_MARKERS.search(pre))


def extract_access_method(row: dict[str, Any]) -> str | None:
    """接入方式归一化：FTTR / 光纤 / 同轴(HFC) / 无线(FWA) / ADSL；无线索返回 None。"""
    if not row.get("is_broadband"):
        return None
    for field in ("broadband", "plan_name", "service_content"):
        text = str(row.get(field) or "")[:400]
        for label, pattern in ACCESS_METHOD_RULES:
            if pattern.search(text):
                return label
    return None


def extract_broadband_mbps(row: dict[str, Any]) -> int | None:
    """下行带宽（Mbps）。从 broadband 字段/套餐名/资费内容提取并取最大值。

    规则：
      - broadband 字段与套餐名中的 ``N M`` 直接识别（1000M、1000M（上行40M）、一条1000M宽带）；
      - ``千兆``/``万兆`` 映射 1000/10000；``N Gbps`` 按 1000 倍换算（``5G套餐`` 里的 5G 不换算）；
      - 前两层未命中时，仅在 service_content 的带宽语境（下行/提速/速率/带宽）附近识别，
        避免把流量数字（20GB）误当带宽。
    """
    if not row.get("is_broadband"):
        return None
    bb = str(row.get("broadband") or "")
    name = str(row.get("plan_name") or "")
    content = str(row.get("service_content") or "")[:400]

    candidates: list[float] = []
    for text in (bb, name):
        for m in re.finditer(r"(\d+(?:\.\d+)?)\s*M(?![0-9A-Za-z])", text):
            if not _is_mobile_speed_context(text, m.start()):
                candidates.append(float(m.group(1)))
        for m in re.finditer(r"(\d+(?:\.\d+)?)\s*Gbps?(?![0-9A-Za-z])", text, re.IGNORECASE):
            if not _is_mobile_speed_context(text, m.start()):
                candidates.append(float(m.group(1)) * 1000)
        if "千兆" in text:
            candidates.append(1000.0)
        if "万兆" in text:
            candidates.append(10000.0)

    if not candidates:
        for m in re.finditer(
            r"(?:下行(?:最高|速率|可达)?|提速(?:至|到)?|速率|带宽)"
            r"[^，。；（）]{0,10}?(\d+(?:\.\d+)?)\s*Mbps?(?![0-9A-Za-z])",
            content,
            re.IGNORECASE,
        ):
            if not _is_mobile_speed_context(content, m.start()):
                candidates.append(float(m.group(1)))
        for m in re.finditer(
            r"(?:下行(?:最高|速率|可达)?|提速(?:至|到)?|速率|带宽)"
            r"[^，。；（）]{0,10}?(\d+(?:\.\d+)?)\s*(?:Gbps?|GB)(?![0-9A-Za-z])",
            content,
            re.IGNORECASE,
        ):
            if not _is_mobile_speed_context(content, m.start()):
                candidates.append(float(m.group(1)) * 1000)
        # 宽带实体语境：加装/第二条/含一条…后紧跟的带宽数字（bb 字段被截断时兜底）
        for m in re.finditer(
            r"(?:宽带|光网|光纤|加装|第二条|一条|含一条|包含|含)[^，。；（）]{0,10}?"
            r"(\d+(?:\.\d+)?)\s*M(?![0-9A-Za-z])",
            content,
        ):
            if not _is_mobile_speed_context(content, m.start()):
                candidates.append(float(m.group(1)))

    if not candidates:
        return None
    return int(round(max(candidates)))


def classify(row: dict[str, Any]) -> dict[str, Any]:
    """Return (publishable, default_show, reason)."""
    out = fix_region(row)
    out = normalize_monthly_fee(out)
    # plan_type 语义规整（2026-08-12 分类审计 7 条实锤）：
    # 会员/场景包/功能包/服务包/公网IP/提速包 不是"流量包"（流量包仅限真数据包）。
    # 保护原值：仅当原 plan_type=流量包 且命中非数据包词 → 改套餐（修审计 7 条）；
    # DATA_PACK 命中仅用于原值缺失/异常时修正，不覆盖源站已正确标"套餐"的真流量包。
    _name = str(out.get("plan_name") or "")
    _pt = str(out.get("plan_type") or "")
    if _pt == "流量包" and NON_DATA_PACK_NAME_RE.search(_name):
        out["plan_type"] = "套餐"
    elif _pt not in ("套餐", "流量包"):
        if DATA_PACK_NAME_RE.search(_name):
            out["plan_type"] = "流量包"
        elif NON_DATA_PACK_NAME_RE.search(_name):
            out["plan_type"] = "套餐"
    out["excluded_phone_contract"] = has_strong_contract(out)
    out["restricted"] = is_restricted(out)
    out["quality_flags"] = quality_flags(out)
    out["is_broadband"] = is_broadband(out)
    out["excluded_campus"] = is_campus_broadband(out)
    # 月租 > 200 元的高端套餐不进 Pages（用户要求：非富即贵系列不展示）
    out["excluded_high_fee"] = bool(
        out.get("monthly_fee") is not None and out.get("monthly_fee") > 200
    )
    out["broadband_mbps"] = extract_broadband_mbps(out)
    out["access_method"] = extract_access_method(out)

    plan_type = str(out.get("plan_type") or "套餐")
    fee = out.get("monthly_fee")
    traffic = out.get("general_traffic_gb")
    fee_ok = fee is not None and 0 < fee <= 69
    traffic_ok = traffic is not None and traffic >= 20

    default_show = False
    if plan_type == "流量包":
        # cost-effective data pack: >=10 GB, <=1 CNY/GB, fee <= 30
        fee_pack_ok = fee is not None and 0 < fee <= 30
        traffic_pack_ok = traffic is not None and traffic >= 10
        per_gb = (fee / traffic) if fee is not None and traffic else None
        if fee_pack_ok and traffic_pack_ok and per_gb is not None and per_gb <= 1.0:
            default_show = True
    elif plan_type == "套餐":
        default_show = fee_ok and traffic_ok

    out["default_show"] = bool(
        default_show and not out["excluded_phone_contract"] and not out["restricted"]
        and not out["quality_flags"]
    )
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--output", default="data/merged.json")
    parser.add_argument("--min-records", type=int, default=30)
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    rows = load_sources(data_dir, SOURCE_FILES)
    if len(rows) < args.min_records:
        print(f"FAIL: only {len(rows)} raw rows (< {args.min_records})", file=sys.stderr)
        return 2

    rows = dedupe(rows)
    classified = [classify(r) for r in rows]
    classified.sort(key=lambda r: (r["default_show"] is False, r.get("source") or "", r.get("monthly_fee") or 0))

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(classified, ensure_ascii=False, indent=1), encoding="utf-8")

    shown = [r for r in classified if r.get("default_show")]
    excluded = [r for r in classified if r.get("excluded_phone_contract")]
    print(f"merged: {len(classified)} rows (sources: {sorted({r.get('source') for r in classified})})")
    print(f"  excluded phone-contract: {len(excluded)}")
    print(f"  default shown: {len(shown)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

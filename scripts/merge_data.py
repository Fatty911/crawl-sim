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
        flags.append("fee_outlier")
    return flags


BROADBAND_KEYWORDS = ["宽带", "光纤", "FTTH", "全光", "单宽", "方宽", "长宽"]


def is_broadband(row: dict[str, Any]) -> bool:
    """True if the plan includes broadband (standalone or bundled with mobile)."""
    name = str(row.get("plan_name") or "")
    broadband = str(row.get("broadband") or "")
    content = str(row.get("service_content") or "")[:200]
    if broadband and broadband not in ("无", "0", "-"):
        return True
    text = f"{name} {content}"
    return any(kw in text for kw in BROADBAND_KEYWORDS)


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
    out["excluded_phone_contract"] = has_strong_contract(row)
    out["restricted"] = is_restricted(row)
    out["quality_flags"] = quality_flags(row)
    out["is_broadband"] = is_broadband(row)
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

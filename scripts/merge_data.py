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


def classify(row: dict[str, Any]) -> dict[str, Any]:
    """Return (publishable, default_show, reason)."""
    out = dict(row)
    out["excluded_phone_contract"] = has_strong_contract(row)

    plan_type = str(row.get("plan_type") or "套餐")
    fee = row.get("monthly_fee")
    traffic = row.get("general_traffic_gb")
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

    out["default_show"] = bool(default_show and not out["excluded_phone_contract"])
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

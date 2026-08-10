#!/usr/bin/env python3
"""Crawl China Broadnet (中国广电) tariff data from 资费公示 API.

API reverse-engineered from https://m.10099.com.cn/costNotice/
  - /contact-web/api/busi/qryAreaList       -> area codes (BJ00=北京, ZZZZ=全国)
  - /contact-web/api/goods/queryTariffCondition -> category tree
  - /contact-web/api/goods/queryTariffAllByCond -> plan details (POST JSON)

No login required. Works over the mihomo proxy via standard env vars.
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

import requests

BASE = "https://m.10099.com.cn/contact-web/api/goods"
SOURCE = "中国广电"
SOURCE_SHORT = "广电"
CHANNEL_ID = "cd_20220914_514144"


def session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "Content-Type": "application/json",
            "Referer": "https://m.10099.com.cn/costNotice/",
            "Origin": "https://m.10099.com.cn",
        }
    )
    return s


def post(s: requests.Session, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    payload = {**payload, "timestamp": int(time.time() * 1000)}
    for attempt in range(4):
        try:
            resp = s.post(f"{BASE}/{path}", json=payload, timeout=30)
            data = resp.json()
            if data.get("status") == "000000":
                return data
            if data.get("status") == "704":  # 查询无数据 -> normal empty result
                return {"status": "000000", "data": []}
            raise RuntimeError(f"api error status={data.get('status')} msg={data.get('message')}")
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            if attempt == 3:
                raise
            print(f"  retry {attempt + 1} after {type(exc).__name__}: {exc}")
            time.sleep(3 * (attempt + 1))
    raise RuntimeError("unreachable")


def fetch_all(s: requests.Session, area_code: str, region_label: str) -> list[dict[str, Any]]:
    """Fetch all plans for one area (ZZZZ=全国, BJ00=北京)."""
    # category tree
    tree = post(s, "queryTariffCondition", {"channelId": CHANNEL_ID, "applicableArea": area_code})
    categories: list[tuple[str, str, str]] = []
    for top in tree.get("data", []):
        type1 = top.get("typeCode", "")
        for second in top.get("childTariffTypes", []):
            type2 = second.get("typeCode", "")
            for third in second.get("childTariffTypes", []):
                type3 = third.get("typeCode", "")
                categories.append((type1, type2, type3))
            if not second.get("childTariffTypes"):
                categories.append((type1, type2, ""))
    if not categories:
        categories = [("GZ", "GZ_TC", "")]
    print(f"  {region_label}: {len(categories)} categories")

    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for type1, type2, type3 in categories:
        data = post(
            s,
            "queryTariffAllByCond",
            {
                "channelId": CHANNEL_ID,
                "type1": type1,
                "type2": type2,
                "type3": type3,
                "productName": "",
                "stateFlag": "1",
                "minPrice": "",
                "maxPrice": "",
                "applicableArea": area_code,
                "productLikeCond": "",
            },
        )
        for record in data.get("data", []):
            if not isinstance(record, dict):
                continue
            rid = str(record.get("id") or "")
            if rid in seen_ids:
                continue
            seen_ids.add(rid)
            rows.append((record, region_label))
        print(f"  {region_label} {type1}/{type2}/{type3}: +{len(data.get('data', []))} (unique {len(rows)})")
        time.sleep(0.5)
    return rows


def normalize(raw: dict[str, Any], region: str) -> dict[str, Any]:
    name = str(raw.get("productName") or "").strip()
    fee = _num(raw.get("productPrice"))  # unit: 分
    fee_unit = str(raw.get("productPriceUnit") or "月").strip()
    # productPrice is in cents; convert to yuan. Yearly products become monthly average.
    fee_yuan = None
    if fee is not None:
        if fee_unit == "年":
            fee_yuan = round(fee / 100 / 12, 2)
        else:
            fee_yuan = round(fee / 100, 2)
    traffic = _num(raw.get("domesticTraffic"))
    orient = _num(raw.get("orientTraffic"))
    voice = _num(raw.get("domesticCall"))
    sms = _num(raw.get("sms"))
    broadband = str(raw.get("bandwidth") or "").strip()
    if not broadband or broadband == "无":
        if re.search(r"宽带|光纤|FTTH|全光|方宽|长宽", name):
            broadband = name
    service_content = str(raw.get("otherContent") or raw.get("tariffAttr") or "").strip()
    overage = str(raw.get("tariffAttr") or "").strip()
    return {
        "source": SOURCE,
        "atomic_source_names": [SOURCE_SHORT],
        "plan_name": name,
        "report_no": str(raw.get("filingNumber") or "").strip(),
        "region": region,
        "plan_type": "套餐" if "TC" in str(raw.get("parentTypeCode") or "") else "流量包",
        "monthly_fee": fee_yuan,
        "fee_text": f"{fee_yuan}元/{fee_unit}" if fee_yuan is not None else "",
        "general_traffic_gb": traffic,
        "orient_traffic_gb": orient,
        "voice_minutes": voice,
        "sms": sms,
        "broadband": broadband,
        "contract": is_contract(name, service_content),
        "contract_desc": contract_desc(name, service_content),
        "valid_period": str(raw.get("validPeriod") or "").strip(),
        "sale_channel": str(raw.get("saleChannel") or "").strip(),
        "use_scope": str(raw.get("applicablePeople") or "").strip(),
        "overage": overage,
        "service_content": service_content,
        "online_date": str(raw.get("onlineDay") or "").strip(),
        "offline_date": str(raw.get("offlineDay") or "").strip(),
        "raw_url": "https://m.10099.com.cn/costNotice/",
        "crawled_at": datetime.now(timezone.utc).isoformat(),
    }


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


CONTRACT_KEYWORDS = [
    "合约", "预存", "送手机", "购机", "0元购", "终端", "电子券", "购机款",
    "得手机", "话费购", "保底", "分期", "违约金",
]


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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="data/broadnet.json")
    parser.add_argument("--min-records", type=int, default=20)
    parser.add_argument("--delay", type=float, default=0.5)
    args = parser.parse_args()

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    s = session()
    rows: list[dict[str, Any]] = []
    for area_code, region_label in (("ZZZZ", "全国"), ("BJ00", "北京")):
        for raw, region in fetch_all(s, area_code, region_label):
            record = normalize(raw, region)
            rows.append(record)
            if args.delay > 0:
                time.sleep(args.delay)

    if len(rows) < args.min_records:
        print(f"FAIL: only {len(rows)} rows (< {args.min_records})", file=sys.stderr)
        return 2

    out_path.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"OK: {len(rows)} rows -> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

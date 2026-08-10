#!/usr/bin/env python3
"""Crawl China Unicom (中国联通) tariff data from the 资费专区 API.

API reverse-engineered from https://imgxx.client.10010.com/zifeizhuanquwt/
  - indexData          -> province list + category tree
  - threeLevelName     -> plan id list (POST form)
  - operateData/{ids}  -> plan details (POST form)

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

BASE = "https://mxx.client.10010.com/servicequerybusiness"
SOURCE = "中国联通"
SOURCE_SHORT = "联通"


def session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": "https://imgxx.client.10010.com/",
            "Origin": "https://imgxx.client.10010.com",
        }
    )
    return s


def post(s: requests.Session, path: str, params: dict[str, str]) -> dict[str, Any]:
    body = {
        "duanlianjieabc": "",
        "channelCode": "",
        "serviceType": "",
        "saleChannel": "",
        "externalSources": "",
        "contactCode": "",
        "version": "WT",
        **params,
        "behaviorId": "CRWLSIM20260810",
    }
    for attempt in range(4):
        try:
            resp = s.post(f"{BASE}/{path}", data=body, timeout=30)
            data = resp.json()
            if data.get("code") == "0000":
                return data
            raise RuntimeError(f"api error code={data.get('code')} msg={data.get('msg')}")
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            if attempt == 3:
                raise
            print(f"  retry {attempt + 1} after {type(exc).__name__}: {exc}")
            time.sleep(3 * (attempt + 1))
    raise RuntimeError("unreachable")


def fetch_plans(s: requests.Session, province_id: str, city_id: str) -> list[dict[str, Any]]:
    """Fetch all mobile plans (套餐/移网) for one region."""
    rows: list[dict[str, Any]] = []
    # all ids for 套餐->移网
    for tariff_attributes, scope in (("1", "本省"), ("2", "全国")):
        for first_level, first_name, second_level, second_name in (
            ("1", "套餐", "1001", "移网"),
            ("2", "加装包", "2001", "流量包"),
        ):
            params: dict[str, str] = {
                "tariffAttributes": tariff_attributes,
                "firstLevel": first_level,
                "secondLevel": second_level,
            }
            if tariff_attributes == "1":
                params["provinceId"] = province_id
                params["cityId"] = city_id
            else:
                params["provinceId"] = ""
                params["cityId"] = ""
            data = post(s, "queryTariffNew/threeLevelName", params)
            ids = [item["id"] for item in data.get("data", {}).get("dataList", [])]
            if not ids:
                print(f"  no plans: {scope}/{first_name}/{second_name}")
                continue
            # operateData expects underscore-joined ids; batch up to 50 per call
            for i in range(0, len(ids), 50):
                batch = ids[i : i + 50]
                detail = post(
                    s,
                    f"queryTariffNew/operateData/{'_'.join(batch)}",
                    {
                        "page": "1",
                        "size": str(len(batch)),
                        **({"provinceId": province_id, "cityId": city_id} if tariff_attributes == "1" else {}),
                    },
                )
                for record in detail.get("data", {}).get("dataList", []):
                    if not isinstance(record, dict):
                        print(f"  skip non-dict record: {str(record)[:80]}")
                        continue
                    rows.append((record, scope, f"{first_name}/{second_name}"))
                print(f"  {scope} {first_name}/{second_name}: +{len(batch)} (total {len(rows)})")
                time.sleep(1)
    return rows


def normalize(raw: dict[str, Any], scope: str, category: str) -> dict[str, Any]:
    details = raw.get("detailsList")
    detail = details[0] if isinstance(details, list) and details and isinstance(details[0], dict) else {}
    name = str(raw.get("name") or detail.get("name") or "").strip()
    report_no = str(raw.get("reportNo") or detail.get("reportNo") or "").strip()
    fee_text = str(detail.get("feesStandard") or "").strip()
    fee = None
    m = re.search(r"(\d+(?:\.\d+)?)", fee_text)
    if m:
        fee = float(m.group(1))
    traffic_text = str(detail.get("commonData") or "").strip()
    traffic = None
    m = re.search(r"(\d+(?:\.\d+)?)", traffic_text)
    if m:
        traffic = float(m.group(1))
    orient_text = str(detail.get("orientTraffic") or "").strip()
    orient = None
    m = re.search(r"(\d+(?:\.\d+)?)", orient_text)
    if m:
        orient = float(m.group(1))
    voice_text = str(detail.get("minute") or "").strip()
    voice = None
    m = re.search(r"(\d+(?:\.\d+)?)", voice_text)
    if m:
        voice = float(m.group(1))
    service_content = str(detail.get("serviceContent") or "").strip()
    overage = str(detail.get("extraFees") or "").strip()
    return {
        "source": SOURCE,
        "atomic_source_names": [SOURCE_SHORT],
        "plan_name": name,
        "report_no": report_no,
        "region": "全国" if scope == "全国" else "北京",
        "plan_type": "套餐" if category.startswith("套餐") else "流量包",
        "monthly_fee": fee,
        "fee_text": fee_text,
        "general_traffic_gb": traffic,
        "orient_traffic_gb": orient,
        "voice_minutes": voice,
        "sms": _to_float(detail.get("sms")),
        "contract": is_contract(name, service_content),
        "contract_desc": contract_desc(name, service_content),
        "valid_period": str(detail.get("validPeriod") or "").strip(),
        "sale_channel": str(detail.get("saleChnl") or "").strip(),
        "use_scope": str(detail.get("useScope") or "").strip(),
        "overage": overage,
        "service_content": service_content,
        "online_date": str(detail.get("startDate") or "").strip(),
        "offline_date": str(detail.get("endDate") or "").strip(),
        "raw_url": f"https://imgxx.client.10010.com/zifeizhuanquwt/",
        "crawled_at": datetime.now(timezone.utc).isoformat(),
    }


def _to_float(value: Any) -> float | None:
    try:
        return float(str(value or "").strip() or 0)
    except ValueError:
        return None


CONTRACT_KEYWORDS = [
    "合约", "预存", "送手机", "购机", "0元购", "终端", "电子券", "购机款",
    "得手机", "话费购", "保底", "分期", "违约金", "在网要求",
]


def is_contract(name: str, content: str) -> bool:
    text = f"{name} {content}"
    hits = [k for k in CONTRACT_KEYWORDS if k in text]
    # pure data plans / discounts are not phone-contract plans; require a strong signal
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
    parser.add_argument("--output", default="data/unicom.json")
    parser.add_argument("--province-id", default="011")
    parser.add_argument("--city-id", default="110")
    parser.add_argument("--min-records", type=int, default=20)
    parser.add_argument("--delay", type=float, default=1.0)
    args = parser.parse_args()

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    s = session()
    index = post(s, "queryTariffNew/indexData", {"provinceId": args.province_id, "cityId": args.city_id})
    prov_name = index.get("data", {}).get("userProName", "")
    print(f"user region: {prov_name} ({args.province_id}/{args.city_id})")

    rows: list[dict[str, Any]] = []
    for raw, scope, category in fetch_plans(s, args.province_id, args.city_id):
        record = normalize(raw, scope, category)
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

#!/usr/bin/env python3
"""Validate release data and stage it with filter config for Pages."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

try:
    from scripts.audit_pages_payload import audit_payload
except ModuleNotFoundError:
    from audit_pages_payload import audit_payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--config", default="config/filter_conditions.json")
    parser.add_argument("--docs-dir", default="docs")
    args = parser.parse_args()
    payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
    errors = audit_payload(payload)
    if errors:
        raise SystemExit("invalid release payload: " + "; ".join(errors))
    docs_data = Path(args.docs_dir) / "data"
    docs_data.mkdir(parents=True, exist_ok=True)
    (docs_data / "latest.json").write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    (docs_data / "filter_conditions.json").write_text(
        json.dumps(config, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    source_counts = {
        source: sum(
            source in item.get("atomic_source_names", [])
            for item in payload["items"]
        )
        for source in payload.get("sources", [])
    }
    manifest = {
        "schemaVersion": payload.get("schema_version", 1),
        "updatedAt": payload["generated_at"],
        "rowCount": payload["count"],
        "sourceCounts": source_counts,
        "files": {
            "latestJson": "data/latest.json",
            "filterConditions": "data/filter_conditions.json",
        },
    }
    (docs_data / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    print(f"prepared {payload['count']} records in {docs_data}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

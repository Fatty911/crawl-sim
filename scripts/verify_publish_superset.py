#!/usr/bin/env python3
"""Fail if a proposed payload drops an eligible published identity."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

try:
    from scripts.merge_data import build_identity_key, meets_publish_requirements
except ModuleNotFoundError:
    from merge_data import build_identity_key, meets_publish_requirements


def identities(payload: dict[str, Any], eligible_only: bool = False) -> set[str]:
    result = set()
    for item in payload.get("items", []):
        if eligible_only and not meets_publish_requirements(item)[0]:
            continue
        # Recompute with the current identity schema so a deliberate identity
        # migration does not look like a total publish shrink merely because
        # the persisted hash values were produced by an older algorithm.
        result.add(build_identity_key(item))
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--candidate", required=True)
    args = parser.parse_args()
    baseline_path = Path(args.baseline)
    if not baseline_path.exists() or baseline_path.stat().st_size == 0:
        print("no baseline release; superset check skipped for first publication")
        return 0
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    candidate = json.loads(Path(args.candidate).read_text(encoding="utf-8"))
    missing = identities(baseline, eligible_only=True) - identities(candidate)
    if missing:
        print(f"publish shrink detected: {len(missing)} identities missing", file=sys.stderr)
        for key in sorted(missing)[:20]:
            print(f"  {key}", file=sys.stderr)
        return 2
    print(f"superset verified: {len(identities(baseline, eligible_only=True))} baseline identities retained")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

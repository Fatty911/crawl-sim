#!/usr/bin/env python3
"""Shared long-run crawl runtime: incremental cursor, time budget, pacing.

Ported from the crawl_phones long-run architecture.  Batch crawls keep their
existing one-shot behavior; a crawler only enters incremental mode when the
caller passes a progress directory.
"""

from __future__ import annotations

import json
import os
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

PROGRESS_NAME = "progress.json"
ITEMS_NAME = "items.jsonl"
ENRICHED_NAME = "enriched.jsonl"


@dataclass
class Progress:
    """Incremental crawl cursor.

    ``current_page`` is the next list page to scan, ``scan_complete`` marks
    the end of the ranking scan, ``processed_ids`` records items already
    enriched so a resumed run never re-fetches a detail page.  The cursor is
    saved unconditionally after every scan/enrich step, exactly like the
    phone crawlers do.
    """

    current_page: int = 1
    scan_complete: bool = False
    processed_ids: list[str] = field(default_factory=list)
    total_items: int = 0
    empty_streak: int = 0  # 连续空页计数（风控空壳页防误判，跨 run 持久）

    @classmethod
    def load(cls, progress_dir: Path) -> "Progress":
        path = progress_dir / PROGRESS_NAME
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        if not isinstance(raw, dict):
            return cls()
        processed = raw.get("processed_ids")
        return cls(
            current_page=int(raw.get("current_page", 1) or 1),
            scan_complete=bool(raw.get("scan_complete", False)),
            processed_ids=[str(value) for value in processed] if isinstance(processed, list) else [],
            total_items=int(raw.get("total_items", 0) or 0),
            empty_streak=int(raw.get("empty_streak", 0) or 0),
        )

    def save(self, progress_dir: Path) -> None:
        progress_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "current_page": self.current_page,
            "scan_complete": self.scan_complete,
            "processed_ids": self.processed_ids,
            "total_items": self.total_items,
            "empty_streak": self.empty_streak,
        }
        path = progress_dir / PROGRESS_NAME
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class Budget:
    """Wall-clock budget for one workflow step (0 disables the limit)."""

    def __init__(self, seconds: int | float):
        self.seconds = float(seconds or 0)
        self.deadline = time.monotonic() + self.seconds if self.seconds > 0 else None

    def expired(self) -> bool:
        return self.deadline is not None and time.monotonic() >= self.deadline

    def remaining(self) -> float:
        if self.deadline is None:
            return float("inf")
        return max(0.0, self.deadline - time.monotonic())


def human_delay(default_delay: float) -> float:
    """Effective request pacing: env overrides win, matching crawl_phones.

    ``CRAWL_MIN_DELAY_SECONDS``/``CRAWL_MAX_DELAY_SECONDS`` set a human-like
    random pause; without them the crawler's own default delay applies.
    """

    try:
        min_delay = float(os.environ.get("CRAWL_MIN_DELAY_SECONDS", "") or 0)
        max_delay = float(os.environ.get("CRAWL_MAX_DELAY_SECONDS", "") or 0)
    except ValueError:
        min_delay = max_delay = 0.0
    if min_delay > 0 and max_delay >= min_delay:
        return random.uniform(min_delay, max_delay)
    return default_delay


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def rewrite_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )


def merge_new_items(
    existing: list[dict[str, Any]],
    page_items: list[dict[str, Any]],
    id_key: Callable[[dict[str, Any]], str],
) -> tuple[list[dict[str, Any]], int]:
    """Append page items unseen so far; return (merged, added_count)."""

    seen = {id_key(item) for item in existing}
    added = 0
    for item in page_items:
        key = id_key(item)
        if key and key not in seen:
            seen.add(key)
            existing.append(item)
            added += 1
    return existing, added


def item_key(item: dict[str, Any]) -> str:
    return str(item.get("source_product_id") or item.get("source_url") or "")

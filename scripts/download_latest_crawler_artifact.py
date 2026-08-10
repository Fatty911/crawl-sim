#!/usr/bin/env python3
"""Download and validate the newest successful crawler artifact via GitHub API."""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import zipfile
from pathlib import Path
from typing import Any

import requests

try:
    from scripts.merge_data import load_records
except ModuleNotFoundError:
    from merge_data import load_records

API = "https://api.github.com"


def api_get(session: requests.Session, url: str, **kwargs: Any) -> requests.Response:
    response = session.get(url, timeout=45, **kwargs)
    if response.status_code >= 400:
        raise RuntimeError(f"GitHub API {response.status_code}: {response.text[:300]}")
    return response


def newest_artifact(
    session: requests.Session, repo: str, workflow: str, prefix: str
) -> dict[str, Any]:
    runs_url = f"{API}/repos/{repo}/actions/workflows/{workflow}/runs"
    # 不要求 run status=success：artifact 在 run 完成前上传（长爬虫 progress），
    # 且 run 完成后 status 才变 success——要求 success 会跳过"刚上传完的 run"，
    # 取到更旧的 artifact（机械师 13:16 artifact 被 12:54 旧版覆盖的根因）
    # 不要求 run status=success：artifact 在 run 完成前上传（长爬虫 progress），
    # 且 run 完成后 status 才变 success——要求 success 会跳过"刚上传完的 run"，
    # 取到更旧的 artifact（机械师 13:16 artifact 被 12:54 旧版覆盖的根因）。
    # 排除明确失败的 run；in_progress/completed(success) 都接受（artifact 新鲜度优先）。
    runs = api_get(
        session, runs_url, params={"branch": "main", "per_page": 20}
    ).json().get("workflow_runs", [])
    runs = [run for run in runs if run.get("conclusion") != "failure"]
    for run in runs:
        artifacts_url = f"{API}/repos/{repo}/actions/runs/{run['id']}/artifacts"
        artifacts = api_get(session, artifacts_url, params={"per_page": 100}).json().get(
            "artifacts", []
        )
        matches = [
            artifact
            for artifact in artifacts
            if not artifact.get("expired") and str(artifact.get("name", "")).startswith(prefix)
        ]
        if matches:
            return sorted(matches, key=lambda value: value.get("created_at", ""), reverse=True)[0]
    raise RuntimeError(f"no unexpired artifact starting with {prefix!r} from {workflow}")


def extract_json(content: bytes, output: Path) -> None:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        candidates = [
            name
            for name in archive.namelist()
            if name.lower().endswith(".json") and not name.endswith("/")
        ]
        if not candidates:
            raise RuntimeError("artifact contains no JSON file")
        preferred = [name for name in candidates if Path(name).name in {"latest.json", "data.json"}]
        chosen = sorted(preferred or candidates)[0]
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(archive.read(chosen))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, help="owner/repository")
    parser.add_argument("--workflow", required=True, help="workflow file name")
    parser.add_argument("--artifact-prefix", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--min-records", type=int, default=50)
    args = parser.parse_args()
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        print("GITHUB_TOKEN is required", file=sys.stderr)
        return 2
    session = requests.Session()
    session.headers.update(
        {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "crawl-sim-pipeline",
        }
    )
    try:
        artifact = newest_artifact(
            session, args.repo, args.workflow, args.artifact_prefix
        )
        response = api_get(session, artifact["archive_download_url"])
        output = Path(args.output)
        extract_json(response.content, output)
        records = load_records(output)
        if len(records) < args.min_records:
            raise RuntimeError(
                f"artifact {artifact['name']} has {len(records)} rows; "
                f"minimum is {args.min_records}"
            )
    except Exception as exc:
        print(f"artifact download failed: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "artifact": artifact["name"],
                "artifact_id": artifact["id"],
                "records": len(records),
                "output": str(output),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


#!/usr/bin/env python3
"""Prepare a verified Open Tennis Data release snapshot for the static site."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

DEFAULT_REPOSITORY = "ryantjx/tennis-match-data"
RELEASE_PREFIX = "data-v3-"
REQUIRED_ASSETS = (
    "manifest.json",
    "matches.parquet",
    "tournaments.parquet",
    "players.parquet",
)
OpenUrl = Callable[..., Any]


class SnapshotError(RuntimeError):
    """Raised when a release cannot safely be prepared for the site."""


def _request_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "open-tennis-data-site-preparer/1",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch_bytes(url: str, *, opener: OpenUrl = urllib.request.urlopen) -> bytes:
    request = urllib.request.Request(url, headers=_request_headers())
    try:
        with opener(request, timeout=60) as response:
            return response.read()
    except (OSError, urllib.error.URLError) as error:
        raise SnapshotError(f"Could not download {url}: {error}") from error


def fetch_json(url: str, *, opener: OpenUrl = urllib.request.urlopen) -> Any:
    payload = fetch_bytes(url, opener=opener)
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SnapshotError(f"Expected JSON from {url}") from error


def select_release(
    releases: Sequence[Mapping[str, Any]],
    *,
    tag: str | None = None,
) -> Mapping[str, Any]:
    candidates = [
        release
        for release in releases
        if not release.get("draft")
        and str(release.get("tag_name", "")).startswith(RELEASE_PREFIX)
    ]
    if tag:
        for release in candidates:
            if release.get("tag_name") == tag:
                return release
        raise SnapshotError(f"Published Open Tennis Data release not found: {tag}")
    if not candidates:
        raise SnapshotError("No published data-v3 release is available")
    return max(
        candidates,
        key=lambda release: str(release.get("published_at") or release.get("created_at") or ""),
    )


def asset_urls(release: Mapping[str, Any]) -> dict[str, str]:
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise SnapshotError("Selected release has no asset list")
    urls = {
        str(asset.get("name")): str(asset.get("browser_download_url"))
        for asset in assets
        if isinstance(asset, dict)
        and asset.get("name")
        and asset.get("browser_download_url")
    }
    missing = sorted(set(REQUIRED_ASSETS) - set(urls))
    if missing:
        raise SnapshotError(f"Selected release is missing: {', '.join(missing)}")
    return urls


def validate_manifest(manifest: Mapping[str, Any], *, expected_tag: str) -> dict[str, Mapping[str, Any]]:
    if manifest.get("product") != "Open Tennis Data":
        raise SnapshotError("Manifest product is not Open Tennis Data")
    if str(manifest.get("product_version")) != "3":
        raise SnapshotError("Manifest product version is not 3")
    if str(manifest.get("schema_version")) != "3.3":
        raise SnapshotError("Manifest schema version is not 3.3")
    if manifest.get("release_tag") != expected_tag:
        raise SnapshotError("Manifest release tag does not match the selected release")
    if manifest.get("release_status") not in {"preview", "stable"}:
        raise SnapshotError("Manifest release status must be preview or stable")
    assets = manifest.get("assets")
    if not isinstance(assets, list):
        raise SnapshotError("Manifest has no asset inventory")
    inventory = {
        str(asset.get("name")): asset
        for asset in assets
        if isinstance(asset, dict) and asset.get("name")
    }
    required_payloads = set(REQUIRED_ASSETS) - {"manifest.json"}
    missing = sorted(required_payloads - set(inventory))
    if missing:
        raise SnapshotError(f"Manifest is missing: {', '.join(missing)}")
    for name in required_payloads:
        entry = inventory[name]
        if not isinstance(entry.get("bytes"), int) or entry["bytes"] < 0:
            raise SnapshotError(f"Manifest byte size is invalid for {name}")
        digest = str(entry.get("sha256", ""))
        if len(digest) != 64:
            raise SnapshotError(f"Manifest checksum is invalid for {name}")
    return inventory


def verify_payload(name: str, payload: bytes, inventory: Mapping[str, Mapping[str, Any]]) -> None:
    expected = inventory[name]
    if len(payload) != expected["bytes"]:
        raise SnapshotError(
            f"{name} size mismatch: expected {expected['bytes']}, received {len(payload)}"
        )
    actual_digest = hashlib.sha256(payload).hexdigest()
    if actual_digest != expected["sha256"]:
        raise SnapshotError(f"{name} checksum mismatch")


def replace_snapshot(output: Path, payloads: Mapping[str, bytes]) -> None:
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.staging-", dir=output.parent))
    backup: Path | None = None
    try:
        for name, payload in payloads.items():
            (staging / name).write_bytes(payload)
        if output.exists():
            backup = Path(
                tempfile.mkdtemp(prefix=f".{output.name}.backup-", dir=output.parent)
            )
            backup.rmdir()
            os.replace(output, backup)
        try:
            os.replace(staging, output)
        except Exception:
            if backup is not None and backup.exists() and not output.exists():
                os.replace(backup, output)
                backup = None
            raise
        if backup is not None:
            shutil.rmtree(backup)
            backup = None
    finally:
        if staging.exists():
            shutil.rmtree(staging)
        if backup is not None and backup.exists():
            shutil.rmtree(backup)


def prepare_snapshot(
    output: Path,
    *,
    tag: str | None = None,
    repository: str = DEFAULT_REPOSITORY,
    opener: OpenUrl = urllib.request.urlopen,
) -> str:
    api_url = f"https://api.github.com/repos/{repository}/releases?per_page=100"
    releases = fetch_json(api_url, opener=opener)
    if not isinstance(releases, list):
        raise SnapshotError("GitHub releases response is not a list")
    release = select_release(releases, tag=tag)
    selected_tag = str(release["tag_name"])
    urls = asset_urls(release)

    manifest_payload = fetch_bytes(urls["manifest.json"], opener=opener)
    try:
        manifest = json.loads(manifest_payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SnapshotError("Release manifest is not valid JSON") from error
    if not isinstance(manifest, dict):
        raise SnapshotError("Release manifest must be a JSON object")
    inventory = validate_manifest(manifest, expected_tag=selected_tag)

    payloads = {"manifest.json": manifest_payload}
    for name in REQUIRED_ASSETS[1:]:
        payload = fetch_bytes(urls[name], opener=opener)
        verify_payload(name, payload, inventory)
        payloads[name] = payload
    replace_snapshot(output, payloads)
    return selected_tag


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download and verify the newest Open Tennis Data v3 site snapshot."
    )
    parser.add_argument("--output", required=True, type=Path, help="Snapshot output directory")
    parser.add_argument("--tag", help="Use one immutable data-v3 release tag")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        selected_tag = prepare_snapshot(args.output, tag=args.tag)
    except SnapshotError as error:
        raise SystemExit(f"error: {error}") from error
    print(f"Prepared {selected_tag} in {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

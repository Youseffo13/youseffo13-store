#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / ".github" / "apps.json"

# A stable release tag: optional "v" prefix and numeric dot-separated parts.
# A trailing "-patch.N" is tolerated so package-only revisions still compare.
VERSION_RE = re.compile(r"^v?(\d+(?:\.\d+)*)(?:-patch\.\d+)?$")
UNSTABLE_RE = re.compile(
    r"(?:alpha|beta|rc|preview|pre|dev|nightly|snapshot|edge|test|canary)",
    re.IGNORECASE,
)
DIGEST_RE = re.compile(r"@sha256:[0-9a-f]{64}$", re.IGNORECASE)


def normalize(version: str) -> tuple[int, ...] | None:
    """Return a comparable version tuple, or None if not numeric."""
    match = VERSION_RE.match(version.strip())
    if not match:
        return None
    parts = [int(part) for part in match.group(1).split(".")]
    # Pad so 1.2 and 1.2.0 compare equal and mixed-length tags sort sanely.
    parts.extend([0] * (6 - len(parts)))
    return tuple(parts)


def is_stable_tag(tag: str) -> bool:
    return normalize(tag) is not None and not UNSTABLE_RE.search(tag)


def split_image(image: str) -> tuple[str, str, str | None]:
    """Split an image reference into (registry, repository, tag)."""
    image = DIGEST_RE.sub("", image.strip())
    tag: str | None = None
    slash = image.rfind("/")
    colon = image.rfind(":")
    if colon > slash:
        image, tag = image[:colon], image[colon + 1 :]

    registry = "docker.io"
    repository = image
    first = image.split("/", 1)[0]
    if "." in first or ":" in first or first == "localhost":
        registry = first
        repository = image.split("/", 1)[1]
    if registry == "docker.io" and "/" not in repository:
        repository = f"library/{repository}"
    return registry, repository, tag


def fetch_json(url: str, headers: dict[str, str] | None = None) -> dict:
    request_headers = {"User-Agent": "youseffo13-store-updater"}
    request_headers.update(headers or {})
    request = urllib.request.Request(url, headers=request_headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def dockerhub_tags(repository: str) -> list[str]:
    tags: list[str] = []
    url: str | None = (
        f"https://hub.docker.com/v2/repositories/{repository}/tags"
        "?page_size=100&ordering=last_updated"
    )
    pages = 0
    while url and pages < 50:
        data = fetch_json(url)
        tags.extend(tag["name"] for tag in data.get("results", []))
        url = data.get("next")
        pages += 1
    return tags


def ghcr_tags(repository: str) -> list[str]:
    token_url = "https://ghcr.io/token?" + urllib.parse.urlencode(
        {"scope": f"repository:{repository}:pull", "service": "ghcr.io"}
    )
    token = fetch_json(token_url)["token"]
    data = fetch_json(
        f"https://ghcr.io/v2/{repository}/tags/list?n=1000",
        headers={"Authorization": f"Bearer {token}"},
    )
    return data.get("tags") or []


def registry_tags(registry: str, repository: str) -> list[str]:
    if registry in {"docker.io", "registry-1.docker.io", "index.docker.io"}:
        return dockerhub_tags(repository)
    if registry == "ghcr.io":
        return ghcr_tags(repository)
    raise ValueError(f"unsupported registry: {registry}")


def best_tag(
    tags: Iterable[str], allow_major: bool, current: tuple[int, ...] | None
) -> str | None:
    candidates: list[tuple[tuple[int, ...], str]] = []
    for tag in tags:
        if not is_stable_tag(tag):
            continue
        version = normalize(tag)
        assert version is not None
        if not allow_major and current is not None and version[0] != current[0]:
            continue
        candidates.append((version, tag))
    if not candidates:
        return None
    # Newest version first; on ties prefer a plain numeric tag over a "v" one.
    candidates.sort(
        key=lambda item: (item[0], item[1][0].isdigit(), -len(item[1])),
        reverse=True,
    )
    return candidates[0][1]


def compose_image(text: str, service: str | None) -> str | None:
    """Find the image of a compose service (default: any service)."""
    current_service: str | None = None
    fallback: str | None = None
    for line in text.splitlines():
        service_match = re.match(r"^  ([A-Za-z0-9_.-]+):\s*$", line)
        if service_match:
            current_service = service_match.group(1)
            continue
        image_match = re.match(r"^\s+image:\s*([^\s#]+)", line)
        if not image_match:
            continue
        image = image_match.group(1)
        if service is None:
            return image
        if current_service == service:
            return image
        if fallback is None:
            fallback = image
    return fallback


def update_manifest(path: Path, version: str) -> bool:
    text = path.read_text(encoding="utf-8")
    new_text, count = re.subn(
        r'(?m)^version:[ \t]*.*$', f'version: "{version}"', text, count=1
    )
    if count == 0:
        raise ValueError(f"{path}: no top-level 'version' field found")
    if new_text == text:
        return False
    path.write_text(new_text, encoding="utf-8")
    return True


def update_compose_image(path: Path, image_ref: str, tag: str) -> bool:
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(
        r"(?m)^([ \t]*image:[ \t]*)"
        + re.escape(image_ref)
        + r"(?::[^\s#]+)?(?:@sha256:[0-9a-f]{64})?([ \t]*(?:#.*)?)$",
        re.IGNORECASE,
    )
    new_text, count = pattern.subn(
        lambda match: f"{match.group(1)}{image_ref}:{tag}{match.group(2)}",
        text,
        count=1,
    )
    if count == 0:
        raise ValueError(f"{path}: image '{image_ref}' not found")
    if new_text == text:
        return False
    path.write_text(new_text, encoding="utf-8")
    return True


def load_config() -> dict[str, dict]:
    if not CONFIG_PATH.exists():
        return {}
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="print changes without writing files"
    )
    args = parser.parse_args()

    config = load_config()
    failures = 0
    updates = 0

    for manifest in sorted(ROOT.glob("*/umbrel-app.yml")):
        app_dir = manifest.parent
        app_id = app_dir.name
        compose = app_dir / "docker-compose.yml"
        if not compose.exists():
            print(f"skip {app_id}: no docker-compose.yml")
            continue

        app_config = config.get(app_id, {})
        service = app_config.get("service")
        allow_major = app_config.get("allowMajor", True)

        image = app_config.get("image") or compose_image(
            compose.read_text(encoding="utf-8"), service
        )
        if not image:
            print(f"skip {app_id}: no image found in compose")
            continue

        registry, repository, _ = split_image(image)
        image_ref = image if not DIGEST_RE.search(image) else DIGEST_RE.sub("", image)
        if ":" in image_ref.split("/")[-1]:
            image_ref = image_ref.rsplit(":", 1)[0]

        manifest_text = manifest.read_text(encoding="utf-8")
        version_match = re.search(r"(?m)^version:[ \t]*[\"']?([^\"'\s]+)", manifest_text)
        if not version_match:
            print(f"skip {app_id}: no version in umbrel-app.yml")
            continue
        current_version = version_match.group(1)
        current = normalize(current_version)

        try:
            tags = registry_tags(registry, repository)
        except (urllib.error.URLError, ValueError, KeyError) as error:
            print(f"skip {app_id}: cannot list tags for {registry}/{repository}: {error}")
            failures += 1
            continue

        latest = best_tag(tags, allow_major, current)
        if latest is None:
            print(f"skip {app_id}: no stable tags for {registry}/{repository}")
            continue

        latest_version = normalize(latest)
        if current is not None and latest_version is not None and latest_version <= current:
            print(f"ok   {app_id}: already on {current_version} (latest {latest})")
            continue

        print(f"bump {app_id}: {current_version} -> {latest}")
        updates += 1
        if args.dry_run:
            continue
        try:
            update_manifest(manifest, latest)
            update_compose_image(compose, image_ref, latest)
        except ValueError as error:
            print(f"error {app_id}: {error}")
            failures += 1

    if args.dry_run:
        print(f"\n{updates} app(s) would be updated (dry run).")
    else:
        print(f"\n{updates} app(s) updated.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

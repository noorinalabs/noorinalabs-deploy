#!/usr/bin/env python3
"""Assert Contract-v6 tag-count invariants for a GHCR image.

Publish-side (per push): exactly FOUR tags per publish-digest:
    sha-<short>, latest, stg-<short>, stg-latest

Promote-side (per promotion): exactly TWO tags per promoted-digest:
    prod-<short>, prod-latest

Canonical contract: noorinalabs-isnad-graph#815 comment 4301538921 (v6).
v6 is substantively identical to v5 (4301487132) / v3 (4301425114) — same
tag shape. v6 exists to lock program-director ruling language.

Usage:
    check_tag_invariants.py <image-path>
        where <image-path> is e.g. `noorinalabs/noorinalabs-isnad-graph`
        (without the `ghcr.io/` prefix).

Auth: uses GH_ACTOR + GH_TOKEN from env if set (supplied by the CI job);
otherwise falls back to an anonymous ghcr.io pull token which works for
public packages.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse
import urllib.request
from collections import defaultdict

REGISTRY = "ghcr.io"

SHA_SHORT_RE = re.compile(r"^sha-([0-9a-f]{7})$")
STG_SHORT_RE = re.compile(r"^stg-([0-9a-f]{7})$")
PROD_SHORT_RE = re.compile(r"^prod-([0-9a-f]{7})$")


def _http_get(url: str, headers: dict[str, str]) -> bytes:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req) as r:
        return r.read()


def _bearer_token(image_path: str) -> str:
    """Fetch a pull-scoped bearer token from ghcr.io."""
    actor = os.environ.get("GH_ACTOR") or ""
    gh_token = os.environ.get("GH_TOKEN") or ""
    scope = f"repository:{image_path}:pull"
    url = f"https://{REGISTRY}/token?scope={urllib.parse.quote(scope, safe=':')}"
    headers: dict[str, str] = {}
    if actor and gh_token:
        import base64
        auth = base64.b64encode(f"{actor}:{gh_token}".encode()).decode()
        headers["Authorization"] = f"Basic {auth}"
    data = json.loads(_http_get(url, headers))
    return data["token"]


def _list_tags(image_path: str, token: str) -> list[str]:
    url = f"https://{REGISTRY}/v2/{image_path}/tags/list"
    headers = {"Authorization": f"Bearer {token}"}
    data = json.loads(_http_get(url, headers))
    return list(data.get("tags") or [])


def _resolve_digest(image_path: str, tag: str, token: str) -> str:
    """Return the content-addressable digest of a tag's manifest.

    Uses HEAD on the v2 manifest endpoint and reads the Docker-Content-Digest
    header, which the registry provides for both manifest-lists and
    single-arch manifests. We accept both v2 manifest-list and OCI index
    media types.
    """
    url = f"https://{REGISTRY}/v2/{image_path}/manifests/{tag}"
    accept = ", ".join([
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ])
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": accept},
        method="HEAD",
    )
    with urllib.request.urlopen(req) as r:
        dig = r.headers.get("Docker-Content-Digest")
    if not dig:
        raise RuntimeError(f"No Docker-Content-Digest for {image_path}:{tag}")
    return dig


def check_publish_side(image_path: str, tags: list[str], token: str) -> list[str]:
    """Group publish-side tags by digest; assert each group is exactly 4.

    We consider only the digest pointed to by `latest`, because that is
    the digest of the most recent main push. A healthy publish-side emits
    {sha-<short>, latest, stg-<short>, stg-latest} all at that digest.
    """
    errors: list[str] = []
    if "latest" not in tags:
        # No main push has ever happened for this image — skip (not a regression).
        print(f"[publish] {image_path}: no `latest` tag yet; skipping (no main push observed).")
        return errors
    latest_digest = _resolve_digest(image_path, "latest", token)

    # Gather all tags that resolve to `latest_digest`.
    grouped: list[str] = ["latest"]
    for tag in tags:
        if tag == "latest":
            continue
        if not (
            SHA_SHORT_RE.match(tag)
            or STG_SHORT_RE.match(tag)
            or tag == "stg-latest"
        ):
            continue
        try:
            d = _resolve_digest(image_path, tag, token)
        except Exception as exc:
            errors.append(f"[publish] failed to resolve {tag}: {exc}")
            continue
        if d == latest_digest:
            grouped.append(tag)

    # Exactly one of each expected shape.
    sha_short = [t for t in grouped if SHA_SHORT_RE.match(t)]
    stg_short = [t for t in grouped if STG_SHORT_RE.match(t)]
    has_latest = "latest" in grouped
    has_stg_latest = "stg-latest" in grouped

    if len(sha_short) != 1:
        errors.append(
            f"[publish] {image_path}: expected exactly 1 sha-<short> tag at latest digest, got {len(sha_short)}: {sha_short}"
        )
    if len(stg_short) != 1:
        errors.append(
            f"[publish] {image_path}: expected exactly 1 stg-<short> tag at latest digest, got {len(stg_short)}: {stg_short}"
        )
    if not has_latest:
        errors.append(f"[publish] {image_path}: `latest` tag missing from latest-digest group")
    if not has_stg_latest:
        errors.append(f"[publish] {image_path}: `stg-latest` tag missing from latest-digest group")

    # Invariant: the group size is exactly 4 — catches the "added a 5th tag" regression too.
    if len(grouped) != 4:
        errors.append(
            f"[publish] {image_path}: Contract-v6 requires EXACTLY 4 tags per push at the publish digest. "
            f"Found {len(grouped)}: {sorted(grouped)}"
        )
    else:
        print(f"[publish] {image_path}: OK — 4 tags at latest digest: {sorted(grouped)}")

    # Short-SHA parity: the 7-char suffixes on sha-* and stg-* must match.
    if len(sha_short) == 1 and len(stg_short) == 1:
        s_sha = SHA_SHORT_RE.match(sha_short[0]).group(1)
        s_stg = STG_SHORT_RE.match(stg_short[0]).group(1)
        if s_sha != s_stg:
            errors.append(
                f"[publish] {image_path}: short-SHA mismatch between sha-{s_sha} and stg-{s_stg} at same digest"
            )

    return errors


def check_promote_side(image_path: str, tags: list[str], token: str) -> list[str]:
    """Group promote-side tags by digest; assert each group is exactly 2.

    Skipped when `prod-latest` does not yet exist (no promotion has run).
    """
    errors: list[str] = []
    if "prod-latest" not in tags:
        print(f"[promote] {image_path}: no `prod-latest` tag yet; skipping (no promotion observed).")
        return errors

    prod_digest = _resolve_digest(image_path, "prod-latest", token)

    grouped: list[str] = ["prod-latest"]
    for tag in tags:
        if tag == "prod-latest":
            continue
        if not PROD_SHORT_RE.match(tag):
            continue
        try:
            d = _resolve_digest(image_path, tag, token)
        except Exception as exc:
            errors.append(f"[promote] failed to resolve {tag}: {exc}")
            continue
        if d == prod_digest:
            grouped.append(tag)

    prod_short = [t for t in grouped if PROD_SHORT_RE.match(t)]
    if len(prod_short) != 1:
        errors.append(
            f"[promote] {image_path}: expected exactly 1 prod-<short> at prod-latest digest, got {len(prod_short)}: {prod_short}"
        )
    if len(grouped) != 2:
        errors.append(
            f"[promote] {image_path}: Contract-v6 requires EXACTLY 2 tags per promotion at the prod digest. "
            f"Found {len(grouped)}: {sorted(grouped)}"
        )
    else:
        print(f"[promote] {image_path}: OK — 2 tags at prod-latest digest: {sorted(grouped)}")

    return errors


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: check_tag_invariants.py <image-path-without-ghcr-prefix>", file=sys.stderr)
        return 2
    image_path = sys.argv[1]
    token = _bearer_token(image_path)
    tags = _list_tags(image_path, token)
    print(f"[info] {image_path}: {len(tags)} tags visible")

    errors: list[str] = []
    errors.extend(check_publish_side(image_path, tags, token))
    errors.extend(check_promote_side(image_path, tags, token))

    if errors:
        print("")
        print("TAG INVARIANT FAILURES:")
        for e in errors:
            print(f"  - {e}")
        return 1
    print(f"[ok] {image_path}: all tag invariants satisfied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Assert Contract-v6 tag-count invariants for a GHCR image.

Publish-side (per push): exactly FOUR tags per publish-digest:
    sha-<short>, latest, stg-<short>, stg-latest

Promote-side (per promotion): exactly TWO tags per promoted-digest:
    prod-<short>, prod-latest

Canonical contract (per Khoury ruling 2026-04-23): noorinalabs-isnad-graph
#815 comment 4301538921 (v6). v6 shape is identical to v5 (4301487132) /
v3 (4301425114); v6 is the canonical citation target.

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

REGISTRY = "ghcr.io"

SHA_SHORT_RE = re.compile(r"^sha-([0-9a-f]{7})$")
STG_SHORT_RE = re.compile(r"^stg-([0-9a-f]{7})$")
PROD_SHORT_RE = re.compile(r"^prod-([0-9a-f]{7})$")

# Upper bound on tags/list pages walked. At 100 tags/page this covers 5,000
# tags — far beyond any real package — while capping a cyclic/self-referential
# server `next` so the walk cannot loop the CI job forever.
_MAX_TAG_PAGES = 50


def _http_get_page(url: str, headers: dict[str, str]) -> tuple[bytes, str]:
    """GET a URL, returning (body, Link-header).

    The registry paginates `tags/list` and signals continuation via the
    RFC 5988 ``Link: <…>; rel="next"`` header, so callers that paginate
    need the response header alongside the body. The header is "" when
    absent (no further pages).
    """
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req) as r:
        body = r.read()
        link = r.headers.get("Link", "") or ""
    return body, link


def _http_get(url: str, headers: dict[str, str]) -> bytes:
    body, _link = _http_get_page(url, headers)
    return body


def _next_page_url(link_header: str) -> str | None:
    """Resolve the ``rel="next"`` target of an RFC 5988 Link header.

    Origin-pinned: the next-page URL is ALWAYS rebuilt as
    ``https://{REGISTRY}{path}?{query}`` using only the path + query of the
    Link target — any scheme/host in the target is discarded. This is a
    security guard, not cosmetics: ``_list_tags`` re-attaches the GHCR
    ``Authorization: Bearer <token>`` on every page, so a malicious or
    cleartext ``Link: <https://attacker/…>; rel="next"`` (or ``http://…``)
    must never receive that token — the bearer credential may only ever go
    to ghcr.io over https. GHCR's own next-link is a relative path, so this
    rewrite is transparent for legitimate pagination. Returns None when
    there is no next page (the terminal condition for the walk).
    """
    if not link_header:
        return None
    for part in link_header.split(","):
        seg = part.strip()
        if 'rel="next"' not in seg:
            continue
        lt = seg.find("<")
        gt = seg.find(">")
        if lt == -1 or gt == -1 or gt < lt:
            continue
        target = seg[lt + 1 : gt].strip()
        split = urllib.parse.urlsplit(target)
        # Keep ONLY path + query; force scheme=https, host=REGISTRY.
        return urllib.parse.urlunsplit(("https", REGISTRY, split.path, split.query, ""))
    return None


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
    """Return the COMPLETE tag set, following Link-header pagination.

    GHCR caps a single `tags/list` page at 100 tags; an image with more
    than 100 tags returns only the first page unless the caller walks the
    ``rel="next"`` Link header. The previous single-GET implementation
    silently truncated, so once a package crossed 100 tags the newest
    publish shorts (`sha-<short>`/`stg-<short>`) fell off the page and the
    invariant check false-failed for them (deploy#496). Walking every page
    is the only complete fix — the endpoint exposes no recency sort.

    The walk is bounded (page cap + visited-URL set) so a cyclic or
    self-referential server ``next`` cannot loop the CI job forever.
    """
    url: str | None = f"https://{REGISTRY}/v2/{image_path}/tags/list"
    headers = {"Authorization": f"Bearer {token}"}
    tags: list[str] = []
    seen: set[str] = set()
    for _ in range(_MAX_TAG_PAGES):
        if not url or url in seen:
            break
        seen.add(url)
        body, link = _http_get_page(url, headers)
        page = json.loads(body).get("tags") or []
        tags.extend(page)
        url = _next_page_url(link)
    return tags


def _resolve_digest(image_path: str, tag: str, token: str) -> str:
    """Return the content-addressable digest of a tag's manifest.

    Uses HEAD on the v2 manifest endpoint and reads the Docker-Content-Digest
    header, which the registry provides for both manifest-lists and
    single-arch manifests. We accept both v2 manifest-list and OCI index
    media types.
    """
    url = f"https://{REGISTRY}/v2/{image_path}/manifests/{tag}"
    accept = ", ".join(
        [
            "application/vnd.oci.image.index.v1+json",
            "application/vnd.oci.image.manifest.v1+json",
            "application/vnd.docker.distribution.manifest.list.v2+json",
            "application/vnd.docker.distribution.manifest.v2+json",
        ]
    )
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
        if not (SHA_SHORT_RE.match(tag) or STG_SHORT_RE.match(tag) or tag == "stg-latest"):
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
            f"[publish] {image_path}: expected exactly 1 sha-<short> tag at latest "
            f"digest, got {len(sha_short)}: {sha_short}"
        )
    if len(stg_short) != 1:
        errors.append(
            f"[publish] {image_path}: expected exactly 1 stg-<short> tag at latest "
            f"digest, got {len(stg_short)}: {stg_short}"
        )
    if not has_latest:
        errors.append(f"[publish] {image_path}: `latest` tag missing from latest-digest group")
    if not has_stg_latest:
        errors.append(f"[publish] {image_path}: `stg-latest` tag missing from latest-digest group")

    # Invariant: the group size is exactly 4 — catches the "added a 5th tag" regression too.
    if len(grouped) != 4:
        errors.append(
            f"[publish] {image_path}: Contract-v6 requires EXACTLY 4 tags per push "
            f"at the publish digest. Found {len(grouped)}: {sorted(grouped)}"
        )
    else:
        print(f"[publish] {image_path}: OK — 4 tags at latest digest: {sorted(grouped)}")

    # Short-SHA parity: the 7-char suffixes on sha-* and stg-* must match.
    # sha_short/stg_short were built by matching SHA_SHORT_RE/STG_SHORT_RE, so
    # the re-match below is always non-None; guard it anyway to stay type-safe.
    if len(sha_short) == 1 and len(stg_short) == 1:
        m_sha = SHA_SHORT_RE.match(sha_short[0])
        m_stg = STG_SHORT_RE.match(stg_short[0])
        if m_sha and m_stg and m_sha.group(1) != m_stg.group(1):
            errors.append(
                f"[publish] {image_path}: short-SHA mismatch between "
                f"sha-{m_sha.group(1)} and stg-{m_stg.group(1)} at same digest"
            )

    return errors


def check_promote_side(image_path: str, tags: list[str], token: str) -> list[str]:
    """Group promote-side tags by digest; assert each group is exactly 2.

    Skipped when `prod-latest` does not yet exist (no promotion has run).
    """
    errors: list[str] = []
    if "prod-latest" not in tags:
        print(
            f"[promote] {image_path}: no `prod-latest` tag yet; skipping (no promotion observed)."
        )
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
            f"[promote] {image_path}: expected exactly 1 prod-<short> at prod-latest "
            f"digest, got {len(prod_short)}: {prod_short}"
        )
    if len(grouped) != 2:
        errors.append(
            f"[promote] {image_path}: Contract-v6 requires EXACTLY 2 tags per "
            f"promotion at the prod digest. Found {len(grouped)}: {sorted(grouped)}"
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

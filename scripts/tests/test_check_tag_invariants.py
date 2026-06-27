"""Unit tests for integration-tests/scripts/check_tag_invariants.py (deploy#496).

These are pure, dependency-light urllib-mock tests — they do NOT touch the
network, a registry, or the integration-test stack. They live in
``scripts/tests/`` (run by the ``pytest (scripts)`` CI job, which installs only
pytest) rather than ``integration-tests/tests/`` because the latter's
``conftest.py`` imports asyncpg/httpx/redis and raises at module-load unless
the live-stack env vars are set — that harness is for the hermetic/remote
integration suite, not a stdlib-only unit test.

Regression under test: GHCR caps ``tags/list`` at 100 tags per page and
paginates via the RFC 5988 ``Link: <…>; rel="next"`` header. The old
single-GET ``_list_tags`` truncated at 100, dropping the newest publish shorts
once a package exceeded 100 tags so the invariant check false-failed (and
promote.yml's plan walk could not resolve the short).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# The script under test lives in integration-tests/scripts/ (a CLI script, not
# an installed package). Mirror the sys.path-insert + import pattern the sibling
# scripts/tests modules use for scripts/*.py, pointed at that directory.
_CTI_DIR = Path(__file__).resolve().parents[2] / "integration-tests" / "scripts"
sys.path.insert(0, str(_CTI_DIR))

import check_tag_invariants as cti  # noqa: E402


def _link(target: str) -> str:
    return f'<{target}>; rel="next"'


def test_next_page_url_relative_rebased_on_registry():
    nxt = cti._next_page_url(_link("/v2/noorinalabs/img/tags/list?last=sha-abc1234&n=100"))
    assert nxt == f"https://{cti.REGISTRY}/v2/noorinalabs/img/tags/list?last=sha-abc1234&n=100"


def test_next_page_url_absolute_ghcr_preserved():
    # A legitimate absolute next on the registry origin is rebuilt to the same
    # URL (path+query preserved; scheme/host forced to https + REGISTRY).
    absolute = "https://ghcr.io/v2/noorinalabs/img/tags/list?last=z&n=100"
    assert cti._next_page_url(_link(absolute)) == absolute


def test_next_page_url_foreign_host_is_pinned_to_registry():
    # Token-exfil guard: an absolute next on an ATTACKER host must be rebuilt
    # to ghcr.io (path+query only), so _list_tags' bearer token can never be
    # sent off-origin.
    nxt = cti._next_page_url(_link("https://attacker.example/v2/x/tags/list?last=y"))
    assert nxt == f"https://{cti.REGISTRY}/v2/x/tags/list?last=y"
    assert "attacker.example" not in (nxt or "")


def test_next_page_url_cleartext_http_is_pinned_and_upgraded():
    # A cleartext http:// next must not downgrade nor leave-origin: rebuilt as
    # https on the registry host.
    nxt = cti._next_page_url(_link("http://attacker.example/v2/x/tags/list?last=y"))
    assert nxt == f"https://{cti.REGISTRY}/v2/x/tags/list?last=y"


def test_next_page_url_missing_leading_slash_is_rooted():
    nxt = cti._next_page_url(_link("v2/img/tags/list?last=q"))
    assert nxt == f"https://{cti.REGISTRY}/v2/img/tags/list?last=q"


def test_next_page_url_none_when_absent_or_not_next():
    assert cti._next_page_url("") is None
    assert cti._next_page_url('<https://ghcr.io/x>; rel="prev"') is None


def test_next_page_url_selects_next_among_multiple_rels():
    header = (
        '<https://ghcr.io/v2/img/tags/list?page=1>; rel="prev", '
        '<https://ghcr.io/v2/img/tags/list?page=3>; rel="next"'
    )
    assert cti._next_page_url(header) == "https://ghcr.io/v2/img/tags/list?page=3"


def test_list_tags_assembles_two_pages(monkeypatch):
    """A 2-page Link-header response must yield the UNION of both pages."""
    page1 = [f"sha-{i:07x}" for i in range(100)]  # exactly the 100-cap page
    page2 = ["sha-e4c0c54", "stg-e4c0c54", "stg-latest", "latest"]  # the tail

    next_url = "https://ghcr.io/v2/noorinalabs/img/tags/list?last=sha-0000063&n=100"
    calls: list[str] = []

    def fake_get_page(url, headers):
        calls.append(url)
        if "last=" not in url:
            return json.dumps({"tags": page1}).encode(), _link(next_url)
        # Second (final) page carries no Link header → walk terminates.
        return json.dumps({"tags": page2}).encode(), ""

    monkeypatch.setattr(cti, "_http_get_page", fake_get_page)

    tags = cti._list_tags("noorinalabs/img", "tok")

    assert tags == page1 + page2
    assert len(calls) == 2  # both pages were fetched
    # The truncation-sensitive tail (newest publish shorts) is present.
    assert "sha-e4c0c54" in tags
    assert "stg-e4c0c54" in tags


def test_list_tags_single_page_no_link(monkeypatch):
    """A package under the cap (no Link header) returns its one page as-is."""

    def fake_get_page(url, headers):
        return json.dumps({"tags": ["latest", "sha-1234567"]}).encode(), ""

    monkeypatch.setattr(cti, "_http_get_page", fake_get_page)
    assert cti._list_tags("noorinalabs/img", "tok") == ["latest", "sha-1234567"]


def test_list_tags_never_fetches_off_origin(monkeypatch):
    """A foreign-host `next` must not cause a fetch (token send) off ghcr.io."""
    calls: list[str] = []

    def fake_get_page(url, headers):
        calls.append(url)
        if "last=" not in url:
            # Server hands back an attacker host; _next_page_url must pin it
            # back to ghcr.io so the SECOND fetch stays on origin.
            return json.dumps({"tags": ["latest"]}).encode(), _link(
                "https://attacker.example/v2/noorinalabs/img/tags/list?last=z"
            )
        return json.dumps({"tags": ["sha-1234567"]}).encode(), ""

    monkeypatch.setattr(cti, "_http_get_page", fake_get_page)

    tags = cti._list_tags("noorinalabs/img", "tok")

    assert tags == ["latest", "sha-1234567"]
    # Every URL the walk fetched (i.e. every place the bearer token was sent)
    # is on the registry origin — the attacker host was never contacted.
    assert all(u.startswith(f"https://{cti.REGISTRY}/") for u in calls), calls
    assert not any("attacker.example" in u for u in calls)


def test_list_tags_terminates_on_cyclic_next(monkeypatch):
    """A cyclic server `next` (a→b→a) terminates via the visited-URL set."""
    base = "/v2/noorinalabs/img/tags/list"
    calls: list[str] = []

    def fake_get_page(url, headers):
        calls.append(url)
        if "cursor=a" in url:
            return json.dumps({"tags": ["a"]}).encode(), _link(f"https://ghcr.io{base}?cursor=b")
        if "cursor=b" in url:
            return json.dumps({"tags": ["b"]}).encode(), _link(f"https://ghcr.io{base}?cursor=a")
        # First (base) page → enter the cycle.
        return json.dumps({"tags": ["base"]}).encode(), _link(f"https://ghcr.io{base}?cursor=a")

    monkeypatch.setattr(cti, "_http_get_page", fake_get_page)

    tags = cti._list_tags("noorinalabs/img", "tok")

    # base → cursor=a → cursor=b → (cursor=a already seen) STOP. Finite.
    assert tags == ["base", "a", "b"]
    assert len(calls) == 3


def test_list_tags_bounded_by_page_cap(monkeypatch):
    """An ever-advancing (never-repeating) `next` is capped at _MAX_TAG_PAGES."""
    counter = {"n": 0}

    def fake_get_page(url, headers):
        counter["n"] += 1
        i = counter["n"]
        # Always a fresh cursor → the visited set never trips; only the page
        # cap can stop the walk.
        return json.dumps({"tags": [f"t{i}"]}).encode(), _link(
            f"https://ghcr.io/v2/noorinalabs/img/tags/list?cursor={i}"
        )

    monkeypatch.setattr(cti, "_http_get_page", fake_get_page)

    tags = cti._list_tags("noorinalabs/img", "tok")

    assert counter["n"] == cti._MAX_TAG_PAGES
    assert len(tags) == cti._MAX_TAG_PAGES

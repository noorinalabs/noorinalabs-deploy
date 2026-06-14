"""Regression guard for the blackbox-probe host partition (deploy#449).

After the #245 vhost split, the ``isnad.{$BASE_DOMAIN}`` Caddy vhost is a pure
frontend + isnad-graph-API surface — all user-service routes (``/auth/*``,
JWKS, the ``/api/v1/user-service/health`` rewrite) live solely on
``users.{$BASE_DOMAIN}``. The post-deploy smoke battery already probes those on
``USER_SERVICE_BASE_URL`` (#414), but the Prometheus blackbox config was left
probing ``isnad.*`` — so ``user-service-health`` 404'd against the
isnad-graph-api ``/api/*`` catch-all, and ``jwks`` / ``auth-login-redirect``
fell through to the SPA. deploy#449 re-pointed them to ``users.*``.

This test parses the committed ``prometheus.{stg,prod}.yml`` blackbox jobs and
asserts the partition holds, so the drift cannot silently come back:

  * every user-service-owned probe targets the ``users.*`` host, never ``isnad.*``
  * the isnad-graph-owned probes stay on the ``isnad.*`` host

It is intentionally dependency-free (regex over the file text) because the
``pytest (scripts)`` CI job installs only ``pytest`` — no PyYAML.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit

PROM_DIR = Path(__file__).resolve().parent.parent.parent / "infra" / "prometheus"
CONFIGS = ["prometheus.stg.yml", "prometheus.prod.yml"]

# Probes owned by user-service — must live on the users.* vhost post-#245.
USER_SERVICE_ROUTES = {"user-service-health", "jwks", "auth-login-redirect"}
# Probes owned by isnad-graph (frontend + api) — must stay on the isnad.* vhost.
ISNAD_ROUTES = {"isnad-health", "narrator-401"}

# Matches a blackbox `- targets: ["<url>"]` entry whose very next line is its
# `labels: { route: "<name>" ... }`. Requiring the `labels:` line immediately
# after the target scopes the match to blackbox probe entries and excludes
# plain scrape targets (alertmanager, api:8000, exporters) that have no route
# label — so a target never gets paired with a far-away route.
_PROBE_RE = re.compile(
    r'targets:\s*\[\s*"(?P<url>[^"]+)"\s*\]\s*\n\s*'
    r'labels:\s*\{\s*route:\s*"(?P<route>[^"]+)"',
)


def _probes(config: str) -> dict[str, str]:
    """Return {route_label: host} for every blackbox target in *config*."""
    text = (PROM_DIR / config).read_text(encoding="utf-8")
    probes: dict[str, str] = {}
    for m in _PROBE_RE.finditer(text):
        probes[m.group("route")] = urlsplit(m.group("url")).hostname or ""
    return probes


def test_configs_exist() -> None:
    for config in CONFIGS:
        assert (PROM_DIR / config).is_file(), f"missing {config}"


def test_user_service_probes_target_users_vhost() -> None:
    for config in CONFIGS:
        probes = _probes(config)
        for route in USER_SERVICE_ROUTES:
            assert route in probes, f"{config}: probe '{route}' missing"
            host = probes[route]
            assert host.startswith("users."), (
                f"{config}: user-service probe '{route}' targets host "
                f"'{host}' — must be the users.* vhost post-#245 (deploy#449)"
            )
            assert not host.startswith("isnad."), (
                f"{config}: user-service probe '{route}' is back on isnad.* "
                f"('{host}') — the #245/#449 partition regressed"
            )


def test_isnad_probes_stay_on_isnad_vhost() -> None:
    for config in CONFIGS:
        probes = _probes(config)
        for route in ISNAD_ROUTES:
            assert route in probes, f"{config}: probe '{route}' missing"
            host = probes[route]
            assert host.startswith("isnad."), (
                f"{config}: isnad-graph probe '{route}' targets host "
                f"'{host}' — expected the isnad.* vhost"
            )

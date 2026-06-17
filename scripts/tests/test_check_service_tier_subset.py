"""Unit tests for scripts/check_service_tier_subset.py (deploy#434).

The gate asserts that every name in the prod tiered-rollout inputs
(``gated_services`` + ``deferred_services``, set on the write-deploy-env
composite in deploy-prod.yml) is a real compose service — i.e. a subset of
``docker compose config --services``. A renamed/removed/typo'd entry silently
drops out of its tier (deploy#429), so we test:

  * the pure subset logic (``find_drift``) catches missing names and passes a
    clean superset,
  * the workflow parser (``tier_lists``) reads the real deploy-{stg,prod}.yml —
    prod sets both inputs, stg leaves them empty,
  * an end-to-end run against the REAL workflows passes when fed a service list
    that includes every tiered name, and fails (exit 1) when one is dropped —
    feeding the list on stdin so no Docker daemon is needed.
"""

from __future__ import annotations

import subprocess
import sys
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import check_service_tier_subset as gate  # noqa: E402

# Real prod tiered-rollout names (deploy-prod.yml). Kept here so a future rename
# of either the compose service OR the workflow input trips a test, not just CI.
PROD_GATED = {"caddy", "frontend", "landing", "api", "user-service"}
PROD_DEFERRED = {"kafka", "kafka-init", "kafka-ui", "kafka-exporter"}

# A `docker compose config --services` view (no --profile) that is a superset of
# every tiered name — the four pipeline workers are correctly absent.
FULL_SERVICES = (
    PROD_GATED
    | PROD_DEFERRED
    | {
        "neo4j",
        "postgres",
        "redis",
        "user-postgres",
        "user-redis",
        "user-service-migrate",
        "prometheus",
        "grafana",
    }
)


def test_read_services_strips_and_skips_blanks() -> None:
    parsed = gate.read_services(StringIO("api\n caddy \n\n\nkafka\n"))
    assert parsed == {"api", "caddy", "kafka"}


def test_find_drift_clean_when_subset() -> None:
    tiers = {"gated_services": PROD_GATED, "deferred_services": PROD_DEFERRED}
    assert gate.find_drift(FULL_SERVICES, tiers) == {}


def test_find_drift_reports_only_unknown_names() -> None:
    tiers = {
        "gated_services": {"api", "caddi"},  # typo: caddi vs caddy
        "deferred_services": {"kafka", "kafka-broker"},  # renamed-away
    }
    drift = gate.find_drift(FULL_SERVICES, tiers)
    assert drift == {
        "gated_services": {"caddi"},
        "deferred_services": {"kafka-broker"},
    }


def test_tier_lists_reads_real_prod_workflow() -> None:
    tiers = gate.tier_lists(gate.REPO_ROOT / ".github/workflows/deploy-prod.yml")
    assert tiers["gated_services"] == PROD_GATED
    assert tiers["deferred_services"] == PROD_DEFERRED


def test_tier_lists_stg_leaves_inputs_empty() -> None:
    tiers = gate.tier_lists(gate.REPO_ROOT / ".github/workflows/deploy-stg.yml")
    assert tiers["gated_services"] == set()
    assert tiers["deferred_services"] == set()


def _run(services: set[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(gate.REPO_ROOT / "scripts/check_service_tier_subset.py")],
        input="\n".join(sorted(services)) + "\n",
        capture_output=True,
        text=True,
    )


def test_main_passes_against_real_workflows_with_full_stack() -> None:
    result = _run(FULL_SERVICES)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Service-tier drift check: OK" in result.stdout


def test_main_fails_when_a_gated_service_is_missing() -> None:
    result = _run(FULL_SERVICES - {"caddy"})
    assert result.returncode == 1
    assert "gated_services" in result.stdout
    assert "caddy" in result.stdout


def test_main_fails_on_empty_stdin() -> None:
    result = subprocess.run(
        [sys.executable, str(gate.REPO_ROOT / "scripts/check_service_tier_subset.py")],
        input="",
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "no services on stdin" in result.stdout

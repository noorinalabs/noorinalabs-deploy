"""Unit tests for scripts/compose_tiered_up.sh (deploy#429).

The script runs the prod rollout as three tiers so an unhealthy non-app service
(a crash-looping Kafka broker) can never abort the app+edge bring-up and leave
caddy/frontend in ``Created`` (total edge outage):

    tier 1  app+edge  ($GATED_SERVICES)   health-GATED   -> failure fails deploy
    tier 2  remainder (observability)     NON-gating
    tier 3  pipeline  ($DEFERRED_SERVICES) NON-gating

A real Docker daemon / prod stack is not available in CI, so we inject a mock
``COMPOSE_CMD`` that (a) answers ``config --services`` with a fixture service
list and (b) records every ``up`` invocation, optionally failing the one whose
argv contains a chosen token. That lets us assert the exact service partition
across the three tiers and the gating contract (tier 1 fatal, tiers 2/3 not).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "compose_tiered_up.sh"

# Fixture stack — a representative subset of compose/docker-compose.prod.yml:
# app+edge + their deps, two observability services, and the Kafka subgraph.
ALL_SERVICES = [
    "neo4j",
    "postgres",
    "redis",
    "api",
    "frontend",
    "landing",
    "user-postgres",
    "user-redis",
    "user-service-migrate",
    "user-service",
    "caddy",
    "prometheus",
    "grafana",
    "kafka",
    "kafka-init",
    "kafka-ui",
    "kafka-exporter",
]
GATED = "caddy frontend landing api user-service"
DEFERRED = "kafka kafka-init kafka-ui kafka-exporter"


def _write_mock(tmp_path: Path, up_log: Path, fail_token: str = "") -> Path:
    """Write a mock `docker compose` that records `up` argv and can fail one."""
    services = "\n".join(ALL_SERVICES)
    mock = tmp_path / "mock_compose.sh"
    mock.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "config" ]; then\n'
        f'  printf "%s\\n" "{services}"\n'
        "  exit 0\n"
        "fi\n"
        # Record the full argv of any non-config (i.e. `up`) invocation.
        f'echo "$*" >> "{up_log}"\n'
        f'fail_token="{fail_token}"\n'
        'if [ -n "$fail_token" ]; then\n'
        '  case " $* " in\n'
        '    *" $fail_token "*) exit 1 ;;\n'
        "  esac\n"
        "fi\n"
        "exit 0\n"
    )
    mock.chmod(0o755)
    return mock


def _run(mock: Path, up_log: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": "/usr/bin:/bin",
            "COMPOSE_CMD": f"sh {mock}",
            "GATED_SERVICES": GATED,
            "DEFERRED_SERVICES": DEFERRED,
        },
    )


def _up_lines(up_log: Path) -> list[str]:
    return [ln for ln in up_log.read_text().splitlines() if ln.strip()]


def test_script_exists() -> None:
    assert SCRIPT.is_file(), f"tiered-up script missing at {SCRIPT}"


def test_three_tiers_partition_services(tmp_path: Path) -> None:
    """Happy path: exactly three `up` calls with the right service sets."""
    up_log = tmp_path / "up.log"
    mock = _write_mock(tmp_path, up_log)
    res = _run(mock, up_log)
    assert res.returncode == 0, res.stderr

    lines = _up_lines(up_log)
    assert len(lines) == 3, f"expected 3 up invocations, got: {lines}"

    tier1, tier2, tier3 = lines

    # tier 1 — app+edge, health-gated with --wait + --force-recreate.
    assert "--wait" in tier1
    assert "--force-recreate" in tier1
    for svc in GATED.split():
        assert f" {svc}" in f" {tier1}", f"{svc} missing from tier 1: {tier1}"
    for svc in DEFERRED.split():
        assert f" {svc} " not in f" {tier1} ", f"{svc} must NOT be in tier 1"

    # tier 2 — remainder: every non-deferred service, and NO deferred one.
    assert "--remove-orphans" in tier2
    assert "--wait" not in tier2, "tier 2 must be non-gating (no --wait)"
    for svc in ALL_SERVICES:
        present = f" {svc} " in f" {tier2} "
        if svc in DEFERRED.split():
            assert not present, f"deferred {svc} leaked into tier 2: {tier2}"
        else:
            assert present, f"non-deferred {svc} missing from tier 2: {tier2}"

    # tier 3 — pipeline only, non-gating (no --wait).
    assert "--wait" not in tier3, "tier 3 must be non-gating (no --wait)"
    for svc in DEFERRED.split():
        assert f" {svc}" in f" {tier3}", f"{svc} missing from tier 3: {tier3}"
    assert "caddy" not in tier3 and "api" not in tier3


def test_pipeline_failure_is_non_fatal(tmp_path: Path) -> None:
    """A failing tier-3 (pipeline) `up` must NOT fail the deploy."""
    up_log = tmp_path / "up.log"
    # `kafka-init` only ever appears in the tier-3 argv, so this fails tier 3.
    mock = _write_mock(tmp_path, up_log, fail_token="kafka-init")
    res = _run(mock, up_log)
    assert res.returncode == 0, f"pipeline failure must be non-gating: {res.stderr}"
    assert len(_up_lines(up_log)) == 3, "all three tiers should still be attempted"
    assert "::warning::tier 3" in res.stdout


def test_app_tier_failure_is_fatal(tmp_path: Path) -> None:
    """A failing tier-1 (app+edge) `up` must fail the deploy before later tiers."""
    up_log = tmp_path / "up.log"
    # `caddy` appears only in the tier-1 argv → tier 1 fails under `set -e`.
    mock = _write_mock(tmp_path, up_log, fail_token="caddy")
    res = _run(mock, up_log)
    assert res.returncode != 0, "app/edge failure must fail the deploy"
    assert len(_up_lines(up_log)) == 1, "must abort after tier 1; no tier 2/3 run"


def test_empty_gated_services_refused(tmp_path: Path) -> None:
    """No GATED_SERVICES → refuse (the composite never calls it that way)."""
    up_log = tmp_path / "up.log"
    mock = _write_mock(tmp_path, up_log)
    res = subprocess.run(
        ["sh", str(SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": "/usr/bin:/bin",
            "COMPOSE_CMD": f"sh {mock}",
            "GATED_SERVICES": "",
            "DEFERRED_SERVICES": DEFERRED,
        },
    )
    assert res.returncode != 0
    assert not up_log.exists(), "no `up` should run when GATED_SERVICES is empty"

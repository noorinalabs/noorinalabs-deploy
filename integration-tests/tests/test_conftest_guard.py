"""Tests for the module-load-time prod-environment guard in conftest.py.

The guard refuses `RUN_MODE=remote` + `ENVIRONMENT in {prod, production}`
at module import time so that direct `pytest` invocations (bypassing
run-tests.sh) cannot reach the auth-mutating fixtures against prod.

These tests subprocess-import the conftest module with controlled env
vars and assert on the exit code + stderr. Subprocess isolation is
required because the guard executes at module load, and the conftest
is already loaded once in this test process — re-importing it here
would not re-run the top-level statements.

See deploy#203 for the issue and run-tests.sh for the matching shell
guard layer.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

CONFTEST_PATH = Path(__file__).parent / "conftest.py"

# Minimum env to satisfy the post-guard URL/DSN checks in conftest. The
# guard fires BEFORE these are needed, but the passes-through cases need
# them so the import reaches the bottom of the module without crashing
# for an unrelated reason.
_HERMETIC_URLS = {
    "USER_SERVICE_URL": "http://user-service:8000",
    "ISNAD_GRAPH_URL": "http://isnad-graph-api:8001",
    # Hermetic-mode DSN-construction env vars (touched only when
    # RUN_MODE=hermetic; provided here so hermetic+prod doesn't crash
    # on missing-env for an unrelated reason).
    "USER_POSTGRES_USER": "test",
    "USER_POSTGRES_PASSWORD": "test",
    "USER_POSTGRES_HOST": "user-postgres",
    "USER_POSTGRES_PORT": "5432",
    "USER_POSTGRES_DB": "test",
    "USER_REDIS_URL": "redis://user-redis:6379/0",
}

_REMOTE_URLS = {
    "USER_SERVICE_BASE_URL": "https://auth.stg.noorinalabs.com",
    "ISNAD_BASE_URL": "https://isnad.stg.noorinalabs.com",
    "USER_SERVICE_URL": "https://auth.stg.noorinalabs.com",
    "ISNAD_GRAPH_URL": "https://isnad.stg.noorinalabs.com",
}


def _import_conftest(env_overrides: dict[str, str]) -> subprocess.CompletedProcess:
    """Run a subprocess that imports conftest.py with the given env, return result."""
    env = {
        # PATH so we can find python; PYTHONPATH so the tests/ dir is on
        # the import path; no pre-existing RUN_MODE/ENVIRONMENT leakage
        # from the parent test process.
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "PYTHONPATH": str(CONFTEST_PATH.parent),
        **env_overrides,
    }
    return subprocess.run(
        [sys.executable, "-c", "import conftest"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_remote_against_prod_raises():
    """RUN_MODE=remote + ENVIRONMENT=prod must abort at module load."""
    result = _import_conftest(
        {
            "RUN_MODE": "remote",
            "ENVIRONMENT": "prod",
            **_REMOTE_URLS,
        }
    )
    assert result.returncode != 0, (
        f"expected non-zero exit; got 0. stdout={result.stdout!r} " f"stderr={result.stderr!r}"
    )
    assert (
        "Refusing to run integration suite in remote mode" in result.stderr
    ), f"expected guard error in stderr; got: {result.stderr!r}"
    assert (
        "ENVIRONMENT='prod'" in result.stderr
    ), f"expected ENVIRONMENT value echoed in stderr; got: {result.stderr!r}"


def test_remote_against_production_raises():
    """The full `production` alias must also abort — matches shell guard."""
    result = _import_conftest(
        {
            "RUN_MODE": "remote",
            "ENVIRONMENT": "production",
            **_REMOTE_URLS,
        }
    )
    assert result.returncode != 0, f"expected non-zero exit; got 0. stderr={result.stderr!r}"
    assert "Refusing to run integration suite in remote mode" in result.stderr
    assert "ENVIRONMENT='production'" in result.stderr


def test_remote_against_stg_passes_through():
    """RUN_MODE=remote + ENVIRONMENT=stg must NOT raise the prod guard."""
    result = _import_conftest(
        {
            "RUN_MODE": "remote",
            "ENVIRONMENT": "stg",
            **_REMOTE_URLS,
        }
    )
    assert result.returncode == 0, (
        f"expected clean import; got rc={result.returncode}. " f"stderr={result.stderr!r}"
    )
    assert "Refusing to run integration suite" not in result.stderr


def test_hermetic_against_prod_passes_through():
    """RUN_MODE=hermetic must NOT gate on ENVIRONMENT.

    Hermetic mode runs against an ephemeral local stack — there is no
    real prod to mutate, so ENVIRONMENT=prod is a non-issue here.
    Gating on it would break a legitimate local-debug pattern.
    """
    result = _import_conftest(
        {
            "RUN_MODE": "hermetic",
            "ENVIRONMENT": "prod",
            **_HERMETIC_URLS,
        }
    )
    assert result.returncode == 0, (
        f"expected clean import; got rc={result.returncode}. " f"stderr={result.stderr!r}"
    )
    assert "Refusing to run integration suite" not in result.stderr

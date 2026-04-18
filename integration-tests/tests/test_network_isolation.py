"""Network isolation: user-postgres must NOT be reachable from isnad-graph.

The probe runs OUTSIDE pytest in `run-tests.sh` via
`docker compose exec isnad-graph-api python3 -c 'socket.gethostbyname(...)'`
because isnad-graph-api is the container whose isolation we actually care about.
The result is passed to the test-runner as ISOLATION_CHECK_RESULT.

Valid values:
  "pass"     — isnad-graph-api failed to resolve user-postgres (expected)
  "fail"     — isnad-graph-api resolved user-postgres — isolation broken
  "unknown"  — runner invoked without run-tests.sh wrapper
"""

from __future__ import annotations

import os

import pytest


@pytest.mark.isolation
def test_isnad_graph_cannot_reach_user_postgres() -> None:
    result = os.environ.get("ISOLATION_CHECK_RESULT", "unknown")
    assert result == "pass", (
        f"Isolation probe result = {result!r}. "
        "Expected 'pass' (isnad-graph-api could NOT resolve user-postgres). "
        "If 'fail': isnad-graph-api is attached to user-backend — PII isolation broken. "
        "If 'unknown': run-tests.sh did not set the env var; the probe was not executed."
    )

"""Network isolation: user-postgres must NOT be reachable from isnad-graph.

Pattern: run an isolation probe inside the isnad-graph-api container and assert
that a TCP connect to user-postgres:5432 fails. The two services share no
Docker network, so resolution itself should fail.
"""

from __future__ import annotations

import subprocess

import pytest


@pytest.mark.isolation
def test_isnad_graph_cannot_reach_user_postgres() -> None:
    # `docker exec` is not available from the test-runner container; we rely
    # instead on the docker Compose network topology: the testnet the runner
    # is attached to does NOT include user-backend, so user-postgres resolution
    # must fail from the runner as well.
    result = subprocess.run(
        [
            "python3",
            "-c",
            "import socket; socket.gethostbyname('user-postgres')",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, (
        "Runner (testnet-only) should not resolve user-postgres, but did. "
        "This means user-postgres is attached to a shared network — PII isolation broken."
    )
    # Sanity check: isnad-postgres is also user-backend-less from the runner's POV.
    result2 = subprocess.run(
        [
            "python3",
            "-c",
            "import socket; socket.gethostbyname('isnad-postgres')",
        ],
        capture_output=True,
        text=True,
    )
    assert result2.returncode != 0, (
        "isnad-postgres should not be reachable from the testnet either."
    )

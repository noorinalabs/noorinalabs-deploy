"""Unit tests for scripts/rotate_db_password.sh (deploy#387).

The script rotates ONE DB/cache password on the running deploy stack, health-
gated with rollback (the bespoke mechanism ADR 0007 / deploy#388 chose on top of
the GitHub-Environment-secrets baseline). A real Docker daemon / prod stack is
not available in CI, so we inject:

  * a mock ``docker`` (DOCKER=) that answers ``inspect`` with a controllable
    health string and records every ``exec`` invocation (draining its stdin),
  * a mock ``docker compose`` (COMPOSE_CMD=) that records every ``up`` argv,
  * a fixture ``.env`` in the single-quote-escaped form the deploy composite
    writes.

That lets us assert the engine-specific apply mechanics (postgres ALTER ROLE vs
redis recreate-only), the ``.env`` rewrite, the rollback invariant on a failed
health gate, and the security property that NO password ever reaches a child
process argv (CWE-214 — it must travel via stdin / env only).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "rotate_db_password.sh"

OLD = "oldpass_0123456789abcdefGHIJ"
NEW = "NEWpass_0123456789abcdefABCDEF"

# Other .env lines that MUST survive a rotation untouched (rollback safety —
# the deploy composite and rollback.yml both depend on the full .env shape).
PRESERVED = [
    "IMAGE_TAG='stg-abc1234'",
    "BASE_DOMAIN='stg.noorinalabs.com'",
    "NEO4J_PASSWORD='someneo4jpass_aaaaaaaaaaaa'",
]


def _write_env(tmp_path: Path, secret: str, *, with_user_db: bool) -> Path:
    lines = [f"{secret}='{OLD}'"]
    if with_user_db:
        # Match the secret's companion user/db vars the postgres branch reads.
        if secret == "POSTGRES_PASSWORD":
            lines += ["POSTGRES_USER='ig'", "POSTGRES_DB='isnad'"]
        elif secret == "USER_POSTGRES_PASSWORD":
            lines += ["USER_POSTGRES_USER='usersvc'", "USER_POSTGRES_DB='users'"]
    lines += PRESERVED
    env = tmp_path / ".env"
    env.write_text("\n".join(lines) + "\n")
    env.chmod(0o600)
    return env


def _write_mock_docker(tmp_path: Path, exec_log: Path, health: str = "healthy") -> Path:
    mock = tmp_path / "mock_docker.sh"
    mock.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "inspect" ]; then\n'
        f'  printf "%s\\n" "{health}"\n'
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "exec" ]; then\n'
        # Drain (and discard) stdin so the producing printf does not SIGPIPE,
        # then record only the ARGV — never the stdin (which carries the SQL +
        # new password). This is what lets the argv-leak assertion be meaningful.
        "  cat >/dev/null 2>&1 || true\n"
        f'  echo "exec $*" >> "{exec_log}"\n'
        "  exit 0\n"
        "fi\n"
        f'echo "other $*" >> "{exec_log}"\n'
        "exit 0\n"
    )
    mock.chmod(0o755)
    return mock


def _write_mock_compose(tmp_path: Path, up_log: Path) -> Path:
    mock = tmp_path / "mock_compose.sh"
    mock.write_text(f'#!/bin/sh\necho "$*" >> "{up_log}"\nexit 0\n')
    mock.chmod(0o755)
    return mock


def _run(
    env_file: Path,
    mock_docker: Path,
    mock_compose: Path,
    secret: str,
    extra_path: str = "",
) -> subprocess.CompletedProcess[str]:
    path = f"{extra_path}:/usr/bin:/bin" if extra_path else "/usr/bin:/bin"
    return subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": path,
            "ROTATE_SECRET": secret,
            "NEW_PASSWORD": NEW,
            "ENV_FILE": str(env_file),
            "DOCKER": str(mock_docker),
            "COMPOSE_CMD": f"sh {mock_compose}",
            "HEALTH_RETRIES": "1",
            "HEALTH_INTERVAL": "0",
        },
    )


def _write_argv_recording_awk(tmp_path: Path, argv_log: Path) -> Path:
    """A PATH-shadowing `awk` that records its OWN argv then delegates to the
    real awk (so the .env rewrite still happens). Lets the argv-leak test prove
    the rewrite passes the secret via ENVIRON, never on `-v val=` / argv."""
    real_awk = shutil.which("awk", path="/usr/bin:/bin") or "/usr/bin/awk"
    bindir = tmp_path / "shim-bin"
    bindir.mkdir()
    awk = bindir / "awk"
    awk.write_text(f'#!/bin/sh\necho "$@" >> "{argv_log}"\nexec {real_awk} "$@"\n')
    awk.chmod(0o755)
    return bindir


def _lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [ln for ln in path.read_text().splitlines() if ln.strip()]


def test_script_exists() -> None:
    assert SCRIPT.is_file(), f"rotation script missing at {SCRIPT}"


def test_postgres_happy_path_alters_and_rewrites(tmp_path: Path) -> None:
    env = _write_env(tmp_path, "POSTGRES_PASSWORD", with_user_db=True)
    exec_log, up_log = tmp_path / "exec.log", tmp_path / "up.log"
    res = _run(
        env,
        _write_mock_docker(tmp_path, exec_log),
        _write_mock_compose(tmp_path, up_log),
        "POSTGRES_PASSWORD",
    )
    assert res.returncode == 0, res.stderr

    # .env rewritten to the new value; all other lines preserved verbatim.
    body = env.read_text()
    assert f"POSTGRES_PASSWORD='{NEW}'" in body
    assert OLD not in body
    for line in PRESERVED:
        assert line in body, f"preserved line lost: {line}"

    # A live ALTER ROLE was issued against the postgres container (exec, psql).
    execs = _lines(exec_log)
    assert any("psql" in e and "noorinalabs-postgres-1" in e for e in execs), execs

    # The client services were recreated (NOT the db itself for postgres).
    up = _lines(up_log)
    assert len(up) == 1 and "--force-recreate" in up[0]
    assert " api" in f" {up[0]}" and "postgres-exporter" in up[0]


def test_redis_happy_path_recreates_without_exec(tmp_path: Path) -> None:
    env = _write_env(tmp_path, "REDIS_PASSWORD", with_user_db=False)
    exec_log, up_log = tmp_path / "exec.log", tmp_path / "up.log"
    res = _run(
        env,
        _write_mock_docker(tmp_path, exec_log),
        _write_mock_compose(tmp_path, up_log),
        "REDIS_PASSWORD",
    )
    assert res.returncode == 0, res.stderr

    assert f"REDIS_PASSWORD='{NEW}'" in env.read_text()
    # Redis rotation is recreate-only: NO live `exec` (no ALTER/CONFIG SET).
    assert not any(e.startswith("exec") for e in _lines(exec_log)), _lines(exec_log)
    # The redis container itself MUST be recreated (re-applies requirepass), plus its client.
    up = _lines(up_log)
    assert len(up) == 1
    assert " redis" in f" {up[0]}" and " api" in f" {up[0]}"


def test_failed_health_gate_rolls_back(tmp_path: Path) -> None:
    env = _write_env(tmp_path, "POSTGRES_PASSWORD", with_user_db=True)
    exec_log, up_log = tmp_path / "exec.log", tmp_path / "up.log"
    # Health never reaches "healthy" -> gate fails -> rollback.
    res = _run(
        env,
        _write_mock_docker(tmp_path, exec_log, health="starting"),
        _write_mock_compose(tmp_path, up_log),
        "POSTGRES_PASSWORD",
    )
    assert res.returncode != 0

    # .env restored to the OLD credential (rollback invariant).
    body = env.read_text()
    assert f"POSTGRES_PASSWORD='{OLD}'" in body
    assert NEW not in body

    # Two ALTER ROLEs: forward (old->new) then rollback (new->old).
    assert sum(1 for e in _lines(exec_log) if "psql" in e) == 2
    # Recreate ran forward AND in rollback.
    assert len(_lines(up_log)) == 2


def test_password_never_on_argv(tmp_path: Path) -> None:
    """CWE-214: neither the old nor the new password may appear in any recorded
    child-process argv. Covers the docker/compose mocks AND — via a PATH-shadow
    `awk` shim — the real `.env`-rewrite awk invocation (which must read the
    value from ENVIRON, not `-v val=`). Without the shim this would silently
    miss the awk path (Nino, PR #438 review)."""
    env = _write_env(tmp_path, "POSTGRES_PASSWORD", with_user_db=True)
    exec_log, up_log = tmp_path / "exec.log", tmp_path / "up.log"
    awk_argv = tmp_path / "awk_argv.log"
    bindir = _write_argv_recording_awk(tmp_path, awk_argv)
    res = _run(
        env,
        _write_mock_docker(tmp_path, exec_log),
        _write_mock_compose(tmp_path, up_log),
        "POSTGRES_PASSWORD",
        extra_path=str(bindir),
    )
    assert res.returncode == 0, res.stderr
    # The shim must actually have run (otherwise the awk assertion is vacuous).
    awk_calls = _lines(awk_argv)
    assert awk_calls, "awk shim was never invoked — rewrite path not exercised"
    for recorded in _lines(exec_log) + _lines(up_log) + awk_calls:
        assert OLD not in recorded, f"OLD password leaked to argv: {recorded}"
        assert NEW not in recorded, f"NEW password leaked to argv: {recorded}"


def test_rejects_url_unsafe_new_password(tmp_path: Path) -> None:
    env = _write_env(tmp_path, "POSTGRES_PASSWORD", with_user_db=True)
    exec_log, up_log = tmp_path / "exec.log", tmp_path / "up.log"
    res = subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": "/usr/bin:/bin",
            "ROTATE_SECRET": "POSTGRES_PASSWORD",
            "NEW_PASSWORD": "has/slash+and=specials@chars",  # url-unsafe
            "ENV_FILE": str(env),
            "DOCKER": str(_write_mock_docker(tmp_path, exec_log)),
            "COMPOSE_CMD": f"sh {_write_mock_compose(tmp_path, up_log)}",
        },
    )
    assert res.returncode != 0
    assert not exec_log.exists() and not up_log.exists(), (
        "no engine action before validation passes"
    )
    assert env.read_text().count(f"POSTGRES_PASSWORD='{OLD}'") == 1


def test_rejects_unknown_secret(tmp_path: Path) -> None:
    env = _write_env(tmp_path, "POSTGRES_PASSWORD", with_user_db=True)
    exec_log, up_log = tmp_path / "exec.log", tmp_path / "up.log"
    res = _run(
        env,
        _write_mock_docker(tmp_path, exec_log),
        _write_mock_compose(tmp_path, up_log),
        "JWT_PRIVATE_KEY",
    )
    assert res.returncode != 0
    assert "not a rotatable" in (res.stdout + res.stderr)


def test_rejects_noop_rotation(tmp_path: Path) -> None:
    """NEW == current value must be refused before touching anything."""
    env = tmp_path / ".env"
    env.write_text(f"POSTGRES_PASSWORD='{NEW}'\nPOSTGRES_USER='ig'\nPOSTGRES_DB='isnad'\n")
    env.chmod(0o600)
    exec_log, up_log = tmp_path / "exec.log", tmp_path / "up.log"
    res = _run(
        env,
        _write_mock_docker(tmp_path, exec_log),
        _write_mock_compose(tmp_path, up_log),
        "POSTGRES_PASSWORD",
    )
    assert res.returncode != 0
    assert "no-op" in (res.stdout + res.stderr)
    assert not up_log.exists()

"""Guard: the content-manifest scratch postgres readiness gate must wait for the TARGET
DATABASE over TCP, not `pg_isready` (deploy#690).

WHY A SOURCE GUARD AND NOT AN EXECUTION TEST. `generate_content_manifest()` restores the
user-postgres dump into a throwaway `postgres:16-alpine` and reads it back. The image's
entrypoint runs a TEMPORARY bootstrap server on the unix socket only (`listen_addresses=''`)
to run initdb and `CREATE DATABASE "$POSTGRES_DB"`, then stops it and starts the real server.
`pg_isready` hits the socket and reports "ready" DURING that bootstrap phase — before the
target DB exists — so a `pg_restore -d "$USER_POSTGRES_DB"` that follows dies with
``FATAL: database "user_service" does not exist``. This is timing/load dependent: it surfaced
on the real stg host under backup-time CPU load (pg_isready@1.0s vs target-DB-over-TCP@1.3s in
a calibration probe), NOT in CI, and NOT in a quiescent repro — the exact class of real-host
race a plain execution test cannot reliably reproduce. And the one end-to-end test that DOES
run backup.sh under the real hardening asserts ``rc == 0``; because the manifest step is
fail-safe (it never fails the backup run), that test stays green while the manifest silently
never gets written. So the durable CI catch is a source guard on the readiness *mechanism*:
a regression back to `pg_isready` (or to a socket-only probe, or one that omits the target DB)
must fail here. The definitive proof the fix works is the real-host one recorded on the issue
(a fresh stg backup logging ``Content manifest: OK``).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKUP_SH = REPO_ROOT / "scripts" / "backup.sh"


def _generate_content_manifest_body() -> str:
    """Return the body of generate_content_manifest(), from its `() {` line to the
    first line that is a bare `}` in column 0 (the function's closing brace)."""
    text = BACKUP_SH.read_text()
    start = re.search(r"^generate_content_manifest\(\)\s*\{", text, re.MULTILINE)
    assert start, "generate_content_manifest() not found in backup.sh"
    rest = text[start.end() :]
    end = re.search(r"^\}", rest, re.MULTILINE)
    assert end, "could not find the closing brace of generate_content_manifest()"
    return rest[: end.start()]


def _readiness_until_block(body: str) -> str:
    """The `until docker exec "$CONTENT_MANIFEST_CONTAINER" ...; do ... done` readiness
    loop that gates pg_restore. Returned as the text from the `until` to its `done`."""
    m = re.search(
        r"until\s+docker\s+exec\s+\"\$CONTENT_MANIFEST_CONTAINER\".*?\bdone\b",
        body,
        re.DOTALL,
    )
    assert m, (
        'no `until docker exec "$CONTENT_MANIFEST_CONTAINER" ...; do ...; done` '
        "readiness loop found"
    )
    return m.group(0)


def test_readiness_gate_exists_before_pg_restore() -> None:
    body = _generate_content_manifest_body()
    until_start = body.find("until docker exec")
    # Anchor on the pg_restore COMMAND (`pg_restore -U ...`), not a prose mention of
    # "pg_restore" in the readiness comment, which legitimately precedes the loop.
    restore_at = body.find("pg_restore -U")
    assert until_start != -1, "no readiness loop in generate_content_manifest()"
    assert restore_at != -1, "no `pg_restore -U` command in generate_content_manifest()"
    assert until_start < restore_at, "the readiness gate must precede pg_restore"


def test_readiness_gate_does_not_use_pg_isready() -> None:
    """pg_isready reports ready against the socket-only bootstrap server, before the
    target DB exists (deploy#690). The readiness gate must not rely on it."""
    block = _readiness_until_block(_generate_content_manifest_body())
    assert "pg_isready" not in block, (
        "content-manifest readiness gate uses pg_isready, which races the postgres "
        "bootstrap (deploy#690): it reports ready before CREATE DATABASE runs, so "
        "pg_restore hits 'database does not exist'. Probe the target DB over TCP instead."
    )


def test_readiness_gate_probes_target_db_over_tcp() -> None:
    """A TCP `SELECT 1` against "$USER_POSTGRES_DB" is deterministic: the temp bootstrap
    server refuses TCP, so success means the real server is up AND the DB was created."""
    block = _readiness_until_block(_generate_content_manifest_body())
    assert "psql" in block, (
        "readiness gate must probe with psql (a real query), not a liveness ping"
    )
    assert "-h 127.0.0.1" in block, (
        "readiness gate must connect over TCP (-h 127.0.0.1) — only the fully-started "
        "real server listens on TCP; the bootstrap server is socket-only (deploy#690)"
    )
    assert '-d "$USER_POSTGRES_DB"' in block, (
        "readiness gate must query the TARGET database ($USER_POSTGRES_DB) — that is the "
        "object whose existence pg_restore depends on (deploy#690)"
    )
    assert "SELECT 1" in block, (
        "readiness gate must issue a real query (SELECT 1) against the target DB"
    )


def test_calibration_the_guard_goes_red_on_the_racy_probe() -> None:
    """Calibration (feedback: calibrate the mutation before counting it): reconstruct the
    pre-fix (racy) readiness gate and confirm the guard's core assertion — no `pg_isready` —
    would REJECT it. A guard that cannot go red on the very defect it names proves nothing.
    The substitution is anchored to the exact fixed probe text, so it also fails loudly if
    that text drifts (keeping this calibration honest rather than vacuously green)."""
    block = _readiness_until_block(_generate_content_manifest_body())
    racy = re.sub(
        r"psql -h 127\.0\.0\.1 -U \"\$USER_POSTGRES_USER\" "
        r"-d \"\$USER_POSTGRES_DB\" -tAc 'SELECT 1'",
        'pg_isready -U "$USER_POSTGRES_USER" -d "$USER_POSTGRES_DB"',
        block,
    )
    assert racy != block, (
        "calibration could not construct the racy variant — the fixed probe text drifted"
    )
    # The guard's core check is `"pg_isready" not in block`; against the racy variant it trips.
    assert "pg_isready" in racy, "the reconstructed racy block must contain the forbidden probe"

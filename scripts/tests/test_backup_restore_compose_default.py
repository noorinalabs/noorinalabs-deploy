"""Drift guard for the backup/restore ``COMPOSE_FILE`` default (deploy#498).

``scripts/restore.sh`` is the rollback safety net for destructive prod data
operations (e.g. the main#723 purge-and-reload). If it resolves a *different*
compose file than ``scripts/backup.sh`` captured against, an unqualified restore
targets the wrong (or a non-existent) stack — and fails mid-rollback, at the
worst possible moment. deploy#498: the two defaults had silently drifted
(``compose/docker-compose.prod.yml`` vs a bare ``docker-compose.prod.yml``).

These tests parse the ``COMPOSE_FILE="${COMPOSE_FILE:-<default>}"`` line out of
each script and assert the two defaults stay in lock-step, plus a smoke check
that the shared default resolves to a file that actually exists in the repo.
The equality test FAILS on the pre-fix tree and PASSES once the defaults align.
"""

from __future__ import annotations

import re
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SCRIPTS_DIR.parent
BACKUP = SCRIPTS_DIR / "backup.sh"
RESTORE = SCRIPTS_DIR / "restore.sh"

# Matches:  COMPOSE_FILE="${COMPOSE_FILE:-compose/docker-compose.prod.yml}"
_DEFAULT_RE = re.compile(
    r'^COMPOSE_FILE="\$\{COMPOSE_FILE:-(?P<default>[^}]+)\}"',
    re.MULTILINE,
)


def _compose_default(script: Path) -> str:
    """Extract the literal ``COMPOSE_FILE`` default the script falls back to."""
    text = script.read_text()
    match = _DEFAULT_RE.search(text)
    assert match is not None, f"no COMPOSE_FILE default assignment found in {script}"
    return match.group("default")


def test_scripts_exist() -> None:
    assert BACKUP.is_file(), f"backup script missing at {BACKUP}"
    assert RESTORE.is_file(), f"restore script missing at {RESTORE}"


def test_backup_and_restore_compose_defaults_match() -> None:
    """The rollback path must default to the same stack backup captured."""
    backup_default = _compose_default(BACKUP)
    restore_default = _compose_default(RESTORE)
    assert backup_default == restore_default, (
        "backup.sh and restore.sh COMPOSE_FILE defaults have drifted: "
        f"backup={backup_default!r} restore={restore_default!r} — an unqualified "
        "restore would roll back a different stack than backup captured (deploy#498)"
    )


def test_compose_default_is_canonical_prod_path() -> None:
    """Guard against silently realigning both to the wrong (bare) path."""
    assert _compose_default(RESTORE) == "compose/docker-compose.prod.yml"


def test_compose_default_resolves_to_existing_file() -> None:
    """Smoke: the shared default must point at a real compose file in the repo."""
    default = _compose_default(BACKUP)
    resolved = REPO_ROOT / default
    assert resolved.is_file(), (
        f"COMPOSE_FILE default {default!r} does not resolve to an existing file "
        f"(looked for {resolved}) — restore/backup would target a missing stack"
    )

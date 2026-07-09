"""Regression guards for the restore-path failure modes found in deploy#560.

``scripts/restore.sh`` had never been executed against a real backup artifact. When it
finally was, it reported ``PostgreSQL: restored`` / ``=== Restore complete ===`` and
exited **0** for every one of these inputs:

* a dump truncated to half its length, with a checksum recomputed to match
* a backup directory containing a dump but **zero** ``.sha256`` files
* a completely **empty** backup directory

Only a checksum mismatch was caught. Three of four failure modes were false greens.

Two further defects meant the Neo4j leg could never have worked at all:

* ``neo4j-admin database load neo4j --from-path=DIR`` resolves the archive by name and
  requires ``DIR/neo4j.dump``; ``backup.sh`` writes ``isnad-neo4j-<ts>.dump``, so the
  loader reported "No matching archives found".
* the ``neo4j:5-community`` entrypoint drops privileges to ``neo4j`` (uid 7474), which
  cannot read or write a bind mount it does not own — ``AccessDeniedException``.

And the data volume was resolved by grepping **every** volume on the host
(``docker volume ls | grep neo4j_data``), so a restore aimed at a scratch stack could
select the production graph volume and ``--overwrite-destination`` it.

The authoritative check is ``scripts/restore_rehearsal.sh``, which drives the real
script against real containers and requires each broken artifact to produce a non-zero
exit *before* trusting the intact one. These tests are the cheap, always-on companion:
they run in the ``pytest (scripts)`` CI job with no Docker, and fail fast if any of the
specific defects is reintroduced textually.
"""

from __future__ import annotations

import re
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SCRIPTS_DIR.parent
RESTORE = SCRIPTS_DIR / "restore.sh"
REHEARSAL = SCRIPTS_DIR / "restore_rehearsal.sh"
REHEARSAL_COMPOSE = REPO_ROOT / "compose" / "docker-compose.rehearsal.yml"


def _restore_text() -> str:
    return RESTORE.read_text()


def _code_only(text: str) -> str:
    """Drop whole-line shell comments.

    The fixes carry comments that quote the *old, buggy* code verbatim so the next
    reader understands what was wrong. A naive substring search over the raw file
    therefore matches the explanation and not the executable line — the guard would
    fire on its own documentation. Every assertion about code shape runs against this
    comment-stripped view; assertions about intent-carrying log strings run against the
    raw text, since those are real statements.
    """
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _function_body(text: str, name: str) -> str:
    """Return the body of shell function ``name`` (``name() { ... }``, brace at col 0)."""
    match = re.search(rf"^{re.escape(name)}\(\) \{{\n(.*?)^\}}", text, re.MULTILINE | re.DOTALL)
    assert match is not None, f"function {name}() not found"
    return match.group(1)


def _function_code(text: str, name: str) -> str:
    """Body of ``name()`` with comment lines removed."""
    return _code_only(_function_body(text, name))


# --------------------------------------------------------------------------
# The swallowed pg_restore exit code
# --------------------------------------------------------------------------


def test_restore_postgres_does_not_downgrade_failure_to_warning() -> None:
    """A non-zero pg_restore exit must not be relabelled a harmless warning.

    The original code ran pg_restore inside an ``if``, and in the ``else`` branch logged
    "pg_restore finished with warnings (this is often normal with --clean)" and fell
    through to success. The premise is false: pg_restore exits 0 when only warnings
    occurred. A non-zero exit means errors were ignored or the input was unreadable.
    """
    body = _function_code(_restore_text(), "restore_postgres")
    assert "often normal with --clean" not in body, (
        "restore_postgres() reintroduced the swallow that let a failed pg_restore "
        "report success (deploy#560)"
    )
    # The message is label-prefixed since deploy#559 ("PostgreSQL (user-service): ...").
    # Match the invariant part, not the exact prefix.
    assert re.search(r'log "ERROR" ".*pg_restore FAILED', body), (
        "restore_postgres() must log an ERROR when pg_restore exits non-zero"
    )
    assert "return 1" in body, "restore_postgres() must return non-zero when pg_restore fails"


def test_restore_postgres_also_catches_ignored_errors() -> None:
    """Defence in depth: pg_restore announces ``errors ignored on restore: N`` itself."""
    body = _function_code(_restore_text(), "restore_postgres")
    assert "errors ignored on restore" in body, (
        "restore_postgres() should fail when pg_restore reports ignored errors, even if "
        "a future flag change makes it exit 0"
    )


# --------------------------------------------------------------------------
# Vacuous checksum verification
# --------------------------------------------------------------------------


def test_verify_checksums_rejects_a_backup_with_no_checksum_files() -> None:
    """Verifying zero files must not be reported as a successful verification.

    ``for f in "$dir"/*.sha256`` with a ``[[ -f ]] || continue`` guard iterates once over
    the unexpanded literal when nothing matches; the guard skips it and the function
    returns success having checked nothing.
    """
    body = _function_code(_restore_text(), "verify_checksums")
    assert "No checksum files found" in body, (
        "verify_checksums() must hard-fail when the backup contains no .sha256 files"
    )
    # The bare-glob-plus-skip shape is exactly what made the vacuous pass possible.
    assert not re.search(r'for\s+checksum_file\s+in\s+"\$\{?dir\}?"/\*\.sha256', body), (
        "verify_checksums() must not iterate a bare glob; use find(1) so an empty "
        "directory yields an empty list rather than one literal iteration"
    )


def test_verify_checksums_treats_a_missing_referenced_file_as_fatal() -> None:
    """A checksum naming a file the backup lacks means the artifact is incomplete."""
    body = _function_code(_restore_text(), "verify_checksums")
    assert re.search(r'log "ERROR" "File referenced by checksum is missing', body), (
        "a checksum referencing an absent file must be an ERROR, not a WARNING"
    )


# --------------------------------------------------------------------------
# Empty backups and the final exit status
# --------------------------------------------------------------------------


def test_empty_backup_is_rejected() -> None:
    """A backup with neither dump is not a backup."""
    text = _code_only(_restore_text())
    assert "no PostgreSQL dump and no Neo4j dump" in text, (
        "restore.sh must refuse a backup containing neither dump; previously this "
        "produced two warnings, '=== Restore complete ===', and exit 0"
    )


def test_restore_exits_nonzero_when_any_store_failed() -> None:
    """The exit status is the contract callers and CI gate on."""
    text = _code_only(_restore_text())
    assert re.search(r'if \[\[ "\$FAILED" -eq 1 \]\]; then\n\s*exit 1', text), (
        "restore.sh must exit 1 when any store failed to restore"
    )
    assert 'log "ERROR" "=== Restore FAILED ==="' in text, (
        "a failed restore must not print the success banner"
    )


# --------------------------------------------------------------------------
# Neo4j: archive name, privilege drop, and volume blast radius
# --------------------------------------------------------------------------


def test_neo4j_dump_is_staged_under_the_name_the_loader_requires() -> None:
    """``database load neo4j --from-path=DIR`` looks for ``DIR/neo4j.dump`` by name."""
    body = _function_code(_restore_text(), "restore_neo4j")
    assert "neo4j.dump" in body, (
        "restore_neo4j() must stage the archive as neo4j.dump; backup.sh names it "
        "isnad-neo4j-<ts>.dump and the loader reports 'No matching archives found'"
    )


def test_neo4j_admin_bypasses_the_privilege_dropping_entrypoint() -> None:
    """The image entrypoint drops to uid 7474, which cannot access the bind mounts."""
    body = _function_code(_restore_text(), "restore_neo4j")
    assert "--entrypoint neo4j-admin" in body, (
        "restore_neo4j() must bypass the neo4j entrypoint, which drops privileges to "
        "neo4j(7474) and then fails with AccessDeniedException on /backups"
    )
    assert "--user 0:0" in body, "neo4j-admin must run as root to read /backups and write /data"


def test_neo4j_volume_is_resolved_through_the_compose_project() -> None:
    """Never select a data volume by grepping every volume on the host.

    ``docker volume ls | grep neo4j_data`` matches across all compose projects. Combined
    with ``--overwrite-destination`` that is how a rehearsal silently destroys the
    production graph.
    """
    body = _function_code(_restore_text(), "restore_neo4j")
    assert not re.search(r"docker volume ls.*grep", body), (
        "restore_neo4j() must not resolve the data volume by grepping host-global "
        "`docker volume ls` output — scope it to COMPOSE_FILE's project"
    )
    assert "ps -aq neo4j" in body, (
        "restore_neo4j() should resolve the neo4j container via the compose project"
    )


# --------------------------------------------------------------------------
# The rehearsal is a check, not a decoration
# --------------------------------------------------------------------------


def test_rehearsal_requires_negative_cases_to_fail() -> None:
    """``expect_fail`` must fail the rehearsal when restore.sh exits 0."""
    text = REHEARSAL.read_text()
    body = _function_code(text, "expect_fail")
    assert re.search(r"if \[\[ \$rc -eq 0 \]\]; then", body), (
        "expect_fail() must treat a zero exit on a broken artifact as a rehearsal failure"
    )
    assert "the check cannot fail" in body


def test_rehearsal_runs_negative_cases_before_the_positive_one() -> None:
    """A green rehearsal must have already demonstrated it can go red."""
    text = REHEARSAL.read_text()
    first_negative = text.index('expect_fail "truncated_dump_matching_checksum"')
    positive = text.index('expect_pass "intact_artifact"')
    assert first_negative < positive, (
        "negative cases must run before the positive one, so a passing rehearsal has "
        "already proven the check is capable of failing"
    )


def test_rehearsal_asserts_on_content_not_just_exit_status() -> None:
    """``pg_restore --clean`` can warn-and-succeed while restoring nothing."""
    text = REHEARSAL.read_text()
    body = _function_code(text, "assert_restored_content")
    assert "SELECT count(*) FROM narrator" in body
    assert "MATCH (n:Narrator) RETURN count(n)" in body
    assert "sampled record" in body and "sampled node" in body


def test_rehearsal_empties_stores_before_the_positive_restore() -> None:
    """Otherwise a restore that does nothing passes by leaving the seed data in place."""
    text = REHEARSAL.read_text()
    empty_at = text.index('empty_scratch_stores\n    if expect_pass "intact_artifact"')
    assert empty_at > 0, (
        "the scratch stores must be emptied immediately before the positive restore, or "
        "the content assertions cannot distinguish a real restore from a no-op"
    )


def test_rehearsal_refuses_a_real_environment_and_a_foreign_compose_file() -> None:
    body = _function_code(REHEARSAL.read_text(), "guard")
    assert "prod | production | stg | staging" in body, (
        "the rehearsal must refuse to run when ENVIRONMENT names a real environment"
    )
    assert "Refusing to run with an inherited COMPOSE_FILE" in body, (
        "an ambient COMPOSE_FILE pointing at prod would aim every restore at production"
    )


# --------------------------------------------------------------------------
# The rehearsal stack itself must be inert
# --------------------------------------------------------------------------


def test_rehearsal_compose_publishes_no_ports_and_uses_scratch_volumes() -> None:
    text = _code_only(REHEARSAL_COMPOSE.read_text())
    assert not re.search(r"^\s*ports:", text, re.MULTILINE), (
        "the rehearsal stack must publish nothing to the host"
    )
    assert "rehearsal_pg_data" in text and "rehearsal_neo4j_data" in text
    assert "noorinalabs_" not in text, (
        "the rehearsal stack must not reference production volume names"
    )


# --------------------------------------------------------------------------
# Backup coverage (deploy#559)
# --------------------------------------------------------------------------

BACKUP = SCRIPTS_DIR / "backup.sh"


def _backup_code() -> str:
    return _code_only(BACKUP.read_text())


def test_backup_dumps_user_postgres() -> None:
    """user-postgres holds the only state no artifact can rebuild.

    Accounts, sessions, RBAC and the ``audit_log`` relocated out of Neo4j. Before
    deploy#559 ``backup.sh`` dumped only neo4j and the isnad postgres.
    """
    code = _backup_code()
    assert "dump_postgres user-postgres" in code, (
        "backup.sh must dump the user-postgres service (accounts, sessions, audit_log)"
    )
    assert "isnad-userpg-" in code, "the user-service dump needs its own artifact name"


def test_backup_userpg_artifact_name_cannot_be_matched_by_the_isnad_glob() -> None:
    """``isnad-pg-*.dump`` must not also select ``isnad-userpg-*.dump``.

    restore.sh finds each dump by glob. If the user-service dump were named such that the
    isnad glob matched it, a restore would load the user database into the isnad database
    silently.
    """
    import fnmatch

    assert not fnmatch.fnmatch("isnad-userpg-20260709-120000.dump", "isnad-pg-*.dump")
    assert fnmatch.fnmatch("isnad-pg-20260709-120000.dump", "isnad-pg-*.dump")
    assert fnmatch.fnmatch("isnad-userpg-20260709-120000.dump", "isnad-userpg-*.dump")


def test_backup_fails_when_user_postgres_dump_fails() -> None:
    """A partial backup missing the irreplaceable store must not exit 0."""
    code = _backup_code()
    assert '"$USER_PG_OK" == "false"' in code, (
        "backup.sh must account for the user-postgres dump in its exit status"
    )


def test_backup_neo4j_bypasses_the_privilege_dropping_entrypoint() -> None:
    """BACKUP_DIR is 0700 root:root; the entrypoint's neo4j(7474) cannot write there."""
    code = _backup_code()
    assert "--entrypoint neo4j-admin" in code and "--user 0:0" in code, (
        "backup.sh's neo4j dump must bypass the entrypoint, which drops privileges to "
        "neo4j(7474) and then fails with AccessDeniedException on /backups"
    )


def test_backup_resolves_neo4j_volume_through_the_compose_project() -> None:
    code = _backup_code()
    assert not re.search(r"docker volume ls.*grep", code), (
        "backup.sh must not select the neo4j data volume by grepping host-global "
        "`docker volume ls` output"
    )


def test_datastore_inventory_exists_and_covers_every_compose_volume() -> None:
    """Every named volume in the prod compose file must appear in docs/DATASTORES.md.

    A stateful service added without a row here is exactly the omission deploy#559 exists
    to prevent: an undocumented gap and a deliberate exclusion are indistinguishable
    during an incident.
    """
    import yaml

    doc = REPO_ROOT / "docs" / "DATASTORES.md"
    assert doc.exists(), "docs/DATASTORES.md must exist"
    text = doc.read_text()

    compose = yaml.safe_load((REPO_ROOT / "compose" / "docker-compose.prod.yml").read_text())
    volumes = list((compose.get("volumes") or {}).keys())
    assert volumes, "expected named volumes in the prod compose file"

    missing = [v for v in volumes if v not in text]
    assert not missing, (
        f"docs/DATASTORES.md does not account for these compose volumes: {missing}. "
        "Add a row stating whether it is backed up and why."
    )

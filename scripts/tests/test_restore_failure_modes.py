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

import os
import re
import subprocess
import tempfile
from pathlib import Path

from manifest_fixture import build_manifest

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


# One concrete run id, substituted into BOTH the producer's `${TIMESTAMP}` and the consumer's
# `${RESTORE_RUN_TS}`, so the two sides are compared against the same run.
TIMESTAMP = "20260709-120000"


def _resolve_latest(root: Path) -> str:
    """Execute restore.sh's SHIPPED `resolve_latest` against a real bucket.

    `:local:` is rclone's local backend, so `${RCLONE_REMOTE}:${B2_BUCKET}/...` resolves to a
    real directory tree and both `rclone lsf` and `backup_is_complete` run for real.
    """
    body = _restore_text()
    fns = body[body.index("backup_is_complete() {") : body.index("\nverify_checksums()")]
    script = (
        "set -uo pipefail\n"
        "log() { :; }\n"
        'RCLONE_REMOTE=":local"\n'
        f'B2_BUCKET="{root}"\n' + fns + "\nresolve_latest\n"
    )
    r = subprocess.run(  # noqa: S603
        ["bash", "-c", script],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    )
    return r.stdout.strip()


def _backup_dump_filenames() -> dict[str, str]:
    """Concrete filenames ``backup.sh`` actually writes, read out of the script.

    Parses ``<NAME>_DUMP_FILE="${LOCAL_BACKUP_PATH}/isnad-...${TIMESTAMP}.dump"`` and
    substitutes a real timestamp, so the names under test are the producer's own — not
    literals restated in the test, which would agree with themselves forever.
    """
    pattern = re.compile(
        r'^(?P<var>[A-Z0-9_]+)_DUMP_FILE="\$\{LOCAL_BACKUP_PATH\}/(?P<name>[^"]+)"',
        re.M,
    )
    found = {
        m.group("var"): m.group("name").replace("${TIMESTAMP}", TIMESTAMP)
        for m in pattern.finditer(_backup_code())
    }
    assert found, "could not parse any *_DUMP_FILE assignment out of backup.sh"
    return found


def _restore_globs() -> dict[str, list[str]]:
    """The ``find -name <glob>`` patterns ``restore.sh`` actually selects each dump by.

    Selection is now bound to the run the manifest attests (deploy#584): the primary branch
    globs ``isnad-<store>-${RESTORE_RUN_TS}.dump`` and a fallback branch keeps the old
    ``isnad-<store>-*.dump`` for artifacts with no manifest. So this ACCUMULATES the patterns
    from both branches — each one has to satisfy the no-cross-match invariant, not just
    whichever happens to be parsed last. ``${RESTORE_RUN_TS}`` is substituted with the same
    concrete timestamp the producer parser uses, so the two sides are compared like for like.
    """
    globs: dict[str, list[str]] = {}
    for line in _code_only(_restore_text()).splitlines():
        m = re.match(r"^\s*(?P<var>[A-Z0-9_]+)_DUMP=\$\(find ", line)
        if not m:
            continue
        # -name 'single' or -name "double"
        found = re.findall(r"""-name ['"]([^'"]+)['"]""", line)
        pats = [p.replace("${RESTORE_RUN_TS}", TIMESTAMP) for p in found]
        globs.setdefault(m.group("var"), []).extend(pats)
    assert globs, "could not parse any *_DUMP=$(find ...) glob out of restore.sh"
    return globs


def test_backup_userpg_artifact_name_cannot_be_matched_by_the_isnad_glob() -> None:
    """The isnad glob must not select the user-service dump ``backup.sh`` really writes.

    restore.sh finds each dump by glob. If the user-service dump were named such that the
    isnad glob matched it, a restore would silently load the user database into the isnad
    database — and the rehearsal would not catch it, because the rehearsal constructs the
    artifact names itself rather than taking them from backup.sh.

    So this reads the producer's names and the consumer's globs from the two scripts and
    checks them against each other. Rename the dump in backup.sh without updating
    restore.sh's glob, or vice versa, and this fails.
    """
    import fnmatch

    names = _backup_dump_filenames()
    globs = _restore_globs()

    for var in ("PG", "USER_PG", "NEO4J"):
        assert var in names, f"backup.sh no longer defines {var}_DUMP_FILE"
        assert var in globs, f"restore.sh no longer selects {var}_DUMP by glob"

    def matches(filename: str, patterns: list[str]) -> bool:
        return any(fnmatch.fnmatch(filename, p) for p in patterns)

    # Each dump is found by its own glob...
    for var in ("PG", "USER_PG"):
        assert matches(names[var], globs[var]), (
            f"restore.sh's {var} glob {globs[var]} does not match the file backup.sh "
            f"writes ({names[var]}) — that dump would be silently skipped on restore"
        )

    # ...and the isnad glob does NOT also swallow the user-service dump. This is the
    # assertion that matters: it is the one that goes red on a bad rename.
    assert not matches(names["USER_PG"], globs["PG"]), (
        f"restore.sh's isnad glob {globs['PG']} also matches the user-service dump "
        f"({names['USER_PG']}) — a restore would load the user database into the isnad "
        f"database"
    )
    assert not matches(names["PG"], globs["USER_PG"]), (
        f"restore.sh's user-service glob {globs['USER_PG']} also matches the isnad dump "
        f"({names['PG']})"
    )


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


# --------------------------------------------------------------------------
# The required-store gate (deploy#559 review — Nurul Hakim, Lucas Ferreira)
# --------------------------------------------------------------------------
def test_absent_dump_is_as_fatal_as_a_failed_one() -> None:
    """A MISSING store must fail the restore, not warn about it.

    restore.sh set ``FAILED=1`` only when a dump was PRESENT and its restore failed. A dump
    that was absent entirely logged a WARNING, recorded ``skipped (no dump)``, and never
    touched ``FAILED`` — so the script printed ``=== Restore complete ===`` and exited **0**
    on a backup with no accounts, no sessions and no ``audit_log`` in it. The all-empty case
    was caught and the one-store-missing case was not: a guard on each side of the hole and
    none in it.

    Reachable by an artifact backup.sh itself produces: a partial upload lands in B2
    date-stamped, checksums cleanly, and ``latest`` selected it by directory NAME.

    backup.sh already refuses to call a partial backup a success (``USER_PG_OK`` is inside
    its non-zero-exit condition). The identical argument applies to the consumer, and had
    not been carried across.
    """
    code = _code_only(_restore_text())
    assert "MISSING_STORES" in code, "restore.sh must compute the set of expected-but-absent stores"
    assert "if [[ ${#MISSING_STORES[@]} -gt 0 ]]; then" in code, (
        "an absent expected store must be tested for, not merely warned about"
    )
    assert "ALLOW_PARTIAL" in code, (
        "the DR escape hatch must be an explicit opt-in, so the default is refuse"
    )
    # Default-deny: the refusal branch must exit non-zero.
    refuse = code.split("if [[ ${#MISSING_STORES[@]} -gt 0 ]]; then", 1)[1].split("fi", 1)[0]
    assert "exit 1" in refuse, (
        "without --allow-partial an incomplete backup must EXIT NON-ZERO. Documentation is "
        "not enforcement: during an incident nobody reads the header comment, they read the "
        "exit code and the last line."
    )


def test_allow_partial_is_opt_in_not_default() -> None:
    code = _code_only(_restore_text())
    assert "ALLOW_PARTIAL=false" in code, (
        "--allow-partial must default to false; a restore that silently tolerates a missing "
        "store is the defect this gate exists to close"
    )


def test_resolve_latest_selects_a_complete_backup() -> None:
    """`latest` must not mean "newest directory name".

    A partial backup is date-stamped and checksums cleanly; at rest it is indistinguishable
    from a complete one. Selecting by name means that the night the user-postgres dump
    fails, `latest` picks precisely the artifact missing the only store no pipeline artifact
    can rebuild — while the previous night's good backup sits there unselected.
    """
    code = _code_only(_restore_text())
    assert "_backup_manifest.txt" in code, (
        "completeness is a property of the artifact and the artifact must declare it"
    )

    # This test used to assert the LITERAL call line —
    #   `if backup_is_complete "${category}/${dir}"; then`
    # — which pinned the guard's SYNTAX rather than its BEHAVIOUR, and went red the moment the
    # call site legitimately grew a third outcome (deploy#584: "could not read the manifest" is
    # not a value of the predicate). A textual assertion is the same instrument that let the
    # `backup_is_complete` regex ship broken through a green suite: it can prove a line was
    # typed and never that it does anything. So RUN it.
    body = code.split("resolve_latest() {", 1)[1].split("\n}", 1)[0]
    assert "skipped" in body, (
        "an operator told nothing about why the newest backup was passed over will assume "
        "the tool is broken and reach for the one it refused"
    )
    assert "backup_is_complete" in body, "resolve_latest must consult the attestation"

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # The NEWEST directory is INCOMPLETE; the older one is complete. Selecting by name
        # picks 07-11 — the artifact missing the store nothing can rebuild.
        for date, complete in (("2026-07-10", True), ("2026-07-11", False)):
            d = root / "daily" / date
            d.mkdir(parents=True)
            (d / f"isnad-pg-{TIMESTAMP}.dump").write_bytes(b"P" * 4096)
            (d / "_backup_manifest.txt").write_text(
                f"BACKUP_MANIFEST complete={'true' if complete else 'false'} "
                f"stores=postgres,user-postgres,neo4j timestamp={TIMESTAMP} category=daily\n"
            )
        (root / "weekly").mkdir()

        assert _resolve_latest(root) == "daily/2026-07-10", (
            "'latest' must select the newest COMPLETE backup, not the newest directory NAME"
        )


def test_backup_declares_its_own_completeness() -> None:
    """The producer attests to what it actually wrote — it is the only party that knows."""
    code = _backup_code()
    assert "_backup_manifest.txt" in code, "backup.sh must write a completeness manifest"
    assert "BACKUP_MANIFEST complete=" in code, (
        "the manifest must declare whether this backup is complete"
    )
    # And the manifest must NOT be the source of the expected set — that would let a
    # partial backup declare itself complete-for-what-it-happens-to-hold.
    rcode = _code_only(_restore_text())
    assert "REQUIRED_STORES" in rcode, (
        "restore.sh must carry its OWN list of required stores. If the expected set came "
        "from the artifact, a partial backup would declare itself complete — the same "
        "circularity as a read-back count, and just as invisible."
    )


def test_every_negative_fixture_must_name_the_reason_it_fails_for() -> None:
    """A negative that passes for the WRONG reason is a test that has stopped testing.

    ``expect_fail`` used to assert only a non-zero exit. When restore.sh learned to refuse an
    incomplete backup, two fixtures that contained only the dump they mutate began being
    rejected for MISSING STORES — *before* their truncated dump was ever read. They still
    passed. The truncation path they exist to exercise was no longer tested at all, and
    nothing said so.

    That is the same defect class as a guard that cannot see what it exists to catch,
    reappearing inside the suite that guards against it. So every negative names the error it
    expects, and the rehearsal fails if it gets a different one.
    """
    text = REHEARSAL.read_text()
    assert 'expect_fail() {\n    local name="$1" src="$2" want="$3"' in text, (
        "expect_fail must take an expected-reason argument"
    )
    assert 'elif ! grep -q "$want" "$logfile"; then' in text, (
        "expect_fail must assert the failure happened for the expected reason"
    )
    # And every call site must supply one.
    calls = [ln for ln in text.splitlines() if ln.strip().startswith("expect_fail ")]
    assert len(calls) >= 6, f"expected at least 6 negative fixtures, found {len(calls)}"
    for ln in calls:
        # name + dir + reason => at least 3 quoted args
        assert ln.count('"') >= 6, (
            f"negative fixture does not name its expected reason: {ln.strip()}"
        )


def test_truncation_fixtures_start_from_a_complete_artifact() -> None:
    """Or the completeness gate rejects them before their truncation is ever exercised."""
    text = REHEARSAL.read_text()
    assert "copy_full_artifact() {" in text, (
        "a helper must build the truncation fixtures from a COMPLETE backup"
    )
    for fx in ('local trunc="${WORK}/fx_truncated"', 'local utrunc="${WORK}/fx_user_truncated"'):
        idx = text.index(fx)
        window = text[idx : idx + 200]
        assert "copy_full_artifact" in window, (
            f"fixture {fx} must start from a complete artifact, or the missing-store gate "
            "refuses it first and the truncation is never tested"
        )


def _resolve_latest_rc(root: str) -> tuple[int, str]:
    """Run the SHIPPED `resolve_latest` and return (rc, combined output).

    `root` is passed through as-is, so a nonexistent path exercises a FAILED LISTING — which
    is what a bad key / wrong bucket / network fault looks like at the first rclone call.
    """
    body = _restore_text()
    fns = body[body.index("list_runs() {") : body.index("\nverify_checksums()")]
    script = (
        "set -uo pipefail\n"
        'log() { shift; echo "$*"; }\n'
        'RCLONE_REMOTE=":local"\n'
        f'B2_BUCKET="{root}"\n' + fns + "\nresolve_latest\n"
    )
    r = subprocess.run(  # noqa: S603
        ["bash", "-c", script],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    return r.returncode, r.stdout + r.stderr


def test_resolve_latest_does_not_report_a_failed_listing_as_no_backups() -> None:
    """FIXING A GUARD DOES NOT HELP IF THE CALL THAT FEEDS IT FAILS OPEN.

    `resolve_latest` listed the categories with `rclone lsf … 2>/dev/null || true`. A bad key,
    a wrong bucket, or a network fault made `dirs` EMPTY — so the loop body never ran,
    `backup_is_complete` was **never called**, and control fell straight through to
    *"No COMPLETE backup found in B2 bucket."*

    So on the single most likely instrument failure — and the one most likely to bite
    mid-incident — the three-outcome guard could not fire **at all**, because a bad credential
    dies at the FIRST rclone call, upstream of it. An empty listing and a failed listing are
    the same empty string, and only one of them is a measurement.
    """
    rc, out = _resolve_latest_rc("/nonexistent/bucket/path")

    assert rc != 0, "a failed listing must not resolve successfully"
    assert "No COMPLETE backup found" not in out, (
        "a failed LISTING must never be reported as 'no complete backup' — that tells an "
        f"operator mid-incident that their backups are gone when the instrument failed: {out}"
    )
    assert "INSTRUMENT failure" in out, f"it must say it could not look: {out}"


def test_resolve_latest_still_resolves_when_the_listing_works(tmp_path: Path) -> None:
    """The POSITIVE control. A guard that refuses everything is not a guard."""
    for date, complete in (("2026-07-10", True), ("2026-07-11", False)):
        d = tmp_path / "daily" / date
        d.mkdir(parents=True)
        (d / f"isnad-pg-{TIMESTAMP}.dump").write_bytes(b"P" * 4096)
        (d / "_backup_manifest.txt").write_text(
            f"BACKUP_MANIFEST complete={'true' if complete else 'false'} "
            f"stores=postgres,user-postgres,neo4j timestamp={TIMESTAMP} category=daily\n"
        )
    (tmp_path / "weekly").mkdir()

    rc, out = _resolve_latest_rc(str(tmp_path))
    assert rc == 0, f"the good control must resolve: {out}"
    assert "daily/2026-07-10" in out


def test_list_backups_does_not_report_a_failed_listing_as_none() -> None:
    """The SAME lie, by a third route — and nobody had named this one.

    `list_backups` used `|| echo "  (none)"`. With a bad key, `restore.sh --list` printed

        === Daily ===
          (none)

    and an operator reads that as "I have no backups." The instrument failed; the bucket may
    be full. Found by walking every rclone call in the file rather than only the one that was
    reported — the pattern, not the incident.
    """
    body = _restore_text()
    fns = body[body.index("list_category() {") : body.index("\nbackup_is_complete()")]
    script = (
        "set -uo pipefail\n"
        'log() { shift; echo "$*"; }\n'
        'RCLONE_REMOTE=":local"\n'
        'B2_BUCKET="/nonexistent/bucket/path"\n' + fns + "\nlist_backups\n"
    )
    r = subprocess.run(  # noqa: S603
        ["bash", "-c", script],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    out = r.stdout + r.stderr

    assert r.returncode != 0, "--list over an unreadable bucket must not exit 0"
    assert "(none)" not in out, (
        f"a failed listing must never be rendered as '(none)' — that is 'you have no "
        f"backups', asserted from an instrument failure: {out}"
    )
    assert "INSTRUMENT failure" in out, f"it must say it could not look: {out}"


# --------------------------------------------------------------------------
# The two streams exist because one is DATA and one is COMMENTARY
# --------------------------------------------------------------------------


def _rclone_notice_env() -> dict[str, str]:
    """Force rclone's config NOTICE onto stderr, exactly as production emits it.

    ``restore.sh`` configures rclone through ``RCLONE_CONFIG_ISNAD_*`` and ships no
    ``rclone.conf``, so on the VPS rclone writes

        NOTICE: Config file ".../rclone.conf" not found - using defaults

    to stderr on every **successful** call. Pointing ``RCLONE_CONFIG`` at a path that does not
    exist reproduces that condition deterministically here, rather than depending on whether
    this machine happens to have a config file.
    """
    return {**os.environ, "RCLONE_CONFIG": "/nonexistent/rclone.conf"}


def test_resolve_latest_does_not_read_rclone_stderr_as_a_backup_directory(tmp_path: Path) -> None:
    """`2>&1` does not CAPTURE the diagnostic — it PROMOTES it to data.

    And note the direction of this regression against the bug it replaced: the original
    swallow (`2>/dev/null || true`) fired only on FAILURE. This one fires on **SUCCESS**,
    because a tool writing to stderr when nothing is wrong is completely ordinary. It corrupts
    the NORMAL path — the one nobody tests as hard.

    Against a HEALTHY bucket holding one good complete backup, folding stderr into the listing
    made `resolve_latest` report two INCOMPLETE backups that do not exist:

        [WARNING] Skipped 2 INCOMPLETE backup(s) when resolving 'latest':
        [WARNING]     incomplete: daily/2026/07/11 05:13:53 NOTICE: Config file … not found

    and it corrupts the warning block added precisely so an operator is not left guessing —
    telling them a lie instead of nothing.
    """
    d = tmp_path / "daily" / "2026-07-11"
    d.mkdir(parents=True)
    (d / f"isnad-pg-{TIMESTAMP}.dump").write_bytes(b"P" * 4096)
    (d / "_backup_manifest.txt").write_bytes(build_manifest(complete=True, run_ts=TIMESTAMP))
    (tmp_path / "weekly").mkdir()

    body = _restore_text()
    fns = body[body.index("list_runs() {") : body.index("\nverify_checksums()")]
    script = (
        "set -uo pipefail\n"
        'log() { shift; echo "$*"; }\n'
        'RCLONE_REMOTE=":local"\n'
        f'B2_BUCKET="{tmp_path}"\n' + fns + "\nresolve_latest\n"
    )
    r = subprocess.run(  # noqa: S603
        ["bash", "-c", script],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
        env=_rclone_notice_env(),
    )
    out = r.stdout + r.stderr

    assert r.returncode == 0, f"the HEALTHY bucket must resolve: {out}"
    assert "NOTICE" not in out, (
        "rclone's stderr was folded into the listing and is being reported as backup "
        f"directories. The diagnostic is COMMENTARY, not DATA:\n{out}"
    )
    assert "INCOMPLETE" not in out.upper() or "Skipped 0" in out, (
        f"a healthy bucket with one good backup must not invent incomplete ones:\n{out}"
    )
    assert r.stdout.strip().splitlines()[-1] == "daily/2026-07-11"


def test_list_backups_does_not_print_rclone_stderr_as_a_backup_name(tmp_path: Path) -> None:
    """`--list` printed a LOG LINE to the operator as a backup name."""
    (tmp_path / "daily" / "2026-07-11").mkdir(parents=True)
    (tmp_path / "weekly").mkdir()

    body = _restore_text()
    fns = body[body.index("list_category() {") : body.index("\nbackup_is_complete()")]
    script = (
        "set -uo pipefail\n"
        'log() { shift; echo "$*"; }\n'
        'RCLONE_REMOTE=":local"\n'
        f'B2_BUCKET="{tmp_path}"\n' + fns + "\nlist_backups\n"
    )
    r = subprocess.run(  # noqa: S603
        ["bash", "-c", script],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
        env=_rclone_notice_env(),
    )
    out = r.stdout + r.stderr

    assert r.returncode == 0, f"a healthy bucket must list cleanly: {out}"
    assert "NOTICE" not in out, f"rclone's stderr is being printed as a backup name:\n{out}"
    assert "2026-07-11/" in r.stdout, f"the real backup must still be listed:\n{out}"


# --------------------------------------------------------------------------
# deploy#589 — the paraphrase in the PRODUCT
# --------------------------------------------------------------------------


def _list_runs(directory: Path) -> list[str]:
    """Execute restore.sh's SHIPPED `list_runs` against a real directory."""
    body = _restore_text()
    fn = body[body.index("list_runs() {") : body.index("\ncount_runs()")]
    r = subprocess.run(  # noqa: S603
        ["bash", "-c", fn + '\nlist_runs "$1"\n', "_", str(directory)],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    )
    return r.stdout.split()


def test_list_runs_sees_every_dump_the_producer_actually_writes(tmp_path: Path) -> None:
    """The run-counter is bound to `backup.sh`'s REAL filenames, not to names I typed.

    `count_runs` feeds the refuse-on-ambiguity gate, so a dump this parser cannot see is a run
    that does not exist as far as the gate is concerned.
    """
    names = _backup_dump_filenames()  # parsed out of backup.sh
    assert names, "no *_DUMP_FILE names parsed from backup.sh"
    for name in names.values():
        # backup.sh compresses the Neo4j dump after writing it.
        for fname in (name, f"{name}.zst"):
            (tmp_path / fname).write_bytes(b"P" * 4096)
        (tmp_path / f"{name}.sha256").write_bytes(b"x")  # sidecar — must be ignored

    assert _list_runs(tmp_path) == [TIMESTAMP], (
        f"every dump backup.sh writes belongs to run {TIMESTAMP}; list_runs saw "
        f"{_list_runs(tmp_path)}. A dump this parser cannot see is a run the "
        "refuse-on-ambiguity gate cannot count."
    )


def test_a_store_rename_cannot_make_a_run_invisible(tmp_path: Path) -> None:
    """deploy#589. The store segment was `[a-z0-9]+` — which CANNOT CONTAIN A HYPHEN.

    Rename `userpg` -> `user-pg` in the producer and every dump of that store becomes invisible
    to `list_runs`. That does not BREAK the parser, it QUIETLY NARROWS it — and what it stops
    seeing is exactly what the guard downstream needs to count:

        run rendered invisible -> count_runs falls to 1 -> the refuse-on-ambiguity gate
        stops refusing -> THE TORN RESTORE COMES BACK -> and nothing goes red.

    **A paraphrase in the product is worse than one in a test, because the test paraphrases
    too, so it stays green.** So the parser must anchor on the RUN ID — whose format is strict
    and self-delimiting — and must not care what the store is called.
    """
    # Two runs. The 08:00 leftover is written under a HYPHENATED store name.
    for name in ("isnad-pg", "isnad-userpg", "isnad-neo4j"):
        (tmp_path / f"{name}-20260711-030100.dump").write_bytes(b"P" * 4096)
    (tmp_path / "isnad-user-pg-20260711-080000.dump").write_bytes(b"S" * 4096)

    runs = _list_runs(tmp_path)
    assert runs == ["20260711-030100", "20260711-080000"], (
        f"a hyphenated store name made an entire run INVISIBLE: saw {runs}. count_runs would "
        "fall to 1, the refuse-on-ambiguity gate would stand down, and the torn restore "
        "would come back with nothing red."
    )


def test_list_runs_does_not_constrain_the_store_charset() -> None:
    """Pin the SHAPE, not the instance — the store segment must not be enumerated.

    A charset for the store name is a paraphrase of the producer's naming, and it is the
    paraphrase that silently narrows. The run id is the only thing worth parsing.
    """
    fn = _code_only(_restore_text())
    fn = fn[fn.index("list_runs() {") : fn.index("count_runs()")]
    assert "[a-z0-9]" not in fn, (
        "list_runs is matching the store name against a character class again. A producer-side "
        "rename would not break it — it would silently narrow it, and the refuse-on-ambiguity "
        "gate downstream would stop refusing. Anchor on the run id instead (deploy#589)."
    )
    assert "[0-9]\\{8\\}-[0-9]\\{6\\}" in fn, (
        "list_runs must anchor on the run id's strict %Y%m%d-%H%M%S shape"
    )

"""stg and prod share one B2 bucket — the remote path MUST carry the environment (deploy#632).

`BACKUP_B2_BUCKET` is a single repo-level secret. Before this guard, `backup.sh` and
`restore.sh` built every remote path as `${B2_BUCKET}/<category>/<date>`, with no
environment anywhere in it. Nothing collided only because prod had never taken a backup.

The fatal one is retention. `prune_old_backups` listed `${B2_BUCKET}/${category}/` and
`rclone purge`d every date dir past the cutoff — unscoped. **Prod's nightly purge would
have deleted stg's backups, and stg's would have deleted prod's.** A backup system whose
retention job destroys the other environment's backups is worse than none, because it
reports success while doing it.

WHAT A GREEN RUN MEANS — AND WHAT IT DOES NOT
---------------------------------------------
Green here means: *no remote path in the two scripts is built without the environment
prefix, and both scripts refuse to run without one.* It does NOT mean "backups are
isolated" — that is a runtime property of the bucket, and the thing that proves it is a
prod backup landing under `prod/` while stg's objects under `stg/` remain untouched. This
is a source-text guard standing in for a runtime property, which is exactly the kind of
proxy that let deploy#613 ship twice. Say what it checks; do not oversell it.

The offender-detector is calibrated below against BOTH classes — including the literal
pre-fix lines, which it must flag — because a filter nobody has watched go red is not
evidence (deploy#574, #624).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKUP_SH = REPO_ROOT / "scripts" / "backup.sh"
RESTORE_SH = REPO_ROOT / "scripts" / "restore.sh"
DEPLOY_STG = REPO_ROOT / ".github" / "workflows" / "deploy-stg.yml"
DEPLOY_PROD = REPO_ROOT / ".github" / "workflows" / "deploy-prod.yml"
VERIFY_ARTIFACT = REPO_ROOT / ".github" / "workflows" / "verify-backup-artifact.yml"

# A remote path is any rclone argument reaching into the bucket. It is SAFE only if the
# environment prefix appears between the bucket and the rest of the path — either spelled
# literally (`${BACKUP_PREFIX}/`) or carried by a variable that already includes it
# (`REMOTE_ROOT`, `REMOTE_SUBDIR`).
#
# MATCH BOTH SPELLINGS OF THE EXPANSION — `${B2_BUCKET}/` AND BARE `$B2_BUCKET/`.
# (#633 review, Lucas Ferreira.) The braced-only form missed the bare `$` spelling, which
# is one character shorter, semantically identical, and idiomatic in these very files
# (`2>>"$LOG_FILE"`, `"$LOCAL_BACKUP_PATH"`). So this line was invisible and called SAFE:
#
#     rclone purge "${RCLONE_REMOTE}:$B2_BUCKET/${category}/${dir}/" 2>>"$LOG_FILE"
#
# — deploy#632 restored verbatim, past a green suite. A line-scan gate must match EVERY
# syntactic form, not the one the author happened to write (`feedback_lint_gate_cover_all_
# syntactic_forms`); it is the same blind-spot class as #624's `-p` hiding inside the
# template name `compose-project-XXXXXX`.
_BUCKET_PATH = re.compile(r"\$\{?B2_BUCKET\}?/(?P<rest>\S*)")
_SAFE_REST = re.compile(r"^\$\{(?:BACKUP_PREFIX|REMOTE_SUBDIR)\}")


def _unprefixed_bucket_paths(text: str) -> list[str]:
    """Lines that reach into the bucket WITHOUT naming the environment."""
    offenders = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue  # a comment is documentation, not a path
        for m in _BUCKET_PATH.finditer(line):
            rest = m.group("rest")
            if not _SAFE_REST.match(rest):
                offenders.append(f"{lineno}: {stripped}")
    return offenders


# ---------------------------------------------------------------------------
# Calibrate the detector BEFORE reading it (deploy#574, #624).
# ---------------------------------------------------------------------------
# The must-flag fixtures are the REAL pre-fix lines, copied verbatim out of git history —
# not names invented here. Invented fixtures inherit the author's assumptions and share the
# guard's blind spot by construction; that is precisely how the #624 filter passed its own
# calibration while letting deploy#613 through.

# `noqa: E501` throughout: these fixtures are production lines copied VERBATIM. Reflowing
# them to satisfy a width limit would make them no longer the thing they are testing —
# which is the entire point of deriving fixtures from real artifacts rather than inventing
# them (deploy#624).
PRE_FIX_LINES_MUST_FLAG = [
    # backup.sh — the destructive one. This is the purge that deletes the other env.
    '    dirs=$(rclone lsf "${RCLONE_REMOTE}:${B2_BUCKET}/${category}/" --dirs-only 2>/dev/null || true)',  # noqa: E501
    '                    rclone purge "${RCLONE_REMOTE}:${B2_BUCKET}/${category}/${dir}/" 2>>"$LOG_FILE" || \\',  # noqa: E501
    # backup.sh — the upload.
    '    if rclone copy "$LOCAL_BACKUP_PATH" "${RCLONE_REMOTE}:${B2_BUCKET}/${BACKUP_SUBDIR}/" \\',
    # restore.sh — resolve_latest listing, and the download.
    '        dirs="$(rclone lsf "${RCLONE_REMOTE}:${B2_BUCKET}/${category}/" --dirs-only 2>"$lerr")" || lrc=$?',  # noqa: E501
    '    if ! rclone copy "${RCLONE_REMOTE}:${B2_BUCKET}/${BACKUP_PATH}/" "$RESTORE_DIR/" --log-level INFO; then',  # noqa: E501
    # THE BARE-`$` SPELLING. Not a hypothetical: the braced-only detector called this SAFE,
    # and it is deploy#632 restored verbatim — the unscoped purge that deletes the other
    # environment's backups (#633 review, Lucas Ferreira).
    '                    rclone purge "${RCLONE_REMOTE}:$B2_BUCKET/${category}/${dir}/" 2>>"$LOG_FILE" || \\',  # noqa: E501
]

POST_FIX_LINES_MUST_NOT_FLAG = [
    '    dirs=$(rclone lsf "${RCLONE_REMOTE}:${B2_BUCKET}/${BACKUP_PREFIX}/${category}/" --dirs-only 2>/dev/null || true)',  # noqa: E501
    '                    rclone purge "${RCLONE_REMOTE}:${B2_BUCKET}/${BACKUP_PREFIX}/${category}/${dir}/" 2>>"$LOG_FILE" || \\',  # noqa: E501
    '    if rclone copy "$LOCAL_BACKUP_PATH" "${RCLONE_REMOTE}:${B2_BUCKET}/${REMOTE_SUBDIR}/" \\',
    # restore.sh routes everything through REMOTE_ROOT, which already carries the prefix,
    # so the bucket name does not appear in the path at all.
    '    if ! rclone copy "${REMOTE_ROOT}/${BACKUP_PATH}/" "$RESTORE_DIR/" --log-level INFO; then',
    # The REMOTE_ROOT definition itself names the bucket AND the prefix — must not flag.
    '    REMOTE_ROOT="${RCLONE_REMOTE}:${B2_BUCKET}/${BACKUP_PREFIX}"',
]


def test_the_detector_separates_prefixed_from_unprefixed() -> None:
    """The instrument must go RED on the real pre-fix lines and GREEN on the fixed ones.

    Run it over BOTH classes and require SEPARATION. A detector that has only ever been
    watched agreeing with the code it was written beside has demonstrated nothing.
    """
    missed = [ln for ln in PRE_FIX_LINES_MUST_FLAG if not _unprefixed_bucket_paths(ln)]
    assert not missed, (
        "The detector FAILED TO FLAG real pre-fix lines — it is inert, and every "
        "assertion below it is worthless:\n  " + "\n  ".join(missed)
    )

    false_alarms = [ln for ln in POST_FIX_LINES_MUST_NOT_FLAG if _unprefixed_bucket_paths(ln)]
    assert not false_alarms, (
        "The detector flagged CORRECT prefixed lines — it would block the fix:\n  "
        + "\n  ".join(false_alarms)
    )


# ---------------------------------------------------------------------------
# The property.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("script", [BACKUP_SH, RESTORE_SH], ids=lambda p: p.name)
def test_every_remote_path_names_its_environment(script: Path) -> None:
    offenders = _unprefixed_bucket_paths(script.read_text())
    assert not offenders, (
        f"{script.name} builds a B2 path that does not name its environment. stg and prod "
        f"share one bucket, so an unprefixed path reaches into the OTHER environment's "
        f"backups — and in prune_old_backups it DELETES them (deploy#632):\n  "
        + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("script", [BACKUP_SH, RESTORE_SH], ids=lambda p: p.name)
def test_script_refuses_to_run_without_a_prefix(script: Path) -> None:
    """Fail CLOSED. No default is tolerable: `""` silently restores the shared prefix and
    `prod` would be catastrophic on stg. An un-deployed host must fail loudly."""
    body = script.read_text()
    assert re.search(r':\s*"\$\{BACKUP_PREFIX:\?', body), (
        f"{script.name} does not hard-require BACKUP_PREFIX. A default reintroduces the "
        f"collision silently — which is the whole failure mode."
    )
    assert not re.search(r"BACKUP_PREFIX:-", body), (
        f"{script.name} supplies a DEFAULT for BACKUP_PREFIX. There is no safe default."
    )


_REFUSAL = "not a bare name"


def _run_with_prefix(script: Path, prefix: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script)],
        env={
            "PATH": "/usr/bin:/bin",
            "B2_KEY_ID": "x",
            "B2_APP_KEY": "x",
            "B2_BUCKET": "x",
            "BACKUP_PREFIX": prefix,
        },
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.parametrize("script", [BACKUP_SH, RESTORE_SH], ids=lambda p: p.name)
def test_a_prefix_that_escapes_its_namespace_is_refused_AS_SUCH(script: Path) -> None:
    """A prefix carrying `/` or `..` climbs out of its own namespace into the other's.

    ATTRIBUTE THE REFUSAL. The first cut asserted only `returncode != 0` and claimed that
    "asserts the refusal, not the wording". It asserted neither — it was a TAUTOLOGY. These
    scripts exit non-zero in this sandbox no matter what, for reasons having nothing to do
    with the prefix, and there is no environment where they would not: `backup.sh` exits 0
    only on a working VPS with docker, rclone and live B2, which is never where a test runs.

    Lucas Ferreira proved it by DELETING the entire hostile-prefix guard from a scratch copy
    so that `BACKUP_PREFIX=../prod` was ACCEPTED — and the suite still reported `9 passed`.
    Side by side, same hostile prefix:

        guard present:  FATAL: BACKUP_PREFIX='../prod' is not a bare name ...   rc=1
        guard DELETED:  mkdir: cannot create '/var/lib/noorinalabs-backups' ... rc=1

    `returncode != 0` cannot separate those. So bind to the message the script itself owns,
    and pair it with a positive control — without the control, "the refusal message is
    absent" would be satisfied by any ambient failure and would prove nothing either.

    This is `restore_rehearsal.sh`'s own standard (it asserts non-zero AND that the failure
    was for the expected reason), and `restore.sh` is parametrized in because its identical
    guard had NO test executing it at all — on the restore side, an escaping prefix is how
    you load the stg graph into prod.
    """
    for hostile in ("../prod", "stg/../prod", "/prod", "PROD", "-prod", ""):
        proc = _run_with_prefix(script, hostile)
        assert proc.returncode != 0, (
            f"{script.name} ACCEPTED BACKUP_PREFIX={hostile!r} — that prefix escapes its "
            f"namespace and can reach the other environment's backups."
        )
        # An empty prefix dies at the `:?` guard, which has its own wording; every other
        # hostile value must be refused BY THE CHARSET CHECK, by name.
        expected = "must be set" if hostile == "" else _REFUSAL
        combined = proc.stderr + proc.stdout
        assert expected in combined, (
            f"{script.name} exited {proc.returncode} on BACKUP_PREFIX={hostile!r}, but NOT "
            f"because it refused the prefix — so this assertion is being satisfied by an "
            f"unrelated ambient failure and proves nothing. Output: {combined[-300:]!r}"
        )


@pytest.mark.parametrize("script", [BACKUP_SH, RESTORE_SH], ids=lambda p: p.name)
def test_a_legal_prefix_is_not_refused(script: Path) -> None:
    """The positive control for the test above.

    Without it, "the refusal message appeared" could be true of EVERY input — including the
    legal ones — and the guard would be rejecting valid deploys while the suite stayed green.
    A one-way oracle proves nothing (deploy#574).

    The script will still fail here (no docker, no real B2) — that is fine and expected. What
    must NOT appear is the charset refusal.
    """
    proc = _run_with_prefix(script, "stg")
    combined = proc.stderr + proc.stdout
    assert _REFUSAL not in combined, (
        f"{script.name} REFUSED the legal prefix 'stg' as malformed. The charset guard is "
        f"rejecting valid input, so the refusal assertions above are vacuous. "
        f"Output: {combined[-300:]!r}"
    )


def test_remote_root_is_actually_built_from_remote_bucket_and_prefix() -> None:
    """REMOTE_ROOT must resolve to `<remote>:<bucket>/<prefix>` — and nothing else.

    This test exists because the guard above went GREEN on a genuinely broken restore.sh.
    Writing this change, a bulk rewrite of `"${RCLONE_REMOTE}:${B2_BUCKET}/` -> `"${REMOTE_ROOT}/`
    also rewrote REMOTE_ROOT'S OWN DEFINITION, leaving:

        REMOTE_ROOT="${REMOTE_ROOT}/${BACKUP_PREFIX}"     # -> "/stg"

    Every "remote" path then pointed at the LOCAL FILESYSTEM. `test_every_remote_path_names_
    its_environment` still passed — the file contained no unprefixed `${B2_BUCKET}/` path,
    because it contained no bucket path at all. A source-text scan cannot see that the string
    it is scanning has been emptied of meaning; only shellcheck noticed, via SC2034
    ("RCLONE_REMOTE appears unused"), which is a different instrument asking a different
    question.

    The proxy is not the property. Pin the property.
    """
    body = RESTORE_SH.read_text()
    m = re.search(r'^\s*REMOTE_ROOT="([^"]*)"\s*$', body, re.MULTILINE | re.DOTALL)
    defs = re.findall(r'^\s*REMOTE_ROOT="([^"]*)"', body, re.MULTILINE)
    real = [d for d in defs if d != ""]  # the "" initialiser is fine; the real one is not
    assert real, "restore.sh never assigns REMOTE_ROOT a value — every remote path is empty"
    assert len(real) == 1, f"REMOTE_ROOT is assigned more than once: {real}"
    definition = real[0]

    assert "REMOTE_ROOT" not in definition, (
        f'REMOTE_ROOT is defined in terms of ITSELF: REMOTE_ROOT="{definition}". It resolves '
        f"to a local path, so every rclone call silently addresses the local filesystem "
        f"instead of B2."
    )
    for required in ("${RCLONE_REMOTE}", "${B2_BUCKET}", "${BACKUP_PREFIX}"):
        assert required in definition, (
            f'REMOTE_ROOT="{definition}" does not contain {required}. It must resolve to '
            f"<remote>:<bucket>/<prefix> — drop any one and it addresses the wrong place."
        )
    assert m is not None


def test_the_two_environments_get_different_prefixes() -> None:
    """The copy-paste guard.

    Both workflows injecting `BACKUP_PREFIX=stg` would pass every other test in this file
    and restore the collision exactly.
    """
    stg = re.search(r"^\s*BACKUP_PREFIX=(\S+)", DEPLOY_STG.read_text(), re.MULTILINE)
    prod = re.search(r"^\s*BACKUP_PREFIX=(\S+)", DEPLOY_PROD.read_text(), re.MULTILINE)
    assert stg, "deploy-stg.yml does not inject BACKUP_PREFIX — stg backups will fail closed"
    assert prod, "deploy-prod.yml does not inject BACKUP_PREFIX — prod backups will fail closed"
    assert stg.group(1) != prod.group(1), (
        f"stg and prod both deploy BACKUP_PREFIX={stg.group(1)!r} — they would share a "
        f"prefix and purge each other, which is the entire bug."
    )


def test_the_b2_artifact_watchdog_scans_only_its_own_environment() -> None:
    """The false-green that was about to arm itself.

    verify-backup-artifact.yml runs a `[stg, prod]` matrix but scanned the bucket ROOT, so
    both legs read the SAME objects. With stg's first-ever backup now in the bucket, the
    PROD leg would report `fresh` on the strength of STAGING'S artifact — on the one check
    that exists because every other backup signal can lie.
    """
    body = VERIFY_ARTIFACT.read_text()
    assert 'B2_ROOT="isnad:${B2_BUCKET}"' not in body, (
        "verify-backup-artifact.yml scans the bucket ROOT. Both matrix legs then read the "
        "same objects and the prod leg reports on staging's backup (deploy#632)."
    )
    assert re.search(r'B2_ROOT="isnad:\$\{B2_BUCKET\}/\$\{\{ matrix\.env \}\}"', body), (
        "verify-backup-artifact.yml must scope its scan to ${{ matrix.env }} — the same "
        "name backup.sh writes its objects under."
    )

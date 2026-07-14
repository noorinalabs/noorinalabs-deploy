"""Retention must know the difference between "nothing to prune" and "I could not look",
and the backup prefix must be bound to the host it is actually running on.

deploy#635 — `prune_old_backups` read its instrument with `2>/dev/null || true`:

    dirs=$(rclone lsf "…/${BACKUP_PREFIX}/${category}/" --dirs-only 2>/dev/null || true)

`2>/dev/null` discards the diagnostic; `|| true` discards the return code. A bad credential,
a network fault, a wrong bucket and a wrong prefix therefore ALL COLLAPSED INTO THE SAME
VALUE as the legitimate "this environment has no old backups": the empty string. The `while`
loop iterated nothing, retention silently no-op'd, and the run went on to log
"=== Backup finished ===" and emit the success gauge. B2 grows without bound and NOTHING
ANYWHERE SAYS SO.

deploy#636 — the charset guard (`^[a-z0-9][a-z0-9-]*$`) refuses `../prod`, but it cannot, by
construction, refuse the accepted value that is catastrophic:

    BACKUP_PREFIX=prod          ...on the STAGING host

Staging then writes into `prod/` and its retention purge — correctly scoped to "its own"
prefix — DELETES PRODUCTION'S BACKUPS. The value is not malformed; it is wrong only RELATIVE
TO THE HOST, and no regex over the value alone can see that.

WHY THESE TESTS RUN THE SCRIPT INSTEAD OF GREPPING IT
----------------------------------------------------
Both bugs are about what the script DOES when an instrument fails, and a source-text scan
cannot see that: the pre-fix line and the fixed line differ by a redirect, and it is the
*runtime consequence* of that redirect — an empty string that reads as a measurement — that
is the defect. So every assertion below drives the REAL `scripts/backup.sh` through the real
`compose_project.sh` and the real `b2_preflight.sh`, under the real `set -euo pipefail`, with
stubbed `docker`/`rclone`/`zstd` on PATH. A harness running prod code under a weaker `set -…`
is not running prod code (`feedback_errexit_kills_assignment_guard`).

WHAT A GREEN RUN HERE DOES NOT MEAN
-----------------------------------
It does not mean the retention path is safe under the unit's hardening. These tests run where
`/tmp` is WRITABLE; under `isnad-backup.service` it is not (`ProtectSystem=strict`, `PrivateTmp`
deliberately unset — deploy#121 Bug A), which is why the stderr capture added here goes through
`scratch_file()` and never a bare `mktemp`. That property is pinned by
`test_scratch_under_hardening.py`, which is structurally capable of failing on it; this module
is not. Saying so in the narrow words, because the wide words are how deploy#613 shipped twice.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKUP_SH = REPO_ROOT / "scripts" / "backup.sh"
DEPLOY_STG = REPO_ROOT / ".github" / "workflows" / "deploy-stg.yml"
DEPLOY_PROD = REPO_ROOT / ".github" / "workflows" / "deploy-prod.yml"

# The REAL bucket, matching the fixtures in test_b2_preflight.py and
# test_compose_project_scoping.py. Not `isnad-graph-backups` — that is a legacy, EMPTY bucket
# that nothing writes to, and naming it in a fixture would quietly teach the next reader the
# same wrong thing the runbook did (deploy#636).
BUCKET = "noorinalabs-backups"

# The words rclone's stub writes to STDERR when the listing fails. The point of the fix is
# that THESE WORDS REACH THE OPERATOR — the pre-fix `2>/dev/null` ate them. An assertion on a
# generic "error" string would pass against a script that printed its own guess instead of
# rclone's diagnosis, which is the deploy#613 failure mode exactly.
RCLONE_LSF_DIAGNOSTIC = "CRITICAL: you must use bucket(s) [...] with this application key"

# A date far enough back that both the daily (7d) and weekly (28d) cutoffs select it.
ANCIENT_DIR = "2020-01-01"


def _write(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


def _stage_mutant(tmp_path: Path, mutant_body: str) -> Path:
    """Write a mutated `backup.sh` into a temp `scripts/` dir that ALSO holds every library it
    could source, and return the mutant's path.

    `backup.sh` sources its siblings by `$(dirname "${BASH_SOURCE[0]}")/<lib>.sh`, and each is
    fail-closed if absent — so a mutant dropped in a bare dir dies on line 1, before it reaches
    the code under test, and any "the diagnostic is absent" assertion then passes VACUOUSLY on
    the silence of a script that never ran. That already bit this module once (the compose_project
    lib), and deploy#661 made it bite again: `backup.sh` now sources `scratch.sh` transitively
    through `compose_project.sh`, and a hand-maintained `(compose_project.sh, b2_preflight.sh)`
    list did not know to stage it. Each PR was green in isolation; the merged tree broke.

    So DERIVE THE SET, don't hand-type it (the lesson that has cost this wave three times): copy
    EVERY sibling `*.sh` from the real scripts dir, then overwrite `backup.sh` with the mutant.
    A new sourced dependency lands in the fixture automatically, and the only cost is copying a
    few unused shell files — cheap, and the correct direction to be wrong in.
    """
    mutant_dir = tmp_path / "scripts"
    mutant_dir.mkdir(parents=True)
    real_scripts = REPO_ROOT / "scripts"
    for sh in real_scripts.glob("*.sh"):
        (mutant_dir / sh.name).write_bytes(sh.read_bytes())
    mutant = mutant_dir / "backup.sh"
    mutant.write_text(mutant_body)
    return mutant


def _stubs(tmp_path: Path, *, lsf_mode: str, purge_mode: str = "ok") -> Path:
    """A stub bin/ that carries the real backup.sh all the way to the retention pass.

    `docker` is STATEFUL — backup.sh stops neo4j and then polls `ps` until it is no longer
    running, so a stub that reported `neo4j:running` forever would hang the stop-wait loop for
    30s and then take a DIFFERENT code path (the stop-timeout branch) than the one under test.
    """
    stub = tmp_path / "stub"
    stub.mkdir(parents=True)
    state = tmp_path / "neo4j_state"
    state.write_text("running")

    _write(
        stub / "docker",
        f"""#!/usr/bin/env bash
STATE="{state}"
args="$*"

# `docker run … neo4j-admin database dump …` — write the dump into the host path bind-mounted
# at /backups, exactly where the real neo4j-admin puts it.
if [[ "$1" == "run" ]]; then
    backups=""
    prev=""
    for a in "$@"; do
        if [[ "$prev" == "-v" && "$a" == *":/backups" ]]; then
            backups="${{a%%:/backups}}"
        fi
        prev="$a"
    done
    [[ -n "$backups" ]] && printf 'neo4j-dump-bytes\\n' > "${{backups}}/neo4j.dump"
    exit 0
fi

if [[ "$1" == "inspect" ]]; then
    printf 'noorinalabs_neo4j_data\\n'
    exit 0
fi

# Everything below is `docker compose -p … -f … <verb> …`.
case "$args" in
    *" stop neo4j"*)  printf 'stopped'  > "$STATE"; exit 0 ;;
    *" start neo4j"*) printf 'running'  > "$STATE"; exit 0 ;;
esac

if [[ "$args" == *"ps -aq neo4j"* ]]; then
    printf 'deadbeefcafe\\n'
    exit 0
fi

if [[ "$args" == *"{{{{.Service}}}}:{{{{.Health}}}}"* ]]; then
    printf 'neo4j:healthy\\n'
    exit 0
fi

if [[ "$args" == *"{{{{.Service}}}}:{{{{.State}}}}"* ]]; then
    printf 'postgres:running\\nuser-postgres:running\\n'
    [[ "$(cat "$STATE")" == "running" ]] && printf 'neo4j:running\\n'
    exit 0
fi

# `exec -T <svc> pg_dump …` — a non-empty dump on stdout. backup.sh rejects a zero-byte one.
if [[ "$args" == *"pg_dump"* ]]; then
    printf 'PGDMP-fake-custom-format-dump\\n'
    exit 0
fi

exit 0
""",
    )

    # `lsf` is the instrument under test. Every OTHER rclone verb succeeds, so a failure in the
    # run can only have come from the one we aimed at — a probe that fails everywhere proves
    # nothing about the path it was supposed to isolate.
    #
    # EVERY HEALTHY MODE EMITS TODAY'S DIRECTORY, because a healthy listing in production
    # NECESSARILY CONTAINS IT: the run uploaded into this prefix a few lines earlier and rclone
    # said it succeeded. A fixture that omitted it would be modelling a state the bucket cannot
    # be in, and the "nothing to prune" assertion built on it would be testing fiction
    # (`feedback_fixture_makes_guard_assertion_inert`). The stub computes the date the same way
    # backup.sh does — `date -u +%Y-%m-%d` — rather than hardcoding it, so this does not rot at
    # midnight UTC or care which weekday CI runs on (the category flips to `weekly` on Sundays).
    lsf_branch = {
        # The listing sees today's upload, and nothing is old enough to prune. THE HEALTHY
        # STEADY STATE of a young environment.
        "ok_nothing_to_prune": "printf '%s/\\n' \"$(date -u +%Y-%m-%d)\"; exit 0",
        # Today's upload plus one ancient directory: retention has work, and does it.
        "ok_old": f"printf '%s/\\n{ANCIENT_DIR}/\\n' \"$(date -u +%Y-%m-%d)\"; exit 0",
        # rc != 0: the instrument failed and SAID SO. Pre-fix, indistinguishable from empty.
        "fail": f"echo '{RCLONE_LSF_DIAGNOSTIC}' >&2; exit 1",
        # rc=0 AND EMPTY — the deploy#634/#638 case. rclone returns exactly this for a listing
        # the key is NOT PERMITTED to see: not a 403, a successful listing of nothing. No rc
        # check can catch it. Only the "where is the directory we just uploaded?" control can.
        "blind_empty": "exit 0",
    }[lsf_mode]

    purge_branch = {
        "ok": "exit 0",
        "fail": "echo 'CRITICAL: delete not permitted for this key' >&2; exit 1",
    }[purge_mode]

    _write(
        stub / "rclone",
        f"""#!/usr/bin/env bash
case "$1" in
    lsd)
        # awk '{{print $NF}}' must yield the bucket name — b2_preflight's visibility probe.
        printf '          -1 2020-01-01 00:00:00        -1 {BUCKET}\\n'
        exit 0
        ;;
    copyto|deletefile|copy) exit 0 ;;
    lsf)   {lsf_branch} ;;
    purge) {purge_branch} ;;
esac
exit 0
""",
    )

    _write(
        stub / "zstd",
        """#!/usr/bin/env bash
# `zstd -3 --rm SRC -o DST`
src=""; dst=""; prev=""
for a in "$@"; do
    [[ "$prev" == "-o" ]] && dst="$a"
    [[ "$a" != -* && "$prev" != "-o" ]] && src="$a"
    prev="$a"
done
[[ -n "$dst" ]] && printf 'zstd-fake\\n' > "$dst"
[[ -n "$src" ]] && rm -f "$src"
exit 0
""",
    )
    return stub


def _run_backup(
    tmp_path: Path,
    *,
    lsf_mode: str = "ok_old",
    purge_mode: str = "ok",
    prefix: str = "stg",
    base_domain: str | None = None,
    script: Path | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Run the real backup.sh as the systemd unit does. Returns (proc, textfile_dir)."""
    stub = _stubs(tmp_path, lsf_mode=lsf_mode, purge_mode=purge_mode)
    textfile_dir = tmp_path / "textfile_collector"

    env = dict(os.environ)
    env["PATH"] = f"{stub}:/usr/bin:/bin"
    env.update(
        B2_KEY_ID="fake-key-id",
        B2_APP_KEY="fake-app-key",
        B2_BUCKET=BUCKET,
        BACKUP_PREFIX=prefix,
        BACKUP_DIR=str(tmp_path / "backups"),
        COMPOSE_FILE="/dev/null",
        # The gauge must land somewhere observable — this is why backup.sh takes TEXTFILE_DIR
        # from the environment at all (deploy#618). Nothing in production sets it.
        TEXTFILE_DIR=str(textfile_dir),
    )
    env.pop("BASE_DOMAIN", None)
    if base_domain is not None:
        env["BASE_DOMAIN"] = base_domain

    proc = subprocess.run(
        ["bash", str(script or BACKUP_SH)],
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
    )
    return proc, textfile_dir


def _retention_gauge(textfile_dir: Path) -> str | None:
    """The value of `isnad_backup_retention_ok`, or None if the gauge was never written."""
    prom = textfile_dir / "isnad_backup_retention.prom"
    if not prom.exists():
        return None
    for line in prom.read_text().splitlines():
        if line.startswith("isnad_backup_retention_ok "):
            return line.split()[1]
    return None


# ---------------------------------------------------------------------------
# Calibrate the harness BEFORE reading anything with it (deploy#574, #641).
# ---------------------------------------------------------------------------


def test_the_harness_reaches_retention_at_all(tmp_path: Path) -> None:
    """THE POSITIVE CONTROL, and it comes first for a reason.

    Every assertion in this module is of the form "when the listing fails, X happens". If the
    harness never got AS FAR AS the listing — a preflight that refused the stub key, a
    project-identity gate that refused the stub docker, a stop-wait loop that timed out — then
    `X did not happen` would be trivially true of a run that never reached the code under test,
    and every test below would be green and worthless. That is the silent zero
    (`feedback_silent_zero_is_not_a_measurement`).

    So: prove the instrument is pointed at the thing. A healthy run must PRUNE the ancient
    directory, which can only happen if the listing ran, returned it, and the purge fired.
    """
    proc, textfile_dir = _run_backup(tmp_path, lsf_mode="ok_old")
    combined = proc.stdout + proc.stderr

    assert "=== Backup started" in combined, f"backup.sh never started:\n{combined[-3000:]}"
    assert "Running retention pruning" in combined, (
        f"the run never REACHED retention — every retention assertion in this module would be "
        f"vacuous:\n{combined[-3000:]}"
    )
    assert f"Pruning: stg/daily/{ANCIENT_DIR}/" in combined, (
        f"retention did not prune the ancient directory the listing offered it, so the purge "
        f"path is not being exercised:\n{combined[-3000:]}"
    )
    assert proc.returncode == 0, f"the healthy run did not succeed:\n{combined[-3000:]}"
    assert _retention_gauge(textfile_dir) == "1"


# ---------------------------------------------------------------------------
# deploy#635 — an empty listing and a failed listing are not the same thing.
# ---------------------------------------------------------------------------


def test_a_failed_listing_is_reported_as_an_instrument_failure(tmp_path: Path) -> None:
    """rc != 0 is NOT "no old backups". It is "I could not look", and it must say so."""
    proc, _ = _run_backup(tmp_path, lsf_mode="fail")
    combined = proc.stdout + proc.stderr

    assert "INSTRUMENT failure" in combined, (
        f"a failed listing was not reported as an instrument failure — it is still "
        f"indistinguishable from 'nothing to prune':\n{combined[-3000:]}"
    )
    # rclone's OWN WORDS, not our paraphrase of them. `2>/dev/null` ate exactly this.
    assert RCLONE_LSF_DIAGNOSTIC in combined, (
        f"rclone's diagnostic was discarded. The operator is told the listing failed but not "
        f"WHY, which is the half of deploy#635 that `2>/dev/null` caused:\n{combined[-3000:]}"
    )


def test_a_failed_listing_drives_the_gauge_to_zero(tmp_path: Path) -> None:
    """The signal has to leave the box. A log line nobody greps is not a signal."""
    proc, textfile_dir = _run_backup(tmp_path, lsf_mode="fail")
    assert _retention_gauge(textfile_dir) == "0", (
        f"isnad_backup_retention_ok is not 0 after a failed listing, so nothing off-box knows "
        f"retention stopped running:\n{(proc.stdout + proc.stderr)[-3000:]}"
    )


def test_a_failed_listing_does_NOT_take_the_backup_down(tmp_path: Path) -> None:
    """The fix must not overcorrect into a worse bug.

    Retention could not LIST, so it could not PURGE: nothing was deleted and no data is at
    risk. Exiting non-zero on that would convert a storage-cost problem into a TOTAL BACKUP
    OUTAGE — and it would abort before `emit_success_metric`, so a night with a perfectly good,
    uploaded, restorable backup would report as a failed backup and BackupStale would fire.
    Strictly worse than the bug being fixed.

    So the artifact is still honoured: the dumps ran, the upload ran, the run exits 0, and the
    success gauge IS emitted. The bad news rides on the retention gauge instead.
    """
    proc, textfile_dir = _run_backup(tmp_path, lsf_mode="fail")
    combined = proc.stdout + proc.stderr

    assert proc.returncode == 0, (
        f"a retention listing failure took the whole backup down (rc={proc.returncode}). The "
        f"backup itself was fine:\n{combined[-3000:]}"
    )
    assert (textfile_dir / "isnad_backup_success.prom").exists(), (
        "the success gauge was suppressed by a retention failure — a complete, restorable "
        "backup is now invisible and BackupStale will fire falsely (deploy#559/#565)."
    )
    assert _retention_gauge(textfile_dir) == "0"


def test_nothing_to_prune_is_still_a_legitimate_measurement(tmp_path: Path) -> None:
    """THE OTHER HALF OF THE SEPARATION, and the one that keeps the fix honest.

    A listing that shows today's upload and no directory old enough to prune is a MEASUREMENT —
    the healthy steady state of a young environment, and of every environment on most nights.
    If the fix flagged that as a failure too, it would be a detector that says FAILED to
    everything, which proves nothing when it says FAILED to the case we care about, and would
    page someone every single night on a perfectly healthy stg.

    A guard that cannot go green is not a guard (deploy#574).
    """
    proc, textfile_dir = _run_backup(tmp_path, lsf_mode="ok_nothing_to_prune")
    combined = proc.stdout + proc.stderr

    assert proc.returncode == 0, f"a healthy listing failed the run:\n{combined[-3000:]}"
    assert "INSTRUMENT failure" not in combined, (
        f"a HEALTHY listing was misreported as an instrument failure. The detector cannot "
        f"separate the two classes, so its verdict carries no information:\n{combined[-3000:]}"
    )
    assert "Pruning:" not in combined, "it pruned something on a listing with nothing old in it."
    assert _retention_gauge(textfile_dir) == "1"


def test_an_rc0_listing_that_cannot_see_todays_upload_is_REFUSED(tmp_path: Path) -> None:
    """rc=0 AND EMPTY — and the rc check is structurally blind to it. (deploy#634/#638)

    rclone returns rc=0 with an empty listing for a prefix the application key is NOT PERMITTED
    to see. It is not a 403; it is a successful listing of nothing. So an exit-code check — the
    whole of the deploy#635 fix — cannot catch this class even in principle: a scoped-away key
    returns byte-for-byte what "no old backups" returns.

    What catches it is the one thing the listing cannot argue with: THIS RUN JUST UPLOADED into
    that prefix and rclone said it succeeded, so today's directory is necessarily there. A
    listing that cannot show it to us is not describing the bucket, and a `purge` decision must
    never be read off it.

    Without this the fix would close the loud failures and leave the silent one — which is the
    failure that actually shipped.
    """
    proc, textfile_dir = _run_backup(tmp_path, lsf_mode="blind_empty")
    combined = proc.stdout + proc.stderr

    assert "does NOT contain" in combined, (
        f"an rc=0 listing that could not see the directory we had just uploaded was accepted as "
        f"'nothing to prune'. A key scoped away from this prefix now reads as a healthy, empty "
        f"environment:\n{combined[-3000:]}"
    )
    assert "REFUSING to prune" in combined, (
        f"the run did not refuse to prune from a listing it had just caught lying:\n"
        f"{combined[-3000:]}"
    )
    assert "Pruning:" not in combined, "it PURGED on the strength of a listing it could not trust."
    assert _retention_gauge(textfile_dir) == "0"
    # Still not an outage: the artifact is good and the success gauge stands.
    assert proc.returncode == 0
    assert (textfile_dir / "isnad_backup_success.prom").exists()


def test_a_failed_purge_also_drives_the_gauge_to_zero(tmp_path: Path) -> None:
    """A purge that failed is retention that did not happen.

    This used to be `|| log "WARNING" …` — the directory stays in B2, still costing money, and
    every other signal reads clean.
    """
    proc, textfile_dir = _run_backup(tmp_path, lsf_mode="ok_old", purge_mode="fail")
    combined = proc.stdout + proc.stderr

    assert "FAILED to prune" in combined, f"a failed purge was not surfaced:\n{combined[-3000:]}"
    assert _retention_gauge(textfile_dir) == "0", (
        f"a failed purge left isnad_backup_retention_ok at 1:\n{combined[-3000:]}"
    )


def test_a_broken_retention_cannot_report_an_unqualified_clean_finish(tmp_path: Path) -> None:
    """The sentence that made this invisible was "=== Backup finished ===" on its own."""
    proc, _ = _run_backup(tmp_path, lsf_mode="fail")
    combined = proc.stdout + proc.stderr

    assert "Retention:                 FAILED" in combined, (
        f"the run summary does not report the retention failure, so an operator reading the "
        f"summary sees an unblemished backup:\n{combined[-3000:]}"
    )


# ---------------------------------------------------------------------------
# THE MUTATION. Prove the assertions above have teeth (deploy#574, #624).
# ---------------------------------------------------------------------------


def test_the_pre_fix_line_would_FAIL_these_assertions(tmp_path: Path) -> None:
    """Reintroduce deploy#635 verbatim and watch the guard go RED.

    An assertion that has only ever been watched agreeing with the code it was written beside
    has demonstrated nothing. So: take the REAL backup.sh, put the historical line back — the
    one copied out of the pre-fix tree, `2>/dev/null || true` — run it against the SAME failing
    rclone, and require that the fixed script's evidence DISAPPEARS.

    If this test ever goes green, the mutation has become inert and every assertion above is
    resting on nothing (`feedback_calibrate_the_mutation_before_counting_it`).
    """
    body = BACKUP_SH.read_text()

    # MUTATE THE WHOLE INSTRUMENTED BLOCK, not just the `lsf` line.
    #
    # Reverting only the redirect leaves the "where is the directory we just uploaded?" control
    # standing, and THAT control catches the failing listing on its own (rc swallowed -> dirs
    # empty -> today's dir missing -> refused). The mutant would then still behave correctly,
    # this calibration would go red for the wrong reason, and I would have "proved" the rc check
    # matters using a script in which it does not. The historical tree had NEITHER guard, so the
    # faithful mutant removes both and restores the original two lines verbatim.
    start_marker = "    local dirs lrc=0 lerr\n"
    end_marker = "    while IFS= read -r dir; do"
    assert start_marker in body and end_marker in body, (
        "backup.sh no longer has the instrumented-listing block this mutation targets — the "
        "mutation would be a no-op and this calibration would be vacuous."
    )
    start = body.index(start_marker)
    end = body.index(end_marker)

    # deploy#635, verbatim: stderr to /dev/null, rc swallowed by `|| true`, no control.
    pre_fix = (
        "    local dirs\n"
        '    dirs=$(rclone lsf "${RCLONE_REMOTE}:${B2_BUCKET}/${BACKUP_PREFIX}/${category}/" '
        "--dirs-only 2>/dev/null || true)\n\n"
    )
    mutant_body = body[:start] + pre_fix + body[end:]
    assert mutant_body != body, "the mutation did not change the script — it is INERT."

    # Stage the mutant beside EVERY library backup.sh could source — see _stage_mutant. A bare
    # tmp dir makes it die on line 1 (a sourced lib is fail-closed if absent), and the two "the
    # diagnostic is absent" assertions below would then PASS VACUOUSLY on the silence of a script
    # that never ran. That is the exact shape this module exists to catch.
    mutant = _stage_mutant(tmp_path, mutant_body)

    proc, textfile_dir = _run_backup(tmp_path, lsf_mode="fail", script=mutant)
    combined = proc.stdout + proc.stderr

    # PROVE THE MUTANT ACTUALLY RAN before reading anything from its silence. Every assertion
    # below is of the form "X is ABSENT from the output", and absence is exactly what a script
    # that crashed at startup also produces.
    assert "Running retention pruning" in combined, (
        f"the mutant never reached retention, so its silence proves nothing and this "
        f"calibration is vacuous:\n{combined[-3000:]}"
    )

    # THE BUG, reproduced: a failed listing is silently indistinguishable from "nothing to
    # prune". No diagnostic, no instrument-failure verdict, and the gauge says everything is
    # fine while retention did precisely nothing.
    assert "INSTRUMENT failure" not in combined, (
        "the pre-fix line still reported an instrument failure — the mutation is not "
        "reproducing deploy#635, so it does not calibrate anything."
    )
    assert RCLONE_LSF_DIAGNOSTIC not in combined, (
        "rclone's diagnostic survived `2>/dev/null` — the mutation is not the historical bug."
    )
    assert _retention_gauge(textfile_dir) == "1", (
        "the pre-fix line did not produce the FALSE-CLEAN gauge that is the entire bug. "
        "Something else in the fix is carrying the signal, and the listing line is not what "
        "these tests are actually measuring."
    )


# ---------------------------------------------------------------------------
# deploy#636 — the prefix must be bound to the host, not merely well-formed.
# ---------------------------------------------------------------------------

_CONTRADICTION = "but this host is"


def test_a_prefix_that_contradicts_the_host_is_REFUSED(tmp_path: Path) -> None:
    """`BACKUP_PREFIX=prod` on the STAGING host. Legal bare name; catastrophic value.

    Unrefused, staging writes its dumps into `prod/` and its retention purge deletes
    production's backups — deploy#632 through the front door, with no attacker and no typo in
    the value itself.
    """
    proc, textfile_dir = _run_backup(
        tmp_path, prefix="prod", base_domain="stg.noorinalabs.com", lsf_mode="ok_old"
    )
    combined = proc.stdout + proc.stderr

    assert proc.returncode != 0, f"the run was ALLOWED to proceed:\n{combined[-3000:]}"
    assert _CONTRADICTION in combined, (
        f"the refusal does not name the contradiction between the prefix and the host:\n"
        f"{combined[-3000:]}"
    )
    # And it must refuse BEFORE it can do any harm — no dump, no upload, and above all no purge.
    assert "Pruning:" not in combined, "it reached the PURGE despite the prefix being refused."
    assert "=== Backup started" not in combined, (
        f"the refusal came too late — the run had already begun dumping:\n{combined[-3000:]}"
    )
    assert _retention_gauge(textfile_dir) is None


def test_the_matching_prefix_is_NOT_refused(tmp_path: Path) -> None:
    """The positive control. Without it, "the refusal fired" could be true of EVERY input —
    including the correct one — and the guard would be refusing every legitimate backup on both
    hosts while this suite stayed green. A one-way oracle proves nothing (deploy#574)."""
    proc, textfile_dir = _run_backup(
        tmp_path, prefix="stg", base_domain="stg.noorinalabs.com", lsf_mode="ok_old"
    )
    combined = proc.stdout + proc.stderr

    assert _CONTRADICTION not in combined, (
        f"the guard REFUSED the correct prefix for this host. It is rejecting valid deploys, "
        f"and the refusal assertion above is vacuous:\n{combined[-3000:]}"
    )
    assert proc.returncode == 0, f"the legitimate run did not succeed:\n{combined[-3000:]}"


@pytest.mark.parametrize("domain", ["noorinalabs.com", "stg.noorinalabs.com"])
def test_each_host_accepts_exactly_its_own_prefix(tmp_path: Path, domain: str) -> None:
    """Both directions of the swap. `stg` on prod is as wrong as `prod` on stg, and a guard
    that only checks one of them leaves the other half of the bug in place."""
    expected = "stg" if domain.startswith("stg.") else "prod"
    wrong = "prod" if expected == "stg" else "stg"

    ok, _ = _run_backup(tmp_path / "ok", prefix=expected, base_domain=domain, lsf_mode="ok_old")
    assert _CONTRADICTION not in (ok.stdout + ok.stderr)

    bad, _ = _run_backup(tmp_path / "bad", prefix=wrong, base_domain=domain, lsf_mode="ok_old")
    assert bad.returncode != 0 and _CONTRADICTION in (bad.stdout + bad.stderr), (
        f"BACKUP_PREFIX={wrong} was accepted on the {expected} host ({domain})."
    )


# Domains that are NOT stg and NOT prod — but which a careless matcher would happily call one.
#
# Every one of these CONTAINS `noorinalabs.com` as a substring. That is the whole point: the
# anchored `case` arms cannot substring-match, but a globbed arm (`*noorinalabs.com*`) can, and
# would classify each of these as **prod**. A restore-drill box, a new environment, a lookalike
# domain — each silently declared production, and then permitted to write into `prod/` and to
# run `rclone purge` against PRODUCTION'S BACKUPS.
UNRECOGNISED_DOMAINS = [
    "restore.noorinalabs.com",  # a restore-drill / rehearsal box
    "dev.noorinalabs.com",  # a future environment nobody told backup.sh about
    "noorinalabs.com.attacker.test",  # a lookalike; suffix-anchored matching also fails here
    "foo.example.com",  # unrelated entirely — the easy case, kept as the control
]


@pytest.mark.parametrize("domain", UNRECOGNISED_DOMAINS)
@pytest.mark.parametrize("prefix", ["stg", "prod"])
def test_a_PRESENT_but_unrecognised_host_warns_and_is_never_classified(
    tmp_path: Path, domain: str, prefix: str
) -> None:
    """deploy#653 — the hole Nino Kavtaradze found: ABSENT was tested, PRESENT-BUT-UNKNOWN was not.

    The code is correct — an anchored `case` cannot substring-match — but nothing PINNED it, and
    "correct today" is not the property we need. The property is that it cannot silently STOP
    being correct. A `*noorinalabs.com*` glob would pass every other test in this file (the arms
    are ordered stg-first, so both KNOWN hosts still resolve correctly) and would quietly
    classify every domain above as **prod**.

    Read what that buys on a restore-drill box called `restore.noorinalabs.com`:

        BACKUP_PREFIX=prod  ->  HOST_ENV=prod  ->  MATCH  ->  ALLOWED
                            ->  it writes into prod/ and PURGES PRODUCTION'S BACKUPS

    That is the single catastrophic direction, and it is the entire reason deploy#636 exists.

    So this asserts the honest outcome for BOTH prefixes: an unrecognised host is not stg and is
    not prod — it is UNKNOWN. Warn, proceed, and testify to nothing. The `prefix` axis is what
    makes the mutant unable to hide: under the glob it is silently ALLOWED for `prod` (no
    warning) and wrongly REFUSED for `stg` (a contradiction that does not exist).
    """
    proc, _ = _run_backup(tmp_path, prefix=prefix, base_domain=domain, lsf_mode="ok_old")
    combined = proc.stdout + proc.stderr

    assert "cannot verify BACKUP_PREFIX" in combined, (
        f"BASE_DOMAIN={domain!r} is neither stg nor prod, but the run did not warn that it "
        f"could not verify the prefix — so it CLASSIFIED this host as something. A matcher that "
        f"substring-matches would call this 'prod', and a prod-prefixed backup here would purge "
        f"production's backups:\n{combined[-2000:]}"
    )
    assert _CONTRADICTION not in combined, (
        f"BASE_DOMAIN={domain!r} was treated as a KNOWN host and found to CONTRADICT "
        f"BACKUP_PREFIX={prefix!r}. It contradicts nothing — it is unrecognised, and the check "
        f"has no business having an opinion about it:\n{combined[-2000:]}"
    )
    assert proc.returncode == 0, (
        f"an unrecognised host refused the run outright:\n{combined[-2000:]}"
    )


def test_the_substring_glob_mutant_is_KILLED(tmp_path: Path) -> None:
    """Calibrate the fixtures above by reintroducing the exact defect they exist to catch.

    Nino Kavtaradze (#644 review, deploy#653): "the unknown-host branch is only tested ABSENT,
    never present-but-unrecognised — a substring-glob mutant survives the suite." It did. This
    kills it.

    Note the arms stay in the SAME ORDER. The mutant is not a reordering trick — `stg` is still
    matched first, so both KNOWN hosts still resolve correctly and every other assertion in this
    module still passes. That is precisely what made it survive: it is invisible everywhere
    except on a host that is neither, which is the case nothing tested.
    """
    body = BACKUP_SH.read_text()

    anchored = (
        'case "${BASE_DOMAIN:-}" in\n'
        '    stg.noorinalabs.com) HOST_ENV="stg" ;;\n'
        '    noorinalabs.com)     HOST_ENV="prod" ;;\n'
        '    *)                   HOST_ENV="" ;;\n'
        "esac"
    )
    assert anchored in body, (
        "backup.sh no longer has the anchored `case` this mutation targets — the mutation would "
        "be a no-op and this calibration would be vacuous."
    )

    globbed = (
        'case "${BASE_DOMAIN:-}" in\n'
        '    *stg.noorinalabs.com*) HOST_ENV="stg" ;;\n'
        '    *noorinalabs.com*)     HOST_ENV="prod" ;;\n'
        '    *)                     HOST_ENV="" ;;\n'
        "esac"
    )
    mutant = _stage_mutant(tmp_path, body.replace(anchored, globbed))

    # THE MUTANT MUST STILL BE CORRECT ON THE KNOWN HOSTS. If it broke those, it would be caught
    # by tests that already exist, it would not be the bug Nino described, and this calibration
    # would be proving something else entirely.
    ok, _ = _run_backup(
        tmp_path / "known", prefix="stg", base_domain="stg.noorinalabs.com", script=mutant
    )
    assert _CONTRADICTION not in (ok.stdout + ok.stderr), (
        "the mutant is not the bug under test — it broke a KNOWN host, so the existing tests "
        "would catch it and it demonstrates nothing about the unknown-host hole."
    )

    # AND IT MUST MISCLASSIFY THE UNKNOWN ONE. `restore.noorinalabs.com` contains
    # `noorinalabs.com`, so the globbed arm calls it PROD — and a prod-prefixed backup on that
    # box is then ALLOWED to purge production's backups. Silently. No warning.
    bad, _ = _run_backup(
        tmp_path / "unknown",
        prefix="prod",
        base_domain="restore.noorinalabs.com",
        script=mutant,
    )
    combined = bad.stdout + bad.stderr
    assert "Running retention pruning" in combined, (
        f"the mutant never reached retention, so its silence proves nothing:\n{combined[-2000:]}"
    )
    assert "cannot verify BACKUP_PREFIX" not in combined, (
        "the substring-glob mutant still WARNED on an unrecognised host — it is not reproducing "
        "deploy#653, so it calibrates nothing."
    )


def test_an_unknown_host_WARNS_and_does_not_testify(tmp_path: Path) -> None:
    """A check that cannot run must say so — and must NOT convert its own blindness into a
    verdict about the system it was checking.

    An absent or unrecognised BASE_DOMAIN means we do not know which host this is. That is not
    evidence the prefix is wrong. Refusing here would take a backup outage on a perfectly
    healthy box (a new environment, a restore drill, a hand-run invocation) and would be
    deploy#613's shape exactly: a check reporting its own breakage as a fact about B2.
    """
    proc, textfile_dir = _run_backup(tmp_path, prefix="stg", base_domain=None, lsf_mode="ok_old")
    combined = proc.stdout + proc.stderr

    assert "cannot verify BACKUP_PREFIX" in combined, (
        f"an unverifiable host produced no warning at all — the operator has no idea the "
        f"binding check was skipped:\n{combined[-3000:]}"
    )
    assert _CONTRADICTION not in combined, (
        "an UNKNOWN host was treated as a CONTRADICTED one. The check is testifying about "
        "something it cannot see."
    )
    assert proc.returncode == 0, (
        f"the run was refused merely because the host could not be identified:\n{combined[-3000:]}"
    )
    assert _retention_gauge(textfile_dir) == "1"


# ---------------------------------------------------------------------------
# deploy#636 — the workflow literal is the source of truth. Bind it to its own env.
# ---------------------------------------------------------------------------


def _workflow_prefix(workflow: Path) -> str:
    m = re.search(r"^\s*BACKUP_PREFIX=(\S+)", workflow.read_text(), re.MULTILINE)
    assert m, f"{workflow.name} does not inject BACKUP_PREFIX at all"
    return m.group(1)


# ---------------------------------------------------------------------------
# deploy#636 — an operator-facing example must not be a loaded gun.
# ---------------------------------------------------------------------------

RUNBOOK = REPO_ROOT / "RUNBOOK.md"
VERIFY_SH = REPO_ROOT / "scripts" / "verify_b2_backup_artifact.sh"

# The legacy bucket. It EXISTS in B2 and it is EMPTY — nothing writes to it. Measured
# 2026-07-14: `isnad-graph-backups` = 0 objects, `noorinalabs-backups` = 27 (9 stg + 18 prod).
EMPTY_LEGACY_BUCKET = "isnad-graph-backups"
LIVE_BUCKET = "noorinalabs-backups"

# An rclone path into the backups remote, as it appears in a RUNNABLE example. The bucket is
# either a `${B2_BUCKET}`-style expansion (correct) or a hardcoded literal (never correct).
# `[A-Z0-9_]+`, not `[A-Z_]+`: the variable is `B2_BUCKET` and it has a DIGIT in it. The first
# cut stopped at `${B`, left `2_BUCKET}/prod` as the "rest", and duly reported the correct line
# as missing its environment — a detector that cannot spell the name of the thing it is looking
# for will fail the fix and pass the bug.
_RCLONE_PATH = re.compile(r"isnad:(?P<bucket>\$\{?[A-Z0-9_]+\}?|[a-z0-9-]+)(?P<rest>/\S*)?")

# The environment must appear immediately under the bucket. A literal `stg`/`prod` and an
# expansion (`${{ matrix.env }}`) both qualify; a bare bucket root does not.
_HAS_ENV = re.compile(r"^/(?:stg|prod|\$)")


def _runnable_rclone_paths() -> list[tuple[str, str]]:
    """Every rclone backups path an operator could COPY AND RUN, as (source, line).

    Prose that *names* the empty bucket in order to warn about it is not a runnable example
    and must not be flagged — otherwise the only way to satisfy this guard would be to delete
    the warning, which is the one thing that must survive. So this reads exactly two things:

      * fenced ```bash blocks in RUNBOOK.md  — what an operator copies
      * `#   B2_ROOT="isnad:…` usage lines   — the shell scripts' copy-paste examples

    The blockquote warning in RUNBOOK.md and the explanatory comments in the scripts are
    neither, and are correctly invisible here.
    """
    out: list[tuple[str, str]] = []

    in_bash = False
    for line in RUNBOOK.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_bash = stripped.startswith("```bash")
            continue
        if in_bash and "isnad:" in line:
            out.append((RUNBOOK.name, line.strip()))

    for line in VERIFY_SH.read_text().splitlines():
        if re.match(r'^#\s+B2_ROOT="isnad:', line.strip()):
            out.append((VERIFY_SH.name, line.strip()))

    return out


def test_the_detector_sees_a_runnable_example_and_ignores_the_warning() -> None:
    """Calibrate before reading. A guard that flags nothing is not a guard, and one that
    flags the WARNING would force someone to delete the warning to get green."""
    found = _runnable_rclone_paths()
    assert found, (
        "the detector found NO runnable rclone examples at all — it is inert, and every "
        "assertion below it is worthless."
    )
    # It must be READING the examples, not merely finding lines: every one it returns must parse
    # into a bucket + path. A detector that matched the lines but extracted nothing from them
    # would pass every assertion below by vacuity.
    parsed = [m for _, line in found for m in _RCLONE_PATH.finditer(line)]
    assert parsed, f"the detector found lines but could not PARSE a single rclone path: {found}"
    assert any(m.group("bucket").startswith("$") for m in parsed), (
        f"the detector cannot see the `${{B2_BUCKET}}` form at all — the very form it is meant "
        f"to require. It would report every correct example as an offender: {found}"
    )
    # And it must be reading the runnable examples, not the blockquote prose that mentions the
    # legacy bucket in order to WARN about it. That warning must survive this guard.
    assert not any(EMPTY_LEGACY_BUCKET in line for _, line in found), (
        f"the detector is picking up the prose warning as if it were a runnable example. It "
        f"would force someone to DELETE the warning to go green: {found}"
    )


def test_no_operator_example_HARDCODES_a_bucket_name() -> None:
    """The property is not "name the right bucket". It is "never name a bucket at all".

    `RUNBOOK.md:346` hardcoded `isnad-graph-backups` — a bucket that exists and holds NOTHING —
    and it did so confidently, for months. An operator doing a DR restore follows the line, gets
    `status=absent dumps=0 bucket_objects=0` from the one check that reads B2 rather than a
    host-local gauge, and concludes THE BACKUPS DO NOT EXIST. Meanwhile the real bucket holds a
    healthy five-hour-old backup.

    The first fix was to swap in the correct literal. That is not enough, and Bereket Tadesse was
    right to block on it: a hardcoded bucket is a stale artifact waiting to happen, and it is
    exactly how the previous one went wrong. `${B2_BUCKET}` resolves to whatever the
    BACKUP_B2_BUCKET secret actually says, which is the only thing that is true BY CONSTRUCTION —
    immune to this entire class of error and to the next rename.

    So the guard forbids ANY literal, including today's correct one. A test that only banned the
    known-bad name would be a blocklist of one, and the next wrong bucket would sail past it.
    """
    offenders = []
    for src, line in _runnable_rclone_paths():
        for m in _RCLONE_PATH.finditer(line):
            if not m.group("bucket").startswith("$"):
                offenders.append(f"{src}: {line}")

    assert not offenders, (
        "a runnable example HARDCODES a bucket name. Use `isnad:${B2_BUCKET}/<env>` — a literal "
        "is a stale artifact waiting to happen, and the last one named an EMPTY bucket, which "
        "reads as 'there are no backups' when copy-pasted during an incident:\n  "
        + "\n  ".join(offenders)
    )


def test_no_operator_example_names_the_EMPTY_bucket() -> None:
    """Belt and braces on the one literal we KNOW is a loaded gun.

    Subsumed by the hardcoding ban above, and kept anyway: this is the specific name that cost
    real time in review, and it names its own failure mode in the assertion message. If someone
    ever relaxes the general rule, this is the line that must still hold.
    """
    offenders = [
        f"{src}: {line}" for src, line in _runnable_rclone_paths() if EMPTY_LEGACY_BUCKET in line
    ]
    assert not offenders, (
        f"a runnable example names the EMPTY legacy bucket `{EMPTY_LEGACY_BUCKET}`. Copy-pasted "
        f"during an incident it returns zero objects and reads as 'there are no backups':\n  "
        + "\n  ".join(offenders)
    )


def test_every_operator_example_names_its_environment() -> None:
    """stg and prod share one bucket. A path at the ROOT reads both environments as one pool,
    so a prod check reports healthy on the strength of a staging backup (deploy#632/#633).

    THE ENVIRONMENT HAS TWO LEGAL SPELLINGS, and a gate that knows only one of them is a gate
    that blocks the correct fix. `verify_b2_backup_artifact.sh` accepts the environment either
    baked into B2_ROOT (`isnad:<bucket>/prod`) or carried alongside it (`B2_PREFIX="prod"`), and
    both are correct. The first cut of this detector matched only the path form and went RED on
    the script's own documented B2_PREFIX example — a line-scan gate must match EVERY syntactic
    form, not the one the author happened to write first
    (`feedback_lint_gate_cover_all_syntactic_forms`).
    """
    offenders = []
    for src, line in _runnable_rclone_paths():
        # The environment may be supplied out-of-band on the same line.
        if re.search(r'B2_PREFIX="?(?:stg|prod|\$)', line):
            continue
        for m in _RCLONE_PATH.finditer(line):
            rest = m.group("rest") or ""
            if not _HAS_ENV.match(rest):
                offenders.append(f"{src}: {line}")

    assert not offenders, (
        "a runnable example reaches into the backups bucket without naming an environment. "
        "stg and prod share it, so this scans BOTH and reports fresh on whichever it finds:\n  "
        + "\n  ".join(offenders)
    )


def test_each_deploy_workflow_injects_ITS_OWN_environment() -> None:
    """`test_the_two_environments_get_different_prefixes` asserts only that stg != prod.

    A full SWAP satisfies that:

        deploy-stg.yml  -> BACKUP_PREFIX=prod
        deploy-prod.yml -> BACKUP_PREFIX=stg

    They differ, so the existing assertion is happy — and it is the most destructive
    configuration possible. Staging purges production's backups on a schedule, and production
    purges staging's, and the deploy writes both values authoritatively into each host's .env
    on every single run, so unlike the hand-edit it never self-repairs.

    "Different from each other" is not the property. "Equal to the environment it deploys" is.
    """
    assert _workflow_prefix(DEPLOY_STG) == "stg", (
        "deploy-stg.yml does not inject BACKUP_PREFIX=stg. Staging would write into — and "
        "purge — another environment's namespace."
    )
    assert _workflow_prefix(DEPLOY_PROD) == "prod", (
        "deploy-prod.yml does not inject BACKUP_PREFIX=prod. Production would write into — and "
        "purge — another environment's namespace."
    )

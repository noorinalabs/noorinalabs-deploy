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
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKUP_SH = REPO_ROOT / "scripts" / "backup.sh"
DEPLOY_STG = REPO_ROOT / ".github" / "workflows" / "deploy-stg.yml"
DEPLOY_PROD = REPO_ROOT / ".github" / "workflows" / "deploy-prod.yml"

BUCKET = "isnad-graph-backups"

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

    # The mutant must live in a directory that ALSO holds the libraries backup.sh sources by
    # `$(dirname "${BASH_SOURCE[0]}")` — compose_project.sh and b2_preflight.sh. Dropping it in a
    # bare tmp dir makes it die on line 1 with "Missing …/compose_project.sh", and then the two
    # "the diagnostic is absent" assertions below PASS VACUOUSLY — absent from the output of a
    # script that never ran. That is not a calibration, it is a coincidence, and it is exactly
    # the shape of bug this module exists to catch. (It happened while writing this test; only
    # the gauge assertion noticed.)
    mutant_dir = tmp_path / "scripts"
    mutant_dir.mkdir(parents=True)
    for lib in ("compose_project.sh", "b2_preflight.sh"):
        (mutant_dir / lib).write_bytes((REPO_ROOT / "scripts" / lib).read_bytes())
    mutant = mutant_dir / "backup.sh"
    mutant.write_text(mutant_body)

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
    import re

    m = re.search(r"^\s*BACKUP_PREFIX=(\S+)", workflow.read_text(), re.MULTILINE)
    assert m, f"{workflow.name} does not inject BACKUP_PREFIX at all"
    return m.group(1)


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

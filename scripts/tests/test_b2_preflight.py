"""Tests for the B2 credential preflight classifier (deploy#559).

The classifier exists because **rclone's error text does not identify the failure.**
Measured against the live B2 account on 2026-07-09 with a bucket-scoped key, a missing
bucket and a bucket the key is not scoped to produce a byte-identical message:

    CRITICAL: Failed to create file system for destination "isnad:<bucket>/":
    you must use bucket(s) [{"3c86..." "noorinalabs-pipeline"}] with this application key

B2 refuses at key-scope before it evaluates the bucket. Separately, a read-only key's
write returns a 401 that rclone renders as *"failed to create bucket"* -- naming the
wrong problem entirely, and one this project has already been misled by.

So ``classify_b2_state`` keys off capability-probe outcomes, never off message text.
These tests drive the real shell function through bash.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
PREFLIGHT = SCRIPTS_DIR / "b2_preflight.sh"
BACKUP = SCRIPTS_DIR / "backup.sh"


def classify(lsd_rc: str, bucket_visible: str, write_rc: str, delete_rc: str) -> str:
    """Invoke the real bash classifier — not a Python re-implementation of it."""
    script = (
        f'source "{PREFLIGHT}"; classify_b2_state {lsd_rc} {bucket_visible} {write_rc} {delete_rc}'
    )
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True)
    return out.stdout.strip()


def explain(verdict: str, bucket: str = "noorinalabs-backups") -> str:
    script = f'B2_BUCKET={bucket}; source "{PREFLIGHT}"; explain_b2_state {verdict}'
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True)
    return out.stdout


# --------------------------------------------------------------------------
# The discriminator the team lead asked for
# --------------------------------------------------------------------------


def test_read_only_key_is_not_misreported_as_a_missing_bucket() -> None:
    """The whole point. Bucket visible + write fails => read-only key, never 'no bucket'.

    rclone would say "failed to create bucket" here. The bucket demonstrably exists —
    the key just listed it.
    """
    assert classify("0", "yes", "1", "skip") == "KEY_READ_ONLY"


def test_unreachable_bucket_is_reported_as_ambiguous_not_as_missing() -> None:
    """A bucket-scoped key cannot tell 'absent' from 'not mine'. Do not pretend it can.

    Both produce the identical rclone message, so claiming either specifically would be
    a guess dressed as a diagnosis.
    """
    assert classify("0", "no", "skip", "skip") == "BUCKET_UNREACHABLE"
    text = explain("BUCKET_UNREACHABLE")
    assert "does not exist" in text and "scoped to a different bucket" in text, (
        "the diagnosis must present BOTH possibilities, since the probe cannot separate them"
    )


def test_read_only_diagnosis_names_and_disowns_the_misleading_rclone_message() -> None:
    text = explain("KEY_READ_ONLY")
    assert "failed to create bucket" in text, (
        "the diagnosis must name rclone's misleading message so the operator does not "
        "chase a missing bucket that exists"
    )
    assert "NOT a missing bucket" in text
    assert "writeFiles" in text


def test_write_capable_but_undeletable_key_is_caught() -> None:
    """backup.sh prunes retention with `rclone purge`; no deleteFiles = unbounded growth."""
    assert classify("0", "yes", "0", "1") == "KEY_CANNOT_DELETE"
    assert "deleteFiles" in explain("KEY_CANNOT_DELETE")


def test_invalid_key_short_circuits_before_bucket_reasoning() -> None:
    """If the key cannot list buckets, nothing downstream is meaningful."""
    assert classify("1", "no", "skip", "skip") == "KEY_INVALID"
    # Even if a later probe somehow "succeeded", an unusable key stays KEY_INVALID.
    assert classify("1", "yes", "0", "0") == "KEY_INVALID"


def test_fully_capable_key_passes() -> None:
    assert classify("0", "yes", "0", "0") == "OK"


def test_every_verdict_has_a_distinct_explanation() -> None:
    verdicts = [
        "OK",
        "PROBE_FAILED",
        "KEY_INVALID",
        "BUCKET_UNREACHABLE",
        "KEY_READ_ONLY",
        "KEY_CANNOT_DELETE",
    ]
    texts = {v: explain(v).strip() for v in verdicts}
    assert len(set(texts.values())) == len(verdicts), "verdicts must not share a diagnosis"
    for v, t in texts.items():
        assert t and "Unknown B2 preflight verdict" not in t, f"{v} has no explanation"


# --------------------------------------------------------------------------
# The classifier must not be error-string parsing
# --------------------------------------------------------------------------


def _code_only(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def test_classifier_does_not_branch_on_rclone_error_text() -> None:
    """Branching on the message is how the next person gets misdiagnosed.

    The strings may appear in the *explanation*, which is prose for a human. They must
    not appear in classify_b2_state(), which decides.
    """
    src = _code_only(PREFLIGHT.read_text())
    start = src.index("classify_b2_state()")
    end = src.index("explain_b2_state()")
    body = src[start:end]
    for needle in ("failed to create bucket", "401", "Unauthorized", "you must use bucket"):
        assert needle not in body, (
            f"classify_b2_state() branches on rclone text ({needle!r}). Classify by "
            "capability-probe outcome; rclone's message names the wrong problem."
        )


# --------------------------------------------------------------------------
# Credential-leak guards
# --------------------------------------------------------------------------


def test_preflight_refuses_to_run_with_rclone_dump_set() -> None:
    """An ambient RCLONE_DUMP=auth once leaked base64 credentials into a PUBLIC log."""
    src = _code_only(PREFLIGHT.read_text())
    assert "RCLONE_DUMP" in src and "Refusing to run" in src, (
        "preflight must hard-refuse when RCLONE_DUMP is set"
    )


def test_rclone_dump_is_never_set_anywhere() -> None:
    for path in (PREFLIGHT, BACKUP, SCRIPTS_DIR / "restore.sh"):
        src = _code_only(path.read_text())
        assert "RCLONE_DUMP=" not in src.replace('RCLONE_DUMP=" ', ""), (
            f"{path.name} must never assign RCLONE_DUMP"
        )


def test_preflight_reports_credential_shape_not_value() -> None:
    src = _code_only(PREFLIGHT.read_text())
    assert "${#B2_KEY_ID}" in src and "${#B2_APP_KEY}" in src, (
        "credentials must be reported by length only"
    )
    assert '"$B2_APP_KEY"' not in src.replace('RCLONE_CONFIG_ISNAD_KEY="${B2_APP_KEY}"', ""), (
        "the app key must never be echoed"
    )


# --------------------------------------------------------------------------
# backup.sh must actually use it, and believe it
# --------------------------------------------------------------------------


def test_backup_runs_the_preflight_before_dumping() -> None:
    src = _code_only(BACKUP.read_text())
    assert "b2_preflight.sh" in src and "preflight_b2" in src

    preflight_at = src.index("preflight_b2")
    dump_at = src.index("Stopping Neo4j for offline dump")
    assert preflight_at < dump_at, (
        "the credential must be verified BEFORE Neo4j is stopped; discovering at upload "
        "time that the key cannot write means the outage bought nothing"
    )


def test_backup_does_not_mask_the_preflight_exit_code_through_a_pipe() -> None:
    """`preflight_b2 | tee` returns tee's status, which is always 0."""
    src = _code_only(BACKUP.read_text())
    assert "if ! preflight_b2 2>&1 | tee" not in src, (
        "piping the preflight into tee masks its verdict behind tee's exit status"
    )


# --------------------------------------------------------------------------
# The guard must reach the operator. Behavioural, in production's invocation form.
# --------------------------------------------------------------------------
#
# The previous version of this file asserted that the literal string `PREFLIGHT_RC=$?`
# appeared in backup.sh. It did appear -- and it was DEAD. backup.sh runs under
# `set -euo pipefail`, where an assignment whose command substitution exits non-zero is
# itself a failing simple command: errexit fires at the assignment and every line below
# it, including the remediation message, is unreachable. A static guard over a line that
# never executes proves only that the line was typed.
#
# The three harnesses that all passed while production was broken:
#   * `./scripts/b2_preflight.sh` standalone -- its own `set -uo pipefail` has no -e
#   * `bash -c 'source …; classify_b2_state …'` -- likewise no -e
#   * a grep over the source                  -- executes nothing
#
# None of them was production's. So this test *runs backup.sh*, under `bash`, with the
# real `set -euo pipefail`, with `rclone` stubbed to fail -- and asserts the diagnosis
# lands on stdout. It fails against the pre-fix tree.


def _run_backup_with_failing_rclone(tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """Invoke backup.sh exactly as the systemd unit does, with a failing rclone."""
    stub = tmp_path / "stub"
    stub.mkdir()

    # `docker compose ps --format json` only needs to succeed for the preflight to be reached.
    (stub / "docker").write_text("#!/usr/bin/env bash\nexit 0\n")
    # A failing rclone drives the KEY_INVALID verdict (cannot even list buckets).
    (stub / "rclone").write_text(
        "#!/usr/bin/env bash\n"
        "echo 'CRITICAL: you must use bucket(s) [...] with this application key' >&2\n"
        "exit 1\n"
    )
    (stub / "zstd").write_text("#!/usr/bin/env bash\nexit 0\n")
    for f in stub.iterdir():
        f.chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{stub}:{env['PATH']}"
    env.update(
        B2_KEY_ID="fake-key-id",
        B2_APP_KEY="fake-app-key",
        B2_BUCKET="noorinalabs-backups",
        BACKUP_DIR=str(tmp_path / "backups"),
        COMPOSE_FILE="/dev/null",
    )
    env.pop("RCLONE_DUMP", None)

    return subprocess.run(
        ["bash", str(BACKUP)], capture_output=True, text=True, env=env, timeout=120
    )


def test_failing_preflight_reaches_the_operator(tmp_path: Path) -> None:
    """The whole point of the guard: the operator must SEE the diagnosis.

    Pre-fix, backup.sh emitted exactly one line -- "Backup script exited with code 1",
    from the EXIT trap -- and nothing else. The verdict, the remediation, and the
    explicit warning that rclone's "failed to create bucket" names the wrong problem were
    all computed and then discarded by errexit.
    """
    proc = _run_backup_with_failing_rclone(tmp_path)
    combined = proc.stdout + proc.stderr

    assert proc.returncode != 0, "a failing preflight must fail the backup"

    assert "B2 preflight failed" in combined, (
        "the remediation message never reached the operator. Under `set -e`, "
        '`OUT="$(preflight_b2 2>&1)"` fails AT THE ASSIGNMENT and everything below it is '
        "unreachable. Use `|| PREFLIGHT_RC=$?` so the assignment sits in a condition context."
    )
    assert "verdict=" in combined, "the preflight verdict must be printed, not swallowed"
    assert "KEY_INVALID" in combined, (
        "a key that cannot list buckets must be diagnosed as KEY_INVALID"
    )


def test_failing_preflight_stops_before_any_dump(tmp_path: Path) -> None:
    """Refusing to dump is the other half of the contract: no outage for nothing."""
    proc = _run_backup_with_failing_rclone(tmp_path)
    combined = proc.stdout + proc.stderr

    assert "refusing to dump databases we cannot upload" in combined
    # backup.sh logs this immediately before it stops Neo4j.
    assert "Stopping Neo4j" not in combined, "the preflight must abort before Neo4j is stopped"


# --------------------------------------------------------------------------
# deploy#613 — the probe must not blame the key for its OWN breakage
# --------------------------------------------------------------------------
#
# `mktemp -d` defaults to /tmp, and /tmp is READ-ONLY under the backup unit
# (ProtectSystem=strict, PrivateTmp deliberately unset — deploy#121 Bug A). The scratch
# allocation failed, its status went unchecked, the redirect to `${tmp}/lsd.out` degraded
# to `/lsd.out` on the read-only root, `lsd_rc` came back 1 — and `lsd_rc != 0` meant
# KEY_INVALID. Every backup this project ever ran died there, blaming a key that was fine:
# the SAME freshly-provisioned write-capable key yielded KEY_INVALID under systemd and OK
# by hand with a writable /tmp (stg, 2026-07-13).
#
# WHY THE OBVIOUS TEST IS WORTHLESS HERE
#
# `assert classify("1", "no", "skip", "skip") == "KEY_INVALID"` passed throughout. It
# still does. It cannot fail, because it asserts the very conflation that IS the bug: it
# is handed the *symptom* (lsd_rc=1) and asked whether the code draws the conclusion the
# code draws. An instrument that returns KEY_INVALID for both a bad key and a broken probe
# has not measured anything, and a test that only ever feeds it one class cannot tell.
# So these tests run the preflight on BOTH classes and require it to SEPARATE them —
# same harness, same stub, one factor varied at a time. Cf. the org memory
# `feedback_silent_zero_is_not_a_measurement`.
#
# Unusable scratch is simulated with an ENOTDIR parent (a *file* standing where a
# directory must be), not with chmod: chmod is a no-op against uid 0, so a root CI runner
# would quietly turn the whole class into a passing OK. ENOTDIR fails for root too.

GOOD_RCLONE = """#!/usr/bin/env bash
# A write-capable, correctly-scoped key: lists the bucket, writes, deletes.
case "$1" in
    lsd) echo "         -1 2026-07-13 00:00:00        -1 ${B2_BUCKET}" ;;
esac
exit 0
"""

BAD_RCLONE = """#!/usr/bin/env bash
# A revoked / wrongly-scoped key: cannot even enumerate buckets.
echo 'CRITICAL: you must use bucket(s) [...] with this application key' >&2
exit 1
"""

# backup.sh's own invocation form, verbatim (scripts/backup.sh § Preflight checks):
# `set -euo pipefail` + the `|| RC=$?` condition-context guard. A harness under a weaker
# `set -…` is not running production's code — deploy#563/#584 were both found this way.
_PRODUCTION_INVOCATION = """
set -euo pipefail
shopt -s inherit_errexit
source "{preflight}"
rc=0
out="$(set +e; preflight_b2 2>&1)" || rc=$?
printf '%s\\n' "$out"
exit "$rc"
"""


def _run_preflight(
    tmp_path: Path,
    *,
    key_is_good: bool,
    scratch_usable: bool,
) -> subprocess.CompletedProcess[str]:
    """Drive the real preflight, exactly as backup.sh drives it.

    Two independent factors, so the four cells can be compared:
      key_is_good     — whether the stubbed rclone behaves like a valid, write-capable key
      scratch_usable  — whether the preflight can allocate its scratch directory at all
    """
    tmp_path.mkdir(parents=True, exist_ok=True)  # each matrix cell gets its own sandbox
    stub = tmp_path / "stub"
    stub.mkdir()
    (stub / "rclone").write_text(GOOD_RCLONE if key_is_good else BAD_RCLONE)
    (stub / "rclone").chmod(0o755)

    # An ENOTDIR parent: `not-a-dir` is a regular file, so both `mkdir -p` and
    # `mktemp -d -p` fail on any path beneath it — for root as well as for us.
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    unusable = str(blocker / "scratch")

    usable = tmp_path / "backups"
    usable.mkdir()

    env = dict(os.environ)
    env["PATH"] = f"{stub}:{env['PATH']}"
    env.update(
        B2_KEY_ID="fake-key-id",
        B2_APP_KEY="fake-app-key",
        B2_BUCKET="noorinalabs-backups",
        BACKUP_DIR=str(usable) if scratch_usable else unusable,
    )
    env.pop("RCLONE_DUMP", None)

    script = _PRODUCTION_INVOCATION.format(preflight=PREFLIGHT)
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, env=env, timeout=120
    )


def _verdict_of(proc: subprocess.CompletedProcess[str]) -> str:
    for line in (proc.stdout + proc.stderr).splitlines():
        if line.startswith("[preflight] verdict="):
            return line.split("=", 1)[1].strip()
    raise AssertionError(f"the preflight printed no verdict at all:\n{proc.stdout}{proc.stderr}")


def test_the_preflight_separates_its_own_breakage_from_a_bad_key(tmp_path: Path) -> None:
    """THE test. One instrument, two failure classes, and it must not conflate them.

    Read the matrix as a 2x2: the verdict has to move with BOTH factors. If the preflight
    answered KEY_INVALID down the whole `scratch_usable=False` column — which is precisely
    what it did before deploy#613 — the key column would be doing no work at all, and the
    "detector" would just be a constant wearing a credential's name.
    """
    verdicts = {
        (key_is_good, scratch_usable): _verdict_of(
            _run_preflight(
                tmp_path / f"k{int(key_is_good)}s{int(scratch_usable)}",
                key_is_good=key_is_good,
                scratch_usable=scratch_usable,
            )
        )
        for key_is_good in (True, False)
        for scratch_usable in (True, False)
    }

    # Calibration first: with a working scratch dir the instrument demonstrably WORKS —
    # it tells a good key from a bad one. Without this cell, PROBE_FAILED below could be
    # an artefact of the stub rather than a real discrimination.
    assert verdicts[(True, True)] == "OK", "a good key with usable scratch must verify"
    assert verdicts[(False, True)] == "KEY_INVALID", (
        "a key that genuinely cannot list buckets is still KEY_INVALID — the fix must not "
        "have blunted the real diagnosis"
    )

    # And now the bug: a broken scratch dir is the PROBE's failure, never the key's.
    assert verdicts[(True, False)] == "PROBE_FAILED", (
        "a good key + unusable scratch was reported as KEY_INVALID before deploy#613. "
        "The probe never ran; it holds no evidence about the credential and must say so."
    )
    assert verdicts[(False, False)] == "PROBE_FAILED", (
        "even when the key IS bad, a preflight that could not run has not LEARNED that. "
        "Reporting KEY_INVALID here would be right by accident — and it is the same "
        "accident that condemned a good key on every scheduled run."
    )

    # The separation, stated as the property rather than as four literals: the verdict
    # under a broken scratch dir must not be a credential verdict at all.
    broken = {verdicts[(True, False)], verdicts[(False, False)]}
    assert broken.isdisjoint({"OK", "KEY_INVALID", "BUCKET_UNREACHABLE", "KEY_READ_ONLY"}), (
        f"a probe that could not run emitted a verdict about the credential: {broken}"
    )


def test_a_good_key_verifies_when_tmp_is_unusable_but_backup_dir_is_writable(
    tmp_path: Path,
) -> None:
    """The regression test: production's exact namespace, reproduced without systemd.

    Under the unit, /tmp is read-only and BACKUP_DIR (/var/lib/noorinalabs-backups) is
    writable via ReadWritePaths. `mktemp -d` honours TMPDIR, so pointing TMPDIR at an
    ENOTDIR path reproduces the read-only /tmp the unit imposes, exactly, in CI.

    Against the pre-fix tree this run yields KEY_INVALID — the stg failure, reproduced.
    After the fix the scratch dir comes from BACKUP_DIR and the same key verifies OK.
    """
    stub = tmp_path / "stub"
    stub.mkdir()
    (stub / "rclone").write_text(GOOD_RCLONE)
    (stub / "rclone").chmod(0o755)

    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()

    env = dict(os.environ)
    env["PATH"] = f"{stub}:{env['PATH']}"
    env.update(
        B2_KEY_ID="fake-key-id",
        B2_APP_KEY="fake-app-key",
        B2_BUCKET="noorinalabs-backups",
        BACKUP_DIR=str(backup_dir),
        TMPDIR=str(blocker / "tmp"),  # stands in for the unit's read-only /tmp
    )
    env.pop("RCLONE_DUMP", None)

    proc = subprocess.run(
        ["bash", "-c", _PRODUCTION_INVOCATION.format(preflight=PREFLIGHT)],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    combined = proc.stdout + proc.stderr

    assert _verdict_of(proc) == "OK", (
        "a valid key must verify with an unusable /tmp, because the scratch dir belongs "
        "under BACKUP_DIR — which the unit grants in ReadWritePaths. This is the stg "
        "failure of deploy#613: same key, KEY_INVALID under systemd, OK by hand."
    )
    assert proc.returncode == 0
    assert "KEY_INVALID" not in combined


def test_scratch_failure_is_fatal_and_leaves_no_scratch_behind(tmp_path: Path) -> None:
    """A scratch failure must abort — never fall through into a credential verdict."""
    proc = _run_preflight(tmp_path, key_is_good=True, scratch_usable=False)
    combined = proc.stdout + proc.stderr

    assert proc.returncode != 0, "an unrunnable preflight must fail, not pass by default"
    assert "scratch_dir=FAILED" in combined, "the operator must be told WHICH thing broke"
    assert "NOT a verdict on the key" in combined, (
        "the diagnosis must explicitly disown the credential, or the next operator rotates "
        "a perfectly good key — which is exactly what deploy#612 nearly did"
    )
    assert "ReadWritePaths" in combined and "BACKUP_DIR" in combined, (
        "the remediation must name the systemd knob that actually governs this"
    )


def test_standalone_invocation_also_refuses_rather_than_blaming_the_key(tmp_path: Path) -> None:
    """`./scripts/b2_preflight.sh` is the form an operator runs on the host by hand."""
    stub = tmp_path / "stub"
    stub.mkdir()
    (stub / "rclone").write_text(GOOD_RCLONE)
    (stub / "rclone").chmod(0o755)

    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")

    env = dict(os.environ)
    env["PATH"] = f"{stub}:{env['PATH']}"
    env.update(
        B2_KEY_ID="fake-key-id",
        B2_APP_KEY="fake-app-key",
        B2_BUCKET="noorinalabs-backups",
        TMPDIR=str(blocker / "tmp"),  # BACKUP_DIR unset: the fallback must break safely too
    )
    env.pop("BACKUP_DIR", None)
    env.pop("RCLONE_DUMP", None)

    proc = subprocess.run(
        ["bash", str(PREFLIGHT)], capture_output=True, text=True, env=env, timeout=120
    )
    combined = proc.stdout + proc.stderr

    assert proc.returncode != 0
    assert _verdict_of(proc) == "PROBE_FAILED"
    assert "KEY_INVALID" not in combined


def test_backup_never_blames_the_key_when_its_staging_dir_is_unwritable(tmp_path: Path) -> None:
    """End-to-end through backup.sh, which stages into BACKUP_DIR before it ever probes.

    If BACKUP_DIR is unusable, backup.sh now cannot reach the preflight at all (it dies at
    its own `mkdir -p`). That is fine — what must NEVER happen, by any path, is that an
    infrastructure fault at the host presents itself to the operator as a bad credential.
    """
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")

    stub = tmp_path / "stub"
    stub.mkdir()
    for name in ("docker", "zstd"):
        (stub / name).write_text("#!/usr/bin/env bash\nexit 0\n")
    (stub / "rclone").write_text(GOOD_RCLONE)
    for f in stub.iterdir():
        f.chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{stub}:{env['PATH']}"
    env.update(
        B2_KEY_ID="fake-key-id",
        B2_APP_KEY="fake-app-key",
        B2_BUCKET="noorinalabs-backups",
        BACKUP_DIR=str(blocker / "scratch"),
        COMPOSE_FILE="/dev/null",
    )
    env.pop("RCLONE_DUMP", None)

    proc = subprocess.run(
        ["bash", str(BACKUP)], capture_output=True, text=True, env=env, timeout=120
    )
    combined = proc.stdout + proc.stderr

    assert proc.returncode != 0
    assert "KEY_INVALID" not in combined, (
        "a host-side storage fault must not be rendered as a credential verdict"
    )
    assert "Stopping Neo4j" not in combined, "and it must not take an outage on the way"


# --------------------------------------------------------------------------
# The classifier's own contract for a probe that never ran
# --------------------------------------------------------------------------


def test_a_probe_that_never_ran_is_not_an_invalid_key() -> None:
    """`lsd_rc` is an exit status. "skip" is not one — it means the probe never ran."""
    assert classify("skip", "unknown", "skip", "skip") == "PROBE_FAILED"
    # It stays PROBE_FAILED whatever the other probes claim: they cannot have run either.
    assert classify("skip", "yes", "0", "0") == "PROBE_FAILED"


def test_a_missing_exit_status_is_never_read_as_evidence() -> None:
    """The original defect in one line: `tmp=""` → no probe → a status that isn't one.

    Anything non-numeric reaching `lsd_rc` means no probe produced it, so it can carry no
    information about the key. Guarding only the literal "skip" would re-open the hole for
    the next caller that passes an empty string, exactly as `tmp=""` once did.
    """
    for not_a_status in ("", "unknown", "null", "skip"):
        assert classify(f"'{not_a_status}'", "no", "skip", "skip") == "PROBE_FAILED", (
            f"lsd_rc={not_a_status!r} is not an exit status; it must not condemn the key"
        )


def test_probe_failed_diagnosis_disowns_the_credential() -> None:
    text = explain("PROBE_FAILED")
    assert "NOT a verdict on the key" in text
    assert "do not rotate" in text.lower()
    assert "ProtectSystem=strict" in text and "ReadWritePaths" in text, (
        "name the systemd hardening that causes this, or the next operator spends the "
        "afternoon on the key instead of on /tmp"
    )

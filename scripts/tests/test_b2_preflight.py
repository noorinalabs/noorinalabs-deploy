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
    verdicts = ["OK", "KEY_INVALID", "BUCKET_UNREACHABLE", "KEY_READ_ONLY", "KEY_CANNOT_DELETE"]
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

"""BEHAVIOURAL tests for the B2 backup-artifact scanner (deploy#583) — it is RUN, not grepped.

Nothing in this project asserts that a *restorable object exists in B2*. Every backup alert
reads a **host-local** textfile gauge, and the single point of trust that an object ever
landed in the bucket is ``rclone copy; echo $?`` — the uploader's own opinion of its upload.
**The whole alerting stack can be green while the bucket is empty.**

``scripts/verify_b2_backup_artifact.sh`` is the only thing that looks in the bucket. These
tests execute it against fixtures and require it to **separate four classes**:

* ``fresh``            — a restorable dump exists and is recent.
* ``absent``           — the ALERT. **An empty ``rclone lsf`` exits 0 and prints nothing** —
  the exact silent zero this whole issue is about, and the one it would be easiest to commit
  inside its own fix.
* ``stale``            — the newest dump is too old.
* ``instrument_error`` — we could not look. **NOT a claim that backups are missing.**

Two bugs found while building it are pinned here, because both were invisible to the first
version of the calibration:

1. **The B2 backend and the local backend disagree.** ``lsf`` on a nonexistent *prefix*
   returns rc=3 on local but **rc=0 with empty output on B2** — so on the backend we actually
   run against, a typo'd path is indistinguishable from an empty bucket. The
   instrument-liveness probe therefore has to sit on the **bucket** (``lsd``: rc=0 real,
   rc=1 missing/bad-key on B2), which separates on both.

2. **rclone prints ModTime in LOCAL time.** Parsing it with ``date -u -d`` is a systematic
   error equal to the box's UTC offset — measured here (UTC−4), an object uploaded *seconds*
   earlier reported ``newest_age_hours=4``. On a box **ahead** of UTC that error *deflates*
   the age, so **a stale backup reads fresh**: a missed alarm on the one check standing under
   an unrecoverable delete. The class-level self-test could not see it — a 4-hour skew does
   not move a fresh fixture out of the fresh class, because the classes are far apart. So the
   calibration asserts the **age value**, not merely the class.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
SCANNER = SCRIPTS_DIR / "verify_b2_backup_artifact.sh"

EXIT_OK = 0
EXIT_ALERT = 1
EXIT_INSTRUMENT = 2

# Must clear the scanner's MIN_DUMP_BYTES floor. The first version of these tests wrote
# b"x" — a 1-byte "dump" — and the scanner's own positive fixture was a ZERO-byte file, so
# neither could produce the bad condition they were meant to exclude. A fixture that cannot
# express the defect cannot test for it.
REAL_DUMP = b"P" * 4096


def _scan(root: Path | str, **env: str) -> tuple[int, str]:
    r = subprocess.run(  # noqa: S603
        ["bash", str(SCANNER)],  # noqa: S607
        env={**os.environ, "B2_ROOT": str(root), **env},
        capture_output=True,
        text=True,
        check=False,
    )
    line = ""
    for ln in r.stdout.splitlines():
        if ln.startswith("B2_BACKUP_ARTIFACT "):
            line = ln
    return r.returncode, line


def _field(line: str, key: str) -> str:
    for tok in line.split():
        k, _, v = tok.partition("=")
        if k == key:
            return v
    return ""


# ---------------------------------------------------------------------------
# The POSITIVE control. Without it every refusal below is vacuous — a scanner
# that only ever says ALERT proves every bucket is empty.
# ---------------------------------------------------------------------------
def test_fresh_dump_is_accepted(tmp_path: Path) -> None:
    (tmp_path / "isnad-pg-now.dump").write_bytes(REAL_DUMP)
    rc, line = _scan(tmp_path)
    assert rc == EXIT_OK, f"a fresh dump must pass: {line}"
    assert _field(line, "status") == "fresh"
    assert _field(line, "dumps") == "1"


def test_fresh_dump_age_is_not_skewed(tmp_path: Path) -> None:
    """The AGE, not just the class — this is the one that catches a clock/TZ error.

    rclone prints ModTime in local time. Parsed as UTC, an object written *now* reported
    4 hours old on this box. A skew in the other direction makes a stale backup read fresh.
    A class-level assertion cannot see this: 4h is still comfortably 'fresh'.
    """
    (tmp_path / "isnad-pg-now.dump").write_bytes(REAL_DUMP)
    rc, line = _scan(tmp_path)
    assert rc == EXIT_OK
    age = int(_field(line, "newest_age_hours"))
    assert 0 <= age <= 1, (
        f"an object written seconds ago reported {age}h old. The scanner's clock disagrees "
        "with rclone's ModTime — and in the other direction this silently turns a STALE "
        "backup into a FRESH one."
    )


# ---------------------------------------------------------------------------
# ABSENT — the silent zero. This is the whole point of the issue.
# ---------------------------------------------------------------------------
def test_empty_prefix_is_an_alert_not_an_ok(tmp_path: Path) -> None:
    """`rclone lsf` on an empty prefix exits 0 and prints NOTHING.

    Reading that as healthy is precisely the defect deploy#583 exists to close, and it would
    be a perfect instance of the bug landing inside its own fix.
    """
    rc, line = _scan(tmp_path)
    assert rc == EXIT_ALERT, "an empty bucket must ALERT, not pass"
    assert _field(line, "status") == "absent"


def test_bucket_with_no_dumps_is_absent(tmp_path: Path) -> None:
    """Lists NON-empty. Restores nothing.

    A bucket holding only `.sha256` files and a `_backup_manifest.txt` is not a backup — and
    it lists non-empty, so "is anything there?" is the wrong question. Only dumps count.
    """
    (tmp_path / "isnad-pg-old.dump.sha256").write_bytes(b"x")
    (tmp_path / "_backup_manifest.txt").write_bytes(b"x")
    rc, line = _scan(tmp_path)
    assert rc == EXIT_ALERT
    assert _field(line, "status") == "absent"
    assert _field(line, "dumps") == "0"


def test_stale_dump_is_an_alert(tmp_path: Path) -> None:
    p = tmp_path / "isnad-pg-old.dump"
    p.write_bytes(REAL_DUMP)
    old = time.time() - (60 * 3600)
    os.utime(p, (old, old))
    rc, line = _scan(tmp_path, MAX_AGE_HOURS="30")
    assert rc == EXIT_ALERT
    assert _field(line, "status") == "stale"


# ---------------------------------------------------------------------------
# INSTRUMENT ERROR — "I could not look" must never be reported as "nothing is there".
# ---------------------------------------------------------------------------
def test_unreachable_bucket_is_not_reported_as_absent(tmp_path: Path) -> None:
    """The distinction that keeps this check honest.

    A credential failure that reads as "absent" would fire a data-loss alarm at 3am over a
    rotated key — and, far worse, it means the two states are conflated, so the reverse
    conflation is one refactor away.
    """
    rc, line = _scan(tmp_path / "does-not-exist")
    assert rc == EXIT_INSTRUMENT, "an unreachable bucket is an INSTRUMENT failure"
    assert _field(line, "status") == "instrument_error"
    assert _field(line, "status") != "absent"


# ---------------------------------------------------------------------------
# The scanner refuses to report from an uncalibrated instrument.
# ---------------------------------------------------------------------------
def test_self_test_separates_all_four_classes() -> None:
    r = subprocess.run(  # noqa: S603
        ["bash", str(SCANNER), "--self-test"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, f"the scanner's own calibration failed:\n{r.stderr}"
    for cls in ("fresh", "absent-empty", "absent-no-dumps", "stale", "instrument-error"):
        assert f"self-test {cls}" in r.stderr, f"self-test does not cover {cls}"


def test_scan_refuses_when_self_test_fails(tmp_path: Path) -> None:
    """An uncalibrated instrument's zero is not a zero.

    Forced here by breaking the clock the calibration checks: with a skewed TZ the fresh
    fixture reports hours old, the self-test fails, and the scanner must refuse to give a
    verdict on the real bucket rather than hand back a reading it cannot vouch for.
    """
    (tmp_path / "isnad-pg-now.dump").write_bytes(REAL_DUMP)
    r = subprocess.run(  # noqa: S603
        ["bash", str(SCANNER)],  # noqa: S607
        env={**os.environ, "B2_ROOT": str(tmp_path), "TZ": "Asia/Tokyo"},
        capture_output=True,
        text=True,
        check=False,
    )
    # TZ is re-pinned to UTC inside the script, so this must still succeed — proving the
    # script does not inherit a hostile clock. If it ever stops pinning TZ, the age
    # assertion in the self-test fires and the scan refuses instead of lying.
    assert r.returncode == EXIT_OK, (
        "the scanner must pin TZ itself and not inherit the caller's — otherwise a runner "
        f"in a non-UTC zone silently mis-ages every backup.\n{r.stderr}"
    )


# ---------------------------------------------------------------------------
# A zero-byte dump restores NOTHING (deploy#584 review — Nino Kavtaradze)
# ---------------------------------------------------------------------------
def test_zero_byte_dump_is_not_restorable(tmp_path: Path) -> None:
    """The PR is titled *assert a RESTORABLE object exists*. A 0-byte dump is not one.

    This is the same argument the scanner already made one level out — *"a bucket holding
    only .sha256 and _backup_manifest.txt lists NON-empty and restores NOTHING"* — and it
    stopped one level short. A bucket holding only zero-byte `.dump` files also lists
    non-empty and restores nothing.

    It is exactly what `pg_dump` failing leaves behind, after the shell redirection has
    already created the target: the file uploads cleanly, rclone returns 0, the host gauge
    goes green — and, before the size floor, THIS check said `fresh` too. The chain of trust
    this scanner exists to break was still broken, one layer out.
    """
    (tmp_path / "isnad-pg-now.dump").write_bytes(b"")
    rc, line = _scan(tmp_path)
    assert rc == EXIT_ALERT, f"a zero-byte dump is not restorable: {line}"
    assert _field(line, "status") == "absent"
    assert _field(line, "reason") == "undersized_dumps"


# ---------------------------------------------------------------------------
# A future timestamp is a BROKEN CLOCK, not a fresh backup (both reviewers)
# ---------------------------------------------------------------------------
def test_future_timestamp_is_an_instrument_error_not_fresh(tmp_path: Path) -> None:
    """`age > MAX` is a ONE-SIDED bound, and the unbounded side reads `fresh` forever.

    rclone PRESERVES the source mtime, so an object's timestamp is the VPS clock at dump
    time. This script pins TZ on the READER side; the WRITER has the identical exposure and
    no guard. A skewed or NTP-failed VPS uploads future-dated dumps — and a one-sided bound
    then reports them fresh.
    """
    p = tmp_path / "isnad-pg-future.dump"
    p.write_bytes(REAL_DUMP)
    future = time.time() + (30 * 24 * 3600)
    os.utime(p, (future, future))
    rc, line = _scan(tmp_path)
    assert rc == EXIT_INSTRUMENT, f"a future timestamp is a broken clock, not a backup: {line}"
    assert _field(line, "status") == "instrument_error"
    assert _field(line, "reason") == "future_timestamp"


def test_one_future_object_cannot_mask_a_stale_bucket(tmp_path: Path) -> None:
    """It does not degrade — it LATCHES.

    `newest` is selected by max epoch, so a single bad-clock object masks an entire stale
    bucket: a genuinely 40-day-old backup beside one future-dated object reported
    `status=fresh`, rc=0. One bad-clock upload converts this job into a permanent green
    light on the exact signal it was built to provide.
    """
    old = tmp_path / "isnad-pg-old.dump"
    old.write_bytes(REAL_DUMP)
    t_old = time.time() - (40 * 24 * 3600)
    os.utime(old, (t_old, t_old))

    fut = tmp_path / "isnad-pg-future.dump"
    fut.write_bytes(REAL_DUMP)
    t_fut = time.time() + (30 * 24 * 3600)
    os.utime(fut, (t_fut, t_fut))

    rc, line = _scan(tmp_path)
    assert rc == EXIT_INSTRUMENT, (
        f"one future-dated object must not mask a 40-day-old bucket: {line}"
    )
    assert _field(line, "status") != "fresh"


# ---------------------------------------------------------------------------
# The production calibration gate must be TWO-SIDED
# ---------------------------------------------------------------------------
def test_self_test_catches_clock_skew_in_BOTH_directions() -> None:
    """A one-sided assertion on a two-sided error is half a control.

    The shell `self_test` — the gate that runs in production before every real reading, on
    the machine where a skew would actually occur — asserted `got_age > want` only. That
    catches INFLATION (a fresh backup reading stale: a false alarm, the annoying direction)
    and is structurally blind to DEFLATION (a stale backup reading fresh: the missed alarm,
    and the direction the guard was written for).

    Proved by removing the TZ pin the guard exists to protect: at UTC-4 the self-test
    failed; at UTC+4 it PASSED, and the scanner then reported `newest_age_hours=-3` as
    `fresh`. The self-test declared the scanner calibrated and the scanner then lied.
    """
    src = SCANNER.read_text()
    assert 'if [[ "$got_age" -lt 0 ]]; then' in src, (
        "the calibration gate must reject a NEGATIVE age — the deflating direction, which "
        "turns a stale backup into a fresh one"
    )
    assert 'if [[ "$got_age" -gt "$want_age" ]]; then' in src, (
        "and it must still reject an inflated age"
    )


# ---------------------------------------------------------------------------
# instrument_error must say WHY (deploy#584 review — Nino Kavtaradze)
# ---------------------------------------------------------------------------
def test_instrument_error_carries_a_diagnostic(tmp_path: Path) -> None:
    """`rc` alone cannot separate a 401 from a typo'd bucket from a network fault.

    Both probes previously discarded rclone's stderr (`2>/dev/null`). An error state that
    cannot say *why* it could not look is a diminished version of the third state this whole
    script is built around — the operator is told "I could not see" and given nothing to act
    on. Verified on real B2: a bad key yields `401 bad_auth_token`, a missing bucket yields
    `directory not found` — two different faults that share an exit code.
    """
    r = subprocess.run(  # noqa: S603
        ["bash", str(SCANNER)],  # noqa: S607
        env={**os.environ, "B2_ROOT": str(tmp_path / "no-such-bucket")},
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == EXIT_INSTRUMENT
    assert "rclone could not list" in r.stderr, (
        "instrument_error must surface rclone's own stderr, or the operator cannot tell a "
        "credential failure from a wrong path"
    )


def test_passing_self_test_does_not_cry_wolf(tmp_path: Path) -> None:
    """The self-test drives the instrument-error path ON PURPOSE.

    Without suppression, every *passing* run printed an alarming "rclone could not list"
    block from a fixture behaving exactly as intended. A check that cries wolf on success is
    a check people learn to scroll past — and this one only matters on the day someone
    actually reads it.
    """
    (tmp_path / "isnad-pg-now.dump").write_bytes(REAL_DUMP)
    r = subprocess.run(  # noqa: S603
        ["bash", str(SCANNER)],  # noqa: S607
        env={**os.environ, "B2_ROOT": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == EXIT_OK
    assert "rclone could not list" not in r.stderr, (
        "a PASSING run must not print an error block from the self-test's own fixtures"
    )


def test_refuses_to_run_under_rclone_dump(tmp_path: Path) -> None:
    """This script PRINTS rclone's stderr, and its output goes to a public CI log.

    `RCLONE_DUMP=auth` makes rclone echo the `Authorization: Basic <base64(keyID:key)>`
    header, which GitHub's masking — an exact-substring match on the raw secret — does NOT
    catch. Refuse rather than redact: a leak is not recoverable, and there is no reason to
    run this under a debug dump.
    """
    (tmp_path / "isnad-pg-now.dump").write_bytes(REAL_DUMP)
    r = subprocess.run(  # noqa: S603
        ["bash", str(SCANNER)],  # noqa: S607
        env={**os.environ, "B2_ROOT": str(tmp_path), "RCLONE_DUMP": "auth"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == EXIT_INSTRUMENT, "RCLONE_DUMP must be refused, not honoured"
    assert "RCLONE_DUMP" in r.stderr

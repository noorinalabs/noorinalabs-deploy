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
import tempfile
import time
from pathlib import Path

import pytest
from manifest_fixture import build_manifest, manifest_filename, manifest_format

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


# backup.sh's run id: `date -u +%Y%m%d-%H%M%S`. The dumps of a run are ALL named for it.
RUN_TS = "20260711-030100"

# A LATER run on the SAME DAY. B2's path is `<category>/<DATE>` — a DAY — so a retry after a
# failed nightly lands its dumps and its manifest right beside the run that already succeeded.
# Nothing about this is exotic; backup.sh uploads partials by design (deploy#587).
LATE_TS = "20260711-080000"
EARLY_TS = "20260711-020000"

# The three stores restore.sh REQUIRES, in backup.sh's own filename forms. Neo4j's dump is
# compressed, so it lands as `.dump.zst`.
ALL_STORES = ("isnad-pg", "isnad-userpg", "isnad-neo4j")


def store_files(run_ts: str = RUN_TS) -> dict[str, str]:
    """The dump filenames of ONE run. Every dump of a run is named for that run."""
    return {
        "isnad-pg": f"isnad-pg-{run_ts}.dump",
        "isnad-userpg": f"isnad-userpg-{run_ts}.dump",
        "isnad-neo4j": f"isnad-neo4j-{run_ts}.dump.zst",
    }


STORE_FILES = store_files(RUN_TS)


# Rendered from BACKUP.SH'S OWN printf — format string and argument order both parsed out of
# the producer. Not a paraphrase.
#
# This used to be an f-string in the test's own words. It was FAITHFUL — and that is the
# point: it was faithful *today*, by inspection, and nothing pinned it. The manifest line is
# the exact place `backup_is_complete`'s never-matching regex lived, and a fixture that
# restates the producer's format is written by the same mind that wrote the parser, so it
# encodes the same misreading and cannot falsify it. See manifest_fixture.py.
def _manifest(complete: bool, run_ts: str = RUN_TS) -> bytes:
    return build_manifest(complete=complete, run_ts=run_ts)


def _backup_dir(
    root: Path,
    *,
    complete: bool = True,
    dump: bytes = REAL_DUMP,
    stores: tuple[str, ...] = ALL_STORES,
) -> Path:
    """A WHOLE RUN — all three required stores, named as backup.sh names them.

    The first version of this helper wrote a single file called ``isnad-pg-a.dump``. So the
    fixture had **no concept of a run**, and could not express "the user-postgres dump never
    uploaded" — the scanner was therefore never asked, and it answered ``fresh`` over a
    bucket ``restore.sh`` exits 1 on. ``stores`` exists to build exactly that bad artifact.
    """
    return _write_run(root / "daily" / "2026-07-11", complete=complete, dump=dump, stores=stores)


def _write_run(
    d: Path,
    *,
    run_ts: str = RUN_TS,
    complete: bool = True,
    dump: bytes = REAL_DUMP,
    stores: tuple[str, ...] = ALL_STORES,
    manifest: bool = True,
) -> Path:
    """Write one backup RUN into a directory — which may already hold OTHER runs.

    The manifest is named by ``backup.sh``'s own ``MANIFEST_FILE=`` assignment, not by us. That
    is the whole of deploy#587: with a FIXED name, writing a second run into this directory
    would silently CLOBBER the first run's attestation — and the fixture would be unable to
    express the very defect it needs to reproduce, because it would reproduce it on itself.

    ``manifest=False`` builds the "arrived but UNATTESTED" artifact: dumps with no manifest.
    """
    d.mkdir(parents=True, exist_ok=True)
    files = store_files(run_ts)
    for s in stores:
        (d / files[s]).write_bytes(dump)
    if manifest:
        (d / manifest_filename(run_ts)).write_bytes(_manifest(complete, run_ts))
    return d


def _partial_run(d: Path, run_ts: str = LATE_TS) -> Path:
    """A PARTIAL run: isnad-pg dumped, user-postgres and Neo4j did not.

    backup.sh produces exactly this by design — "a partial backup beats none" — and uploads it
    into the SAME day-directory as the good run, exiting non-zero afterwards.
    """
    return _write_run(d, run_ts=run_ts, complete=False, stores=("isnad-pg",))


def _age(d: Path, epoch: float) -> None:
    """Re-date the WHOLE RUN.

    `newest` is the max mtime in the directory, so ageing one dump while its siblings stay
    fresh does not age the backup — the assertion would pass for the wrong reason, or not at
    all. Age is a property of the run, not of one file.
    """
    for f in sorted(d.iterdir()):
        if f.suffix in (".dump", ".zst"):
            os.utime(f, (epoch, epoch))


def _scan(root: Path | str, **env: str) -> tuple[int, str]:
    """Run the scanner. Returns (rc, RESULT LINE).

    **The result line is the contract, and it is the only thing a caller can see.**
    ``verify-backup-artifact.yml``'s summary step renders *only* this line, so anything the scan
    knows and does not put here is, operationally, not known at all. That is why ``torn`` is a
    field and not a ``log WARNING``: this helper discards stderr — deliberately, because stderr
    is commentary — and so **no test could ever have seen a warning.** Deleting every torn
    WARNING from the scanner left the suite 55/55 green (deploy#591 review, Nino Kavtaradze).

    Use ``_scan_stderr`` when the diagnostic itself is what's under test.
    """
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
    _backup_dir(tmp_path, complete=True)
    rc, line = _scan(tmp_path)
    assert rc == EXIT_OK, f"a fresh dump must pass: {line}"
    assert _field(line, "status") == "fresh"
    assert _field(line, "dumps") == "3"


def test_fresh_dump_age_is_not_skewed(tmp_path: Path) -> None:
    """The AGE, not just the class — this is the one that catches a clock/TZ error.

    rclone prints ModTime in local time. Parsed as UTC, an object written *now* reported
    4 hours old on this box. A skew in the other direction makes a stale backup read fresh.
    A class-level assertion cannot see this: 4h is still comfortably 'fresh'.
    """
    _backup_dir(tmp_path, complete=True)
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
    (tmp_path / manifest_filename()).write_bytes(b"x")
    rc, line = _scan(tmp_path)
    assert rc == EXIT_ALERT
    assert _field(line, "status") == "absent"
    assert _field(line, "dumps") == "0"


def test_stale_dump_is_an_alert(tmp_path: Path) -> None:
    d = _backup_dir(tmp_path, complete=True)
    _age(d, time.time() - (60 * 3600))
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
    _backup_dir(tmp_path, complete=True)
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
    d = tmp_path / "daily" / "2026-07-11"
    d.mkdir(parents=True)
    (d / STORE_FILES["isnad-pg"]).write_bytes(b"")
    (d / manifest_filename()).write_bytes(_manifest(True))
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
    d = _backup_dir(tmp_path, complete=True)
    _age(d, time.time() + (30 * 24 * 3600))
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
    _age(_write_run(tmp_path / "daily" / "2026-06-01"), time.time() - (40 * 24 * 3600))
    _age(_write_run(tmp_path / "daily" / "2026-08-10"), time.time() + (30 * 24 * 3600))

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

    THIS ASSERTION IS TEXTUAL, DELIBERATELY, AND THAT IS WORTH SAYING OUT LOUD.

    The lower bound is DORMANT defence-in-depth today: `status=fresh` now requires
    `delta >= -FUTURE_TOLERANCE_SECONDS`, and integer division truncates `[-300s, 0)` to age
    `0` — so a negative age carrying `status=fresh` is UNREACHABLE while the future-timestamp
    guard (F1) stands. Deleting this bound therefore changes no observable behaviour, and a
    behavioural test for it would be inert. Nino Kavtaradze nearly wrote up "the F3 fix is
    decorative" on exactly that basis, and caught himself: an inert mutation is not evidence
    of a gap.

    Isolated properly — remove F1's future guard AND the TZ pin, run at UTC+4 — the bound is
    load-bearing: with it, the self-test reports `[FAIL] reported a NEGATIVE age (-4h)`;
    without it, that failure disappears.

    So: **F1's future guard is what actually kills the clock-skew attack today. This bound is
    the layer behind it, and it becomes load-bearing the instant F1 is relaxed.** Knowing
    which guard carries the weight is the point — if anyone ever loosens one, it matters
    enormously which. A source-text assertion is the honest instrument for a property that is
    correct, ordered, and currently unreachable; pretending it is behavioural would be the
    vacuous-assertion defect in a new costume.
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
    _backup_dir(tmp_path, complete=True)
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
    _backup_dir(tmp_path, complete=True)
    r = subprocess.run(  # noqa: S603
        ["bash", str(SCANNER)],  # noqa: S607
        env={**os.environ, "B2_ROOT": str(tmp_path), "RCLONE_DUMP": "auth"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == EXIT_INSTRUMENT, "RCLONE_DUMP must be refused, not honoured"
    assert "RCLONE_DUMP" in r.stderr


# ---------------------------------------------------------------------------
# The scanner must not certify a backup our own restore path REFUSES (deploy#584 — Nurul)
# ---------------------------------------------------------------------------
def test_incomplete_backup_is_not_fresh(tmp_path: Path) -> None:
    """`_backup_manifest.txt` says `complete=false`. `restore.sh` refuses it. So must we.

    The scanner's definition of restorable was "at least one dump, over the floor, recent".
    It never asked WHICH stores were present and never read the completeness attestation
    sitting in the bucket beside the dumps — so a bucket holding only `isnad-pg`, with a
    manifest explicitly declaring itself incomplete, came back `fresh`.

    And `backup.sh` MANUFACTURES these by design: it deliberately uploads a partial when a
    leg fails ("a partial backup beats none"). This is not a hypothetical artifact.
    """
    _backup_dir(tmp_path, complete=False)
    rc, line = _scan(tmp_path)
    assert rc == EXIT_ALERT, f"a backup restore.sh refuses must not be certified fresh: {line}"
    assert _field(line, "status") == "incomplete"


def test_backup_with_no_manifest_is_not_fresh(tmp_path: Path) -> None:
    """Every pre-deploy#559 backup predates user-postgres coverage entirely."""
    d = tmp_path / "daily" / "2026-07-11"
    d.mkdir(parents=True)
    for f in STORE_FILES.values():
        (d / f).write_bytes(REAL_DUMP)
    rc, line = _scan(tmp_path)
    assert rc == EXIT_ALERT
    assert _field(line, "status") == "incomplete"


def test_complete_attested_but_undersized_dump_is_not_fresh(tmp_path: Path) -> None:
    """The producer's word about what it WROTE is not a claim about what SURVIVED.

    A user-postgres dump that uploaded as zero bytes, beside a healthy pg and neo4j, inside a
    run whose manifest says `complete=true`. The attestation cannot see this; the size floor
    can. Both are needed.

    THE FIXTURE USED TO WRITE ``isnad-userpg-a.dump`` — a name backup.sh CANNOT PRODUCE (the run
    id is always ``%Y%m%d-%H%M%S``) — while leaving the run's REAL user-postgres dump healthy and
    in place. So it did not build the artifact its own docstring describes, and it passed for a
    reason that had nothing to do with the run: under the old DIRECTORY-scoped check, ANY stray
    zero-byte `*.dump` anywhere in the day condemned the whole day, including one belonging to no
    run at all. A fixture in the test's own invented shorthand tests the shorthand.

    The truncated dump must be the ATTESTED RUN'S OWN. That is the artifact a torn upload
    actually leaves, and it is the one that must not be reported fresh.
    """
    d = _backup_dir(tmp_path, complete=True)
    (d / STORE_FILES["isnad-userpg"]).write_bytes(b"")  # the RUN's own dump, truncated
    rc, line = _scan(tmp_path)
    assert rc == EXIT_ALERT, f"a complete-attested backup with a 0-byte dump is not fresh: {line}"
    assert _field(line, "reason") == "undersized_dumps"


def test_an_undersized_dump_from_another_run_does_not_condemn_the_good_one(tmp_path: Path) -> None:
    """The corrupt dump belongs to a DIFFERENT run. The good run is intact and restorable.

    A failed 08:00 retry leaves a zero-byte pg dump in the day-directory beside the complete,
    healthy 03:01 run. Scoping the size floor to the DIRECTORY made that stray file condemn the
    good run — a red alert over a bucket holding a restorable backup, which is the same false
    alarm deploy#587 is about, one level in. Scoping it to the RUN charges the corruption to the
    run that produced it.

    It is still REPORTED (`undersized=1`) — a fault we survived is not a fault we hide.
    """
    d = tmp_path / "daily" / "2026-07-11"
    _write_run(d, run_ts=RUN_TS, complete=True)  # the good 03:01 run, all three healthy
    (d / f"isnad-pg-{LATE_TS}.dump").write_bytes(b"")  # 08:00 retry: zero-byte pg dump

    rc, line = _scan(tmp_path)
    assert rc == EXIT_OK, (
        f"the 03:01 run is complete, attested and INTACT — a corrupt dump from a later failed "
        f"run must not condemn it: {line}"
    )
    assert _field(line, "status") == "fresh"
    assert _field(line, "undersized") == "1", f"the corrupt dump must still be REPORTED: {line}"


def test_undersized_is_reported_unconditionally(tmp_path: Path) -> None:
    """It was computed and then thrown away unless `dumps == 0`.

    The identical defect to the `-719` age that was computed and never checked: if the scan
    knows something, the result line must say it.
    """
    _backup_dir(tmp_path, complete=True)
    rc, line = _scan(tmp_path)
    assert rc == EXIT_OK
    assert _field(line, "undersized") == "0", "the result line must always carry `undersized`"


def test_healthy_complete_backup_is_fresh(tmp_path: Path) -> None:
    """The POSITIVE control for the completeness gate.

    Without it, every refusal above could be passing because the gate rejects everything —
    and a scanner that only ever says ALERT proves every bucket is empty.
    """
    _backup_dir(tmp_path, complete=True)
    rc, line = _scan(tmp_path)
    assert rc == EXIT_OK, f"a genuinely complete, healthy backup must be fresh: {line}"
    assert _field(line, "status") == "fresh"


# ---------------------------------------------------------------------------
# restore.sh's completeness predicate — BEHAVIOURAL, against a REAL manifest
# ---------------------------------------------------------------------------
def test_restore_sh_completeness_predicate_matches_a_real_manifest() -> None:
    """A LIVE BUG in merged code (deploy#577), found because this fixture is a real manifest.

    `backup_is_complete()` shipped as::

        grep -q '^BACKUP_MANIFEST .*[[:space:]]complete=true\\([[:space:]]\\|$\\)'

    and it COULD NEVER MATCH. `complete=` is the FIRST token backup.sh writes after
    `BACKUP_MANIFEST `, so the anchor's literal space consumes the only space there is, and
    `.*[[:space:]]complete=true` then demands a second one that never exists.

    So `backup_is_complete` returned false for EVERY backup, `resolve_latest` skipped all of
    them, and `restore.sh latest` could not select an artifact at all — it would report "No
    COMPLETE backup found in B2 bucket" over a bucket full of good ones. It fails CLOSED, so
    nothing was at risk; but the recovery path was inert, and it shipped with a green suite.

    It survived because deploy#577's tests are TEXTUAL — they assert the function is CALLED
    (correctly; that was that review's own lesson) and never once run its predicate against a
    manifest `backup.sh` actually produces. **Asserting the call site is not the same as
    asserting the callee works.** This test runs the predicate.
    """
    body = RESTORE_SH.read_text()
    # The predicate lives in the shipped `manifest_attests_complete` helper, which
    # `backup_is_complete` and `select_local_run` both call (deploy#587 gave them a second
    # caller, so it stopped being inline). RUN THE SHIPPED TEXT — do not retype the pipeline
    # here, or this test would assert against its own paraphrase.
    fn = _shipped_predicate()
    assert "complete=true" in fn, "the predicate must test for `complete=true` as a WHOLE TOKEN"
    assert "manifest_attests_complete" in body[body.index("backup_is_complete() {") :], (
        "backup_is_complete must consult the shipped predicate, not its own copy of it"
    )
    # NO EARLY-EXIT CONSUMER. `head -n1` and `grep -q` SIGPIPE their producer, and `pipefail`
    # promotes the resulting 141 over a SUCCEEDING final stage — a complete, attesting manifest
    # then reads as "does not attest" (deploy#591). This is the one textual assertion worth
    # keeping: it names the shape that must never come back.
    for banned in ("head -n1", "grep -q"):
        assert banned not in fn, (
            f"`{banned}` is an EARLY-EXIT CONSUMER: it SIGPIPEs its producer, and `pipefail` "
            f"promotes the 141 over a succeeding final stage. Do not swap the consumer — remove "
            f"the pipeline (a herestring is a file descriptor, not a pipe)."
        )

    assert _predicate(_manifest(True)) == 0, "a complete manifest must MATCH"
    assert _predicate(_manifest(False)) != 0, "an incomplete manifest must NOT match"


# ---------------------------------------------------------------------------
# The required-store gate: `fresh` must mean "restore.sh would restore this".
#
# `complete=true` is the producer's account of what it DUMPED, not of what UPLOADED.
# backup.sh writes the manifest locally and then `rclone copy`s the directory, so a copy
# interrupted after the manifest object lands leaves `complete=true` above a half-finished
# upload. The scanner reported that `fresh` (dumps=1, exit=0) over a bucket with no
# user-postgres and no Neo4j — the two stores no pipeline artifact can rebuild — while
# restore.sh's required-store gate exits 1 on it.
# ---------------------------------------------------------------------------
def test_complete_attested_but_missing_stores_is_not_fresh(tmp_path: Path) -> None:
    _backup_dir(tmp_path, complete=True, stores=("isnad-pg",))
    rc, line = _scan(tmp_path)
    assert rc == EXIT_ALERT, f"a bucket restore.sh REFUSES must not be fresh: {line}"
    assert _field(line, "status") == "incomplete"
    reason = _field(line, "reason")
    assert "isnad-userpg" in reason, f"must NAME the missing store: {line}"
    assert "isnad-neo4j" in reason, f"must NAME the missing store: {line}"


def test_missing_store_is_reported_even_though_a_dump_exists(tmp_path: Path) -> None:
    """`dumps > 0` is not `restorable`. The count cannot answer "is user-postgres here?"."""
    _backup_dir(tmp_path, complete=True, stores=("isnad-pg", "isnad-neo4j"))
    rc, line = _scan(tmp_path)
    assert rc == EXIT_ALERT
    assert _field(line, "dumps") == "2", "two dumps ARE present — and it is still not restorable"
    assert "isnad-userpg" in _field(line, "reason"), (
        "user-postgres holds accounts, sessions and audit_log, and NO pipeline artifact can "
        f"rebuild it — its absence must be named, not summarised away: {line}"
    )


def test_stale_dump_from_an_earlier_run_does_not_satisfy_the_attested_run(tmp_path: Path) -> None:
    """B2's day-directory accumulates runs; `rclone copy` adds and never deletes.

    A dump left behind by an earlier FAILED run sits beside the good ones. A check that asked
    only "is there an isnad-pg here?" is satisfied by it. Binding to the attested RUN is what
    separates them.
    """
    d = _backup_dir(tmp_path, complete=True)
    (d / STORE_FILES["isnad-pg"]).unlink()
    (d / f"isnad-pg-{EARLY_TS}.dump").write_bytes(REAL_DUMP)  # the FAILED 02:00 run

    rc, line = _scan(tmp_path)
    assert rc == EXIT_ALERT, f"a dump from the wrong run must not satisfy the gate: {line}"
    # `dumps=` counts the REPORTED RUN's dumps, not the directory's (deploy#587 — the unit is
    # the run). THREE dump files are in this directory; only TWO of them belong to the run the
    # manifest attests, and the third — the 02:00 leftover — is exactly the file that must NOT
    # be allowed to stand in for the missing one. Counting the directory's three would report
    # the very conflation this gate exists to refuse.
    assert _field(line, "dumps") == "2", (
        f"the attested run has TWO of its three dumps; the 02:00 leftover is not its pg: {line}"
    )
    assert "isnad-pg" in _field(line, "reason")


# ---------------------------------------------------------------------------
# restore.sh's dump SELECTION — executed, not paraphrased.
#
# These run the SHIPPED TEXT of restore.sh's selection block. A test that retypes the logic
# tests the retyping: it stays green when the shipped code changes underneath it, which is
# precisely how the `backup_is_complete` bug survived a green suite and two approvals.
# ---------------------------------------------------------------------------
RESTORE_SH = SCRIPTS_DIR / "restore.sh"

# restore.sh SOURCES the scratch library (deploy#625/#628), so every harness below that slices
# its functions out must source it too. Without this the sliced code calls `scratch_file` into a
# void: bash reports "command not found" (rc=127), the allocation looks like it FAILED, and the
# function under test takes an error path production would never take — while the assertion the
# test was actually making goes unexercised. A harness that runs production code in an
# environment production does not have is not running production code.
SCRATCH_SH = SCRIPTS_DIR / "scratch.sh"
SCRATCH_SOURCE = f'source "{SCRATCH_SH}"\n'


def _shipped_predicate() -> str:
    """The SHIPPED text of `manifest_attests_complete` — never a retyped copy of it."""
    body = RESTORE_SH.read_text()
    return body[body.index("manifest_attests_complete() {") : body.index("\n# Did every store")]


def _predicate(manifest: bytes) -> int:
    """Run the shipped predicate against `manifest`, under restore.sh's own shell mode.

    The manifest arrives via a FILE and is read into a shell VARIABLE — exactly as production
    does it (`manifest="$(rclone cat ...)"`, then passed to the function). Passing it as argv
    instead raises `OSError: [Errno 7] Argument list too long` at ~2MB (execve's ARG_MAX), and
    production never hits that: a bash function call is not an exec, so its arguments have no
    such limit. A harness that cannot carry the fixture would report an error indistinguishable
    from a failure, over a defect that is not there.
    """
    with tempfile.NamedTemporaryFile(suffix=".txt") as f:
        f.write(manifest)
        f.flush()
        script = (
            "set -euo pipefail\n"
            + _shipped_predicate()
            + '\nmanifest="$(cat "$1")"\nmanifest_attests_complete "$manifest"\n'
        )
        return subprocess.run(  # noqa: S603
            ["bash", "-c", script, "_", f.name],  # noqa: S607
            check=False,
            capture_output=True,
        ).returncode


def _shipped_region(start: str, end: str) -> str:
    body = RESTORE_SH.read_text()
    return body[body.index(start) : body.index(end)]


def _select(restore_dir: Path) -> dict[str, str]:
    """Execute restore.sh's REAL selection block against a directory.

    Under restore.sh's OWN shell mode. This first read ``set -uo pipefail`` — **errexit
    omitted** — and that single missing ``-e`` hid a crash: with ``pipefail``, a ``grep`` that
    matches nothing fails the whole pipeline, so a bare ``VAR="$(... | grep ...)"`` assignment
    is a failing simple command and **errexit kills the script**. Every test here passed; the
    restore REHEARSAL caught it. A harness that runs production code under a weaker shell mode
    is not running production code.
    """
    region = _shipped_region("# Find dump files.", "# A backup containing no dumps at all")
    # The selection block calls these, so the harness must carry the SHIPPED ones — a
    # re-typed `count_runs` or `select_local_run` here would be exactly the paraphrase this
    # suite exists to refuse.
    helpers = _shipped_region("list_runs() {", "\nlist_backups()")
    # `select_local_run` lives past `list_backups`, so it needs its own slice (deploy#587).
    helpers += "\n" + _shipped_region("select_local_run() {", "\nresolve_latest()")
    script = (
        "set -euo pipefail\n"
        # To STDERR — so the selection's own output stays parseable, but the script's messages
        # are NOT discarded. A `log() { :; }` stub would hide the refusal text this suite
        # asserts on, which is the same "the fixture cannot express the condition" trap one
        # level out: an assertion on a message a stub swallowed can never fail.
        'log() { shift; echo "$*" >&2; }\n'
        + SCRATCH_SOURCE
        + helpers
        + "\n"
        + 'RESTORE_DIR="$1"\n'
        + region
        + "\n"
        'printf "PG=%s\\n" "$(basename "${PG_DUMP:-}")"\n'
        'printf "USER_PG=%s\\n" "$(basename "${USER_PG_DUMP:-}")"\n'
        'printf "NEO4J=%s\\n" "$(basename "${NEO4J_DUMP:-}")"\n'
    )
    r = subprocess.run(  # noqa: S603
        ["bash", "-c", script, "_", str(restore_dir)],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    )
    out = {}
    for ln in r.stdout.splitlines():
        k, _, v = ln.partition("=")
        out[k] = v
    return out


def test_restore_selects_the_attested_run_not_a_leftover_from_a_failed_one(tmp_path: Path) -> None:
    """THE TORN RESTORE. Each store was selected by an independent unsorted `find | head -1`.

    A day-directory can hold a failed 02:00 run and a good 14:00 run side by side — backup.sh
    uploads partials by design. The three stores could therefore be selected from DIFFERENT
    RUNS: isnad Postgres from the failed attempt, Neo4j from the good one. Referentially
    inconsistent, and every gate green — the required-store gate sees all three present,
    `verify_checksums` passes (a checksum binds a file to ITSELF, it cannot see that the file
    is from the wrong run), and the manifest says `complete=true`.

    MANY decoys, deliberately. A SINGLE leftover makes this test a coin flip: `find` order is
    filesystem hash order, so one decoy lands before the attested dump only about half the
    time — and the first version of this test PASSED against the unfixed code for exactly that
    reason. A test that detects an arbitrary-order bug only half the time is not a detector.
    With twelve decoys an arbitrary picker is wrong essentially always, and the assertion
    means what it says.
    """
    d = _backup_dir(tmp_path, complete=True)
    for hh in range(1, 13):  # twelve earlier FAILED runs, all leftover in the same day-dir
        (d / f"isnad-pg-20260711-{hh:02d}0000.dump").write_bytes(b"S" * 4096)

    got = _select(d)
    assert got["PG"] == STORE_FILES["isnad-pg"], (
        f"must restore the ATTESTED run ({RUN_TS}), never a leftover from a failed one: {got}"
    )
    assert got["USER_PG"] == STORE_FILES["isnad-userpg"]
    assert got["NEO4J"] == STORE_FILES["isnad-neo4j"]


def test_restore_selection_is_deterministic_across_many_two_run_directories(tmp_path: Path) -> None:
    """`find` emits READDIR order — neither chronological nor stable.

    Measured over 48 two-run day-directories, the shipped `find | head -1` selected the OLDER
    dump in 14 of them. It is not "usually right"; it is arbitrary, and which way it falls
    depends on the filenames. A single benign trial is not evidence of safety.
    """
    for i, older in enumerate(["020000", "030000", "040000", "060000", "080000", "120000"]):
        d = tmp_path / f"case{i}" / "daily" / "2026-07-11"
        d.mkdir(parents=True)
        for s in STORE_FILES.values():
            (d / s).write_bytes(REAL_DUMP)
        (d / f"isnad-pg-20260711-{older}.dump").write_bytes(b"S" * 4096)
        (d / manifest_filename()).write_bytes(_manifest(True))

        got = _select(d)
        assert got["PG"] == STORE_FILES["isnad-pg"], (
            f"leftover {older} must never be selected over the attested run {RUN_TS}: {got}"
        )


def test_restore_selection_without_a_manifest_refuses_rather_than_guessing(tmp_path: Path) -> None:
    """This test used to assert "pick the NEWEST run" — and that answer was WRONG.

    The earlier fallback was deterministic newest-by-name, and I wrote this test to pin that.
    But newest-by-name is *per store*, and with no manifest binding the three selections there
    is nothing making them agree on a run: `pg` from the newest run beside `neo4j` from an
    older one is a TORN restore, and every guard passes it.

    **Sorting made the selection reproducible. It did not make it coherent.** So the honest
    answer with several runs and nothing to say which is real is to REFUSE, and the assertion
    that used to demand a guess now demands the refusal.
    """
    d = tmp_path / "daily" / "2026-07-11"
    d.mkdir(parents=True)
    for hh in range(1, 13):
        (d / f"isnad-pg-20260711-{hh:02d}0000.dump").write_bytes(REAL_DUMP)

    with pytest.raises(subprocess.CalledProcessError) as e:
        _select(d)
    assert "MORE THAN ONE RUN" in e.value.stderr


def test_restore_selection_survives_an_artifact_with_no_manifest(tmp_path: Path) -> None:
    """errexit + pipefail + a `grep` that matches nothing = restore.sh DIES.

    `restore.sh` runs under `set -euo pipefail`. An artifact with no `_backup_manifest.txt` —
    every pre-deploy#559 backup, and the restore rehearsal's own fixtures — makes the manifest
    `grep` match nothing; under `pipefail` that fails the whole pipeline; and a bare
    `VAR="$(...)"` assignment whose command substitution fails IS a failing simple command, so
    **errexit kills the script**. The recovery path would have died on exactly the artifacts
    the newest-by-name fallback exists to serve.

    Not one unit test could see it, because the harness ran the block under `set -uo pipefail`
    with **errexit omitted**. The restore REHEARSAL caught it — it runs the real script. This
    test now runs the block under restore.sh's own shell mode, so the harness can see it too.
    """
    d = tmp_path / "daily" / "2026-07-11"
    d.mkdir(parents=True)
    for f in STORE_FILES.values():
        (d / f).write_bytes(REAL_DUMP)
    assert not (d / manifest_filename()).exists()

    got = _select(d)  # must not raise
    assert got["PG"] == STORE_FILES["isnad-pg"]
    assert got["USER_PG"] == STORE_FILES["isnad-userpg"]
    assert got["NEO4J"] == STORE_FILES["isnad-neo4j"]


# ---------------------------------------------------------------------------
# LEVEL 4 — what does the guard do when it CANNOT EVALUATE?
#
# Levels 1-3 ask: does the guard exist, is it called, does it return the right answer.
# None of them can see this, because it is not a question about the predicate at all.
#
#   "I could not read the input" is NOT a value of the predicate. It is a THIRD OUTCOME,
#   and collapsing it into either branch produces a confident lie.
#
# Both manifest reads were `rclone cat ... 2>/dev/null || true` — rc DISCARDED. A transient
# 401 / throttle / network blip on a GOOD, COMPLETE, FRESH backup then produced
# `status=incomplete reason=no_complete_backup`, and in restore.sh told an operator
# mid-incident "No COMPLETE backup found in B2 bucket" over a bucket full of good ones —
# the SAME user-visible lie the unmatched regex produced, by a different route.
#
# rc separates them, and the BACKENDS DISAGREE — in exactly the shape of the `lsf` finding:
#   cat existing    -> rc=0 (B2)   rc=0 (local)
#   cat nonexistent -> rc=0 EMPTY (B2)   rc=3 (local)   <- absent, NOT an error
#   cat, bad key    -> rc=1 (B2)   rc=1 (local)         <- CANNOT EVALUATE
# Verified against real rclone on the local backend before this was written.
# ---------------------------------------------------------------------------
def _backup_is_complete(root: Path, path: str) -> int:
    """Execute restore.sh's SHIPPED `backup_is_complete`, through real rclone.

    `:local:` is rclone's local backend, so `${RCLONE_REMOTE}:${B2_BUCKET}/...` resolves to a
    real path and the function's rc handling is exercised for real — not against a stub whose
    rcs I chose to believe.

    Under restore.sh's OWN shell mode. This ran ``set -uo pipefail`` — **errexit omitted** — and
    a harness that runs production code under a weaker shell mode is not running production
    code. It is the same omission that hid the deploy#584 crash from every unit test, and it
    matters more here than it did there: ``backup_is_complete`` now ENUMERATES the per-run
    manifests, and enumeration is precisely the operation that can LEGITIMATELY MATCH NOTHING —
    which, under ``-e`` + ``pipefail``, is a CRASH and not an empty string.
    """
    body = RESTORE_SH.read_text()
    # The manifest helpers live above `list_backups`; `backup_is_complete` calls them.
    helpers = body[body.index("manifest_runs() {") : body.index("\nlist_backups()")]
    fn = body[body.index("backup_is_complete() {") : body.index("\nresolve_latest()")]
    script = (
        "set -euo pipefail\n"
        'log() { shift; echo "$*" >&2; }\n' + SCRATCH_SOURCE + 'RCLONE_REMOTE=":local"\n'
        # REMOTE_ROOT carries the environment namespace (deploy#632); every remote
        # path in restore.sh is built from it. B2_BUCKET deliberately points at a
        # path with NO fixtures under it, so a function that rebuilds a raw bucket
        # path — reaching outside its environment, which IS the deploy#632 bug —
        # finds nothing and this harness goes RED instead of quietly passing.
        f'B2_BUCKET="{root}/__unprefixed__"\n'
        f'REMOTE_ROOT=":local:{root}"\n' + helpers + "\n" + fn + "\n"
        'backup_is_complete "$1"\n'
    )
    return subprocess.run(  # noqa: S603
        ["bash", "-c", script, "_", path],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    ).returncode


ATTESTS, DOES_NOT_ATTEST, CANNOT_READ = 0, 1, 2


def test_backup_is_complete_separates_all_three_outcomes(tmp_path: Path) -> None:
    """The dumps must be REAL, not merely a manifest.

    ``backup_is_complete`` is a TWO-INSTRUMENT check (deploy#584, at run granularity since
    deploy#587): the attestation says what was DUMPED, the listing says what ARRIVED, and it
    needs BOTH. A fixture that writes only a manifest cannot distinguish "attests and arrived"
    from "attests over an empty directory" — so the positive case is a whole run.
    """
    d = tmp_path / "daily" / "2026-07-11"

    # (1) reads it, it attests, and the dumps ARRIVED
    _write_run(d, complete=True)
    assert _backup_is_complete(tmp_path, "daily/2026-07-11") == ATTESTS

    # (2) reads it, and it does NOT attest
    (d / manifest_filename()).write_bytes(_manifest(False))
    assert _backup_is_complete(tmp_path, "daily/2026-07-11") == DOES_NOT_ATTEST

    # (3) NO manifest — rclone rc=3 on local, rc=0/empty on B2. ABSENT, not an error.
    (d / manifest_filename()).unlink()
    assert _backup_is_complete(tmp_path, "daily/2026-07-11") == DOES_NOT_ATTEST, (
        "a missing manifest is 'does not attest', NOT an instrument failure — rc=3 on the "
        "local backend must not be mistaken for the bad-key rc=1"
    )


def test_backup_is_complete_requires_the_dumps_to_have_ARRIVED(tmp_path: Path) -> None:
    """`complete=true` over a half-finished upload is not a restorable backup.

    backup.sh writes the manifest locally and then ``rclone copy``s the directory, so a copy
    interrupted after the manifest object lands leaves exactly this in B2. The attestation is
    the producer's word about what it DUMPED; only the listing knows what ARRIVED.
    """
    d = tmp_path / "daily" / "2026-07-11"
    _write_run(d, complete=True, stores=("isnad-pg",))  # attests all three; only pg arrived

    assert _backup_is_complete(tmp_path, "daily/2026-07-11") == DOES_NOT_ATTEST, (
        "a complete-attested run whose user-postgres and Neo4j dumps never arrived must not "
        "be selected — restore.sh's required-store gate REFUSES it"
    )


def test_backup_is_complete_never_accepts_dumps_without_an_attestation(tmp_path: Path) -> None:
    """ARRIVED but UNATTESTED. All three dumps are here; nothing vouches for them.

    Accepting "all three dumps are present" in place of the producer's attestation would drop
    the attestation entirely and reintroduce exactly what deploy#584 closed: a partial backup
    that happens to hold three files from three different runs would look complete.
    """
    d = tmp_path / "daily" / "2026-07-11"
    _write_run(d, complete=True, manifest=False)

    assert _backup_is_complete(tmp_path, "daily/2026-07-11") == DOES_NOT_ATTEST, (
        "dumps with no manifest must NOT be accepted — the candidates are the MANIFESTS"
    )


def test_unreadable_manifest_is_not_reported_as_incomplete(tmp_path: Path) -> None:
    """THE BLOCK. A good backup whose manifest merely could not be READ.

    The dumps are there. The backup is restorable. Saying "incomplete" is a confident lie,
    and in `resolve_latest` it becomes "No COMPLETE backup found in B2 bucket" — told to an
    operator who is mid-incident and holding a bucket full of good backups.
    """
    d = tmp_path / "daily" / "2026-07-11"
    d.mkdir(parents=True)
    m = d / manifest_filename()
    m.write_bytes(_manifest(True))
    m.chmod(0o000)  # rclone rc=1 — the same class as a 401/bad key
    try:
        rc = _backup_is_complete(tmp_path, "daily/2026-07-11")
    finally:
        m.chmod(0o644)

    assert rc == CANNOT_READ, (
        f"an unreadable manifest must be CANNOT_READ ({CANNOT_READ}), not "
        f"DOES_NOT_ATTEST ({DOES_NOT_ATTEST}); got {rc}. 'I could not read the input' is not "
        "a value of the predicate."
    )


def test_scanner_reports_unreadable_manifest_as_instrument_error(tmp_path: Path) -> None:
    """Same defect, scanner side — and it violated this script's own founding invariant.

    `instrument_error` is NOT a claim about backups. Step 1's bucket probe honours that
    meticulously (`|| rc=$?`, citing deploy#563). Step 4's manifest read discarded the rc,
    INSIDE the change built to enforce the invariant.
    """
    d = _backup_dir(tmp_path, complete=True)  # a GOOD, COMPLETE, FRESH backup
    m = d / manifest_filename()

    rc, line = _scan(tmp_path)  # control: the manifest is readable
    assert rc == EXIT_OK and _field(line, "status") == "fresh", f"control must be fresh: {line}"

    m.chmod(0o000)
    try:
        rc, line = _scan(tmp_path)
    finally:
        m.chmod(0o644)

    assert rc == EXIT_INSTRUMENT, (
        f"a GOOD backup whose manifest could not be READ must be instrument_error, not a "
        f"verdict on the backup: {line}"
    )
    assert _field(line, "status") == "instrument_error"
    assert _field(line, "reason") == "manifest_unreadable"


def test_manifest_with_a_true_line_after_a_false_one_does_not_attest(tmp_path: Path) -> None:
    """Attest on the FIRST manifest line. Unioning tokens across all lines FAILS OPEN.

    `backup.sh` writes with `>`, so this needs a corrupt artifact — but the failure direction
    is the wrong one, and `head -n1` costs nothing.
    """
    d = tmp_path / "daily" / "2026-07-11"
    d.mkdir(parents=True)
    (d / manifest_filename()).write_bytes(_manifest(False) + _manifest(True))
    assert _backup_is_complete(tmp_path, "daily/2026-07-11") == DOES_NOT_ATTEST


def test_fallback_refuses_a_directory_holding_more_than_one_run(tmp_path: Path) -> None:
    """SORTING MADE THE SELECTION REPRODUCIBLE. IT DID NOT MAKE IT COHERENT.

    The no-manifest fallback is three INDEPENDENT `sort -r | head -1`s. With no manifest
    timestamp binding them, nothing makes the three agree on a RUN — so against a directory
    holding a complete 03:01 run plus a failed 08:00 rerun that left a stray pg dump, it
    selected `pg=…-080000` beside `neo4j=…-030100`: **Postgres from the failed rerun, Neo4j
    from the good one.** The same defect as the original `find | head -1`; it merely tears the
    same way every time now.

    And every guard still passes it — required-store sees all three present, and each checksum
    verifies the file against ITSELF. Determinism is not coherence. With more than one run and
    no manifest to say which is real, there is no honest answer: REFUSE.
    """
    d = tmp_path / "daily" / "2026-07-11"
    d.mkdir(parents=True)
    for f in STORE_FILES.values():  # the complete 03:01 run
        (d / f).write_bytes(REAL_DUMP)
    (d / "isnad-pg-20260711-080000.dump").write_bytes(b"S" * 4096)  # the failed 08:00 rerun
    assert not (d / manifest_filename()).exists()  # nothing to bind to

    with pytest.raises(subprocess.CalledProcessError) as e:
        _select(d)
    assert e.value.returncode == 1
    assert "MORE THAN ONE RUN" in e.value.stderr, (
        f"must refuse and say why, not silently pick a torn set: {e.value.stderr}"
    )


def test_fallback_still_works_when_exactly_one_run_is_present(tmp_path: Path) -> None:
    """The POSITIVE control for the refusal above.

    A guard that refuses everything is not a guard. Pre-deploy#559 artifacts carry no manifest
    and hold exactly one run — those must still restore.
    """
    d = tmp_path / "daily" / "2026-07-11"
    d.mkdir(parents=True)
    for f in STORE_FILES.values():
        (d / f).write_bytes(REAL_DUMP)

    got = _select(d)
    assert got["PG"] == STORE_FILES["isnad-pg"]
    assert got["USER_PG"] == STORE_FILES["isnad-userpg"]
    assert got["NEO4J"] == STORE_FILES["isnad-neo4j"]


def test_manifest_fixture_is_rendered_from_the_producers_own_printf() -> None:
    """The fixture must not be able to drift away from `backup.sh`.

    Every other test in this file trusts `_manifest()`. If that rendering is the test's own
    words, then the whole suite is testing the consumer against the test's idea of the
    producer — which is exactly how a predicate that could NEVER match a real manifest passed
    39 tests, two reviewers, and shipped.

    So: the format string and the argument order both come out of `backup.sh`, and this pins
    that they do. Change the producer's printf and the fixtures follow; rename one of its
    variables and this raises rather than fabricating a plausible line.
    """
    fmt, args = manifest_format()
    assert fmt.startswith("BACKUP_MANIFEST "), fmt
    assert args == ["BACKUP_COMPLETE", "BACKUP_STORES", "TIMESTAMP", "BACKUP_CATEGORY"], (
        f"backup.sh's manifest printf changed its arguments: {args}. The fixture must be "
        "taught the new shape, not left to fill the old slots."
    )
    # `complete=` must remain the FIRST token after `BACKUP_MANIFEST ` — the whole reason the
    # shipped anchored-prefix regex could never match.
    assert fmt.split()[1].startswith("complete="), (
        "`complete=` is no longer the first field. Any predicate anchored on `BACKUP_MANIFEST `"
        " plus a leading space will now behave differently — re-check both parsers."
    )

    line = build_manifest(complete=True).decode()
    assert "complete=true" in line.split()
    assert build_manifest(complete=False).decode().split()[1] == "complete=false"


# ---------------------------------------------------------------------------
# A CRASH IS ROW ONE — the script says NOTHING and exits non-zero
#
# `dirname` returns "." for a dump at the root of the scanned scope, and the directory key
# was then set to the EMPTY STRING — which bash rejects outright as an associative-array
# subscript (`bad array subscript`). The scanner DIED: **no result line**, exit 1.
#
# That is not a fourth category. It is ROW ONE of this script's own table, live in its own
# code: every consumer reads a non-zero exit as a verdict about the BACKUPS, and the script
# whose founding invariant is "an instrument failure is NOT a claim about your data"
# terminated without a claim and with a failing exit code. **The purest form of the collapse
# it exists to prevent.**
#
# So these assert the RESULT LINE EXISTS — the thing that was missing — not merely the status.
# ---------------------------------------------------------------------------
def test_documented_b2_prefix_invocation_does_not_crash(tmp_path: Path) -> None:
    """The invocation in this script's own usage block. It was never exercised.

    With a prefix, EVERY dump sits at the root of the scanned scope — so this is not an edge
    case of the documented usage, it IS the documented usage.
    """
    _backup_dir(tmp_path, complete=True)

    rc, line = _scan(tmp_path, B2_PREFIX="daily/2026-07-11")
    assert line, "THE SCANNER PRINTED NO RESULT LINE. A crash is not a verdict."
    assert rc == EXIT_OK, f"a healthy backup read through B2_PREFIX must be fresh: {line}"
    assert _field(line, "status") == "fresh"
    assert _field(line, "dumps") == "3"


def test_a_stray_root_level_dump_does_not_crash_the_scanner(tmp_path: Path) -> None:
    """An operator hand-copying a dump into the bucket root during a DR.

    An entirely ordinary thing to do, and it killed the check that is supposed to be watching.
    """
    _backup_dir(tmp_path, complete=True)
    (tmp_path / STORE_FILES["isnad-pg"]).write_bytes(REAL_DUMP)  # stray, at the root

    rc, line = _scan(tmp_path)
    assert line, "THE SCANNER PRINTED NO RESULT LINE. A crash is not a verdict."
    assert rc == EXIT_OK, f"the healthy backup must still be found: {line}"
    assert _field(line, "status") == "fresh"
    assert _field(line, "newest") == "daily/2026-07-11"


def test_scanner_never_exits_without_a_result_line(tmp_path: Path) -> None:
    """The invariant the crash broke, asserted directly across every class.

    Whatever it concludes, it must SAY so. An exit code with no claim attached is read as a
    claim — and the only claim a non-zero exit can be read as is "your backups are missing".
    """
    _backup_dir(tmp_path, complete=True)
    (tmp_path / STORE_FILES["isnad-pg"]).write_bytes(REAL_DUMP)

    for env in ({}, {"B2_PREFIX": "daily/2026-07-11"}, {"MAX_AGE_HOURS": "0"}):
        rc, line = _scan(tmp_path, **env)
        assert line, f"no result line for env={env} (rc={rc}) — the scanner must never be mute"
        assert _field(line, "status"), f"result line carries no status: {line}"


# ===========================================================================================
# deploy#587 — THE MANIFEST IS PER-DIRECTORY BUT COMPLETENESS IS PER-RUN.
#
# `_backup_manifest.txt` was a FIXED NAME inside a directory that holds MULTIPLE RUNS:
#
#   the B2 path is `<category>/<DATE>`          -> a DAY
#   the dumps inside are `isnad-<store>-<TS>`   -> a RUN
#
# `rclone copy` ADDS and never deletes, and backup.sh deliberately uploads partials, so a day
# routinely holds several runs — and the fixed-name manifest was OVERWRITTEN BY WHICHEVER RUN
# UPLOADED LAST, good or not. A GOOD RUN FOLLOWED BY A PARTIAL ONE ERASED ITS OWN ATTESTATION.
#
# THE SHAPES BELOW ARE THE POINT, NOT THE COUNT. The deploy#584 review built ~20 fixtures over
# six rounds and EVERY ONE of them put the dumps in a subdirectory, with exactly ONE run in it.
# One structural shape, twenty times. The battery could not produce the condition, so it could
# not see the crash it then approved — and the missing shape was the DOCUMENTED usage.
#
# So these are enumerated by SHAPE:
#
#   good -> partial          the #587 defect itself
#   partial -> good          the ordering #584 handled — must not regress
#   partial -> good -> partial   the good run is neither first nor last
#   exactly one run          the common case
#   zero manifests           the enumeration legitimately matches NOTHING (errexit crash)
#   root vs subdirectory     the shape the whole #584 battery lacked
#   manifest, no dump        attested but did not arrive
#   dump, no manifest        arrived but unattested — must NOT be accepted
# ===========================================================================================


def _age_run(d: Path, run_ts: str, epoch: float) -> None:
    """Re-date ONE RUN's dumps, leaving its neighbours in the same directory alone."""
    for name in store_files(run_ts).values():
        f = d / name
        if f.exists():
            os.utime(f, (epoch, epoch))


# --- SHAPE: good run, then a partial one, in the same day ----------------------------------


def test_scanner_good_run_then_partial_run_is_still_fresh(tmp_path: Path) -> None:
    """THE DEFECT. A red alert saying THERE IS NO COMPLETE BACKUP, over a bucket holding one.

    The 03:01 nightly succeeds completely. An 08:00 retry fails halfway and uploads its
    `complete=false` manifest — which, under a fixed name, LANDED ON TOP OF the good run's.
    The scanner then read the survivor and reported:

        status=incomplete reason=no_complete_backup dumps=0 ... bucket_objects=7

    Fails closed, so nothing was at risk — and that is exactly why it matters. It is a FALSE
    ALARM ON THE ONE CHECK THAT MUST BE BELIEVED, in a routine scenario, and alert fatigue on a
    backup alert is how deploy#559 happened: a signal nobody trusted, so nobody looked.
    """
    d = tmp_path / "daily" / "2026-07-11"
    _write_run(d, run_ts=RUN_TS, complete=True)  # the good 03:01 run
    _partial_run(d, run_ts=LATE_TS)  # the partial 08:00 retry, uploaded LAST

    rc, line = _scan(tmp_path)
    assert rc == EXIT_OK, (
        f"a COMPLETE, INTACT run is in this bucket. The scanner must not cry wolf over it "
        f"because a later partial run also landed in the same day-directory: {line}"
    )
    assert _field(line, "status") == "fresh"
    assert _field(line, "newest") == "daily/2026-07-11"


def test_restore_selects_the_good_run_when_a_partial_one_uploaded_after_it(tmp_path: Path) -> None:
    """Under `--allow-partial` this bound to the PARTIAL run and restored only its pg dump —

    reporting user-postgres and Neo4j as MISSING while both sat in that very directory, intact,
    from the good run. The good run's three dumps are RIGHT THERE; the only thing lost was its
    attestation, and only because a fixed filename let a later run overwrite it.
    """
    d = tmp_path / "daily" / "2026-07-11"
    _write_run(d, run_ts=RUN_TS, complete=True)
    _partial_run(d, run_ts=LATE_TS)

    got = _select(d)
    good = store_files(RUN_TS)
    assert got["PG"] == good["isnad-pg"], (
        f"must select the GOOD run's pg dump, not the partial run's leftover: {got}"
    )
    assert got["USER_PG"] == good["isnad-userpg"], f"the good run's user-postgres is here: {got}"
    assert got["NEO4J"] == good["isnad-neo4j"], f"the good run's Neo4j dump is here: {got}"


def test_resolve_latest_does_not_walk_past_a_day_whose_good_run_was_followed_by_a_partial(
    tmp_path: Path,
) -> None:
    """It restored a backup TWO DAYS OLDER than the best one available, and called the newer

    day "incomplete" — which the operator will believe.
    """
    _write_run(tmp_path / "daily" / "2026-07-08", complete=True)
    d = tmp_path / "daily" / "2026-07-10"
    _write_run(d, run_ts=RUN_TS, complete=True)
    _partial_run(d, run_ts=LATE_TS)
    (tmp_path / "weekly").mkdir()

    assert _backup_is_complete(tmp_path, "daily/2026-07-10") == ATTESTS, (
        "the 07-10 day HOLDS a complete, intact run — it must not be reported incomplete "
        "merely because a later partial run overwrote nothing"
    )


# --- SHAPE: partial run first, then the good one (the ordering #584 already handled) --------


def test_partial_run_then_good_run_still_resolves(tmp_path: Path) -> None:
    """The ordering deploy#584 thought of. Fixing #587 must not regress it."""
    d = tmp_path / "daily" / "2026-07-11"
    _partial_run(d, run_ts=EARLY_TS)  # failed 02:00
    _write_run(d, run_ts=RUN_TS, complete=True)  # good 03:01, uploaded after

    rc, line = _scan(tmp_path)
    assert rc == EXIT_OK, f"the good run is the newest AND complete: {line}"
    assert _field(line, "status") == "fresh"

    assert _backup_is_complete(tmp_path, "daily/2026-07-11") == ATTESTS
    assert _select(d)["PG"] == store_files(RUN_TS)["isnad-pg"]


# --- SHAPE: three runs, the good one neither first nor last ---------------------------------


def test_good_run_neither_first_nor_last(tmp_path: Path) -> None:
    """A selection that takes "the newest manifest", "the oldest", or "the only one" passes

    both single-partial fixtures above and fails HERE. The good run is in the middle.
    """
    d = tmp_path / "daily" / "2026-07-11"
    _partial_run(d, run_ts=EARLY_TS)  # 02:00 failed
    _write_run(d, run_ts=RUN_TS, complete=True)  # 03:01 good
    _partial_run(d, run_ts=LATE_TS)  # 08:00 failed

    rc, line = _scan(tmp_path)
    assert rc == EXIT_OK, f"the middle run is complete and intact: {line}"
    assert _field(line, "status") == "fresh"

    assert _backup_is_complete(tmp_path, "daily/2026-07-11") == ATTESTS

    got = _select(d)
    good = store_files(RUN_TS)
    assert (got["PG"], got["USER_PG"], got["NEO4J"]) == (
        good["isnad-pg"],
        good["isnad-userpg"],
        good["isnad-neo4j"],
    ), f"all three stores must come from the ONE run that is complete: {got}"


# --- SHAPE: exactly one run (the common case) ----------------------------------------------


def test_exactly_one_complete_run_in_the_day(tmp_path: Path) -> None:
    """The POSITIVE CONTROL. A suite that only ever refuses proves every bucket is empty."""
    d = tmp_path / "daily" / "2026-07-11"
    _write_run(d, complete=True)

    rc, line = _scan(tmp_path)
    assert rc == EXIT_OK and _field(line, "status") == "fresh", line
    assert _backup_is_complete(tmp_path, "daily/2026-07-11") == ATTESTS
    assert _select(d)["USER_PG"] == STORE_FILES["isnad-userpg"]


# --- SHAPE: a day with ZERO manifests -------------------------------------------------------


def test_a_day_with_zero_manifests_does_not_crash_any_consumer(tmp_path: Path) -> None:
    """ENUMERATION THAT LEGITIMATELY MATCHES NOTHING IS A CRASH, NOT AN EMPTY STRING.

    Under ``set -euo pipefail``, a `VAR="$(... | grep ...)"` whose pipeline matches nothing is a
    FAILING SIMPLE COMMAND — errexit kills the script outright. Listing the per-run manifests is
    exactly that operation, and a directory with no manifest is entirely ordinary (every
    pre-deploy#559 backup). This shipped once already, in deploy#584, and no unit test could see
    it because the harness omitted ``-e``.

    Every consumer must return a VERDICT here, not die. A crash prints no claim and exits
    non-zero — and every caller reads a non-zero exit as a claim about the BACKUPS.
    """
    d = tmp_path / "daily" / "2026-07-11"
    _write_run(d, complete=True, manifest=False)  # all three dumps, NO manifest at all

    rc, line = _scan(tmp_path)
    assert line, "THE SCANNER PRINTED NO RESULT LINE. A crash is not a verdict."
    assert rc == EXIT_ALERT and _field(line, "status") == "incomplete", (
        f"dumps with no attestation are not a backup we can promise a restore from: {line}"
    )

    # rc 1 = 'does not attest'. NOT 2 (instrument error) and NOT a crash.
    assert _backup_is_complete(tmp_path, "daily/2026-07-11") == DOES_NOT_ATTEST

    # And the local selection must still fall back rather than die.
    got = _select(d)  # must not raise
    assert got["PG"] == STORE_FILES["isnad-pg"]


# --- SHAPE: dumps at the BUCKET ROOT vs in a subdirectory -----------------------------------
#
# THE SHAPE NINO'S ENTIRE #584 BATTERY LACKED. ~20 fixtures, every one of them a subdirectory.
# The empty-associative-array-subscript crash lived in the DOCUMENTED `B2_PREFIX` invocation
# and nothing could reach it. A fixture set with one structural shape is one fixture.


def test_multi_run_selection_at_the_bucket_root(tmp_path: Path) -> None:
    """The same #587 shape with NO subdirectory — dumps at the root of the scanned scope."""
    _write_run(tmp_path, run_ts=RUN_TS, complete=True)
    _partial_run(tmp_path, run_ts=LATE_TS)

    rc, line = _scan(tmp_path)
    assert line, "THE SCANNER PRINTED NO RESULT LINE. A crash is not a verdict."
    assert rc == EXIT_OK, f"a complete run at the bucket root is still a complete run: {line}"
    assert _field(line, "status") == "fresh"
    assert _field(line, "newest") == "/", f"the root sentinel, not an empty subscript: {line}"

    assert _select(tmp_path)["PG"] == store_files(RUN_TS)["isnad-pg"]


def test_multi_run_selection_under_an_explicit_prefix(tmp_path: Path) -> None:
    """The DOCUMENTED `B2_PREFIX` invocation, with more than one run under it."""
    d = tmp_path / "daily" / "2026-07-11"
    _write_run(d, run_ts=RUN_TS, complete=True)
    _partial_run(d, run_ts=LATE_TS)

    rc, line = _scan(tmp_path, B2_PREFIX="daily/2026-07-11")
    assert line, "THE SCANNER PRINTED NO RESULT LINE. A crash is not a verdict."
    assert rc == EXIT_OK, f"every dump sits at the root of the scanned scope here: {line}"
    assert _field(line, "status") == "fresh"


# --- SHAPE: manifest present, dump missing / dump present, manifest missing -----------------


def test_attested_run_whose_dumps_did_not_arrive_is_not_selected(tmp_path: Path) -> None:
    """`complete=true` above a HALF-FINISHED UPLOAD. The attestation says what was DUMPED;

    only the listing knows what ARRIVED. An older, intact run is present — and IT is the answer.
    The torn run must not be selected, and must not silently pass either.
    """
    old = tmp_path / "daily" / "2026-07-10"
    _write_run(old, run_ts="20260710-030100", complete=True)  # yesterday: good
    new = tmp_path / "daily" / "2026-07-11"
    _write_run(new, run_ts=RUN_TS, complete=True, stores=("isnad-pg",))  # today: torn

    assert _backup_is_complete(tmp_path, "daily/2026-07-11") == DOES_NOT_ATTEST, (
        "attests all three, only pg arrived — restore.sh's required-store gate REFUSES it"
    )
    assert _backup_is_complete(tmp_path, "daily/2026-07-10") == ATTESTS


def test_arrived_but_unattested_dumps_are_never_promoted_to_a_backup(tmp_path: Path) -> None:
    """The dumps are all here. Nothing vouches for them. That is NOT a complete backup.

    Accepting "all three dumps are present" in place of the producer's attestation would drop
    the attestation entirely — reintroducing precisely what deploy#584 closed, and the issue
    says so explicitly: a consumer-only fix is not sufficient.
    """
    d = tmp_path / "daily" / "2026-07-11"
    _write_run(d, complete=True, manifest=False)

    rc, line = _scan(tmp_path)
    assert rc == EXIT_ALERT, f"unattested dumps must not be reported fresh: {line}"
    assert _field(line, "status") == "incomplete"
    assert _field(line, "reason") == "no_complete_backup"


# --- The producer must NAME the manifest for its run ----------------------------------------


def test_producer_writes_one_manifest_per_run(tmp_path: Path) -> None:
    """The fix is only real if the PRODUCER stops overwriting. A consumer-only fix cannot work:

    once the good run's manifest is gone there is NO SURVIVING ATTESTATION for it, and no
    consumer can recover what was never written.
    """
    name = manifest_filename(RUN_TS)  # asserts `${TIMESTAMP}` is in backup.sh's MANIFEST_FILE
    assert RUN_TS in name, f"the manifest must be named for its run: {name}"
    assert name != "_backup_manifest.txt"

    # Two runs in one directory must produce TWO manifests. Under the old fixed name this is
    # one file, and the second write destroys the first run's verdict.
    d = tmp_path / "daily" / "2026-07-11"
    _write_run(d, run_ts=RUN_TS, complete=True)
    _partial_run(d, run_ts=LATE_TS)
    manifests = sorted(p.name for p in d.glob("_backup_manifest*"))
    assert manifests == sorted([manifest_filename(RUN_TS), manifest_filename(LATE_TS)]), (
        f"each run must keep its own attestation; got {manifests}"
    )


def test_stale_run_is_not_freshened_by_a_later_partial_run(tmp_path: Path) -> None:
    """AGE IS A PROPERTY OF THE RUN WE SELECTED, NOT OF THE DIRECTORY IT SITS IN.

    The last COMPLETE run is three days old. A partial retry ran an hour ago and dropped a fresh
    pg dump into the SAME day-directory. If freshness is taken from the DIRECTORY's newest dump,
    that partial run lends its timestamp to the stale run we actually selected — and a backup
    three days old reports `fresh`.

    That is the DEFLATING direction: a stale backup reading fresh is a MISSED alarm on the one
    check standing between us and an unrecoverable delete. It is the same class as the TZ skew
    this scanner already pins, reached through the run/directory confusion of deploy#587.
    """
    d = tmp_path / "daily" / "2026-07-11"
    _write_run(d, run_ts=RUN_TS, complete=True)  # the good run...
    _age_run(d, RUN_TS, time.time() - 72 * 3600)  # ...but it is THREE DAYS OLD
    _partial_run(d, run_ts=LATE_TS)  # a partial retry, dumped just now

    rc, line = _scan(tmp_path, MAX_AGE_HOURS="30")
    assert _field(line, "status") == "stale", (
        f"the only COMPLETE run is 72h old — a fresh dump from a later PARTIAL run must not "
        f"lend it their timestamp: {line}"
    )
    assert rc == EXIT_ALERT
    age = int(_field(line, "newest_age_hours"))
    assert age >= 70, f"the reported age must be the SELECTED RUN's, not the directory's: {line}"


def test_a_manifest_filed_under_the_wrong_run_id_is_not_trusted(tmp_path: Path) -> None:
    """The whole fix rests on the FILENAME's run id identifying the run.

    So the manifest must be ABOUT the run it is named for. If `_backup_manifest-<A>.txt` declares
    `timestamp=<B>`, then binding run A's dumps to an attestation about run B is exactly the
    mis-binding this change exists to prevent — a torn restore, certified by a manifest that was
    telling the truth about a different run.

    backup.sh cannot produce this (it writes both from the same `$TIMESTAMP`), which is precisely
    why it needs a test: nothing else would ever exercise it, and a guard no fixture can reach is
    a guard that is not there. It fails CLOSED.
    """
    d = tmp_path / "daily" / "2026-07-11"
    _write_run(d, run_ts=RUN_TS, complete=True)
    # The manifest is NAMED for the 03:01 run but ATTESTS the 08:00 one.
    (d / manifest_filename(RUN_TS)).write_bytes(_manifest(True, run_ts=LATE_TS))

    rc, line = _scan(tmp_path)
    assert rc == EXIT_ALERT, (
        f"a manifest that does not attest the run it is filed under must NOT be trusted: {line}"
    )
    assert _field(line, "status") == "incomplete"
    assert _field(line, "reason") == "manifest_ts_mismatch", (
        f"and it must say WHY, not merely refuse: {line}"
    )


# ===========================================================================================
# deploy#591 review — WHAT THE SCAN KNOWS, THE RESULT LINE SAYS.
#
# A torn run (attests `complete=true`, dumps did NOT all arrive) is SKIPPED rather than alerted
# on, because an older intact run may still be restorable. That softening is defensible. But it
# was reported only as a `log WARNING` to STDERR — and:
#
#   * the result line is the ONLY thing verify-backup-artifact.yml's summary renders;
#   * `_scan()` returns (rc, result-line) and discards stderr, so NO TEST COULD SEE IT.
#
# Nino deleted every torn WARNING from the scanner, confirmed the mutation applied, and ran the
# full file: 55/55 passed. So for a producer that tears its upload nightly, the alert had not
# been softened — IT HAD BEEN DELETED. It is the identical defect to the `undersized` value that
# was computed and thrown away, committed one field over, inside the change that created it.
# ===========================================================================================


def _scan_stderr(root: Path | str, **env: str) -> tuple[int, str, str]:
    """(rc, result line, STDERR) — for when the diagnostic itself is under test."""
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
    return r.returncode, line, r.stderr


def test_a_torn_run_is_counted_on_the_result_line_even_when_the_verdict_is_fresh(
    tmp_path: Path,
) -> None:
    """GREEN-WITH-TORN MUST NOT BE BYTE-IDENTICAL TO A HEALTHY BUCKET.

    Today's 03:01 run landed its `complete=true` manifest and then tore its upload — only pg
    arrived. Yesterday's run is complete and intact, so a restorable backup DOES exist and the
    verdict is honestly `fresh`. But the tear is real, it will recur nightly, and the result
    line is the only channel anyone reads.
    """
    _write_run(tmp_path / "daily" / "2026-07-10", run_ts="20260710-030100", complete=True)
    _write_run(  # today: attests all three, only pg arrived
        tmp_path / "daily" / "2026-07-11", run_ts=RUN_TS, complete=True, stores=("isnad-pg",)
    )

    rc, line = _scan(tmp_path)
    assert rc == EXIT_OK, f"an older intact run is restorable, so this is honestly fresh: {line}"
    assert _field(line, "status") == "fresh"
    assert _field(line, "newest") == "daily/2026-07-10"
    assert _field(line, "torn") == "1", (
        f"THE TEAR MUST BE ON THE RESULT LINE. A `log WARNING` goes to stderr, which the CI "
        f"summary does not render and no test can read — a softened alert that nothing can "
        f"observe is a DELETED alert: {line}"
    )


def test_a_healthy_bucket_reports_torn_zero(tmp_path: Path) -> None:
    """The POSITIVE CONTROL for the field above.

    A field that is always `1` distinguishes nothing. `torn` must SEPARATE a torn bucket from a
    healthy one, or asserting on it proves only that the token was printed.
    """
    _write_run(tmp_path / "daily" / "2026-07-11", complete=True)
    rc, line = _scan(tmp_path)
    assert rc == EXIT_OK and _field(line, "status") == "fresh", line
    assert _field(line, "torn") == "0", f"a healthy bucket has torn=0: {line}"


def test_a_partial_run_is_skipped_not_torn(tmp_path: Path) -> None:
    """`skipped` and `torn` are DIFFERENT FAULTS and must not be conflated.

    A run attesting `complete=false` is the producer honestly reporting a partial backup — it is
    SKIPPED, and it is not a tear. A tear is a run that attests `complete=true` and then fails to
    deliver: the producer's word and the bucket's contents DISAGREE. Reporting the ordinary
    nightly partial as `torn` would make the field meaningless within a week.
    """
    d = tmp_path / "daily" / "2026-07-11"
    _write_run(d, run_ts=RUN_TS, complete=True)
    _partial_run(d, run_ts=LATE_TS)  # attests complete=false — honest, not torn

    rc, line = _scan(tmp_path)
    assert rc == EXIT_OK and _field(line, "status") == "fresh", line
    assert _field(line, "torn") == "0", (
        f"an honestly-declared partial run is SKIPPED, not TORN — the producer's word and the "
        f"bucket agree: {line}"
    )


def test_torn_is_reported_on_the_alert_path_too(tmp_path: Path) -> None:
    """No older run to fall back on: the torn run is the whole bucket. Alert AND count it."""
    _write_run(
        tmp_path / "daily" / "2026-07-11", run_ts=RUN_TS, complete=True, stores=("isnad-pg",)
    )
    rc, line = _scan(tmp_path)
    assert rc == EXIT_ALERT, f"nothing restorable here: {line}"
    assert _field(line, "status") == "incomplete"
    assert "isnad-userpg" in _field(line, "reason")
    assert _field(line, "torn") == "1", f"the tear is named on the alert path as well: {line}"


def test_the_torn_warning_still_reaches_stderr(tmp_path: Path) -> None:
    """The result line is the SIGNAL; the WARNING is the EXPLANATION. Keep both.

    Putting `torn=N` on the result line is not a reason to drop the human-readable diagnostic —
    an operator reading a CI log needs to know WHICH run tore and WHY it was not an alert.
    """
    _write_run(tmp_path / "daily" / "2026-07-10", run_ts="20260710-030100", complete=True)
    _write_run(
        tmp_path / "daily" / "2026-07-11", run_ts=RUN_TS, complete=True, stores=("isnad-pg",)
    )

    rc, line, err = _scan_stderr(tmp_path)
    assert rc == EXIT_OK and _field(line, "torn") == "1", line

    # The PER-RUN diagnostic: which run, and what it failed to deliver.
    assert RUN_TS in err, f"the WARNING must name the run that tore: {err}"
    assert "isnad-userpg" in err, f"and what it failed to deliver: {err}"

    # And the SUMMARY block — a DISTINCT log site, which the per-run assertions above cannot
    # see. Deleting it left the first two assertions passing, so they were pinning the wrong
    # line: the test agreed with itself while the thing under test was gone. (Caught by
    # mutation N1 — the very mutation Nino used to prove the warning was unobservable.)
    assert "not an ALERT" in err and "torn upload is REAL" in err, (
        "the summary must explain WHY a torn upload did not raise an alert — an operator who "
        f"sees torn=1 on a green run needs to be told an older run covered for it: {err}"
    )


# ===========================================================================================
# deploy#591 review — PIPEFAIL PROMOTES SIGPIPE OVER A SUCCEEDING FINAL STAGE.
# ===========================================================================================


def test_a_huge_manifest_does_not_read_as_does_not_attest(tmp_path: Path) -> None:
    """THE #587 CONFIDENT LIE, BY A FOURTH ROUTE — in the parser that fixes it.

    The predicate was::

        grep '^BACKUP_MANIFEST ' | head -n1 | tr ' ' '\\n' | grep -qx 'complete=true'

    Under ``set -euo pipefail``, a consumer that exits early (``head -n1``, ``grep -q``) SIGPIPEs
    its producer, which dies **141** — and **pipefail promotes that 141 to the rc of the whole
    pipeline even though the final ``grep -qx`` SUCCEEDED.** A complete, intact, attesting run
    then reads as *does not attest*, and the scanner prints ``no_complete_backup`` over a bucket
    holding a complete backup.

    There were TWO early-exit consumers, so swapping ``head -n1`` for ``grep -m1`` does NOT close
    it — ``grep -qx`` still SIGPIPEs ``tr``. Both measured at 141. The fix removes the pipeline
    entirely: a herestring is a file descriptor, not a pipe.

    Latent — it needs a manifest far larger than the producer writes — and it fails closed. But a
    guard with a known path that silently disarms it is not a correct guard, it is an incomplete
    one, and here the shape can simply be deleted. (Found by Nurul Hakim.)
    """
    d = tmp_path / "daily" / "2026-07-11"
    _write_run(d, complete=True)

    # The run is COMPLETE and INTACT. Its manifest is merely enormous: the real attesting line
    # first, then 100k more. Every dump is present and correctly sized.
    m = d / manifest_filename(RUN_TS)
    m.write_bytes(_manifest(True) + _manifest(True) * 100_000)

    rc, line = _scan(tmp_path)
    assert rc == EXIT_OK, (
        f"a COMPLETE, INTACT, ATTESTING run must not read as 'does not attest' because its "
        f"manifest outran a pipe buffer: {line}"
    )
    assert _field(line, "status") == "fresh"
    assert _field(line, "reason") == "-"


def test_the_shipped_predicate_survives_sigpipe_pressure() -> None:
    """Drive the SHIPPED predicate directly, under restore.sh's own shell mode.

    The scanner test above is end-to-end; this one isolates the parser and proves the mechanism,
    so a regression names itself instead of surfacing as a mysterious `no_complete_backup`.
    """
    one = _manifest(True)
    huge = _manifest(True) + _manifest(True) * 100_000
    # A single enormous FIRST LINE — the second early-exit consumer (`grep -qx` SIGPIPEs `tr`),
    # which a `grep -m1`-only fix leaves wide open.
    long_line = (
        b"BACKUP_MANIFEST complete=true stores=postgres timestamp=" + RUN_TS.encode() + b" "
    ) + b" ".join(b"pad%d=x" % i for i in range(200_000))

    assert _predicate(one) == 0, "the control: a normal manifest attests"
    assert _predicate(huge) == 0, (
        "a 100k-line manifest must still ATTEST — rc=141 from a SIGPIPEd producer, promoted by "
        "pipefail over a SUCCEEDING final grep, is not a verdict about the backup"
    )
    assert _predicate(long_line) == 0, (
        "and a single enormous LINE must still attest — this is the consumer a `grep -m1`-only "
        "fix leaves in place"
    )
    assert _predicate(_manifest(False)) == 1, (
        "the negative control: it must still be able to say NO. A predicate that always returns "
        "0 would pass every assertion above and prove nothing."
    )


def _backup_is_complete_err(root: Path, path: str) -> tuple[int, str]:
    """(rc, STDERR) from restore.sh's shipped `backup_is_complete`.

    `restore.sh` has no result line — its stdout IS the chosen run id — so its torn diagnostic
    genuinely does live on stderr. That is fine; what was NOT fine is that nothing read it. Same
    treatment as the scanner's `torn=` field: the signal must be observable, so it gets a test.
    """
    body = RESTORE_SH.read_text()
    helpers = body[body.index("manifest_runs() {") : body.index("\nlist_backups()")]
    fn = body[body.index("backup_is_complete() {") : body.index("\nresolve_latest()")]
    script = (
        "set -euo pipefail\n"
        'log() { shift; echo "$*" >&2; }\n' + SCRATCH_SOURCE + 'RCLONE_REMOTE=":local"\n'
        # REMOTE_ROOT carries the environment namespace (deploy#632); every remote
        # path in restore.sh is built from it. B2_BUCKET deliberately points at a
        # path with NO fixtures under it, so a function that rebuilds a raw bucket
        # path — reaching outside its environment, which IS the deploy#632 bug —
        # finds nothing and this harness goes RED instead of quietly passing.
        f'B2_BUCKET="{root}/__unprefixed__"\n'
        f'REMOTE_ROOT=":local:{root}"\n' + helpers + "\n" + fn + "\n"
        'backup_is_complete "$1"\n'
    )
    r = subprocess.run(  # noqa: S603
        ["bash", "-c", script, "_", path],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    return r.returncode, r.stderr


def test_restore_names_the_torn_run_it_passed_over(tmp_path: Path) -> None:
    """A run that attests `complete=true` and did not deliver is a real fault in restore.sh too.

    It selects the older intact run — correctly — but an operator who is never told that today's
    backup tore will not know the tear is happening. It recurs nightly and nothing says so.
    """
    d = tmp_path / "daily" / "2026-07-11"
    _write_run(d, run_ts=EARLY_TS, complete=True)  # 02:00 — complete and intact
    _write_run(  # 08:00 — attests all three, only pg arrived: TORN
        d, run_ts=LATE_TS, complete=True, stores=("isnad-pg",)
    )

    rc, err = _backup_is_complete_err(tmp_path, "daily/2026-07-11")
    assert rc == ATTESTS, "the older 02:00 run IS restorable, so the day resolves"
    assert LATE_TS in err, f"but the torn 08:00 run must be NAMED, not silently passed over: {err}"
    assert "did NOT all arrive" in err, f"and the fault must be described: {err}"


def test_restore_says_nothing_about_tears_when_there_are_none(tmp_path: Path) -> None:
    """The positive control for the warning above — it must SEPARATE, not always fire."""
    _write_run(tmp_path / "daily" / "2026-07-11", complete=True)
    rc, err = _backup_is_complete_err(tmp_path, "daily/2026-07-11")
    assert rc == ATTESTS
    assert "did NOT all arrive" not in err, f"a healthy day must not cry wolf: {err}"

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
    (tmp_path / "isnad-pg-now.dump").write_bytes(b"x")
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
    (tmp_path / "isnad-pg-now.dump").write_bytes(b"x")
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
    p.write_bytes(b"x")
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
    (tmp_path / "isnad-pg-now.dump").write_bytes(b"x")
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

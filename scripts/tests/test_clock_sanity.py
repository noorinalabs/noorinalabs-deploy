"""BEHAVIOURAL tests for the VPS clock-sanity assertion (deploy#585) — the code is RUN, not grepped.

**Nothing asserted that the VPS clock was sane, and the clock is a load-bearing input to every
backup-freshness judgement we make.** ``rclone`` PRESERVES the source file's mtime on upload, so a
backup object's timestamp in B2 *is* the VPS clock at dump time.

deploy#584 pinned the READER side: a future-dated object is now refused as
``instrument_error reason=future_timestamp``. That is the right refusal — but **refusing a bad
reading is not the same as knowing the clock is good**, and the writer had no guard at all.

The error is TWO-SIDED, so the assertion must be:

* **Forward skew** — dumps stamped in the future. Pre-#584 this read ``fresh`` *forever*: it did not
  degrade, it **LATCHED**. One bad-clock upload turned the bucket check into a permanent green
  light.
* **Backward skew** — the quieter one. Healthy nightly backups stamp OLD and read ``stale``, paging
  someone for nothing; or, with a generous ``MAX_AGE_HOURS``, a genuinely stale bucket reads
  ``fresh``.

**A one-sided assertion on a two-sided error is half a control.**

The existing tests for ``assert_host_state.sh`` (``test_host_state_assertions.py``) are TEXTUAL —
they grep the script for substrings. That is a fine way to pin a *shape*, and a useless way to learn
whether a guard can actually go red. Everything here executes the shipped code.

Two things this suite refuses to do, both learned the hard way in deploy#591:

1. **It does not test a retyped copy of the script.** The clock code is sliced out of
   ``assert_host_state.sh``'s own text (``_shipped_clock_code``) and run — including under the
   script's own ``set`` line, parsed from the file rather than restated, because *a harness running
   production code under a weaker shell mode is not running production code.*

2. **It verifies its own instrument before believing any result.** A skew that silently fails to
   apply reports ``sane`` — which reads as *"the positive control passed"*, not as *"the fixture
   never reached the code."* So ``test_the_skew_instrument_is_faithful`` runs first, and every skew
   assertion checks the **offset VALUE**, not merely the class. **A control that agrees with you is
   the hardest kind to catch.**
"""

from __future__ import annotations

import contextlib
import itertools
import os
import re
import shutil
import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
ASSERT_SH = SCRIPTS_DIR / "assert_host_state.sh"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

# The script's rc contract for the clock classifier.
CLOCK_SANE = 0
CLOCK_BAD = 1
CLOCK_UNMEASURABLE = 2

REAL_DATE = shutil.which("date") or "/usr/bin/date"

# The reference must be read WITHOUT the network (CI must not depend on reaching cloudflare.com)
# and WITHOUT the shim (or host and reference would move together and the offset would stay 0 —
# a false `sane` that looks exactly like a passing positive control). An absolute path to the real
# `date` is both.
TRUE_TIME = f"{REAL_DATE} -u +%s"


# ---------------------------------------------------------------------------
# The SHIPPED code — sliced out of the script, never retyped.
# ---------------------------------------------------------------------------
def _shipped_clock_code() -> str:
    body = ASSERT_SH.read_text()
    return body[
        body.index("# --- clock sanity (deploy#585)") : body.index("# Calibration-only entry point")
    ]


def _shipped_set_line() -> str:
    """The script's OWN shell mode, parsed from it — never restated here.

    ``restore.sh``'s harness ran production code under ``set -uo pipefail`` while the script itself
    ran ``set -euo pipefail``, and that single missing ``-e`` hid a crash from every unit test
    (deploy#584/#591). Reading the mode out of the file means the harness follows the script if
    anyone ever tightens it, instead of quietly diverging.
    """
    m = re.search(r"^set -[a-zA-Z]+ pipefail$", ASSERT_SH.read_text(), re.MULTILINE)
    assert m, "assert_host_state.sh has no `set -... pipefail` line — has its shell mode changed?"
    return m.group(0)


def _skew_shim(tmp_path: Path) -> Path:
    """A `date` that LIES about the clock — a real skew of the clock as the script observes it.

    Invocations that PARSE an explicit date (``-d``) are pure string parsing and are not affected by
    a broken system clock, so they pass through untouched. That carve-out is load-bearing: the
    external reference is parsed with ``date -u -d "<http date>"``, and a shim that skewed *that*
    too would move the host and the reference together, leaving the offset at 0. The check would
    report ``sane`` and the test would "prove" the guard works while never having skewed anything.
    """
    d = tmp_path / "shim"
    d.mkdir(exist_ok=True)
    shim = d / "date"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        'for a in "$@"; do\n'
        f'  case "$a" in -d|--date|--date=*) exec {REAL_DATE} "$@" ;; esac\n'
        "done\n"
        f"now=$({REAL_DATE} +%s)\n"
        f'exec {REAL_DATE} -d "@$(( now + ${{CLOCK_TEST_SKEW:-0}} ))" "$@"\n'
    )
    shim.chmod(0o755)
    return d


def _probe(
    tmp_path: Path,
    *,
    skew: int = 0,
    ref: str = TRUE_TIME,
    ntp: str = "yes",
    sync_service: str = "systemd-timesyncd.service",
    max_offset: str = "60",
) -> tuple[int, str]:
    """Run the SHIPPED `probe_clock` with the host clock skewed by `skew` seconds.

    Returns (rc, the HOST_CLOCK signal line).
    """
    script = f"{_shipped_set_line()}\n" + _shipped_clock_code() + '\nprobe_clock "$1" "$2"\n'
    env = {
        **os.environ,
        "PATH": f"{_skew_shim(tmp_path)}:{os.environ['PATH']}",
        "CLOCK_TEST_SKEW": str(skew),
        "CLOCK_REF_EPOCH_CMD": ref,
        "CLOCK_MAX_OFFSET_SECONDS": max_offset,
    }
    r = subprocess.run(  # noqa: S603
        ["bash", "-c", script, "_", ntp, sync_service],  # noqa: S607
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    line = ""
    for ln in r.stdout.splitlines():
        if ln.startswith("HOST_CLOCK "):
            line = ln
    return r.returncode, line


def _field(line: str, key: str) -> str:
    for tok in line.split():
        k, _, v = tok.partition("=")
        if k == key:
            return v
    return ""


# ---------------------------------------------------------------------------
# A LOCAL HTTP REFERENCE — so the PRODUCTION path is what gets tested.
#
# Everything above drives the reference through `CLOCK_REF_EPOCH_CMD`, which is the *injection
# seam*. That tests the classifier honestly, but it never once exercised the path a real host
# takes: `curl -I` → `Date:` header → `http_date_epoch` → offset. A guard tested only through its
# test seam is a guard whose production path is unproven — which is uncomfortably close to the
# defect this whole issue is about.
#
# `http.server` emits an RFC-7231 `Date:` header from the machine's real clock, so pointing
# CLOCK_REF_URLS at 127.0.0.1 exercises the real curl path with a genuine external-shaped
# reference — and needs no network, so CI cannot flake on reaching cloudflare.com.
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def _local_reference(fail_first: int = 0) -> Iterator[str]:
    """`fail_first` drops that many connections before answering — a transient egress blip.

    It closes the socket without writing a response, so curl reports an empty reply and exits
    non-zero. A 500 would NOT do: curl treats a served error page as a successful fetch, and the
    response still carries a `Date:` header, so the read would succeed and the retry would never
    be exercised. A fixture that cannot produce the failure cannot test the recovery.
    """
    from http.server import BaseHTTPRequestHandler, HTTPServer

    seen = itertools.count(1)

    class Handler(BaseHTTPRequestHandler):
        def handle_one_request(self) -> None:
            if next(seen) <= fail_first:
                self.close_connection = True
                return  # no response at all — curl: "Empty reply from server"
            super().handle_one_request()

        def do_HEAD(self) -> None:  # noqa: N802
            self.send_response(200)  # send_response emits the `Date:` header for us
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass  # do not spray the pytest output

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{srv.server_port}"
    finally:
        srv.shutdown()
        srv.server_close()


def _probe_via_http(
    tmp_path: Path, *, skew: int = 0, urls: str | None = None, max_offset: str = "60"
) -> tuple[int, str]:
    """Drive `probe_clock` through the REAL `curl` reference path — no injection seam."""
    script = f"{_shipped_set_line()}\n" + _shipped_clock_code() + '\nprobe_clock "$1" "$2"\n'
    with _local_reference() as url:
        env = {
            **os.environ,
            "PATH": f"{_skew_shim(tmp_path)}:{os.environ['PATH']}",
            "CLOCK_TEST_SKEW": str(skew),
            "CLOCK_REF_URLS": urls if urls is not None else url,
            "CLOCK_MAX_OFFSET_SECONDS": max_offset,
            "CLOCK_REF_TIMEOUT": "5",
            "CLOCK_REF_RETRY_DELAY": "0",
        }
        env.pop("CLOCK_REF_EPOCH_CMD", None)
        r = subprocess.run(  # noqa: S603
            ["bash", "-c", script, "_", "yes", "systemd-timesyncd.service"],  # noqa: S607
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
    line = ""
    for ln in r.stdout.splitlines():
        if ln.startswith("HOST_CLOCK "):
            line = ln
    return r.returncode, line


# ---------------------------------------------------------------------------
# VERIFY THE INSTRUMENT — before any result from it is believed.
#
# deploy#591 lost two mutation runs to exactly this: a regex that stripped 4 of 5 sites, and a
# `git stash` that healed the mutant while pytest was still running. **Both report GREEN, which
# reads as "the guard fired."** A skew that does not apply is the same trap wearing this issue's
# clothes: it reports `sane`, which reads as a passing positive control.
# ---------------------------------------------------------------------------
def test_the_skew_instrument_is_faithful(tmp_path: Path) -> None:
    """The shim must lie about NOW, and must NOT lie about date PARSING.

    If it failed the first, every RED below would be unreachable and the suite would pass by
    reporting `sane` everywhere. If it failed the second, the reference would move with the host,
    the offset would stay 0, and the suite would *also* pass by reporting `sane` everywhere. Both
    broken instruments produce a green suite. Neither produces evidence.
    """
    shim_dir = _skew_shim(tmp_path)
    env = {**os.environ, "PATH": f"{shim_dir}:{os.environ['PATH']}", "CLOCK_TEST_SKEW": "3600"}

    def sh(cmd: str) -> str:
        return subprocess.run(  # noqa: S603
            ["bash", "-c", cmd],  # noqa: S607
            env=env,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    # It is actually the `date` being resolved.
    assert sh("command -v date") == str(shim_dir / "date"), "the shim is not on PATH"

    # (1) A NOW read is skewed, by exactly the amount asked for.
    real_now = int(
        subprocess.run(  # noqa: S603
            [REAL_DATE, "-u", "+%s"], capture_output=True, text=True, check=True
        ).stdout
    )
    assert int(sh("date -u +%s")) - real_now == pytest.approx(3600, abs=2), (
        "the shim does not skew the clock — every skew assertion in this file would be inert, "
        "and would report `sane`"
    )

    # (2) Date PARSING is untouched — the property the whole method rests on.
    fixed = "Sat, 11 Jul 2026 20:25:03 GMT"
    assert (
        sh(f'date -u -d "{fixed}" +%s')
        == subprocess.run(  # noqa: S603
            [REAL_DATE, "-u", "-d", fixed, "+%s"], capture_output=True, text=True, check=True
        ).stdout.strip()
    ), (
        "the shim skews `date -d` parsing too, so the reference moves WITH the host clock and the "
        "offset stays 0 — the check would report `sane` under any skew"
    )


# ---------------------------------------------------------------------------
# THE POSITIVE CONTROL. Without it every RED below is vacuous: a classifier that
# only ever says `skewed` passes both skew tests and proves nothing.
# ---------------------------------------------------------------------------
def test_a_sane_clock_passes(tmp_path: Path) -> None:
    rc, line = _probe(tmp_path, skew=0)
    assert rc == CLOCK_SANE, f"an unskewed clock must pass: {line}"
    assert _field(line, "status") == "sane"
    assert abs(int(_field(line, "offset_seconds"))) <= 1


def test_a_clock_inside_tolerance_passes(tmp_path: Path) -> None:
    """45s of drift is not a broken clock. The check must not be a hair-trigger."""
    rc, line = _probe(tmp_path, skew=45)
    assert rc == CLOCK_SANE, f"45s is within the 60s bound: {line}"
    assert _field(line, "status") == "sane"


# ---------------------------------------------------------------------------
# BOTH DIRECTIONS. This is the acceptance bar of the issue.
# ---------------------------------------------------------------------------
def test_forward_skew_is_RED(tmp_path: Path) -> None:
    """The LATCHING one. A clock running ahead stamps dumps in the future.

    Pre-deploy#584 the bucket check read those `fresh` **forever** — it did not degrade, it latched,
    and one bad-clock upload turned the only look-in-the-bucket signal we have into a permanent
    green light.
    """
    rc, line = _probe(tmp_path, skew=3600)
    assert rc == CLOCK_BAD, f"a clock 1h AHEAD must fail: {line}"
    assert _field(line, "status") == "skewed"
    assert _field(line, "reason") == "ahead", "the direction must be NAMED — the fixes differ"


def test_backward_skew_is_RED(tmp_path: Path) -> None:
    """The QUIETER one, and the reason a one-sided check is half a control.

    A clock running behind stamps healthy nightly backups OLD: they read `stale` and page someone
    at 3am over a bucket that is fine. And with a generous `MAX_AGE_HOURS`, a genuinely stale
    bucket still reads `fresh` — the missed alarm.
    """
    rc, line = _probe(tmp_path, skew=-3600)
    assert rc == CLOCK_BAD, f"a clock 1h BEHIND must fail: {line}"
    assert _field(line, "status") == "skewed"
    assert _field(line, "reason") == "behind", "the direction must be NAMED — the fixes differ"


@pytest.mark.parametrize("skew", [3600, -3600, 600, -600, 120, -120])
def test_the_reported_offset_TRACKS_the_injected_skew(tmp_path: Path, skew: int) -> None:
    """THE INSTRUMENT CHECK — assert the VALUE, not the class.

    A class-level assertion cannot tell "the guard fired because the clock is 1h out" from "the
    guard fired for some other reason" — and, far worse, it cannot tell a skew that **never
    applied** from a clock that is fine: both report `sane`. deploy#591's own scanner learned this
    one level out (*"the calibration asserts the AGE value, not merely the class"*), because a
    4-hour
    TZ error does not move a fresh fixture out of the fresh class.

    So: the number the check reports must equal the number injected. If the shim ever stops
    intercepting, this is what fails — loudly — instead of the suite going quietly green.
    """
    _, line = _probe(tmp_path, skew=skew)
    got = int(_field(line, "offset_seconds"))
    assert got == pytest.approx(skew, abs=2), (
        f"injected a {skew}s skew and the check reported {got}s. Either the measurement is wrong, "
        "or the skew never reached the code under test — and the latter makes every other "
        "assertion in this file vacuous."
    )


# ---------------------------------------------------------------------------
# "I could not look" is a THIRD outcome — never a verdict on the clock (deploy#584).
# ---------------------------------------------------------------------------
def test_unreachable_reference_is_not_a_verdict_on_the_clock(tmp_path: Path) -> None:
    rc, line = _probe(tmp_path, ref="exit 1")
    assert rc == CLOCK_UNMEASURABLE, f"an unreadable reference is an instrument failure: {line}"
    assert _field(line, "status") == "unknown"
    assert _field(line, "reason") == "reference_unreachable"
    assert _field(line, "status") != "skewed", "we did not measure a skew — we measured nothing"


def test_unmeasurable_does_not_PASS_either(tmp_path: Path) -> None:
    """An unverifiable clock is still not a verified clock.

    The whole issue is that *refusing a bad reading is not the same as knowing the clock is good*.
    A check that shrugged and passed when it could not reach the reference would reintroduce that
    gap inside its own fix.
    """
    rc, _ = _probe(tmp_path, ref="exit 1")
    assert rc != CLOCK_SANE


def test_a_garbage_reference_is_not_silently_read_as_zero(tmp_path: Path) -> None:
    """An HTML error page is not an epoch, and must not fall into the arithmetic.

    `$(( now - ref ))` with a non-numeric `ref` treats it as 0 — which would report the host as
    skewed by ~56 years and fire a clock alarm over a captive portal.
    """
    rc, line = _probe(tmp_path, ref="echo '<html>502 Bad Gateway</html>'")
    assert rc == CLOCK_UNMEASURABLE, f"garbage must be refused, not arithmetic'd: {line}"
    assert _field(line, "status") == "unknown"
    assert _field(line, "offset_seconds") == "unknown"


# ---------------------------------------------------------------------------
# NTP state is its own signal, and it is not the same signal as the offset.
# ---------------------------------------------------------------------------
def test_unsynchronised_ntp_fails_even_when_the_offset_is_fine(tmp_path: Path) -> None:
    """Today's small offset is not a promise about tonight's 03:00 dump.

    A clock nothing is disciplining will drift. The offset being fine *right now* is a snapshot;
    NTPSynchronized=yes is the thing that keeps it that way.
    """
    rc, line = _probe(tmp_path, skew=0, ntp="no")
    assert rc == CLOCK_BAD, f"an undisciplined clock must fail even at offset 0: {line}"
    assert _field(line, "status") == "unsynchronised"
    assert _field(line, "reason") == "ntp_not_synchronised"


def test_no_time_sync_service_fails(tmp_path: Path) -> None:
    rc, line = _probe(tmp_path, skew=0, ntp="yes", sync_service="none")
    assert rc == CLOCK_BAD, f"nothing is disciplining this clock: {line}"
    assert _field(line, "status") == "unsynchronised"
    assert _field(line, "reason") == "no_sync_service"


def test_unreadable_ntp_state_is_UNKNOWN_not_NO(tmp_path: Path) -> None:
    """`timedatectl` erroring is not the same fact as NTPSynchronized=no.

    One means "this clock is not being disciplined"; the other means "I could not find out". Folding
    the second into the first is the same conflation deploy#584 refused between `absent` and
    `instrument_error`.
    """
    rc, line = _probe(tmp_path, skew=0, ntp="unreadable")
    assert rc == CLOCK_UNMEASURABLE
    assert _field(line, "status") == "unknown"
    assert _field(line, "reason") == "ntp_state_unreadable"


def test_a_measured_skew_outranks_an_unreadable_ntp_state(tmp_path: Path) -> None:
    """Hard evidence first. A measured 1h offset is a FACT; report it, do not downgrade it to
    "I could not read timedatectl" — but keep BOTH on the line, because a check that knows
    something and does not say it has thrown the finding away (deploy#591)."""
    rc, line = _probe(tmp_path, skew=3600, ntp="unreadable")
    assert rc == CLOCK_BAD
    assert _field(line, "status") == "skewed"
    assert _field(line, "ntp_sync") == "unreadable", "no field may be dropped from the signal line"


# ---------------------------------------------------------------------------
# The clock is its OWN signal. "The clock is wrong" and "the backup is missing"
# have different fixes and must never be confused.
# ---------------------------------------------------------------------------
def test_the_clock_signal_is_distinct_from_the_backup_signals(tmp_path: Path) -> None:
    _, line = _probe(tmp_path, skew=3600)
    assert line.startswith("HOST_CLOCK "), "the clock must be its own signal"
    assert "B2_BACKUP_ARTIFACT" not in line
    # A skewed clock must never be dressed up in the backup scanner's vocabulary.
    for backup_word in ("stale", "absent", "fresh", "incomplete"):
        assert _field(line, "status") != backup_word


def test_the_signal_line_always_carries_every_field(tmp_path: Path) -> None:
    """If the check knows something, the line says it (deploy#591).

    The B2 scanner computed `undersized` and then threw it away unless `dumps == 0`; the same
    change computed an age of -719 and never checked it. A field that exists on the happy path and
    vanishes on the sad one is a field nobody can alert on.
    """
    for kwargs in (
        {"skew": 0},
        {"skew": 3600},
        {"skew": -3600},
        {"ref": "exit 1"},
        {"ntp": "no"},
    ):
        _, line = _probe(tmp_path, **kwargs)  # type: ignore[arg-type]
        for field in (
            "status",
            "reason",
            "offset_seconds",
            "max_offset_seconds",
            "ntp_sync",
            "sync_service",
        ):
            assert _field(line, field), f"{kwargs} dropped `{field}` from the signal: {line}"


# ---------------------------------------------------------------------------
# The reference parser, against REAL header blocks — curl's verbatim output.
#
# These fixtures are `curl -sS -I <url>` captured to a file, NOT a hand-written approximation of
# what an HTTP response looks like. A fixture written in the test's own words is written by the same
# mind that wrote the parser, so it encodes the same misreading and cannot falsify it
# (deploy#589/#591). Cloudflare's real response carries three things I would never have invented:
# an HTTP/2 `103 Early Hints` response BEFORE the 200 (so the block holds two responses and only
# the second has a date), lower-cased HTTP/2 header names, and CRLF line endings.
#
# (This comment once wrapped so that a line began `# type:` — which mypy reads as a type-annotation
# comment and rejects as invalid syntax. Left as a note because the failure is so unobvious.)
# ---------------------------------------------------------------------------
def _http_date_epoch(headers: str) -> tuple[int, str]:
    script = f"{_shipped_set_line()}\n" + _shipped_clock_code() + '\nhttp_date_epoch "$1"\n'
    r = subprocess.run(  # noqa: S603
        ["bash", "-c", script, "_", headers],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    return r.returncode, r.stdout.strip()


def _independent_oracle(headers: str) -> int:
    """Parse the `Date:` header with a COMPLETELY DIFFERENT implementation.

    The first version of this test compared the shell parser against a date string I had typed in
    by hand, copied out of an exploratory `curl` I ran *before* capturing the fixture. It was a
    second stale — so the test failed, and it failed for a reason that had nothing to do with the
    code under test. **That is the paraphrase defect, committed inside the test written to warn
    about it** (deploy#589): a fixture in the test's own words tests the words.

    An expectation I retype is not an oracle. `email.utils.parsedate_to_datetime` is: it is RFC-5322
    parsing by a different author in a different language, and if it agrees with `sed` +
    `date -u -d`
    on a real Cloudflare response then the shell parser reads real HTTP dates correctly. It also
    re-derives from the fixture, so re-capturing the fixtures cannot rot it.
    """
    from email.utils import parsedate_to_datetime

    for line in headers.splitlines():
        if line.lower().startswith("date:"):
            return int(parsedate_to_datetime(line.split(":", 1)[1].strip()).timestamp())
    raise AssertionError("fixture carries no Date header")


@pytest.mark.parametrize("fixture", ["http_headers_cloudflare.txt", "http_headers_google.txt"])
def test_http_date_epoch_parses_a_real_response(fixture: str) -> None:
    raw = (FIXTURES / fixture).read_text()
    rc, got = _http_date_epoch(raw)
    assert rc == 0, f"failed to parse a REAL {fixture} header block"
    assert int(got) == _independent_oracle(raw), (
        f"{fixture}: the shell parser (sed + `date -u -d`) disagrees with an independent RFC-5322 "
        "parser about a real HTTP response's Date header"
    )


def test_http_date_epoch_survives_the_early_hints_response(tmp_path: Path) -> None:
    """Cloudflare's block holds a `103` response with NO date, then the `200` that has one.

    A parser that took the first *response* rather than the first `date:` *header* would find
    nothing and report the reference unreachable — degrading a working check into a permanent
    `unknown` against the one CDN we listed first.
    """
    raw = (FIXTURES / "http_headers_cloudflare.txt").read_text()
    assert "103" in raw.splitlines()[0], "the fixture no longer exercises the early-hints case"
    assert not any(ln.lower().startswith("date:") for ln in raw.split("\n\n")[0].splitlines()), (
        "the fixture's FIRST response must carry no date — that is the case being pinned"
    )
    rc, got = _http_date_epoch(raw)
    assert rc == 0 and got.isdigit()


def test_http_date_epoch_reports_absence_rather_than_crashing() -> None:
    """A header block with no `Date:` at all.

    `sed -n …p` exits 0 when it matches nothing; `grep` exits 1 and, under a stricter `set -e`,
    would CRASH the script rather than return "no reference" (deploy#563/#584/#591 — and #591's
    correction: that is a property of `grep` specifically, not a rule to generalise). The right
    behaviour is rc=1, which `reference_epoch` turns into `unknown`.
    """
    rc, got = _http_date_epoch("HTTP/2 200\r\ncontent-type: text/html\r\n\r\n")
    assert rc == 1, "a block with no Date header must report failure, not succeed with garbage"
    assert got == ""


def test_reference_parsing_is_immune_to_the_broken_host_clock(tmp_path: Path) -> None:
    """The property the entire check depends on, asserted rather than assumed.

    `date -d <string>` is pure string parsing — it does not consult the system clock. If it did,
    the reference would move WITH the broken clock, the offset would stay 0, and the check would
    report `sane` on a host whose clock is hours out. That is not a hypothetical: it is precisely
    what a careless shim does, and it is why `_skew_shim` passes `-d` through.
    """
    raw = (FIXTURES / "http_headers_google.txt").read_text()
    script = f"{_shipped_set_line()}\n" + _shipped_clock_code() + '\nhttp_date_epoch "$1"\n'
    env = {
        **os.environ,
        "PATH": f"{_skew_shim(tmp_path)}:{os.environ['PATH']}",
        "CLOCK_TEST_SKEW": "86400",  # the host clock is a full DAY out
    }
    r = subprocess.run(  # noqa: S603
        ["bash", "-c", script, "_", raw],  # noqa: S607
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    _, unskewed = _http_date_epoch(raw)
    assert r.stdout.strip() == unskewed, (
        "the parsed reference moved when the host clock moved — the offset would then always be 0 "
        "and the check would be structurally incapable of detecting any skew at all"
    )


# ---------------------------------------------------------------------------
# THE CALIBRATION — and proof that its own cases are load-bearing.
#
# The script runs `self_test_clock` on the real host before every real reading, and refuses to
# report a verdict if it fails. That is the acceptance bar of this issue turned into code that
# executes in production: skew a clock and WATCH THE CHECK FAIL, every deploy.
#
# But a self-test is just more code, and it can be vacuous too. `feedback_calibrate_the_mutation
# _before_counting_it` (deploy#574): *an INERT mutation is not evidence.* So each mutation below is
# first proved to have CHANGED THE TEXT, and only then is its effect on the calibration counted.
# ---------------------------------------------------------------------------
def _run_self_test(
    source: str | None = None, tmp_path: Path | None = None
) -> subprocess.CompletedProcess[str]:
    target = ASSERT_SH
    if source is not None:
        assert tmp_path is not None
        target = tmp_path / "assert_host_state.sh"
        target.write_text(source)
    return subprocess.run(  # noqa: S603
        ["bash", str(target), "--self-test"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_shipped_calibration_separates_all_four_classes() -> None:
    """It must SEPARATE them — sane, ahead, behind, unmeasurable — not merely run."""
    r = _run_self_test()
    assert r.returncode == 0, f"the shipped calibration failed:\n{r.stderr}"
    for case in ("sane", "skew-ahead", "skew-behind", "unmeasurable"):
        assert f"self-test {case}: OK" in r.stderr, f"the calibration does not cover `{case}`"


def _mutate(old: str, new: str) -> str:
    """Mutate the shipped source, and PROVE the mutation landed.

    An inert mutation that changes nothing produces a passing calibration, which reads exactly like
    "the guard held". deploy#574's lesson, and deploy#591 lost a whole mutation run to a regex that
    silently matched 4 of 5 sites.
    """
    src = ASSERT_SH.read_text()
    assert old in src, f"the mutation target is not in the source — it would be INERT: {old!r}"
    mutated = src.replace(old, new)
    assert mutated != src, "the mutation did not change the text"
    return mutated


def test_calibration_FAILS_if_the_classifier_stops_detecting_skew(tmp_path: Path) -> None:
    """Break the classifier so nothing is ever skewed. The calibration must catch it.

    This is what proves cases (2) and (3) of the self-test are load-bearing rather than decorative:
    with the skew detection gone, the guard on the real host would report `sane` on a clock that is
    an hour out — and the calibration is the only thing standing between that and a green deploy.
    """
    mutated = _mutate('status="skewed"; reason="ahead"', 'status="sane"; reason="ok"')
    r = _run_self_test(mutated, tmp_path)
    assert r.returncode != 0, (
        "the classifier no longer detects a forward skew and the calibration still PASSED — the "
        "self-test is decorative and the guard is unprotected"
    )
    assert "self-test skew-ahead: FAILED" in r.stderr


def test_calibration_FAILS_if_the_classifier_stops_detecting_backward_skew(tmp_path: Path) -> None:
    mutated = _mutate('status="skewed"; reason="behind"', 'status="sane"; reason="ok"')
    r = _run_self_test(mutated, tmp_path)
    assert r.returncode != 0, (
        "backward skew is undetected and the calibration still passed — the quiet direction is "
        "exactly the one that ships broken"
    )
    assert "self-test skew-behind: FAILED" in r.stderr


def test_calibration_FAILS_if_the_classifier_cries_wolf_on_a_good_clock(tmp_path: Path) -> None:
    """THE POSITIVE CONTROL, mutated.

    A classifier that says `skewed` about everything passes both skew cases. Without case (1) the
    calibration would bless it — and *"an oracle that only says BLOCKED proves every guard holds"*
    (deploy#574). This is the test that proves the positive control is not ornamental.
    """
    mutated = _mutate('status="sane"; reason="ok"', 'status="skewed"; reason="ahead"')
    r = _run_self_test(mutated, tmp_path)
    assert r.returncode != 0, (
        "a classifier that calls every clock skewed passed its own calibration — the positive "
        "control is not doing any work"
    )
    assert "self-test sane: FAILED" in r.stderr


def test_calibration_FAILS_if_unmeasurable_is_collapsed_into_a_verdict(tmp_path: Path) -> None:
    """ "I could not look" must never become "the clock is fine" — nor "the clock is broken"."""
    mutated = _mutate(
        'status="unknown"; reason="reference_unreachable"', 'status="sane"; reason="ok"'
    )
    r = _run_self_test(mutated, tmp_path)
    assert r.returncode != 0, (
        "an unreachable reference now reports the clock SANE, and the calibration passed — this is "
        "the deploy#584 conflation (`absent` vs `instrument_error`) reintroduced on the clock"
    )
    assert "self-test unmeasurable: FAILED" in r.stderr


# ---------------------------------------------------------------------------
# THE PRODUCTION PATH — real curl, real Date: header, no injection seam.
#
# Every skew test above reaches the classifier through `CLOCK_REF_EPOCH_CMD`. These reach it the
# way the VPS does.
# ---------------------------------------------------------------------------
def test_the_real_curl_path_detects_a_forward_skew(tmp_path: Path) -> None:
    rc, line = _probe_via_http(tmp_path, skew=3600)
    assert rc == CLOCK_BAD, f"the PRODUCTION reference path must catch a clock 1h ahead: {line}"
    assert _field(line, "status") == "skewed"
    assert _field(line, "reason") == "ahead"
    assert int(_field(line, "offset_seconds")) == pytest.approx(3600, abs=3)


def test_the_real_curl_path_detects_a_backward_skew(tmp_path: Path) -> None:
    rc, line = _probe_via_http(tmp_path, skew=-3600)
    assert rc == CLOCK_BAD, f"the PRODUCTION reference path must catch a clock 1h behind: {line}"
    assert _field(line, "status") == "skewed"
    assert _field(line, "reason") == "behind"
    assert int(_field(line, "offset_seconds")) == pytest.approx(-3600, abs=3)


def test_the_real_curl_path_passes_a_sane_clock(tmp_path: Path) -> None:
    """The positive control for the production path — without it the two above are vacuous."""
    rc, line = _probe_via_http(tmp_path, skew=0)
    assert rc == CLOCK_SANE, f"an unskewed clock must pass through the real path: {line}"
    assert _field(line, "status") == "sane"
    assert abs(int(_field(line, "offset_seconds"))) <= 2


def test_a_dead_reference_does_not_sink_a_live_one(tmp_path: Path) -> None:
    """URL FALLBACK — note this is NOT the retry, and calling it one would leave the retry untested.

    A dead URL ahead of a live one is handled by the inner URL loop on the FIRST attempt; the
    attempts loop never runs. `test_a_transient_failure_is_retried` is the one that exercises the
    retry, and it needs a reference that fails and then recovers.
    """
    with _local_reference() as good:
        rc, line = _probe_via_http(tmp_path, skew=0, urls=f"http://127.0.0.1:1 {good}")
    assert rc == CLOCK_SANE, f"a dead reference must not sink a read a live one can serve: {line}"
    assert _field(line, "status") == "sane"


def test_a_transient_failure_is_retried(tmp_path: Path) -> None:
    """THE RETRY. Failing closed on an unreachable reference is right; on one blip it is brittle.

    An egress hiccup would otherwise fail closed and block a promote over a dropped packet (Nino
    Kavtaradze, deploy#585 review). The reference here drops the first connection outright — curl
    gets an empty reply — and answers the second. Only the attempts loop can recover that.
    """
    with _local_reference(fail_first=1) as url:
        rc, line = _probe_via_http(tmp_path, skew=0, urls=url)
    assert rc == CLOCK_SANE, (
        f"a reference that blipped once and then answered must be RETRIED, not written off: {line}"
    )
    assert _field(line, "status") == "sane"


def test_an_entirely_unreachable_reference_still_fails_CLOSED(tmp_path: Path) -> None:
    """The retry must not become a way to eventually shrug and pass."""
    rc, line = _probe_via_http(tmp_path, skew=0, urls="http://127.0.0.1:1 http://127.0.0.1:2")
    assert rc == CLOCK_UNMEASURABLE, f"no reference at all must be `unknown`, not `sane`: {line}"
    assert _field(line, "status") == "unknown"
    assert _field(line, "reason") == "reference_unreachable"


# ---------------------------------------------------------------------------
# REVIEW FINDING (Aisha Idrissi): the production read rode the calibration's own seam.
#
# `reference_epoch` honoured `CLOCK_REF_EPOCH_CMD` UNCONDITIONALLY, and production went through
# the same function. So `CLOCK_REF_EPOCH_CMD='date -u +%s'` compared the clock TO ITSELF — a host
# 99,999s out reported `offset_seconds=0 status=sane` and a green PASS.
#
# It is the same shape as a `date` shim that skews parsing as well as `now` — the reference and
# the subject moving together, offset pinned at 0 — which `_skew_shim` is explicitly written to
# avoid. The guard against it survived one layer down, in the seam the guard itself rides on.
# ---------------------------------------------------------------------------
def _full_script(tmp_path: Path, *, skew: int = 0, source: str | None = None, **env_over: str):
    target = ASSERT_SH
    if source is not None:
        target = tmp_path / "assert_host_state.sh"
        target.write_text(source)
    with _local_reference() as url:
        env = {
            **os.environ,
            "PATH": f"{_skew_shim(tmp_path)}:{os.environ['PATH']}",
            "CLOCK_TEST_SKEW": str(skew),
            "CLOCK_REF_URLS": url,
            "CLOCK_REF_TIMEOUT": "5",
            "CLOCK_REF_RETRY_DELAY": "0",
        }
        env.pop("CLOCK_REF_EPOCH_CMD", None)
        env.update(env_over)
        return subprocess.run(  # noqa: S603
            ["bash", str(target)],  # noqa: S607
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )


def test_production_read_REFUSES_an_injected_reference(tmp_path: Path) -> None:
    """The regression test for the seam. It must REFUSE — loudly — not silently unset.

    If this variable is set on a real host, something is wrong that an operator needs to be told
    about. Quietly unsetting it would fix the offset and hide the fact that someone was in a
    position to inject a reference at all.
    """
    r = _full_script(tmp_path, skew=99999, CLOCK_REF_EPOCH_CMD="date -u +%s")

    assert "CLOCK_REF_EPOCH_CMD is set" in r.stdout, (
        f"an injected reference must be refused, loudly: {r.stdout}"
    )
    assert not any(ln.strip().startswith("HOST_CLOCK") for ln in r.stdout.splitlines()), (
        "the script emitted a clock VERDICT while its reference was caller-supplied"
    )
    assert r.returncode != 0


def test_an_injected_reference_can_no_longer_certify_a_wildly_skewed_clock(tmp_path: Path) -> None:
    """THE EXACT REPRO, pinned. A clock 99,999s out once reported `sane` with `offset_seconds=0`.

    Note what makes this so quiet: the offset is not merely *wrong*, it is *0* — the most
    reassuring number the field can hold — because the subject was measured against itself.
    """
    r = _full_script(tmp_path, skew=99999, CLOCK_REF_EPOCH_CMD="date -u +%s")
    assert "status=sane" not in r.stdout, (
        f"a clock 99,999s out was certified sane by comparing it to itself: {r.stdout}"
    )
    assert "[PASS] host clock is sane" not in r.stdout


def test_the_self_test_may_still_inject_its_own_reference() -> None:
    """The gate must not break the legitimate injection point it is protecting.

    A fix that closed the seam by removing `CLOCK_REF_EPOCH_CMD` outright would take the
    calibration down with it — and the calibration is the thing that makes this guard trustworthy.
    Gate the PRODUCTION call; leave the self-test's use of it alone.
    """
    r = _run_self_test()
    assert r.returncode == 0, f"the gate broke the calibration's own injection: {r.stderr}"
    for case in ("sane", "skew-ahead", "skew-behind", "unmeasurable"):
        assert f"self-test {case}: OK" in r.stderr


# ---------------------------------------------------------------------------
# REVIEW FINDING (Aisha Idrissi): CLOCK_MAX_OFFSET_SECONDS was not regex-guarded.
#
# `CLOCK_MAX_OFFSET_SECONDS=60s` — an easy fat-finger — ERRORS both `(( ))` comparisons, which
# short-circuits both skew branches FALSE and drops through to the terminal `else`: `status=sane
# rc=0` on a clock an hour out. The calibration already caught it (its own `skew` arithmetic breaks
# the same way, so the control fails and the script refuses) — this makes the diagnosis precise
# rather than incidental.
# ---------------------------------------------------------------------------
def test_a_nonnumeric_threshold_is_REFUSED_not_read_as_sane(tmp_path: Path) -> None:
    rc, line = _probe(tmp_path, skew=3600, max_offset="60s")
    assert rc != CLOCK_SANE, f"a clock 1h out passed because the THRESHOLD was unreadable: {line}"
    assert _field(line, "status") == "unknown"
    assert _field(line, "reason") == "invalid_threshold"


def test_a_nonnumeric_threshold_fails_the_calibration_too(tmp_path: Path) -> None:
    """Defence in depth, and it must stay in depth: BOTH layers refuse."""
    r = _full_script(tmp_path, skew=3600, CLOCK_MAX_OFFSET_SECONDS="60s")
    assert "status=sane" not in r.stdout
    assert "[PASS] host clock is sane" not in r.stdout
    assert r.returncode != 0


def test_a_valid_threshold_is_still_honoured(tmp_path: Path) -> None:
    """The positive control for the threshold guard — it must not refuse every threshold."""
    rc, line = _probe(tmp_path, skew=120, max_offset="300")
    assert rc == CLOCK_SANE, f"120s of skew is inside a 300s bound: {line}"
    assert _field(line, "status") == "sane"


def test_a_passing_calibration_does_not_cry_wolf(tmp_path: Path) -> None:
    """The calibration drives the instrument-error path ON PURPOSE (case 4).

    If a *passing* host-state run printed that alarming machinery, operators would learn to scroll
    past the clock section — and it only matters on the day someone actually reads it (deploy#591's
    `test_passing_self_test_does_not_cry_wolf`, same trap, one script over).
    """
    r = _full_script(tmp_path, skew=0)
    clock = [ln for ln in r.stdout.splitlines() if "HOST_CLOCK" in ln or "clock" in ln.lower()]
    assert any("[PASS] clock check calibrated" in ln for ln in clock), (
        f"the clock section did not run: {r.stdout}"
    )
    assert any("status=sane" in ln for ln in clock), f"clock should be sane here: {clock}"
    assert "self-test unmeasurable" not in r.stderr, (
        "a PASSING run must not print the calibration's own instrument-error fixtures"
    )


def test_the_script_refuses_a_verdict_when_its_calibration_fails(tmp_path: Path) -> None:
    """A check that cannot be shown to fail is not evidence that anything passed.

    With a broken classifier the script must decline to say ANYTHING about the real clock, rather
    than hand back a reading it cannot vouch for. It is the posture deploy#591's scanner takes
    (`test_scan_refuses_when_self_test_fails`), and the reason this issue exists at all: an
    unproven guard in front of a proven one is worse than no guard, because it is believed.
    """
    mutated = _mutate('status="skewed"; reason="behind"', 'status="sane"; reason="ok"')
    r = _full_script(tmp_path, skew=0, source=mutated)
    assert "FAILED ITS OWN CALIBRATION" in r.stdout, (
        f"the script reported on the real clock from an uncalibrated instrument: {r.stdout}"
    )
    # And it must not have emitted a verdict line about the real clock.
    assert not any(ln.strip().startswith("HOST_CLOCK") for ln in r.stdout.splitlines()), (
        "the script printed a HOST_CLOCK verdict despite failing its own calibration"
    )
    assert r.returncode != 0

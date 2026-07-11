"""Unit tests for scripts/classify_load_rc.sh (deploy#569 + #570).

The batch load step in deploy-data-load.yml classifies the graph-loader's exit
code into a load-outcome decision. The bug these tests lock down: the workflow
used to treat EVERY non-zero rc as "load failed" and skip the post-load
read-back, so a COMMITTED-with-findings load (rc=5 VALIDATION_FINDINGS) was
mis-reported as a failure and its read-back was hidden.

The rc->decision contract (see the script header, authoritative source is the
da repo's src/exit_codes.py, da#384):

    rc=0  ............... SUCCESS   (graph fully written)
    rc=5  VALIDATION_FINDINGS  FINDINGS  (graph committed; findings present)
    everything else non-zero  FAILURE   (nothing / incomplete / other stage)

The workflow reads the stdout token: SUCCESS and FINDINGS both run the
read-back (the graph committed); only FINDINGS emits a warning; FAILURE skips
the read-back and fails the job. These tests exercise the pure classifier the
same way scripts/tests/test_kafka_logdir_preflight.py exercises its detector.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "classify_load_rc.sh"


def _classify(rc: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(SCRIPT), rc],
        capture_output=True,
        text=True,
        check=False,
    )


def test_script_exists() -> None:
    assert SCRIPT.is_file(), f"classifier script missing at {SCRIPT}"


def test_success_rc_zero() -> None:
    """A clean committed load -> SUCCESS (read-back runs, job green)."""
    result = _classify("0")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "SUCCESS"


def test_validation_findings_rc_five_is_committed_not_failure() -> None:
    """rc=5 VALIDATION_FINDINGS is a COMMITTED load -> FINDINGS, NOT FAILURE.

    This is the core deploy#569/#570 regression: the graph WAS written, so the
    workflow must read it back and warn, never report a failure and hide the
    read-back.
    """
    result = _classify("5")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "FINDINGS"


def test_load_failed_rc_one_is_failure() -> None:
    """rc=1 LOAD_FAILED — nothing committed -> FAILURE (skip read-back, fail)."""
    result = _classify("1")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "FAILURE"


def test_db_unreachable_rc_eight_is_failure() -> None:
    """rc=8 DB_UNREACHABLE — nothing written -> FAILURE."""
    result = _classify("8")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "FAILURE"


def test_refused_rows_rc_six_is_failure_not_findings() -> None:
    """rc=6 REFUSED_ROWS — malformed-id/partial-graph data loss -> FAILURE.

    The graph is partially committed, but an incomplete load is not an
    acceptable outcome for a trustworthy re-run; it must fail the job, not be
    downgraded to a findings warning.
    """
    result = _classify("6")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "FAILURE"


def test_missing_dependency_rc_four_is_failure_not_findings() -> None:
    """rc=4 is MISSING_DEPENDENCY (a resolve-stage abort), NOT findings.

    Guards against the stale framing that rc=4 == VALIDATION_FINDINGS: the
    authoritative da contract puts VALIDATION_FINDINGS at 5 and MISSING_DEPENDENCY
    at 4, so rc=4 must map to FAILURE.
    """
    result = _classify("4")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "FAILURE"


@pytest.mark.parametrize("rc", ["3", "7", "9", "10", "11", "255"])
def test_other_stage_and_unknown_codes_fail_closed(rc: str) -> None:
    """Every other non-zero rc (other stages / unknown) fails closed -> FAILURE."""
    result = _classify(rc)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "FAILURE"


def test_exactly_one_token_on_stdout() -> None:
    """The workflow reads a single decision token — nothing else on stdout."""
    for rc in ("0", "5", "1"):
        out = _classify(rc).stdout
        assert out.strip() in {"SUCCESS", "FINDINGS", "FAILURE"}
        assert len(out.splitlines()) == 1, f"rc={rc} produced multi-line stdout: {out!r}"


def test_missing_argument_is_usage_error() -> None:
    result = subprocess.run(
        ["sh", str(SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "usage" in result.stderr.lower()


def test_non_integer_argument_is_usage_error() -> None:
    result = _classify("abc")
    assert result.returncode == 2
    assert "non-negative integer" in result.stderr

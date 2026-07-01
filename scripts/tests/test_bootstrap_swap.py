"""Unit tests for the Step-5 swap helpers in scripts/bootstrap-vps.sh (deploy#506).

``bootstrap-vps.sh`` is a monolithic, privileged residual-bootstrap script that is
delivered standalone via ``curl … | bash`` (RUNBOOK.md), so its swap logic cannot
live in a sourced library and the full script cannot be run in CI. Instead the two
pure swap helpers are defined inline and exposed through a constrained
``--selftest <helper> [args…]`` dispatch that runs a single helper and exits — with
no bootstrap side effects — so we can exercise them here.

Two latent edges from the review of PR #505 are covered:

  1. dd-fallback size parsing — ``swap_size_to_mib`` must convert a G/M/K
     ``SWAP_SIZE`` to whole MiB (``8`` / ``8G`` / ``8g`` -> ``8192``; ``8192M`` ->
     ``8192``; ``2097152K`` -> ``2048``) and REJECT an unrecognized unit, a
     sub-MiB KiB value (``512K``), or a non-integer instead of silently
     mis-sizing the file via broken ``$(( ))`` arithmetic. Multi-unit parsing
     generalizes the G-only #506 helper (deploy#507).

  2. fstab guard for a foreign active swap — ``swap_fstab_entry_wanted`` must
     succeed only when ``$SWAPFILE`` actually exists, so Step 5 never appends a
     ``/swapfile`` fstab line on a host whose active swap is a swap partition
     (which would ENOENT-fail the .swap unit on reboot).

These helpers exist only after the #506 fix, so the whole module is red against the
pre-fix script (the ``--selftest`` dispatch is absent) and green after it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "bootstrap-vps.sh"


def _selftest(*args: str) -> subprocess.CompletedProcess[str]:
    """Run one swap helper via the script's --selftest dispatch."""
    return subprocess.run(
        ["bash", str(SCRIPT), "--selftest", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_script_exists() -> None:
    assert SCRIPT.is_file(), f"bootstrap script missing at {SCRIPT}"


# ── swap_size_to_mib: accepted GiB forms ────────────────────────────────────


def test_size_default_8g_is_8192_mib() -> None:
    result = _selftest("swap_size_to_mib", "8G")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "8192"


def test_size_lowercase_g_suffix() -> None:
    result = _selftest("swap_size_to_mib", "8g")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "8192"


def test_size_bare_integer_is_treated_as_gib() -> None:
    result = _selftest("swap_size_to_mib", "4")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "4096"


# ── swap_size_to_mib: accepted MiB / KiB forms (deploy#507) ─────────────────


def test_size_mebibyte_suffix_accepted() -> None:
    """The exact override from the #506 issue (4096M) now parses to 4096 MiB."""
    result = _selftest("swap_size_to_mib", "4096M")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "4096"


def test_size_mebibyte_matches_default_8g() -> None:
    """8192M is the MiB spelling of the 8G default — same 8192 result."""
    result = _selftest("swap_size_to_mib", "8192M")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "8192"


def test_size_lowercase_m_suffix() -> None:
    result = _selftest("swap_size_to_mib", "512m")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "512"


def test_size_kibibyte_whole_mib_accepted() -> None:
    """A KiB value divisible by 1024 is a whole number of MiB: 2097152K -> 2048."""
    result = _selftest("swap_size_to_mib", "2097152K")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "2048"


def test_size_lowercase_k_suffix_whole_mib() -> None:
    result = _selftest("swap_size_to_mib", "1048576k")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "1024"


# ── swap_size_to_mib: rejected (sub-MiB / bad unit / malformed) forms ───────


def test_size_sub_mib_kibibyte_is_rejected() -> None:
    """512K is 0.5 MiB — not expressible as an integer dd `count=`, so rejected."""
    result = _selftest("swap_size_to_mib", "512K")
    assert result.returncode != 0
    assert "whole number of MiB" in result.stderr
    assert result.stdout.strip() == ""


def test_size_terabyte_suffix_is_rejected() -> None:
    """An unrecognized unit (T) is rejected, not silently mis-sized."""
    result = _selftest("swap_size_to_mib", "1T")
    assert result.returncode != 0
    assert "unsupported SWAP_SIZE" in result.stderr
    assert result.stdout.strip() == ""


def test_size_double_unit_is_rejected() -> None:
    """`8GB` strips only the trailing G, leaving `8B` — must be rejected."""
    result = _selftest("swap_size_to_mib", "8GB")
    assert result.returncode != 0


def test_size_empty_is_rejected() -> None:
    result = _selftest("swap_size_to_mib", "")
    assert result.returncode != 0


def test_size_non_numeric_is_rejected() -> None:
    result = _selftest("swap_size_to_mib", "lots")
    assert result.returncode != 0


# ── swap_fstab_entry_wanted: existence-gated fstab guard ─────────────────────


def test_fstab_wanted_true_when_swapfile_exists(tmp_path: Path) -> None:
    swapfile = tmp_path / "swapfile"
    swapfile.write_bytes(b"")
    result = _selftest("swap_fstab_entry_wanted", str(swapfile))
    assert result.returncode == 0, result.stderr


def test_fstab_not_wanted_when_swapfile_absent(tmp_path: Path) -> None:
    """Foreign active swap (a partition) leaves $SWAPFILE absent → no fstab line."""
    absent = tmp_path / "swapfile"  # never created
    result = _selftest("swap_fstab_entry_wanted", str(absent))
    assert result.returncode != 0


# ── --selftest dispatch is restricted to the known helpers (runs as root) ────


def test_selftest_rejects_unknown_target() -> None:
    result = _selftest("id")  # not a swap helper — must not execute
    assert result.returncode == 2
    assert "unknown --selftest target" in result.stderr

"""Unit tests for scripts/kafka_logdir_preflight.sh (deploy#428).

The preflight scanner runs inside the apache/kafka container against the
persisted ``kafka_data`` volume during deploy. A real volume is not available
in CI, so we exercise the pure directory-classification logic against fixture
directory layouts built in a tmp dir — clean vs stray-config / stray-data —
asserting the exit code and the reported offenders.

Exit-code contract (see the script header):
    0  clean (also: log-dir absent/empty — a fresh volume)
    1  usage error (no log-dir argument)
    2  stray non-topic directories found
"""

from __future__ import annotations

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "kafka_logdir_preflight.sh"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _mk_topic_dirs(root: Path, names: list[str]) -> None:
    for name in names:
        (root / name).mkdir(parents=True)


def test_script_exists_and_executable() -> None:
    assert SCRIPT.is_file(), f"detector script missing at {SCRIPT}"


def test_clean_logdir_passes(tmp_path: Path) -> None:
    """Valid topic-partition dirs + legitimate root files → exit 0."""
    _mk_topic_dirs(
        tmp_path,
        ["__cluster_metadata-0", "pipeline.dedup.done-0", "pipeline.dlq-2"],
    )
    root_files = (
        "meta.properties",
        "bootstrap.checkpoint",
        "recovery-point-offset-checkpoint",
        ".lock",
    )
    for f in root_files:
        (tmp_path / f).write_text("")
    result = _run(str(tmp_path))
    assert result.returncode == 0, result.stderr + result.stdout


def test_stray_config_dir_is_detected(tmp_path: Path) -> None:
    """The Bitnami-era ``config/`` leftover that triggered #428 → exit 2."""
    _mk_topic_dirs(tmp_path, ["__cluster_metadata-0", "config"])
    (tmp_path / "meta.properties").write_text("")
    result = _run(str(tmp_path))
    assert result.returncode == 2, result.stdout
    assert "config" in result.stdout
    # The valid topic-partition dir must NOT be reported as stray.
    assert "__cluster_metadata-0" not in result.stdout


def test_stray_data_and_lost_found_detected(tmp_path: Path) -> None:
    """A nested ``data/`` mis-layout and an ext4 ``lost+found/`` are stray."""
    _mk_topic_dirs(tmp_path, ["__cluster_metadata-0", "data", "lost+found"])
    result = _run(str(tmp_path))
    assert result.returncode == 2, result.stdout
    assert "data" in result.stdout
    assert "lost+found" in result.stdout


def test_delete_and_future_suffix_dirs_are_valid(tmp_path: Path) -> None:
    """In-progress delete/future replica dirs are valid topic-partition forms."""
    _mk_topic_dirs(
        tmp_path,
        [
            "topic-0",
            "pipeline.raw.landed-1.abc123ef-delete",
            "pipeline.enrich.done-3.0000000000000000abcd-future",
        ],
    )
    result = _run(str(tmp_path))
    assert result.returncode == 0, result.stdout


def test_empty_logdir_passes(tmp_path: Path) -> None:
    """A fresh (empty) volume has nothing to reject → exit 0."""
    result = _run(str(tmp_path))
    assert result.returncode == 0, result.stdout


def test_missing_logdir_passes(tmp_path: Path) -> None:
    """A non-existent log dir (volume not yet formatted) is treated as clean."""
    result = _run(str(tmp_path / "does-not-exist"))
    assert result.returncode == 0, result.stdout


def test_no_argument_is_usage_error() -> None:
    result = _run()
    assert result.returncode == 1
    assert "usage" in result.stderr.lower()

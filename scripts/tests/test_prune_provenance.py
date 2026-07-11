"""BEHAVIOURAL tests for the prune provenance gate — it is RUN, not grepped.

This file exists because the previous guard was pinned by substring. Weronika Zielinska
mutated the workflow to forge a manifest on the box when B2 had none — three lines,
``[ -s "$M" ] || printf 'CANONICAL_MANIFEST …' > "$M"`` — and the whole suite still
reported ``10 passed``. The property that justifies merging deploy#574 ahead of da#428,
that the prune is **inert without provenance**, was guarded by a layer that could not see
it being removed. A guard asserted by substring is not a guard.

So the gate was extracted to ``scripts/verify_prune_provenance.sh`` and these tests
**execute** it against fixture directories and watch it refuse. A forged manifest, an
``env``-var bypass, or a relaxed condition now fails here regardless of what the workflow
text happens to say, because nothing here reads the workflow text.

The contract under test (da#428) splits provenance by **who can honestly attest to what**:

* ``_resolve_run.txt`` — written by **resolve**, the process that produced the parquet. It
  alone has an in-memory tally taken *before* the write, so its count does not agree with a
  short file, and it alone knows whether the run finished.
* ``_manifest.txt`` — written by ``publish_parquet.py``, a **separate process** that starts
  after resolve exits, reads ``data/curated/`` off disk and uploads it (no pyarrow import,
  no tally — verified at da ``origin/main``). It may declare only **md5 and bytes**. Any
  count it computed would be a read-back of the file it is uploading, which agrees with a
  truncated one *by construction*.

That split is what makes a read-back implementation of da#428 structurally impossible
rather than merely discouraged — so the gate **refuses a manifest that carries a count**.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
GATE = SCRIPTS_DIR / "verify_prune_provenance.sh"

PARQUET = "narrators_canonical.parquet"
GOOD_COUNT = 129234


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()  # noqa: S324 — matches the manifest's own digest


def _curated(
    tmp_path: Path,
    *,
    parquet: bytes | None = b"PAR1-pretend-canonical-parquet",
    resolve_run: str | None = None,
    manifest: str | None = None,
    md5_override: str | None = None,
) -> Path:
    """Build a curated/ dir. Defaults are a COMPLETE, self-consistent, honest artifact."""
    d = tmp_path / "curated"
    d.mkdir(parents=True, exist_ok=True)

    if parquet is not None:
        (d / PARQUET).write_bytes(parquet)

    if resolve_run is None and parquet is not None:
        resolve_run = (
            f"RESOLVE_RUN canonical_ids={GOOD_COUNT} run_status=complete git_sha=abc1234\n"
        )
    if resolve_run is not None:
        (d / "_resolve_run.txt").write_text(resolve_run)

    if manifest is None and parquet is not None:
        digest = md5_override or _md5(parquet)
        manifest = f"CANONICAL_MANIFEST file={PARQUET} md5={digest} bytes={len(parquet)}\n"
    if manifest is not None:
        (d / "_manifest.txt").write_text(manifest)

    return d


def _run(curated: Path, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["bash", str(GATE)],  # noqa: S607
        env={**os.environ, "CURATED_DIR": str(curated), **env},
        capture_output=True,
        text=True,
        check=False,
    )


# ---------------------------------------------------------------------------
# The positive case — and it must actually pass, or every refusal below is vacuous.
# ---------------------------------------------------------------------------
def test_honest_complete_artifact_is_accepted(tmp_path: Path) -> None:
    r = _run(_curated(tmp_path))
    assert r.returncode == 0, f"the gate rejected an honest artifact:\n{r.stderr}"
    assert f"PRUNE_PROVENANCE canonical_ids={GOOD_COUNT}" in r.stdout
    assert "run_status=complete" in r.stdout


# ---------------------------------------------------------------------------
# INERTNESS — the property that lets deploy#574 merge ahead of da#428.
# ---------------------------------------------------------------------------
def test_no_provenance_at_all_refuses(tmp_path: Path) -> None:
    """Every ref published today looks exactly like this. The prune must be inert."""
    d = tmp_path / "curated"
    d.mkdir()
    (d / PARQUET).write_bytes(b"PAR1-pretend")
    r = _run(d)
    assert r.returncode != 0, "a keep-set with NO provenance must not be prunable"
    assert "_resolve_run.txt" in r.stderr


def test_manifest_alone_is_not_provenance(tmp_path: Path) -> None:
    """A manifest without the resolve record proves only that bytes moved.

    This is the forge Weronika ran: synthesise a plausible manifest and the old textual
    guard was satisfied. The publisher cannot attest to completeness, so a manifest on its
    own must not unlock the prune.
    """
    d = _curated(tmp_path, resolve_run="")
    (d / "_resolve_run.txt").unlink(missing_ok=True)
    r = _run(d)
    assert r.returncode != 0, "a manifest alone must not satisfy the provenance gate"
    assert "_resolve_run.txt" in r.stderr


def test_resolve_record_alone_refuses(tmp_path: Path) -> None:
    """No manifest means the bytes here cannot be tied to the bytes published."""
    d = _curated(tmp_path)
    (d / "_manifest.txt").unlink()
    r = _run(d)
    assert r.returncode != 0
    assert "_manifest.txt" in r.stderr


def test_gate_never_creates_provenance_itself(tmp_path: Path) -> None:
    """The gate must REFUSE a missing record, never synthesise one.

    Directly pins the shape of Weronika's mutation: a step that writes the artifact it is
    supposed to be checking. If the gate ever manufactures its own evidence, this fails.
    """
    d = tmp_path / "curated"
    d.mkdir()
    (d / PARQUET).write_bytes(b"PAR1-pretend")
    _run(d)
    assert not (d / "_resolve_run.txt").exists(), "the gate fabricated a resolve record"
    assert not (d / "_manifest.txt").exists(), "the gate fabricated a manifest"


# ---------------------------------------------------------------------------
# The relocation of the circularity — the finding this whole file is named for.
# ---------------------------------------------------------------------------
def test_manifest_carrying_a_count_is_refused(tmp_path: Path) -> None:
    """A publisher that declares a count has READ THE PARQUET BACK.

    ``publish_parquet.py`` is a separate process from resolve, with no pyarrow import and
    no tally. The only count it could compute is ``pq.read_table(...).num_rows`` of the file
    it is uploading — which agrees perfectly with a truncated one. Such a count would sail
    through every gate wearing the words "producer-signed provenance" and a green md5.

    So the gate refuses it outright. This is what makes the read-back implementation of
    da#428 *structurally impossible* instead of merely discouraged.
    """
    payload = b"PAR1-pretend"
    d = _curated(
        tmp_path,
        parquet=payload,
        manifest=(
            f"CANONICAL_MANIFEST file={PARQUET} canonical_ids={GOOD_COUNT} "
            f"md5={_md5(payload)} bytes={len(payload)}\n"
        ),
    )
    r = _run(d)
    assert r.returncode != 0, (
        "a manifest declaring canonical_ids must be refused — the publisher has no tally, "
        "so that count is a read-back and agrees with a short parquet"
    )
    assert "canonical_ids" in r.stderr


# ---------------------------------------------------------------------------
# Run completion — invisible to any count equality of any kind.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("status", ["partial", "stopped_at_limit", "failed"])
def test_incomplete_run_is_refused(tmp_path: Path, status: str) -> None:
    """A run that halted after writing a coherent partial file is self-consistent.

    Its tally — taken before the write — matches what it managed to write, `missing` is 0,
    and every derived gate goes green. Only the producer can attest that it reached the end,
    so it must say so explicitly and we must check.
    """
    d = _curated(
        tmp_path,
        resolve_run=f"RESOLVE_RUN canonical_ids={GOOD_COUNT} run_status={status} git_sha=abc1234\n",
    )
    r = _run(d)
    assert r.returncode != 0, f"run_status={status} must not be prunable"
    assert "did NOT complete" in r.stderr


def test_missing_run_status_is_refused(tmp_path: Path) -> None:
    """An absent field must be a stop, not an empty string that reads as fine.

    The parser this replaces hard-coded ``[0-9a-f]`` and could not read a word value at
    all — it would have returned empty here and, unchecked, waved the artifact through.
    """
    d = _curated(tmp_path, resolve_run=f"RESOLVE_RUN canonical_ids={GOOD_COUNT} git_sha=abc1234\n")
    r = _run(d)
    assert r.returncode != 0
    assert "run_status" in r.stderr


def test_run_status_is_parsed_as_a_word_not_silently_dropped(tmp_path: Path) -> None:
    """Positive control for the parser fix: a WORD value must actually be read.

    Without this, `test_incomplete_run_is_refused` could pass for the wrong reason — an
    unparseable `run_status` refused as "missing" rather than as "not complete".
    """
    d = _curated(tmp_path)
    r = _run(d)
    assert r.returncode == 0
    assert "run_status=complete" in r.stdout, "the word value was not parsed"


# ---------------------------------------------------------------------------
# Byte integrity — a SEPARATE property from completeness.
# ---------------------------------------------------------------------------
def test_md5_mismatch_is_refused(tmp_path: Path) -> None:
    d = _curated(tmp_path, md5_override="0" * 32)
    r = _run(d)
    assert r.returncode != 0
    assert "md5 mismatch" in r.stderr


def test_zero_canonical_ids_is_refused(tmp_path: Path) -> None:
    """A zero-id keep-set would delete every narrator."""
    d = _curated(
        tmp_path, resolve_run="RESOLVE_RUN canonical_ids=0 run_status=complete git_sha=a\n"
    )
    r = _run(d)
    assert r.returncode != 0


# ---------------------------------------------------------------------------
# The operator count is corroboration, and cannot skip the gate.
# ---------------------------------------------------------------------------
def test_operator_count_is_optional(tmp_path: Path) -> None:
    r = _run(_curated(tmp_path), EXPECTED_CANONICAL_IDS="")
    assert r.returncode == 0, "a blank operator count must simply be skipped"


def test_operator_count_disagreeing_is_refused(tmp_path: Path) -> None:
    """Catches the one thing the producer's record cannot: the wrong ref, meant-A-typed-B."""
    r = _run(_curated(tmp_path), EXPECTED_CANONICAL_IDS="999")
    assert r.returncode != 0
    assert "disagree" in r.stderr


@pytest.mark.parametrize(
    "bypass",
    [
        {"SKIP_PROVENANCE": "true"},
        {"ALLOW_NO_MANIFEST": "true"},
        {"BYPASS_COMPLETENESS": "1"},
        {"FORCE": "true"},
        {"DRY_RUN": "false"},
        {"EXPECTED_CANONICAL_IDS": str(GOOD_COUNT)},
    ],
)
def test_no_environment_variable_can_unlock_a_provenance_less_artifact(
    tmp_path: Path, bypass: dict[str, str]
) -> None:
    """The `env:` surface is a bypass channel the input allow-list cannot see (deploy#579).

    A workflow-level `SKIP_PROVENANCE: ${{ vars.SKIP_PROVENANCE }}` leaves `set(inputs)`
    untouched, so the input allow-list stays green while the guard is gutted. This asserts
    the property where it actually lives — in the gate's behaviour — so no env var, named or
    unnamed, can make a provenance-less artifact prunable.
    """
    d = tmp_path / "curated"
    d.mkdir()
    (d / PARQUET).write_bytes(b"PAR1-pretend")
    r = _run(d, **bypass)
    assert r.returncode != 0, (
        f"env {bypass} unlocked a keep-set with no provenance — the gate must have no "
        "environment-variable escape hatch"
    )

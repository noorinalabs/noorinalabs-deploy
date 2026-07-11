"""BEHAVIOURAL tests for the WORKFLOW↔GATE SEAM — the ssh block is EXECUTED, not grepped.

This file exists because of a bug that CI, shellcheck, and 21 green behavioural tests all
missed: the caller's `canonical_ids` extractor was written single-quoted with doubled
backslashes, so sed received a literal ``\\(`` and **matched nothing, ever**. The gate would
emit ``canonical_ids=129234``; the caller would extract empty; the workflow would abort on
the first *honest* artifact anyone published — and the completeness binding, the one gate
standing between us and da#426's 83.9% deletion, was **unreachable dead code**.

Every layer was green. **The workflow could not prune.**

Two lessons are baked in here:

1. **Hardening the gate did not harden the hand that feeds it.** Forging provenance *inside*
   ``verify_prune_provenance.sh`` is caught by test_prune_provenance.py. Forging it *in the
   caller* — the identical three lines, one file over — was invisible: it issues a real prune
   on a short parquet with not one test red. A guard is only as good as its caller.

2. **A positive control on the caller's extraction would alone have caught it.** An
   extractor that can never match returns empty, and empty reads as "absent". That is the
   silent zero this whole workflow exists to refuse, and it was sitting in the code that
   refuses it.

So these tests run the real ssh `script:` body against fixtures, with the external world
(git/docker/rclone/cypher-shell) stubbed on PATH. The stubs are deliberately dumb: they
record what they were asked to do, so a test can assert **that no prune was issued**, which
is the only assertion that actually means "inert".
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest
import yaml

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SCRIPTS_DIR.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "graph-prune-narrators.yml"

PARQUET = "narrators_canonical.parquet"
GOOD_COUNT = 129234
GRAPH_TOTAL = 160614
# da#426's measured short-parquet case: f=0.20 of the canonical rows retained.
SHORT_COUNT = 25847


def _ssh_script() -> str:
    doc = yaml.safe_load(WORKFLOW.read_text())
    for step in doc["jobs"]["prune-narrators"]["steps"]:
        if step.get("id") == "prune":
            return str(step["with"]["script"])
    raise AssertionError("no step id 'prune'")


def _stub(bin_dir: Path, name: str, body: str) -> None:
    p = bin_dir / name
    p.write_text("#!/usr/bin/env bash\n" + body)
    p.chmod(0o755)


def _sandbox(
    tmp_path: Path,
    *,
    canonical_in_parquet: int = GOOD_COUNT,
    declared: int | None = GOOD_COUNT,
    run_status: str = "complete",
    publish_provenance: bool = True,
    manifest_md5_correct: bool = True,
) -> tuple[Path, Path]:
    """A fake VPS. Returns (repo_root, b2_dir).

    `canonical_in_parquet` is what the loader's dry-run REPORTS reading out of the parquet;
    `declared` is what resolve's record says it wrote. Making these differ is exactly the
    da#426 short-parquet case, and the completeness binding must catch it.
    """
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "compose").mkdir()
    # The real gate — not a stub. The seam under test is workflow -> THIS.
    (root / "scripts" / "verify_prune_provenance.sh").write_bytes(
        (SCRIPTS_DIR / "verify_prune_provenance.sh").read_bytes()
    )
    (root / "compose" / "docker-compose.prod.yml").write_text("services: {}\n")
    (root / ".env").write_text("NEO4J_PASSWORD=x\n")

    b2 = tmp_path / "b2" / "curated"
    b2.mkdir(parents=True)
    payload = b"PAR1" + b"x" * 32
    (b2 / PARQUET).write_bytes(payload)
    if publish_provenance:
        if declared is not None:
            (b2 / "_resolve_run.txt").write_text(
                f"RESOLVE_RUN canonical_ids={declared} run_status={run_status} git_sha=abc\n"
            )
        digest = hashlib.md5(payload).hexdigest() if manifest_md5_correct else "0" * 32  # noqa: S324
        (b2 / "_manifest.txt").write_text(
            f"CANONICAL_MANIFEST file={PARQUET} md5={digest} bytes={len(payload)}\n"
        )

    bins = tmp_path / "bin"
    bins.mkdir()
    audit = tmp_path / "audit.log"

    _stub(bins, "git", "exit 0\n")
    _stub(
        bins,
        "rclone",
        f'''
case "$1" in
  lsf)  ls "{b2}" 2>/dev/null ;;
  copy) mkdir -p "$3"; cp "{b2}"/* "$3"/ 2>/dev/null || true ;;
esac
exit 0
''',
    )
    # The loader. `prune-narrators --dry-run` reports what the PARQUET actually holds.
    _stub(
        bins,
        "docker",
        f'''
echo "$@" >> "{audit}"
for a in "$@"; do
  case "$a" in
    *"count(n)"*)
      echo "narrators"
      if grep -q REAL_PRUNE_ISSUED "{audit}" 2>/dev/null; then
        echo "{canonical_in_parquet}"
      else
        echo "{GRAPH_TOTAL}"
      fi
      exit 0 ;;
  esac
done
for a in "$@"; do
  if [ "$a" = "prune-narrators" ]; then
    for b in "$@"; do [ "$b" = "--dry-run" ] && DRY=1; done
    ORPH=$(( {GRAPH_TOTAL} - {canonical_in_parquet} ))
    S="PRUNE_NARRATORS_SUMMARY canonical={canonical_in_parquet} graph_total={GRAPH_TOTAL}"
    if [ -n "${{DRY:-}}" ]; then
      # dry-run: orphans = would-delete, deleted = 0
      echo "$S orphans=${{ORPH}} deleted=0 missing=0"
    else
      # real run: `orphans` is the POST-count and is 0 — da#413's contract, and Gate D
      # asserts it. A stub that reports orphans=${{ORPH}} after a real prune describes an
      # impossible graph (it deleted them and they are still there), and the workflow
      # correctly refuses it. That bug lived here undetected because the old positive
      # control asserted on a string printed BEFORE the prune, so it never got this far.
      echo "REAL_PRUNE_ISSUED" >> "{audit}"
      echo "$S orphans=0 deleted=${{ORPH}} missing=0"
    fi
    exit 0
  fi
done
exit 0
''',
    )
    return root, audit


def _run_ssh_block(root: Path, tmp_path: Path, **env: str) -> subprocess.CompletedProcess[str]:
    # The ONLY edit to the real ssh body is the checkout path — everything else, including
    # every guard, runs exactly as it does on the VPS. That is the point: a harness that
    # rewrites the code under test proves things about the rewrite.
    script = _ssh_script().replace("cd /opt/noorinalabs-deploy", f'cd "{root}"')
    path = f"{tmp_path / 'bin'}:{os.environ['PATH']}"
    return subprocess.run(  # noqa: S603
        ["bash", "-c", script],  # noqa: S607
        env={
            **os.environ,
            "PATH": path,
            "HOME": str(tmp_path),
            "ENV_NAME": "stg",
            "PARQUET_REF": "staged/narrator-resolve/test",
            "EXPECTED_CANONICAL_IDS": "",
            "DRY_RUN": "true",
            "IMAGE": "img:test",
            "PIPELINE_B2_KEY_ID": "k",
            "PIPELINE_B2_KEY": "s",
            "GITHUB_TOKEN": "t",
            "GITHUB_ACTOR": "a",
            **env,
        },
        capture_output=True,
        text=True,
        check=False,
    )


# ---------------------------------------------------------------------------
# THE POSITIVE CONTROL. This alone would have caught the shipped bug.
# ---------------------------------------------------------------------------
def test_caller_actually_extracts_the_producer_tally(tmp_path: Path) -> None:
    """The caller must READ the number the gate emits.

    The shipped extractor was single-quoted with doubled backslashes and matched nothing, so
    the workflow aborted on an honest artifact with "provenance gate exited 0 but emitted no
    canonical_ids" — and the completeness binding below it never ran. Every other test was
    green. An extractor that can never match is a silent zero.
    """
    root, _ = _sandbox(tmp_path)
    r = _run_ssh_block(root, tmp_path)
    assert "provenance gate exited 0 but emitted no canonical_ids" not in r.stdout + r.stderr, (
        "the caller failed to parse the gate's output — the extractor matches nothing"
    )
    assert f"resolve declared {GOOD_COUNT} distinct canonical ids" in r.stdout, (
        f"the caller did not extract the tally.\nstdout:\n{r.stdout}\nstderr:\n{r.stderr}"
    )
    assert r.returncode == 0, f"an honest artifact must not abort:\n{r.stderr}"


# ---------------------------------------------------------------------------
# INERTNESS — asserted on BEHAVIOUR (no prune issued), not on text.
# ---------------------------------------------------------------------------
def test_no_provenance_in_b2_issues_no_prune(tmp_path: Path) -> None:
    """Every ref published today looks like this."""
    root, audit = _sandbox(tmp_path, publish_provenance=False)
    r = _run_ssh_block(root, tmp_path)
    assert r.returncode != 0, "a keep-set with no provenance must abort the workflow"
    assert "REAL_PRUNE_ISSUED" not in (audit.read_text() if audit.exists() else "")


def test_caller_does_not_forge_provenance(tmp_path: Path) -> None:
    """Nino moved Weronika's forge from the GATE into the CALLER — three lines, one file over.

    Forging inside the gate is caught by test_prune_provenance.py. Forging here was invisible:
    it issues a real prune on the da#426 short parquet with not one test red. So assert on the
    pulled directory: the caller must never manufacture the evidence it is about to check.
    """
    root, audit = _sandbox(tmp_path, publish_provenance=False)
    _run_ssh_block(root, tmp_path)

    # UNCONDITIONAL. These used to sit behind `if data.exists():` — and with no provenance in
    # B2, `verify_present` aborts BEFORE the rclone pull ever creates that directory, so
    # `data.exists()` was False, both assertions were SKIPPED, and the test passed on its
    # remaining line — which is satisfied by verify_present refusing and says nothing about
    # forging (deploy#581). The test named for the anti-forge property did not test it.
    #
    # A non-existent path does not exist, so dropping the guard costs nothing in the honest
    # case and makes the assertions bite in the case that matters: a caller which drops the
    # objects from verify_present AND synthesizes them locally reaches the pull, creates this
    # directory, and fills it with its own evidence.
    data = tmp_path / ".cache" / "noorinalabs-graph-prune" / "data" / "curated"
    assert not (data / "_resolve_run.txt").exists(), "the caller fabricated a resolve record"
    assert not (data / "_manifest.txt").exists(), "the caller fabricated a manifest"
    assert "REAL_PRUNE_ISSUED" not in (audit.read_text() if audit.exists() else "")


# ---------------------------------------------------------------------------
# The completeness binding — demonstrated, not reasoned about.
# ---------------------------------------------------------------------------
def test_da426_short_parquet_is_blocked_end_to_end(tmp_path: Path) -> None:
    """The parquet holds 25,847 ids; resolve declared 129,234. This is da#426's f=0.20.

    Left alone it deletes 83.9% of the graph with missing=0 and every derived gate green. The
    completeness binding must stop it — and this asserts it *runs*, which is precisely what
    the extractor bug prevented.
    """
    root, audit = _sandbox(tmp_path, canonical_in_parquet=SHORT_COUNT, declared=GOOD_COUNT)
    r = _run_ssh_block(root, tmp_path, DRY_RUN="false")
    assert r.returncode != 0, "a short parquet must be refused"
    assert "COMPLETENESS CHECK FAILED" in r.stdout + r.stderr, (
        "the completeness binding did not fire — it may be unreachable"
    )
    assert "REAL_PRUNE_ISSUED" not in (audit.read_text() if audit.exists() else "")


def test_honest_artifact_reaches_the_real_prune(tmp_path: Path) -> None:
    """The positive control for the whole seam — and it must assert on the SENTINEL.

    Every negative in this file asserts ``REAL_PRUNE_ISSUED not in audit``. Those assertions
    are worth exactly nothing unless something establishes that the audit trail CAN record a
    prune. An earlier version of this test — despite its name — never checked that the real
    prune was reached: its only assertion was on ``"COMPLETENESS: parquet yields exactly"``,
    **a string printed BEFORE the prune is issued**. It would have printed identically had the
    workflow aborted one line later.

    Weronika Zielinska disabled the sentinel entirely (the docker stub never writes
    ``REAL_PRUNE_ISSUED``, so the audit trail is dead) and the suite reported **8 passed**.
    Every "no prune was issued" assertion in this file was vacuous: they would all pass
    against a harness that can never record a prune at all.

    That is the exact shape this whole PR has been about — a green that means either the
    guard held or the instrument recorded nothing — sitting inside the harness whose entire
    job is to prove the guard holds. So this test now asserts the prune was REALLY issued,
    which is what arms every negative below it.
    """
    root, audit = _sandbox(tmp_path)
    r = _run_ssh_block(root, tmp_path, DRY_RUN="false")
    assert r.returncode == 0, (
        f"an honest artifact must prune cleanly.\nstdout:\n{r.stdout}\nstderr:\n{r.stderr}"
    )
    assert "COMPLETENESS: parquet yields exactly" in r.stdout, (
        f"the completeness binding did not pass an honest artifact.\n{r.stdout}\n{r.stderr}"
    )
    # THE line that arms the rest of the file.
    assert audit.exists() and "REAL_PRUNE_ISSUED" in audit.read_text(), (
        "the audit trail did not record a prune on an HONEST artifact. Either the workflow "
        "never reached the real prune, or the sentinel is dead — and if the sentinel is dead, "
        "every `REAL_PRUNE_ISSUED not in audit` assertion in this file is vacuous."
    )


@pytest.mark.parametrize("status", ["stopped_at_limit", "partial"])
def test_incomplete_run_blocked_at_the_seam(tmp_path: Path, status: str) -> None:
    root, audit = _sandbox(tmp_path, run_status=status)
    r = _run_ssh_block(root, tmp_path, DRY_RUN="false")
    assert r.returncode != 0
    assert "REAL_PRUNE_ISSUED" not in (audit.read_text() if audit.exists() else "")


def test_bad_md5_blocked_at_the_seam(tmp_path: Path) -> None:
    root, audit = _sandbox(tmp_path, manifest_md5_correct=False)
    r = _run_ssh_block(root, tmp_path, DRY_RUN="false")
    assert r.returncode != 0
    assert "REAL_PRUNE_ISSUED" not in (audit.read_text() if audit.exists() else "")

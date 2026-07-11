"""BEHAVIOURAL tests for the LOAD↔STAMP seam — deploy-data-load's ssh block is EXECUTED.

The prune gate (deploy#580) refuses unless the graph says which resolve run it was loaded
from. That is worth exactly nothing unless the LOAD actually writes it. A stamp asserted by
grepping the workflow's text is not a stamp — this repo has now shipped that mistake twice
(a forged manifest inside the gate, then the identical forge relocated to the caller, both
green). So the real ssh body runs here against stubs, and the assertions are on WHAT IT DID.

The property under test is not merely "a stamp is written". It is the ORDER:

    in_progress   BEFORE the loader touches the graph
    complete      ONLY after the loader commits

A load that dies mid-write therefore leaves the graph stamped `in_progress`, and the prune
gate refuses it against ANY ref — including its own. That is the fail-closed direction and it
matters: a half-loaded graph holds narrators that no keep-set names, so a prune deletes
exactly those. rc=6 (REFUSED_ROWS) is precisely that shape — a partial graph — and it must
NOT reach `complete`.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SCRIPTS_DIR.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "deploy-data-load.yml"

REF = "staged/narrator-resolve/2026-07-11-4f2a1c9"
IMAGE = "ghcr.io/noorinalabs/noorinalabs-data-acquisition-load:stg-latest"

# da's exit-code contract (src/exit_codes.py), as classify_load_rc.sh reads it.
RC_SUCCESS = 0
RC_LOAD_FAILED = 1
RC_VALIDATION_FINDINGS = 5  # the graph IS committed; findings are advisory
RC_REFUSED_ROWS = 6  # a PARTIAL graph — the case the two-phase stamp exists for


def _ssh_script() -> str:
    doc = yaml.safe_load(WORKFLOW.read_text())
    for step in doc["jobs"]["data-load"]["steps"]:
        if step.get("id") == "load":
            return str(step["with"]["script"])
    raise AssertionError("no step id 'load' in deploy-data-load.yml")


def _stub(bin_dir: Path, name: str, body: str) -> None:
    p = bin_dir / name
    p.write_text("#!/usr/bin/env bash\n" + body)
    p.chmod(0o755)


def _sandbox(tmp_path: Path, *, loader_rc: int = RC_SUCCESS) -> tuple[Path, Path]:
    """A fake VPS. Returns (repo_root, audit_log).

    The audit log is an ORDERED record of what the workflow asked docker to do, which is what
    lets a test assert that the stamp preceded the load rather than merely accompanied it.
    """
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "compose").mkdir()
    # The REAL scripts — not stubs. The seam under test is workflow -> THESE.
    for s in ("graph_load_provenance.sh", "classify_load_rc.sh"):
        (root / "scripts" / s).write_bytes((SCRIPTS_DIR / s).read_bytes())
    (root / "compose" / "docker-compose.prod.yml").write_text("services: {}\n")
    (root / ".env").write_text("NEO4J_PASSWORD=x\n")

    bins = tmp_path / "bin"
    bins.mkdir()
    audit = tmp_path / "audit.log"

    _stub(bins, "git", "exit 0\n")
    _stub(
        bins,
        "rclone",
        """
case "$1" in
  lsf)  echo narrators_canonical.parquet
        echo narrator_mentions_resolved.parquet
        echo narrator_mentions_resolved_muhaddithat.parquet
        echo hadiths_bukhari.parquet
        echo collections_all.parquet
        echo network_edges_a.parquet
        echo parallel_links.parquet ;;
  copy) mkdir -p "$3"; : > "$3/narrators_canonical.parquet" ;;
esac
exit 0
""",
    )
    # Records every docker invocation IN ORDER. A stamp is a MERGE on :LoadProvenance; the
    # loader is the only `run`. Both land in the same log, so ORDER is assertable.
    _stub(
        bins,
        "docker",
        f'''
for a in "$@"; do
  case "$a" in
    *"MERGE (p:LoadProvenance"*)
      status="$(printf '%s' "$a" | sed -n "s/.*p.load_status = '\\([a-z_]*\\)'.*/\\1/p")"
      ref="$(printf '%s' "$a" | sed -n "s/.*p.parquet_ref = '\\([^']*\\)'.*/\\1/p")"
      echo "STAMP status=${{status}} ref=${{ref}}" >> "{audit}"
      exit 0 ;;
    *"MATCH (h:Hadith)"*)
      echo "source_corpus, hadiths"; echo '"bukhari", 7563'
      exit 0 ;;
  esac
done
for a in "$@"; do
  if [ "$a" = "run" ]; then
    echo "LOADER_RAN" >> "{audit}"
    exit {loader_rc}
  fi
done
exit 0
''',
    )
    return root, audit


def _run_ssh_block(root: Path, tmp_path: Path, **env: str) -> subprocess.CompletedProcess[str]:
    # The ONLY edit to the real ssh body is the checkout path. Every guard runs exactly as it
    # does on the VPS — a harness that rewrites the code under test proves things about the
    # rewrite.
    script = _ssh_script().replace("cd /opt/noorinalabs-deploy", f'cd "{root}"')
    return subprocess.run(  # noqa: S603
        ["bash", "-c", script],  # noqa: S607
        env={
            **os.environ,
            "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "ENV_NAME": "stg",
            "PARQUET_REF": REF,
            "LOAD_ARGS": "load",
            "DRY_RUN": "false",
            "IMAGE": IMAGE,
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


def _audit(audit: Path) -> list[str]:
    return audit.read_text().splitlines() if audit.exists() else []


# ---------------------------------------------------------------------------
# THE POSITIVE CONTROL — and it arms every negative below it.
# ---------------------------------------------------------------------------
def test_a_committed_load_stamps_the_ref_it_loaded(tmp_path: Path) -> None:
    """The whole of deploy#580 rests on this actually happening.

    If the load does not stamp, the prune gate refuses forever and the feature is an outage;
    if the stamp records the wrong ref, the gate is worse than useless. Assert the ref VALUE,
    not merely that some stamp occurred.
    """
    root, audit = _sandbox(tmp_path, loader_rc=RC_SUCCESS)
    r = _run_ssh_block(root, tmp_path)
    log = _audit(audit)
    assert r.returncode == 0, f"a clean load must succeed:\n{r.stdout}\n{r.stderr}"
    assert f"STAMP status=complete ref={REF}" in log, (
        f"the load did not stamp the ref it loaded — the prune gate will refuse forever.\n{log}"
    )
    assert "LOADER_RAN" in log, (
        "the loader never ran, so this test's stamps prove nothing about a real load"
    )


def test_the_stamp_precedes_the_write_and_completes_after_it(tmp_path: Path) -> None:
    """ORDER is the safety property, not the mere presence of a stamp.

    `in_progress` must be on the graph BEFORE the loader can touch it, so that a load which
    dies mid-write leaves a graph the prune gate will refuse. A stamp written only at the end
    would leave a crashed load looking exactly like the previous good one.
    """
    root, audit = _sandbox(tmp_path, loader_rc=RC_SUCCESS)
    _run_ssh_block(root, tmp_path)
    log = _audit(audit)

    assert f"STAMP status=in_progress ref={REF}" in log, "no pre-write stamp"
    i_pre = log.index(f"STAMP status=in_progress ref={REF}")
    i_load = log.index("LOADER_RAN")
    i_post = log.index(f"STAMP status=complete ref={REF}")
    assert i_pre < i_load, (
        "the graph was written BEFORE it was stamped in_progress. A load that dies mid-write "
        "would then leave the PREVIOUS run's stamp intact, and the prune gate would happily "
        "prune a half-loaded graph against a keep-set that names none of what was half-loaded."
    )
    assert i_load < i_post, "'complete' was stamped before the loader committed"


@pytest.mark.parametrize("rc", [RC_LOAD_FAILED, RC_REFUSED_ROWS])
def test_a_failed_load_never_reaches_complete(tmp_path: Path, rc: int) -> None:
    """rc=6 REFUSED_ROWS is the one that matters: it can leave a PARTIAL graph.

    A partial graph holds narrators that no keep-set names, so a prune deletes exactly those —
    whatever ref it is pointed at. The stamp must stay `in_progress`, which the prune gate
    refuses against ANY ref, including its own. Re-running the load is what clears it.
    """
    root, audit = _sandbox(tmp_path, loader_rc=rc)
    r = _run_ssh_block(root, tmp_path)
    log = _audit(audit)
    assert r.returncode != 0, f"loader rc={rc} must fail the job"
    assert f"STAMP status=in_progress ref={REF}" in log, "the pre-write stamp must still be set"
    assert not any("status=complete" in line for line in log), (
        f"loader rc={rc} FAILED but the graph was stamped 'complete'. The prune gate would "
        "then treat a failed/partial load as a trustworthy one."
    )


def test_findings_rc5_does_stamp_complete(tmp_path: Path) -> None:
    """rc=5 VALIDATION_FINDINGS COMMITTED the graph — advisory findings, not a failed load.

    Without this, `test_a_failed_load_never_reaches_complete` could be satisfied by a workflow
    that simply never stamps `complete` at all. This is the control that separates 'refuses the
    bad rc' from 'refuses every rc' (deploy#574 — an oracle that only ever says BLOCKED proves
    every guard holds).
    """
    root, audit = _sandbox(tmp_path, loader_rc=RC_VALIDATION_FINDINGS)
    r = _run_ssh_block(root, tmp_path)
    log = _audit(audit)
    assert r.returncode == 0, "rc=5 committed the graph; the job must not fail"
    assert f"STAMP status=complete ref={REF}" in log, (
        "rc=5 COMMITTED a graph but never stamped it complete — the prune gate would refuse a "
        "perfectly good load forever"
    )


def test_a_dry_run_stamps_nothing(tmp_path: Path) -> None:
    """A dry run writes nothing to the graph, so it must claim nothing about it."""
    root, audit = _sandbox(tmp_path)
    r = _run_ssh_block(root, tmp_path, DRY_RUN="true")
    assert r.returncode == 0
    assert not _audit(audit), f"a dry run touched the graph: {_audit(audit)}"


def test_an_injecting_ref_is_refused_before_the_loader_runs(tmp_path: Path) -> None:
    """`parquet_ref` is free text and is interpolated into a Cypher literal with write access.

    The refusal must land BEFORE the loader — a load that runs and then fails to stamp leaves
    a graph nobody can attribute to a ref. Asserted on behaviour: no stamp, and no loader.
    """
    root, audit = _sandbox(tmp_path)
    r = _run_ssh_block(root, tmp_path, PARQUET_REF="staged/x'; MATCH (n) DETACH DELETE n; //")
    log = _audit(audit)
    assert r.returncode != 0, "a Cypher-injecting parquet_ref was accepted"
    assert "LOADER_RAN" not in log, "the loader ran on a ref the graph could not be stamped with"
    assert not any(line.startswith("STAMP") for line in log)

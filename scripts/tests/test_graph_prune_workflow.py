"""Regression guards for the destructive graph-prune workflow (deploy#557 / #574).

``graph-prune-narrators.yml`` DETACH DELETEs Narrator nodes from the live graph. Its
safety properties are the kind that fail *silently* and *irreversibly*, so they get
textual guards here rather than trusting review to catch a regression.

The load-bearing one is the **completeness binding**. Every other gate in that workflow
— the whole-graph sanity check, ``EXPECTED_POST``, and post-op Gates A-D — is derived
from the keep-set it is meant to validate. They prove the prune obeyed the parquet; none
can ask whether it was the *right* parquet, because ``parquet_ref`` is free text.

``missing`` (da#414) closes the STALE-parquet door. It does **not** close the SHORT-parquet
door, and no magnitude ceiling can: on a truncated-but-same-generation parquet every id in
the keep-set really is in the graph, so ``missing`` is exactly 0 — it does not merely miss
that case, it *certifies* it. da#426 measured the real subcommand against a 160,614-node
graph: at ``f=0.20`` of canonical rows retained it deletes **83.9%** of the graph and
proceeds, with ``missing=0``. The ceiling cannot be tightened either, because a legitimate
full re-mint prune deletes 55.4% — the legitimate and catastrophic bands overlap.

So the only thing that can separate them is provenance: the keep-set must match a count
declared *independently of the artifact*.

**And the declared count this workflow can obtain today is only a partial floor.** The
number a resolve run reports is a **read-back** of the parquet it just wrote
(``src/resolve/__init__.py:344`` — the sole writer of ``canonical_narrator_count``), so it
agrees with a short file. That catches a truncated *upload* and a wrong ``parquet_ref``
whose count differs; it does **not** catch a partial write or a resolve stopped early,
which are da#426's actual cases. The real fix is da#428: a count threaded from the
producer's **in-memory tally before the write**, plus a run-completion assertion for
"stopped early", which no count equality of any kind can see. These tests pin the guard
that exists — they do not certify that the door is closed.

These are cheap textual assertions running in the ``pytest (scripts)`` CI job. They are
the only automated gate on this workflow's shell: actionlint does not shellcheck an
``ssh-action`` ``with: script:`` body (deploy#555), so that script has no linter at all.
"""

from __future__ import annotations

from pathlib import Path

import yaml

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SCRIPTS_DIR.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "graph-prune-narrators.yml"


def _ssh_script() -> str:
    """The remote script body of the prune step — the code that actually deletes."""
    doc = yaml.safe_load(WORKFLOW.read_text())
    for step in doc["jobs"]["prune-narrators"]["steps"]:
        if step.get("id") == "prune":
            return str(step["with"]["script"])
    raise AssertionError("no step with id 'prune' in graph-prune-narrators.yml")


def _code_only(text: str) -> str:
    """Drop whole-line shell comments.

    Absence-assertions ("the old broken form must not come back") have to read CODE, not
    prose. This script's comments quote the very constructs those tests forbid — the
    unanchored da#419 regex is written out verbatim to explain why it was wrong — so a
    naive substring check would match the explanation and fail on a correct script. That
    is the same class of error as an assertion that can never fail: one that can never
    pass. Mirrors the helper in test_restore_failure_modes.py.
    """
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def test_completeness_binding_asserts_equality_against_a_declared_count() -> None:
    """The keep-set must equal what its producing run declared, or the prune must refuse.

    Without this the workflow deletes 83.9% of the graph on a short-but-valid parquet and
    prints a passing verification (da#426). Note this is EQUALITY against a declaration,
    not a floor: a `--min-canonical-ids` threshold is still magnitude, only approached
    from below, and would accept a larger parquet from the wrong generation.
    """
    code = _code_only(_ssh_script())
    assert 'CANON_N}" -ne "${EXPECTED_CANONICAL_IDS}' in code, (
        "the completeness binding must assert the canonical ids READ from the parquet "
        "EQUAL the count the producing run DECLARED; without that equality a truncated "
        "parquet is indistinguishable from a complete one"
    )
    # A floor would be a magnitude threshold wearing a provenance costume.
    for floor in ('-lt "${EXPECTED_CANONICAL_IDS}"', '-ge "${EXPECTED_CANONICAL_IDS}"'):
        assert floor not in code, (
            f"the binding must be equality, not a floor ({floor}) — a floor accepts any "
            "parquet at-or-above it, including a larger one from the wrong generation"
        )


def test_declared_count_is_required_and_has_no_break_glass() -> None:
    """`expected_canonical_ids` is a REQUIRED input, and nothing can wave it through.

    An override on an irreversible op with no provenance is the flag that gets set at
    03:00 by someone who just wants the job green.
    """
    doc = yaml.safe_load(WORKFLOW.read_text())
    # `on` is parsed by PyYAML as the boolean True (YAML 1.1) — look it up either way.
    trigger = doc.get("on", doc.get(True))
    inputs = trigger["workflow_dispatch"]["inputs"]

    assert "expected_canonical_ids" in inputs, (
        "the workflow must require the operator to declare the producing run's canonical "
        "count — it is the only signal not derived from the keep-set being validated"
    )
    assert inputs["expected_canonical_ids"].get("required") is True, (
        "expected_canonical_ids must be REQUIRED; an optional provenance check is not one"
    )
    assert "default" not in inputs["expected_canonical_ids"], (
        "a default would let the operator skip the declaration without noticing"
    )

    # ALLOW-list, not a deny-list. The previous version of this test matched input names
    # against ("force", "override", "skip", "allow", "ignore") — and Nino Kavtaradze
    # defeated it in review by adding a real override input named `bypass_completeness`,
    # which the suite passed. `unsafe_*`, `disable_*`, `emergency_*`, `no_verify` sail
    # through it too.
    #
    # A deny-list of names can only ever catch the names someone thought of, so it is
    # exactly the shape of guard this whole wave has been about: one that cannot see the
    # thing it exists to catch, while reporting green. The input set here is small and
    # stable, so enumerate it. Adding an input is then a deliberate act that has to come
    # through this test — which is the point, because the input surface of a destructive
    # workflow is precisely where a break-glass would appear.
    assert set(inputs) == {
        "env",
        "parquet_ref",
        "expected_canonical_ids",
        "image",
        "dry_run",
    }, (
        f"unexpected workflow_dispatch input set: {sorted(inputs)}. Any NEW input on a "
        "destructive workflow must be justified here — this test is an allow-list "
        "precisely so that a break-glass cannot arrive under a name nobody deny-listed."
    )


def test_manifest_when_present_must_agree_with_the_operator() -> None:
    """The manifest (da#428) is machine provenance; it does not merely decorate.

    It is not required — nothing publishes it yet — but a manifest that CONTRADICTS the
    operator means one of them describes a different run, and on a destructive op that is
    a stop, not a coin-toss.
    """
    code = _code_only(_ssh_script())
    assert "_manifest.txt" in code, "the prune must pull curated/_manifest.txt when present"
    assert 'MF_CANON_IDS}" -ne "${EXPECTED_CANONICAL_IDS}' in code, (
        "a present manifest must be cross-checked against the operator's declaration"
    )
    assert 'GOT_MD5}" != "${MF_MD5}' in code, (
        "a present manifest must be used to verify the bytes we pulled are the bytes "
        "that were published"
    )


def test_dry_run_fails_safe() -> None:
    """`= "true"` fails OPEN: any unexpected value falls through to a REAL prune.

    dry_run arrives as a string through the ssh-action `envs:` passlist, so "True", "1",
    a value dropped from the passlist (empty), or a typo would all mean "delete for real".
    """
    script = _ssh_script()
    assert '[ "${DRY_RUN}" != "false" ]' in script, (
        'the dry-run test must be `!= "false"` (fail-safe), never `= "true"` (fail-open)'
    )


def test_gate_d_asserts_equality_not_an_upper_bound() -> None:
    """`-gt` only refuses a SUPERSET; over-deletion leaves a strict subset and passes.

    The right-hand side must be `CANON_N - MISSING_N` — the canonical ids the graph
    actually holds — and NOT a bare `CANON_N`, which would false-FAIL every legitimately
    partial load (missing > 0). A false-FAIL on a destructive op sends an operator to
    "restore from backup" after a prune that did exactly the right thing.
    """
    code = _code_only(_ssh_script())
    assert 'CANON_IN_GRAPH="$(( CANON_N - MISSING_N ))"' in code, (
        "survivors must be reconciled against the canonical ids present in THIS graph"
    )
    assert 'NARR_POST}" -ne "${CANON_IN_GRAPH}' in code, (
        "Gate D must assert equality; `-gt` waves through an over-deleting prune"
    )
    assert 'NARR_POST}" -gt "${CANON_N}' not in code, "the weak `-gt` Gate D must be gone"


def test_summary_key_extraction_cannot_be_hijacked() -> None:
    """A key must match a WHOLE TOKEN, not a substring (da#419).

    The old `sed -n "s/.*PRUNE_NARRATORS_SUMMARY .*$2=\\(...\\)"` is unanchored and greedy,
    so asking for `deleted` also matches the tail of a future `edges_deleted=` and, being
    greedy, returns the rightmost. `missing=` happens not to collide today, so this is a
    trap armed for the next key rather than a live miscount.
    """
    code = _code_only(_ssh_script())
    assert 'sed -n "s/.*PRUNE_NARRATORS_SUMMARY .*$2=' not in code, (
        "the unanchored greedy key regex (da#419) must not come back"
    )
    # Whole-token form: split the line on spaces, then require an exact `key=<int>` token.
    # The `^$2=` anchor is the whole property — a key can no longer match the tail of a
    # longer one. (Raw strings: the YAML block scalar preserves the shell's backslashes
    # verbatim, so the text on disk really does contain `\n` and `\\(` as characters.)
    assert r"| tr ' ' '\n'" in code, "summary_field must split the summary line into tokens"
    assert 'sed -n "s/^$2=' in code, (
        "summary_field must anchor the key to the START of a token (^key=), or a future "
        "key ending in an existing key's name will hijack it (da#419)"
    )


def test_missing_is_reported_but_never_asserted_to_be_zero() -> None:
    """`missing == 0` is not a safety property — it CERTIFIES the short-parquet case.

    It is also wrong in the other direction: a legitimately partial load (REFUSED_ROWS,
    STOPPED_AT_LIMIT, nodes-only) leaves canonical ids unloaded, so missing > 0 is normal
    and a hard refusal would block correct prunes. da#414's own contract places this
    policy in the workflow, and the workflow's answer must be "report, don't refuse".
    """
    code = _code_only(_ssh_script())
    assert '"${MISSING_N}" -ne 0' not in code, (
        "a `missing != 0` refusal blocks legitimate partial loads AND certifies the "
        "short-parquet case it appears to guard — the completeness binding is the real gate"
    )
    assert 'MISSING_N}" -gt 0' in code, "a non-zero `missing` should still be surfaced"


def test_counts_reach_the_job_summary() -> None:
    """A human approving an irreversible op must see what it will do (deploy#575).

    The summary carried env/parquet_ref/dry_run/image — everything except the outcome —
    while the counts stayed buried in the ssh step's log.
    """
    doc = yaml.safe_load(WORKFLOW.read_text())
    steps = doc["jobs"]["prune-narrators"]["steps"]
    prune = next(s for s in steps if s.get("id") == "prune")
    assert prune["with"].get("capture_stdout") is True, (
        "the remote stdout must be captured for the Report step to read the counts"
    )
    assert "PRUNE_RESULT" in _ssh_script(), "the remote script must emit a PRUNE_RESULT line"

    report = next(s for s in steps if s.get("name") == "Report")
    body = str(report["run"])
    assert "steps.prune.outputs.stdout" in str(report.get("env", {})), (
        "the Report step must consume the captured remote stdout"
    )
    for key in ("canonical", "orphans", "deleted", "missing"):
        assert f"result_field {key}" in body, f"the summary must surface `{key}`"
    # And absence must be stated, not rendered as zeros.
    assert "Not reported" in body, (
        "when no PRUNE_RESULT line was emitted the summary must say so rather than print "
        "zeros — a fabricated 'deleted 0' on a destructive op is worse than an honest unknown"
    )

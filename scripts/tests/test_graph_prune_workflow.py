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

**No human-supplied count can be that declaration.** The only count the pipeline produces
is a **read-back** of the parquet being pruned against (``src/resolve/__init__.py:344`` —
the sole writer of ``canonical_narrator_count``), so it agrees with a short file. An
operator copying it copies the short file's own count. Told instead to supply "the count
you expect from the corpus", they are stuck the other way: a legitimate re-run *changes*
the count, so a mismatch is expected and a match proves nothing. A required input with no
correct value is not a gate — it is a foot-gun wearing the costume of one.

So ``curated/_manifest.txt`` is **REQUIRED**, and its absence is a hard refusal. That makes
this workflow **inert until da#428 publishes one** — which is the point: it cannot do the
dangerous thing at all, so it carries no residual.

da#428 must write the count from the producer's **in-memory tally before the write**, and
must also carry a **run-completion assertion**: "resolve stopped early" is invisible to any
count equality, because a run that halts after writing a coherent partial file is
internally consistent by construction.

These are cheap textual assertions running in the ``pytest (scripts)`` CI job. They are
the only automated gate on this workflow's shell: actionlint does not shellcheck an
``ssh-action`` ``with: script:`` body (deploy#555), so that script has no linter at all.
"""

from __future__ import annotations

import re
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


def test_completeness_binding_asserts_equality_against_the_producer_tally() -> None:
    """The keep-set must equal what its producing run declared, or the prune must refuse.

    Without this the workflow deletes 83.9% of the graph on a short-but-valid parquet and
    prints a passing verification (da#426). EQUALITY against a producer-signed declaration
    — not a floor: a `--min-canonical-ids` threshold is still magnitude, only approached
    from below, and would accept a larger parquet from the wrong generation.
    """
    code = _code_only(_ssh_script())
    assert 'CANON_N}" -ne "${PROV_CANON_IDS}' in code, (
        "the completeness binding must assert the canonical ids READ from the parquet "
        "EQUAL the count RESOLVE declared in curated/_resolve_run.txt; without that "
        "is indistinguishable from a complete one"
    )
    # A floor would be a magnitude threshold wearing a provenance costume.
    for floor in ('-lt "${PROV_CANON_IDS}"', '-ge "${PROV_CANON_IDS}"'):
        assert floor not in code, (
            f"the binding must be equality, not a floor ({floor}) — a floor accepts any "
            "parquet at-or-above it, including a larger one from the wrong generation"
        )


def test_manifest_is_required_so_the_workflow_is_inert_without_provenance() -> None:
    """No manifest, no prune — which is what lets #574 merge ahead of da#428.

    An earlier revision made `expected_canonical_ids` a REQUIRED input and the manifest
    optional. That was not merely weaker; the input was UN-SOURCEABLE. The only count the
    pipeline produces is a read-back of the parquet being pruned against
    (``src/resolve/__init__.py:344``), so an operator copying it copies the short file's
    own count and the check passes on exactly the artifact it exists to reject. Told
    instead to supply "the count you expect from the corpus", they are stuck the other
    way: a legitimate re-run changes the count, so a mismatch is expected and a match
    proves nothing.

    A required input with no correct value is not a gate; it is a foot-gun wearing the
    costume of one. The manifest is the only declarer that can work, because it is written
    from the producer's in-memory tally BEFORE the write.
    """
    code = _code_only(_ssh_script())
    assert (
        'verify_present "${B2_BASE}/curated" "narrators_canonical.parquet" '
        '"_manifest.txt" "_resolve_run.txt"' in code
    ), "BOTH provenance objects must be in the REQUIRED B2 presence list"
    assert "bash scripts/verify_prune_provenance.sh" in code, (
        "the provenance gate must be DELEGATED to the script — inline, it can only be "
        "pinned by grepping this workflow's text, and a forged manifest passes that "
        "(Weronika's mutation). The behavioural proof lives in test_prune_provenance.py."
    )
    assert 'if [ "${PROV_RC}" -ne 0 ]; then' in code, (
        "a non-zero rc from the provenance gate must abort before any deletion"
    )


def test_operator_count_is_optional_and_cross_checked_when_supplied() -> None:
    """Corroboration, not the gate — and it may not become required again.

    It catches one thing the manifest cannot: an operator who meant run A and typed run
    B's parquet_ref. B's manifest and B's parquet agree with each other perfectly, so every
    other check passes. Supplied, it must equal the manifest. Blank, it is skipped.
    """
    doc = yaml.safe_load(WORKFLOW.read_text())
    trigger = doc.get("on", doc.get(True))
    inputs = trigger["workflow_dispatch"]["inputs"]

    assert inputs["expected_canonical_ids"].get("required") is False, (
        "expected_canonical_ids must NOT be required — there is no correct value an "
        "operator can source for it (the pipeline's only count is a read-back of the "
        "parquet being pruned against)"
    )
    code = _code_only(_ssh_script())
    assert 'EXPECTED_CANONICAL_IDS="${EXPECTED_CANONICAL_IDS}"' in code, (
        "the operator count must be passed through to the provenance gate"
    )
    # Its *behaviour* — skipped when blank, refused when it disagrees — is asserted by
    # RUNNING the gate in test_prune_provenance.py, not by grepping this workflow. That is
    # the whole lesson of Weronika's forged-manifest mutation: a guard pinned by substring
    # is not pinned at all.


def test_no_break_glass_input() -> None:
    """Nothing may wave the provenance check through."""
    doc = yaml.safe_load(WORKFLOW.read_text())
    # `on` is parsed by PyYAML as the boolean True (YAML 1.1) — look it up either way.
    trigger = doc.get("on", doc.get(True))
    inputs = trigger["workflow_dispatch"]["inputs"]

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


def test_env_surface_is_allow_listed_too() -> None:
    """The `env:` block is a bypass channel the INPUT allow-list cannot see (deploy#579).

    A workflow-level `SKIP_PROVENANCE: ${{ vars.SKIP_PROVENANCE }}` leaves `set(inputs)`
    untouched, so the input allow-list stays green while the guard is gutted. Allow-list the
    env keys of the destructive step too.

    Belt and braces: the gate's own behavioural suite proves no env var of ANY name can
    unlock a provenance-less artifact (test_prune_provenance.py), because that property is
    asserted where it lives — in the gate's behaviour — rather than in this file's grep.
    """
    doc = yaml.safe_load(WORKFLOW.read_text())
    prune = next(s for s in doc["jobs"]["prune-narrators"]["steps"] if s.get("id") == "prune")
    assert set(prune.get("env", {})) == {
        "ENV_NAME",
        "PARQUET_REF",
        "EXPECTED_CANONICAL_IDS",
        "DRY_RUN",
        "IMAGE",
        "PIPELINE_B2_KEY_ID",
        "PIPELINE_B2_KEY",
        "GITHUB_TOKEN",
        "GITHUB_ACTOR",
    }, (
        f"unexpected env keys on the destructive step: {sorted(prune.get('env', {}))}. "
        "Any NEW env var must be justified here — this is an allow-list precisely so a "
        "bypass cannot arrive through a channel the input allow-list does not watch."
    )

    # And the `envs:` passlist is the actual TRANSPORT to the VPS: a var absent from it
    # never reaches the gate at all, whatever the step env says. This is the structural
    # complement to the gate's behavioural suite, and it is the one that matters, because
    # a behavioural test can only try env names someone thought to write down — the exact
    # deny-list weakness that let `bypass_completeness` through the first time. Set
    # equality here needs nobody to have guessed the name.
    passlist = [v.strip() for v in str(prune["with"]["envs"]).split(",") if v.strip()]
    assert set(passlist) == set(prune["env"]), (
        f"the ssh `envs:` passlist and the step `env:` block must name exactly the same "
        f"vars; drift is a channel. envs={sorted(passlist)} env={sorted(prune['env'])}"
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
    assert r"| tr ' ' '\n'" in code, "the extractor must split the record line into tokens"
    assert 'sed -n "s/^$3=' in code, (
        "record_field must anchor the key to the START of a token (^key=), or a future "
        "key ending in an existing key's name will hijack it (da#419)"
    )
    # Exactly ONE extractor IN THE ssh BODY. There used to be two, and one of them — the
    # provenance one, single-quoted with doubled backslashes — could never match, so the
    # workflow aborted on the first honest artifact and the completeness binding was
    # unreachable dead code. Two extractors in one shell is one too many to keep right.
    #
    # SCOPE, precisely, because a claim that overreaches is its own defect: `code` is the ssh
    # `script:` body. The Report step has a THIRD extractor (`result_field`) — a separate
    # step, a separate shell on the runner, so it CANNOT share this function. It is correctly
    # double-quoted, and its failure mode is a cosmetic summary rather than a delete. Nobody
    # should read this assertion as covering it. (It renders the summary a prod approver
    # reads, so it is not nothing — it is just not this.)
    assert code.count('| sed -n "s/^$') == 1, (
        "there must be exactly one token extractor in the ssh body; a second one is a "
        "second chance to get the shell quoting wrong, and that is precisely what happened"
    )
    assert re.search(r"summary_field\(\) \{\s*record_field ", _ssh_script()), (
        "summary_field must delegate to record_field, not re-implement the extraction"
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

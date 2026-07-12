"""BEHAVIOURAL tests for the GRAPH-side load-provenance gate — it is RUN, not grepped.

The hole (deploy#580): ``graph-prune-narrators.yml`` prunes the graph to the canonical set
of whatever ``parquet_ref`` an operator types, and **every gate it has is derived from that
parquet**. They prove the prune obeyed the keep-set. None of them can ask whether it was the
**right** keep-set.

The case they all miss is a **genuine but older, smaller run** — not corrupt, not foreign, a
real earlier resolve run. Its resolve record is valid, its md5 matches, its ids are a *subset*
of what the graph now holds so ``missing`` is 0, and the completeness equality passes because
the parquet really does hold exactly what its producing run declared. Every automated check
goes green and the prune deletes every narrator loaded since.

``missing`` is **structurally blind** here and no fix to it can help: it bounds
``canonical − graph``, none of which can be *deleted*. The over-deletion lives entirely in
``graph − canonical`` — precisely the set the tool exists to delete.

So the load stamps the ref it loaded onto the graph, and the prune refuses unless it is
pruning against that same ref. It is the only check independent of **both** the parquet and
the operator.

THE TEST THAT IS THE DELIVERABLE
--------------------------------
``test_a_valid_but_wrong_ref_passes_every_parquet_gate_and_is_still_refused`` builds ref **B**
as a *genuinely good artifact* — complete resolve record, correct md5, internally consistent —
and **first proves that the pre-existing gate accepts it** by executing
``verify_prune_provenance.sh`` against it and asserting rc 0. Only then does it assert that
this gate refuses it.

That ordering is the whole point. A fixture that cannot produce the failure the guard exists
to catch makes the guard's assertion **vacuous** — an ``assert not id.startswith(BAD)`` where
no fixture can produce ``BAD``, a mention-weighted A/B that structurally cannot see the class
it is deleting. If B were corrupt or foreign it would be caught by gates that already exist,
and this one would be **inert while appearing to work**. B must be a good artifact that is
merely the WRONG ONE, and the existing gate saying "yes, this is fine" is what proves it.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
GATE = SCRIPTS_DIR / "graph_load_provenance.sh"
# The PRE-EXISTING, parquet-derived gate. Ref B must pass THIS for the critical test to mean
# anything at all.
PARQUET_GATE = SCRIPTS_DIR / "verify_prune_provenance.sh"

PARQUET = "narrators_canonical.parquet"

# A = the run the graph was actually loaded from (today's re-run from `parse`).
REF_A = "staged/narrator-resolve/2026-07-11-4f2a1c9"
COUNT_A = 129234
# B = a REAL, EARLIER, SMALLER resolve run. Published correctly, complete, self-consistent.
# It is not corrupt and it is not foreign. It is simply not the one the graph holds.
REF_B = "staged/narrator-resolve/2026-06-28-9b1e7d3"
COUNT_B = 120417

IMAGE = "ghcr.io/noorinalabs/noorinalabs-data-acquisition-load:prod-latest"


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()  # noqa: S324 — matches the manifest's own digest


def _run(mode: str, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["bash", str(GATE), mode],  # noqa: S607
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        check=False,
    )


def _stamp(ref: str = REF_A, status: str = "complete", image: str = IMAGE, args: str = "load"):
    return _run("stamp", PARQUET_REF=ref, LOAD_STATUS=status, IMAGE=image, LOAD_ARGS=args)


def _graph_says(
    ref: str = REF_A,
    status: str = "complete",
    *,
    stamped_at: str = "2026-07-11T04:12:03Z",
    quoted: bool = True,
) -> str:
    """Render what `cypher-shell --format plain` returns for the read-back query.

    `quoted=True` is the real transport: cypher-shell renders a string column in double
    quotes. The gate must be agnostic to that envelope, so both forms are exercised.
    """
    line = (
        f"GRAPH_LOAD_PROVENANCE parquet_ref={ref} load_status={status} "
        f"stamped_at={stamped_at} image={IMAGE}"
    )
    row = f'"{line}"' if quoted else line
    return f"provenance\n{row}\n"


def _verify(prune_ref: str, provenance: str, **env: str) -> subprocess.CompletedProcess[str]:
    return _run("verify", PRUNE_PARQUET_REF=prune_ref, PROVENANCE_OUTPUT=provenance, **env)


# ===========================================================================
# THE CRITICAL TEST — the one the whole gate exists for.
# ===========================================================================
def _publish_run(tmp_path: Path, ref: str, count: int) -> Path:
    """Publish a resolve run to a fake B2 prefix: a COMPLETE, HONEST, self-consistent artifact.

    Nothing here is subtly wrong. The parquet is whole, the resolve record's tally is the
    tally, the run completed, the md5 is the md5 of the bytes. This is what a good publish
    looks like — and `verify_prune_provenance.sh` will say so.
    """
    d = tmp_path / ref.replace("/", "_") / "curated"
    d.mkdir(parents=True)
    payload = f"PAR1-{ref}-{count}".encode() + b"\x00" * 64
    (d / PARQUET).write_bytes(payload)
    (d / "_resolve_run.txt").write_text(
        f"RESOLVE_RUN canonical_ids={count} run_status=complete git_sha=abc1234\n"
    )
    (d / "_manifest.txt").write_text(
        f"CANONICAL_MANIFEST file={PARQUET} md5={_md5(payload)} bytes={len(payload)}\n"
    )
    return d


def test_a_valid_but_wrong_ref_passes_every_parquet_gate_and_is_still_refused(
    tmp_path: Path,
) -> None:
    """Load ref A. Prune against a VALID, record-bearing, internally-consistent ref B. REFUSE.

    B is a real earlier resolve run — smaller, complete, correctly published. **Step 1 proves
    it**: the pre-existing parquet-derived gate is executed against B and *accepts* it, rc 0,
    emitting B's own tally. Every other gate in the prune workflow behaves the same way on B
    (its ids are a subset of the graph, so ``missing`` is 0 and the completeness equality
    holds), which is exactly why no parquet-derived gate can save us here.

    If this test used a corrupt or foreign B, step 1 would fail and the refusal in step 2
    would prove **nothing** — it would be caught by the gates that already exist, and this
    gate would be inert while appearing to work.
    """
    # --- Step 1: B is a GOOD artifact. Not asserted — DEMONSTRATED, by the existing gate. ---
    curated_b = _publish_run(tmp_path, REF_B, COUNT_B)
    parquet_gate = subprocess.run(  # noqa: S603
        ["bash", str(PARQUET_GATE)],  # noqa: S607
        env={**os.environ, "CURATED_DIR": str(curated_b)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert parquet_gate.returncode == 0, (
        "the fixture for ref B is not a valid artifact, so this test proves NOTHING about the "
        "load-provenance gate — a B that the pre-existing gates already reject would be caught "
        "without this gate, and the guard under test would be vacuous.\n"
        f"verify_prune_provenance.sh said:\n{parquet_gate.stderr}"
    )
    assert f"PRUNE_PROVENANCE canonical_ids={COUNT_B}" in parquet_gate.stdout, (
        "the existing gate did not read B's tally — B must be a run that fully checks out"
    )

    # --- Step 2: and the graph-derived gate refuses it anyway. ---
    r = _verify(REF_B, _graph_says(ref=REF_A))
    assert r.returncode != 0, (
        "the prune was allowed against a ref the graph was NOT loaded from. Every parquet-"
        "derived gate green (step 1 just proved it), and the prune would delete every narrator "
        "loaded since ref B."
    )
    # The diagnostic must name BOTH refs — an operator staring at an irreversible refusal needs
    # to see what the graph holds and what they typed, side by side.
    assert REF_A in r.stderr, "the diagnostic does not name the ref the graph was LOADED from"
    assert REF_B in r.stderr, "the diagnostic does not name the ref the operator TYPED"
    assert "WRONG REF" in r.stderr
    assert "NO node was deleted." in r.stderr


def test_the_stamped_ref_proceeds(tmp_path: Path) -> None:
    """The positive control — and without it every refusal above is unfalsifiable.

    A gate that refuses everything is not a gate, it is an outage. If this ever goes red while
    the refusals stay green, the gate has become an unconditional 'no' and the refusals prove
    nothing.
    """
    curated_a = _publish_run(tmp_path, REF_A, COUNT_A)
    parquet_gate = subprocess.run(  # noqa: S603
        ["bash", str(PARQUET_GATE)],  # noqa: S607
        env={**os.environ, "CURATED_DIR": str(curated_a)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert parquet_gate.returncode == 0, "ref A must also be a valid artifact"

    r = _verify(REF_A, _graph_says(ref=REF_A))
    assert r.returncode == 0, f"the gate refused the ref the graph WAS loaded from:\n{r.stderr}"
    assert f"LOAD_PROVENANCE_OK parquet_ref={REF_A}" in r.stdout


# ===========================================================================
# Absence and incompleteness are refusals, never silent passes.
# ===========================================================================
def test_unstamped_graph_refuses() -> None:
    """Every graph loaded before deploy#580 looks exactly like this: zero rows, header only.

    An absent stamp must be a STOP. A gate whose instrument reads nothing has not measured
    'fine' — it has not measured (feedback_silent_zero_is_not_a_measurement).
    """
    r = _verify(REF_A, "provenance\n")
    assert r.returncode != 0, "an unstamped graph must not be prunable"
    assert "NO load-provenance stamp" in r.stderr


def test_empty_readback_refuses() -> None:
    """Not even a header — the pathological case. Still a refusal, never a crash-into-pass."""
    r = _verify(REF_A, "")
    assert r.returncode != 0
    assert "NO load-provenance stamp" in r.stderr


def test_incomplete_load_refuses_even_against_its_own_ref() -> None:
    """A load that died mid-write leaves `in_progress`. Refuse it against ANY ref — its own too.

    This is the fail-closed direction and it is deliberate: a half-loaded graph holds narrators
    that no keep-set names, so a prune deletes exactly those, whatever ref it is pointed at.
    """
    r = _verify(REF_A, _graph_says(ref=REF_A, status="in_progress"))
    assert r.returncode != 0, "a graph whose last load did not complete must not be prunable"
    assert "did NOT complete" in r.stderr


def test_unset_properties_refuse() -> None:
    """A node missing its properties projects `<unset>`, which must refuse — not read as fine."""
    unset = (
        "GRAPH_LOAD_PROVENANCE parquet_ref=<unset> load_status=<unset> "
        "stamped_at=<unset> image=<unset>"
    )
    r = _verify(REF_A, f'provenance\n"{unset}"\n')
    assert r.returncode != 0


@pytest.mark.parametrize("quoted", [True, False])
def test_verify_is_agnostic_to_the_transport_quoting(quoted: bool) -> None:
    """`cypher-shell --format plain` quotes string columns. Parse the ROW, not the envelope.

    Asserted in BOTH renderings so the gate cannot come to depend on a quoting detail of the
    transport — and, more importantly, so a reorder of the projected fields can never start
    contaminating a value with a stray quote.
    """
    ok = _verify(REF_A, _graph_says(ref=REF_A, quoted=quoted))
    assert ok.returncode == 0, f"quoted={quoted} rendering was not parsed:\n{ok.stderr}"
    bad = _verify(REF_B, _graph_says(ref=REF_A, quoted=quoted))
    assert bad.returncode != 0, f"quoted={quoted}: a wrong ref slipped through"


@pytest.mark.parametrize(
    "bypass",
    [
        {"SKIP_PROVENANCE": "true"},
        {"ALLOW_UNSTAMPED_GRAPH": "true"},
        {"FORCE": "true"},
        {"DRY_RUN": "true"},
        {"GRAPH_PARQUET_REF": REF_B},
        {"LOAD_STATUS": "complete"},
        {"PARQUET_REF": REF_B},
    ],
)
def test_no_environment_variable_can_unlock_an_unstamped_graph(bypass: dict[str, str]) -> None:
    """The `env:` surface is a bypass channel an input allow-list cannot see (deploy#579).

    `GRAPH_PARQUET_REF` and `LOAD_STATUS` are in this list on purpose: they are the names an
    implementer would most plausibly reach for to "inject" the graph's side of the comparison.
    The graph's answer must come from the GRAPH — via PROVENANCE_OUTPUT — and from nowhere else.
    """
    r = _verify(REF_A, "provenance\n", **bypass)
    assert r.returncode != 0, (
        f"env {bypass} unlocked a prune against an unstamped graph — the gate must have no "
        "environment-variable escape hatch"
    )


# ===========================================================================
# The stamp side — and the injection surface it closes.
# ===========================================================================
def test_stamp_records_the_ref_and_the_status() -> None:
    r = _stamp(ref=REF_A, status="complete")
    assert r.returncode == 0, r.stderr
    assert "MERGE (p:LoadProvenance {scope: 'graph'})" in r.stdout
    assert f"p.parquet_ref = '{REF_A}'" in r.stdout
    assert "p.load_status = 'complete'" in r.stdout


@pytest.mark.parametrize("status", ["in_progress", "complete"])
def test_stamp_accepts_both_phases(status: str) -> None:
    assert _stamp(status=status).returncode == 0


@pytest.mark.parametrize("status", ["done", "COMPLETE", "true", "", "complete;DROP"])
def test_stamp_refuses_an_unknown_status(status: str) -> None:
    """`complete` is the token the prune unlocks on. A typo must refuse here, not be read as
    'not complete' downstream for the wrong reason."""
    assert _stamp(status=status).returncode != 0


@pytest.mark.parametrize(
    "ref",
    [
        "staged/x' SET p.load_status = 'complete",  # the injection
        "staged/x'; MATCH (n) DETACH DELETE n; //",  # the one that matters
        'staged/x" OR 1=1',
        "staged/x\\",
        "/staged/x",  # absolute
        "staged/x/",  # trailing slash — two spellings of one prefix breaks a string equality
        "noorinalabs-pipeline/staged/x",  # bucket name included
        "staged/../../etc/passwd",
        "",
    ],
)
def test_stamp_refuses_an_unsafe_parquet_ref(ref: str) -> None:
    """`parquet_ref` is free-text `workflow_dispatch` input interpolated into a single-quoted
    Cypher literal, in a statement with write access to the graph.

    "Only maintainers can dispatch" is exactly the reasoning that ships an injection bug. The
    charset is whitelisted, not escaped: a `'` cannot reach the statement at all.
    """
    r = _stamp(ref=ref)
    assert r.returncode != 0, f"unsafe parquet_ref {ref!r} was accepted into a Cypher literal"


@pytest.mark.parametrize(
    "image",
    ["ghcr.io/x/y:tag' SET p.load_status='complete", "img\\", "img with space", ""],
)
def test_stamp_refuses_an_unsafe_image(image: str) -> None:
    """`image` is a free-text input too, and it is stamped. The same injection surface."""
    assert _stamp(image=image).returncode != 0


def test_stamp_accepts_a_digest_pinned_image() -> None:
    r = _stamp(image="ghcr.io/noorinalabs/x@sha256:" + "a" * 64)
    assert r.returncode == 0, r.stderr


def test_stamp_accepts_the_nodes_only_load_args() -> None:
    """'load --nodes-only' has a SPACE. Legal in the stored property, and it must not be
    projected into the space-delimited provenance line (see the round-trip test below)."""
    r = _stamp(args="load --nodes-only")
    assert r.returncode == 0, r.stderr
    assert "p.load_args = 'load --nodes-only'" in r.stdout


# ===========================================================================
# DRIFT GUARD — the writer, the projector and the parser must agree.
# ===========================================================================
# A stamped property is either a QUOTED LITERAL we control (`p.x = 'v'`) or a Cypher
# EXPRESSION the database computes (`p.stamped_at = toString(datetime())`). Both are stamped;
# only the first has a value a test can know. Matching just the literal form is what an
# earlier version of this file did — and the round-trip guard below immediately failed,
# reporting that the stamp "never SETs p.stamped_at". It does. The guard was right and the
# fixture's model of the producer was wrong, which is the correct way round for that to go.
_SET_LITERAL = re.compile(r"p\.(\w+) = '([^']*)'")
_SET_EXPR = re.compile(r"p\.(\w+) = ([A-Za-z][\w.]*\()")

# What the DB computes for an expression-valued property. Space-free, because the provenance
# line is space-delimited — and the round-trip below asserts exactly that of every field.
DB_COMPUTED = {"stamped_at": "2026-07-11T04:12:03.000000000Z"}


def _stamp_properties(status: str = "complete", ref: str = REF_A) -> dict[str, str]:
    """The properties the STAMP actually SETs, read out of the stamp's own Cypher.

    Expression-valued properties resolve to the value the DATABASE would compute, since the
    test cannot know it — but they must still be present, or a rename of one goes unnoticed.
    """
    out = _stamp(ref=ref, status=status).stdout
    props = dict(_SET_LITERAL.findall(out))
    for prop, _expr in _SET_EXPR.findall(out):
        assert prop in DB_COMPUTED, (
            f"the stamp SETs p.{prop} to a Cypher expression this fixture cannot evaluate. "
            "Teach it what the database computes (DB_COMPUTED) rather than dropping the field "
            "— a field the fixture silently omits is a field the drift guard stops guarding."
        )
        props[prop] = DB_COMPUTED[prop]
    return props


def _projection() -> list[tuple[str, str]]:
    """The (line-key, node-property) pairs the READ-QUERY actually projects, read out of it."""
    q = _run("read-query").stdout
    return re.findall(r"\+ ' (\w+)=' \+ coalesce\(p\.(\w+),", q)


def test_stamp_write_read_query_and_verify_agree_on_every_field() -> None:
    """Round-trip the three modes through each other — no hand-written provenance line.

    This is the deploy#589 guard. A producer-side rename does not break a downstream parser,
    it QUIETLY NARROWS it: the guard stops guarding and every test stays green. So the fixture
    here is not written in the test's words. It is BUILT from the stamp's own SET clause and
    the read-query's own projection, and then fed to verify.

    It goes red if the stamp renames a property the read-query projects (the projection would
    find no value), if the read-query projects a key verify cannot parse, or if verify starts
    reading a field nobody writes.
    """
    props = _stamp_properties()
    projection = _projection()
    assert projection, "the read-query projects nothing — the parser has nothing to read"

    rendered = []
    for key, prop in projection:
        assert prop in props, (
            f"read-query projects p.{prop} as '{key}=', but the stamp never SETs p.{prop}. "
            f"The stamp writes {sorted(props)}. A rename on one side silently narrows the "
            "other — that is the whole failure mode this test exists for."
        )
        value = props[prop]
        assert " " not in value, (
            f"p.{prop}={value!r} contains a SPACE and is projected into the space-delimited "
            "provenance line — it would split into two tokens and corrupt the parse of every "
            "field after it. Store it, do not project it (this is why load_args is not here)."
        )
        rendered.append(f"{key}={value}")

    line = "GRAPH_LOAD_PROVENANCE " + " ".join(rendered)
    r = _verify(props["parquet_ref"], f'provenance\n"{line}"\n')
    assert r.returncode == 0, (
        f"verify could not read the line its own read-query projects from its own stamp:\n"
        f"{line}\n{r.stderr}"
    )
    assert f"LOAD_PROVENANCE_OK parquet_ref={props['parquet_ref']}" in r.stdout


def test_load_args_is_stored_but_never_projected() -> None:
    """It is the one stamped field that can contain a space, and the line is space-delimited."""
    assert "load_args" in _stamp_properties(), "load_args must be stored for forensics"
    assert "load_args" not in dict(_projection()), (
        "load_args ('load --nodes-only') is projected into the space-delimited provenance "
        "line — its space splits the token and corrupts every field after it"
    )


def test_verify_reads_the_status_the_stamp_writes() -> None:
    """Positive control on the status token: `in_progress` from the stamp must REFUSE at verify.

    Without this, `test_incomplete_load_refuses_even_against_its_own_ref` could pass for the
    wrong reason — an unparseable status refused as '<unreadable>' rather than as 'not
    complete'. The refusal must be caused by the value the producer actually writes.
    """
    props = _stamp_properties(status="in_progress")
    assert props["load_status"] == "in_progress", "the stamp does not write the status verbatim"
    line = (
        f"GRAPH_LOAD_PROVENANCE parquet_ref={props['parquet_ref']} "
        f"load_status={props['load_status']} stamped_at=2026-07-11T04:12:03Z image={IMAGE}"
    )
    r = _verify(props["parquet_ref"], f'provenance\n"{line}"\n')
    assert r.returncode != 0
    assert "did NOT complete" in r.stderr, (
        "the refusal did not come from the status VALUE — it may be refusing because the "
        "status was unreadable, which would make the incompleteness check vacuous"
    )


# ===========================================================================
# Locale robustness — `token_value` must not depend on collation order (deploy#601).
# ===========================================================================
# `token_value` extracted `key=value` with the bracket RANGE `[!-~]`. GNU sed evaluates a
# range by the locale's COLLATION order, not codepoint order: under a glibc language locale
# such as en_US.UTF-8 (the locale on every deploy VPS) the collated span `!`..`~` excludes
# ordinary ref bytes (`/`, `-`, `.`), so `parquet_ref=staged/narrator-resolve/<date>-<sha>`
# extracted to EMPTY and `verify` false-refused a correctly-stamped graph with "NO stamp".
# Every test above runs under C / C.UTF-8 (codepoint order), which does NOT reproduce it —
# the same test-vs-prod gap as the sibling verify_prune_provenance.sh matcher (deploy#599).
# The fix is the POSIX class `[[:graph:]]`, which is locale-independent.
_QUIRK_LOCALE_CANDIDATES = (
    "en_US.UTF-8",
    "en_US.utf8",
    "en_GB.UTF-8",
    "de_DE.UTF-8",
    "fr_FR.UTF-8",
    "es_ES.UTF-8",
)


def _locale_reproducing_the_range_quirk() -> str | None:
    """First installed locale under which sed's ``[!-~]`` FAILS to match a ref-like token.

    An INSTRUMENT CHECK, not a formality: a green assertion under a locale that does not
    reproduce the quirk (C / C.UTF-8) proves nothing about the fix, so the test below refuses
    to run under one. An *uninstalled* locale makes glibc fall back to C — where ``[!-~]``
    matches — so the probe rejects it too.
    """
    probe = "staged/narrator-resolve/2026-07-12-bd133e6\n"  # the '/','-','.' bytes at issue
    for loc in _QUIRK_LOCALE_CANDIDATES:
        env = {**os.environ, "LC_ALL": loc, "LC_COLLATE": loc, "LANG": loc}
        try:
            r = subprocess.run(  # noqa: S603
                ["sed", "-n", r"s/^\([!-~][!-~]*\)$/HIT/p"],  # noqa: S607
                input=probe,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            continue
        if r.returncode == 0 and "HIT" not in r.stdout:
            return loc
    return None


@pytest.mark.parametrize("quoted", [True, False])
def test_verify_parses_the_ref_under_a_collation_quirk_locale(quoted: bool) -> None:
    """`verify` must accept a correctly-stamped graph regardless of the VPS's locale (deploy#601).

    Runs the real `verify` under a locale proven above to break the old ``[!-~]`` range, in
    both transport renderings. Before the ``[[:graph:]]`` fix this refuses with "NO stamp"
    (the ref token extracts empty); after it, rc=0.
    """
    loc = _locale_reproducing_the_range_quirk()
    if loc is None:
        pytest.skip(
            "no installed locale reproduces the [!-~] collation quirk "
            "(C / C.UTF-8 use codepoint order); cannot exercise deploy#601 here"
        )
    assert loc is not None  # narrow for mypy --ignore-missing-imports (pytest.skip untyped)
    ok = _verify(
        REF_A,
        _graph_says(ref=REF_A, quoted=quoted),
        LC_ALL=loc,
        LC_COLLATE=loc,
        LANG=loc,
        LC_CTYPE=loc,
    )
    assert ok.returncode == 0, (
        f"verify refused a correctly-stamped graph under {loc} (quoted={quoted}): the "
        f"parquet_ref token matcher is locale-collation-sensitive (deploy#601).\n{ok.stderr}"
    )
    assert f"LOAD_PROVENANCE_OK parquet_ref={REF_A}" in ok.stdout

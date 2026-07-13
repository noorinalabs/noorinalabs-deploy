"""BEHAVIOURAL tests for flag_over_merged_narrators.sh — it is RUN, not grepped.

The script builds the Cypher that graph-flag-over-merged.yml runs against the LIVE stg
graph to set ``over_merged=true`` + ``over_merge_note`` on the producer's curated set of
canonical Narrator ids (deploy#603, the "flag-now" leg of main#928/#958/da#443).

Two properties have to hold and a comment cannot enforce either, so they are executed here:

1. INJECTION. Every id and the note land inside a single-quoted Cypher literal in a
   statement with WRITE access to the graph. The charset is whitelisted, not escaped —
   a ``'`` or ``\\`` cannot reach the statement at all — and the tests below feed the
   classic ``'; MATCH (n) DETACH DELETE n; //`` payload through every mode and require a
   refusal. "Only maintainers can dispatch" is exactly the reasoning that ships an
   injection, so the guard is proven, not asserted.

2. THE CONTRACT. The SET statement must be *exactly* da#447's shape
   (``over_merged=true``, ``over_merge_note=$note``, matched on ``n.id IN $ids``), and the
   ``:param`` lines the workflow prepends must carry precisely the file's ids. A drift on
   either side would flag the wrong nodes or none, silently.

The committed id file ships as a PLACEHOLDER (comments only). ``test_committed_id_file_is_inert``
pins the fail-safe: until the producer's real ids are wired in, a dispatch flags nothing.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
BUILDER = SCRIPTS_DIR / "flag_over_merged_narrators.sh"
COMMITTED_ID_FILE = SCRIPTS_DIR / "over_merged_narrator_ids.txt"

NOTE = "Over-merged canonical narrator: fuses distinct transmitters (main#928 / da#443)"


def _run(
    *args: str, ids_file: Path | None = None, note: str | None = None
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ}
    if ids_file is not None:
        env["IDS_FILE"] = str(ids_file)
    if note is not None:
        env["NOTE"] = note
    return subprocess.run(  # noqa: S603
        ["bash", str(BUILDER), *args],  # noqa: S607
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _ids_file(tmp_path: Path, *ids: str, preamble: str = "# a comment\n\n") -> Path:
    f = tmp_path / "ids.txt"
    f.write_text(preamble + "".join(f"{i}\n" for i in ids))
    return f


# ===========================================================================
# THE CONTRACT — the SET statement is exactly da#447's shape.
# ===========================================================================
def test_set_query_is_the_da447_contract() -> None:
    r = _run("set-query")
    assert r.returncode == 0, r.stderr
    q = r.stdout.strip()
    assert q == (
        "MATCH (n:Narrator) WHERE n.id IN $ids "
        "SET n.over_merged = true, n.over_merge_note = $note RETURN count(n) AS flagged;"
    ), f"SET statement drifted from the da#447 contract:\n{q}"


def test_count_queries_are_parameterized_on_ids() -> None:
    pre = _run("precount-query").stdout
    post = _run("postcount-query").stdout
    assert "WHERE n.id IN $ids RETURN count(n) AS matched;" in pre
    # The post-count is the verification gate: it must require over_merged=true, not just
    # membership, or a run that flagged NOTHING would still "verify".
    assert "n.id IN $ids AND n.over_merged = true RETURN count(n)" in post


# ===========================================================================
# param-ids / id-count — the ids the workflow binds are exactly the file's.
# ===========================================================================
def test_param_ids_carries_exactly_the_file_ids_in_order(tmp_path: Path) -> None:
    f = _ids_file(tmp_path, "nar:aaaa-1111", "nar:bbbb-2222", "nar:cccc-3333")
    r = _run("param-ids", ids_file=f)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == ":param ids => ['nar:aaaa-1111', 'nar:bbbb-2222', 'nar:cccc-3333']"


def test_id_count_ignores_comments_and_blanks(tmp_path: Path) -> None:
    f = _ids_file(tmp_path, "nar:a-1", "nar:b-2", preamble="# header\n\n#nother\n")
    assert _run("id-count", ids_file=f).stdout.strip() == "2"


def test_whitespace_around_ids_is_trimmed(tmp_path: Path) -> None:
    f = tmp_path / "ids.txt"
    f.write_text("  nar:aaaa-1111  \n\tnar:bbbb-2222\t\n")
    assert (
        _run("param-ids", ids_file=f).stdout.strip()
        == ":param ids => ['nar:aaaa-1111', 'nar:bbbb-2222']"
    )


# ===========================================================================
# INJECTION — the guard is proven, not asserted.
# ===========================================================================
INJECTION_IDS = [
    "nar:x'; MATCH (n) DETACH DELETE n; //",  # the one that matters
    "nar:x' SET n.pwned = true //",
    'nar:x" OR 1=1',
    "nar:x\\",
    "nar:x with space",
    "nar:x}{",
    "nar:x`whoami`",  # a backtick
]


@pytest.mark.parametrize("bad", INJECTION_IDS)
def test_param_ids_refuses_an_unsafe_id(tmp_path: Path, bad: str) -> None:
    f = tmp_path / "ids.txt"
    f.write_text(bad + "\n")
    r = _run("param-ids", ids_file=f)
    assert r.returncode != 0, f"unsafe id {bad!r} was accepted into a Cypher literal:\n{r.stdout}"


@pytest.mark.parametrize("bad", INJECTION_IDS)
def test_id_count_also_refuses_an_unsafe_id(tmp_path: Path, bad: str) -> None:
    """id-count must fail on a bad id too — the workflow reads it BEFORE the flag, so a
    mode that counted a poisoned file as 'fine' would green-light the dispatch."""
    f = tmp_path / "ids.txt"
    f.write_text(bad + "\n")
    assert _run("id-count", ids_file=f).returncode != 0


@pytest.mark.parametrize(
    "bad",
    [
        "x' SET n.pwned = true //",
        "note with a ' quote",
        'note "quoted"',
        "back\\slash",
        "brace}{",
        "dollar$sign",
    ],
)
def test_param_note_refuses_an_unsafe_note(bad: str) -> None:
    assert _run("param-note", note=bad).returncode != 0, f"unsafe note {bad!r} accepted"


def test_param_note_emits_a_quoted_literal() -> None:
    r = _run("param-note", note=NOTE)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == f":param note => '{NOTE}'"


def test_param_note_refuses_empty() -> None:
    assert _run("param-note", note="").returncode != 0


# ===========================================================================
# FAIL-SAFE — empty set refuses; the committed placeholder is inert.
# ===========================================================================
def test_empty_id_set_refuses(tmp_path: Path) -> None:
    f = tmp_path / "ids.txt"
    f.write_text("# only comments\n\n")
    r = _run("param-ids", ids_file=f)
    assert r.returncode != 0
    assert "no canonical ids configured" in r.stderr
    # id-count reports 0 rather than erroring — the workflow reads it to say "nothing wired".
    assert _run("id-count", ids_file=f).stdout.strip() == "0"


def test_committed_id_file_is_inert() -> None:
    """The file ships as a placeholder: a dispatch on this PR must flag NOTHING.

    When the producer's ids are wired in, this test flips to asserting the count — it is
    here to guarantee the PR cannot silently ship a live-flagging set no reviewer saw.
    """
    assert COMMITTED_ID_FILE.exists()
    assert _run("id-count", ids_file=COMMITTED_ID_FILE).stdout.strip() == "0", (
        "the committed id file is no longer empty — if the real ids were wired in, update "
        "this test to assert their exact count so the flagged set stays reviewed"
    )
    assert _run("param-ids", ids_file=COMMITTED_ID_FILE).returncode != 0


# ===========================================================================
# stdin composition — the exact stream the workflow pipes to cypher-shell.
# ===========================================================================
def test_stdin_flag_is_params_then_set(tmp_path: Path) -> None:
    """The positive control: the flag phase is the two :param lines then the SET, in order."""
    f = _ids_file(tmp_path, "nar:aaaa-1111", "nar:bbbb-2222")
    r = _run("stdin", "flag", ids_file=f, note=NOTE)
    assert r.returncode == 0, r.stderr
    lines = r.stdout.strip().splitlines()
    assert lines[0] == ":param ids => ['nar:aaaa-1111', 'nar:bbbb-2222']"
    assert lines[1] == f":param note => '{NOTE}'"
    assert lines[2].startswith("MATCH (n:Narrator) WHERE n.id IN $ids SET n.over_merged = true")
    assert lines[2].endswith("RETURN count(n) AS flagged;")


@pytest.mark.parametrize("phase", ["precount", "postcount"])
def test_stdin_count_phases_bind_ids_but_not_note(tmp_path: Path, phase: str) -> None:
    f = _ids_file(tmp_path, "nar:aaaa-1111")
    r = _run("stdin", phase, ids_file=f)  # no NOTE: count phases must not need it
    assert r.returncode == 0, r.stderr
    lines = r.stdout.strip().splitlines()
    assert lines[0] == ":param ids => ['nar:aaaa-1111']"
    assert "note" not in r.stdout, "a count phase bound $note it does not use"
    assert lines[1].startswith("MATCH (n:Narrator) WHERE n.id IN $ids")


def test_stdin_flag_refuses_an_unsafe_id(tmp_path: Path) -> None:
    f = tmp_path / "ids.txt"
    f.write_text("nar:x'; MATCH (n) DETACH DELETE n; //\n")
    assert _run("stdin", "flag", ids_file=f, note=NOTE).returncode != 0


def test_unknown_mode_and_phase_refuse(tmp_path: Path) -> None:
    assert _run("bogus-mode").returncode != 0
    assert _run("stdin", "bogus-phase", ids_file=_ids_file(tmp_path, "nar:a-1")).returncode != 0

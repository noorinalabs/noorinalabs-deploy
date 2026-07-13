"""BEHAVIOURAL tests for flag_over_merged_narrators.sh — it is RUN, not grepped.

The script builds the Cypher that graph-flag-over-merged.yml runs against the LIVE stg
graph to set ``over_merged=true`` on the producer's curated set of canonical Narrator ids
(deploy#603, the "flag-now" leg of main#928/#958/da#443).

BOOLEAN ONLY: the live op sets ``over_merged = true`` and does NOT set ``over_merge_note`` —
the per-node note is free-text prose (with ``"``, ``->``, ``%``) that rides the typed parquet
reload, not this cypher path (deploy#603 revision). So there is no note to escape here; ids
are ``nar:<uuid>`` and whitelisted.

Two properties have to hold and a comment cannot enforce either, so they are executed here:

1. INJECTION. Every id lands inside a single-quoted Cypher literal in a statement with WRITE
   access to the graph. The charset is whitelisted, not escaped — a ``'`` or ``\\`` cannot
   reach the statement at all — and the tests below feed the classic
   ``'; MATCH (n) DETACH DELETE n; //`` payload through every mode and require a refusal.
   "Only maintainers can dispatch" is exactly the reasoning that ships an injection, so the
   guard is proven, not asserted.

2. THE CONTRACT. The SET statement must be *exactly* the boolean shape
   (``SET n.over_merged = true`` matched on ``n.id IN $ids``, no note), and the ``:param``
   line the workflow prepends must carry precisely the file's ids.

``test_committed_id_file_has_the_eight_final_ids`` pins the committed set to the producer's
final resolution (da#448) so the flagged set stays reviewed.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
BUILDER = SCRIPTS_DIR / "flag_over_merged_narrators.sh"
COMMITTED_ID_FILE = SCRIPTS_DIR / "over_merged_narrator_ids.txt"

# The producer's final resolved ids (da#448) — byte-identical to the durable reload.
EIGHT_IDS = [
    "nar:9b793066-94f6-5f99-ae1c-b2636515fa9b",
    "nar:afc30d02-f7ed-5d09-bc3b-4535a27dc0fb",
    "nar:5b41b11e-b18a-5c18-b6e9-2091cb7d3cdf",
    "nar:cb879ada-0dac-575a-a071-ffe2d265129c",
    "nar:0806983d-8571-5a6f-b22e-62647a71cd4c",
    "nar:3f7c1e28-2632-5214-bbec-c17ac5c021a9",
    "nar:49618f4b-7a1b-5c35-8c95-5b837b965857",
    "nar:cf44624b-1f1b-5c9e-b498-625d55191fd2",
]


def _run(*args: str, ids_file: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = {**os.environ}
    if ids_file is not None:
        env["IDS_FILE"] = str(ids_file)
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
# THE CONTRACT — the SET statement is the boolean-only shape.
# ===========================================================================
def test_set_query_is_the_boolean_only_contract() -> None:
    r = _run("set-query")
    assert r.returncode == 0, r.stderr
    q = r.stdout.strip()
    assert q == (
        "MATCH (n:Narrator) WHERE n.id IN $ids SET n.over_merged = true RETURN count(n) AS flagged;"
    ), f"SET statement drifted from the boolean-only contract:\n{q}"
    # Guard against the note creeping back into the live op.
    assert "over_merge_note" not in q, (
        "the live SET must NOT write over_merge_note (it rides the reload)"
    )


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


# ===========================================================================
# FAIL-SAFE — empty set refuses.
# ===========================================================================
def test_empty_id_set_refuses(tmp_path: Path) -> None:
    f = tmp_path / "ids.txt"
    f.write_text("# only comments\n\n")
    r = _run("param-ids", ids_file=f)
    assert r.returncode != 0
    assert "no canonical ids configured" in r.stderr
    # id-count reports 0 rather than erroring — the workflow reads it to say "nothing wired".
    assert _run("id-count", ids_file=f).stdout.strip() == "0"


# ===========================================================================
# stdin composition — the exact stream the workflow pipes to cypher-shell.
# ===========================================================================
def test_stdin_flag_is_param_ids_then_set(tmp_path: Path) -> None:
    """The positive control: the flag phase is the :param ids line then the boolean SET."""
    f = _ids_file(tmp_path, "nar:aaaa-1111", "nar:bbbb-2222")
    r = _run("stdin", "flag", ids_file=f)
    assert r.returncode == 0, r.stderr
    lines = r.stdout.strip().splitlines()
    assert lines[0] == ":param ids => ['nar:aaaa-1111', 'nar:bbbb-2222']"
    assert lines[1] == (
        "MATCH (n:Narrator) WHERE n.id IN $ids SET n.over_merged = true RETURN count(n) AS flagged;"
    )
    assert "note" not in r.stdout, "the flag stream must not carry any note"


@pytest.mark.parametrize("phase", ["precount", "postcount"])
def test_stdin_count_phases_bind_ids(tmp_path: Path, phase: str) -> None:
    f = _ids_file(tmp_path, "nar:aaaa-1111")
    r = _run("stdin", phase, ids_file=f)
    assert r.returncode == 0, r.stderr
    lines = r.stdout.strip().splitlines()
    assert lines[0] == ":param ids => ['nar:aaaa-1111']"
    assert lines[1].startswith("MATCH (n:Narrator) WHERE n.id IN $ids")


def test_stdin_flag_refuses_an_unsafe_id(tmp_path: Path) -> None:
    f = tmp_path / "ids.txt"
    f.write_text("nar:x'; MATCH (n) DETACH DELETE n; //\n")
    assert _run("stdin", "flag", ids_file=f).returncode != 0


def test_unknown_mode_and_phase_refuse(tmp_path: Path) -> None:
    assert _run("bogus-mode").returncode != 0
    assert _run("stdin", "bogus-phase", ids_file=_ids_file(tmp_path, "nar:a-1")).returncode != 0


# ===========================================================================
# The committed id file — the reviewed, final flagged set (da#448).
# ===========================================================================
def test_committed_id_file_has_the_eight_final_ids() -> None:
    """Pins the committed set to the producer's final resolution (da#448).

    If the set changes, this test must be updated deliberately — the flagged nodes are a
    reviewed artifact, not something that should drift silently.
    """
    assert COMMITTED_ID_FILE.exists()
    assert _run("id-count", ids_file=COMMITTED_ID_FILE).stdout.strip() == str(len(EIGHT_IDS))
    r = _run("param-ids", ids_file=COMMITTED_ID_FILE)
    assert r.returncode == 0, r.stderr
    expected = ":param ids => [" + ", ".join(f"'{i}'" for i in EIGHT_IDS) + "]"
    assert r.stdout.strip() == expected, "the committed ids drifted from the da#448 resolved set"

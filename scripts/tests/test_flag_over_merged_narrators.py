"""BEHAVIOURAL tests for flag_over_merged_narrators.py — it is RUN, not grepped.

The builder produces the Cypher that graph-flag-over-merged.yml runs against the LIVE stg
graph: ``over_merged=true`` + a verbatim ``over_merge_note`` on the producer's resolved
canonical ids (deploy#603, the flag-now leg of main#928/#958/da#443).

Three properties have to hold and a comment cannot enforce any of them, so they are executed:

1. ESCAPING (not whitelisting). The producer's notes are verbatim prose with apostrophes
   and double-quotes (``collectors'``, ``("father of Abd Allah")``) and must be written
   BYTE-IDENTICAL to what the durable reload writes. So notes are escaped into single-quoted
   Cypher literals — a ``'`` becomes ``\\'`` and cannot terminate the literal — while being
   preserved exactly. The injection tests feed the ``'; MATCH (n) DETACH DELETE n //``
   payload as a note and require it ESCAPED (present, quote neutralised), not refused.

2. THE CONTRACT. The SET must be exactly da#447's UNWIND shape
   (``MATCH (n:Narrator {id: r.id}) SET n.over_merged = true, n.over_merge_note = r.note``),
   and the pre-check must project ONE row per id so a miss is named, not summed away.

3. IDS ARE STRUCTURAL. A canonical id is ``nar:<uuid>`` — whitelisted (no quote can be one).
   A non-canonical id is refused, because ids come from the producer, not from names
   (da#371/#376 drift).

``test_committed_flags_file_is_the_eight_final_rows`` pins the committed data file to the
producer's final resolved set (da#448) so the flagged set stays reviewed.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
BUILDER = SCRIPTS_DIR / "flag_over_merged_narrators.py"
COMMITTED_FLAGS = SCRIPTS_DIR / "over_merged_narrator_flags.json"

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


def _run(*args: str, rows_file: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = {**os.environ}
    if rows_file is not None:
        env["ROWS_FILE"] = str(rows_file)
    return subprocess.run(  # noqa: S603
        ["python3", str(BUILDER), *args],  # noqa: S607
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _rows_file(tmp_path: Path, rows: list[dict[str, str]]) -> Path:
    f = tmp_path / "rows.json"
    f.write_text(json.dumps(rows), encoding="utf-8")
    return f


# ===========================================================================
# THE CONTRACT.
# ===========================================================================
def test_set_query_is_the_da447_unwind_contract() -> None:
    q = _run("set-query").stdout.strip()
    assert q == (
        "UNWIND $rows AS r MATCH (n:Narrator {id: r.id}) "
        "SET n.over_merged = true, n.over_merge_note = r.note RETURN count(n) AS flagged;"
    ), f"SET drifted from the da#447 UNWIND contract:\n{q}"


def test_precheck_projects_one_row_per_id() -> None:
    q = _run("precheck-query").stdout
    # OPTIONAL MATCH so a miss yields matched=0 (named), not a dropped row.
    assert "UNWIND $rows AS r OPTIONAL MATCH (n:Narrator {id: r.id})" in q
    assert "r.id + ' matched=' + toString(count(n))" in q


def test_postcount_requires_over_merged_true() -> None:
    q = _run("postcount-query").stdout
    assert "WHERE n.over_merged = true RETURN count(n) AS flagged;" in q


# ===========================================================================
# param-rows / count — exactly the file's rows, in order.
# ===========================================================================
def test_param_rows_emits_the_rows_in_order(tmp_path: Path) -> None:
    f = _rows_file(
        tmp_path,
        [{"id": "nar:aaaa-1111", "note": "first"}, {"id": "nar:bbbb-2222", "note": "second"}],
    )
    out = _run("param-rows", rows_file=f).stdout.strip()
    assert out == (
        ":param rows => [{id: 'nar:aaaa-1111', note: 'first'}, "
        "{id: 'nar:bbbb-2222', note: 'second'}]"
    )


def test_count_is_the_row_count(tmp_path: Path) -> None:
    f = _rows_file(tmp_path, [{"id": "nar:a-1", "note": "n"}, {"id": "nar:b-2", "note": "n"}])
    assert _run("count", rows_file=f).stdout.strip() == "2"


# ===========================================================================
# ESCAPING — verbatim notes made safe, and the injection neutralised.
# ===========================================================================
def test_apostrophe_in_a_note_is_escaped(tmp_path: Path) -> None:
    f = _rows_file(tmp_path, [{"id": "nar:a-1", "note": "collectors' teachers"}])
    out = _run("param-rows", rows_file=f).stdout
    assert r"note: 'collectors\' teachers'" in out


def test_double_quote_in_a_note_is_kept_literal(tmp_path: Path) -> None:
    """A ``"`` is not special inside a single-quoted Cypher literal — keep it verbatim."""
    f = _rows_file(tmp_path, [{"id": "nar:a-1", "note": '("father of X")'}])
    assert "note: '(\"father of X\")'" in _run("param-rows", rows_file=f).stdout


def test_backslash_in_a_note_is_escaped(tmp_path: Path) -> None:
    f = _rows_file(tmp_path, [{"id": "nar:a-1", "note": "a\\b"}])
    assert r"note: 'a\\b'" in _run("param-rows", rows_file=f).stdout


def test_newline_and_tab_are_encoded_so_the_line_stays_one_line(tmp_path: Path) -> None:
    f = _rows_file(tmp_path, [{"id": "nar:a-1", "note": "a\nb\tc"}])
    out = _run("param-rows", rows_file=f).stdout
    assert r"note: 'a\nb\tc'" in out
    assert out.rstrip("\n").count("\n") == 0, "the :param line must not contain a raw newline"


def test_note_injection_is_escaped_not_broken_out(tmp_path: Path) -> None:
    """The payload's ``'`` must become ``\\'`` — present, but unable to terminate the literal."""
    payload = "x'; MATCH (n) DETACH DELETE n //"
    f = _rows_file(tmp_path, [{"id": "nar:a-1", "note": payload}])
    out = _run("param-rows", rows_file=f).stdout
    assert r"note: 'x\'; MATCH (n) DETACH DELETE n //'" in out
    # The dangerous form — a bare, unescaped quote closing the literal early — must NOT appear.
    assert "note: 'x';" not in out


def test_control_char_in_a_note_is_refused(tmp_path: Path) -> None:
    f = _rows_file(tmp_path, [{"id": "nar:a-1", "note": "a\x00b"}])
    assert _run("param-rows", rows_file=f).returncode != 0


# ===========================================================================
# IDS are structural — a non-canonical id is refused.
# ===========================================================================
@pytest.mark.parametrize(
    "bad_id",
    [
        "nar:x'; MATCH (n) DETACH DELETE n; //",
        'nar:x" OR 1=1',
        "nar:x\\",
        "nar:x with space",
        "not-a-nar-id",
        "narsomething",
        "",
    ],
)
def test_unsafe_or_non_canonical_id_is_refused(tmp_path: Path, bad_id: str) -> None:
    f = _rows_file(tmp_path, [{"id": bad_id, "note": "n"}])
    assert _run("param-rows", rows_file=f).returncode != 0, f"id {bad_id!r} was accepted"


# ===========================================================================
# Malformed data — every shape refuses, none silently passes.
# ===========================================================================
def test_not_json_refuses(tmp_path: Path) -> None:
    f = tmp_path / "rows.json"
    f.write_text("this is not json", encoding="utf-8")
    assert _run("param-rows", rows_file=f).returncode != 0


def test_missing_file_refuses(tmp_path: Path) -> None:
    assert _run("param-rows", rows_file=tmp_path / "nope.json").returncode != 0


@pytest.mark.parametrize(
    "payload",
    [
        [],  # empty list — flags nothing
        {"id": "nar:a-1", "note": "n"},  # object, not a list
        [{"id": "nar:a-1"}],  # missing note
        [{"note": "n"}],  # missing id
        [{"id": "nar:a-1", "note": ""}],  # empty note
        [{"id": "nar:a-1", "note": "  "}],  # whitespace-only note
        [{"id": "nar:a-1", "note": "n"}, {"id": "nar:a-1", "note": "dup"}],  # duplicate id
    ],
)
def test_malformed_payload_refuses(tmp_path: Path, payload: object) -> None:
    f = tmp_path / "rows.json"
    f.write_text(json.dumps(payload), encoding="utf-8")
    assert _run("param-rows", rows_file=f).returncode != 0


# ===========================================================================
# stdin composition — the exact stream the workflow pipes to cypher-shell.
# ===========================================================================
def test_stdin_flag_is_param_rows_then_set(tmp_path: Path) -> None:
    f = _rows_file(tmp_path, [{"id": "nar:aaaa-1111", "note": "why"}])
    out = _run("stdin", "flag", rows_file=f).stdout.strip().splitlines()
    assert out[0] == ":param rows => [{id: 'nar:aaaa-1111', note: 'why'}]"
    assert out[1].startswith(
        "UNWIND $rows AS r MATCH (n:Narrator {id: r.id}) SET n.over_merged = true"
    )


@pytest.mark.parametrize(
    ("phase", "needle"),
    [
        ("precheck", "OPTIONAL MATCH (n:Narrator {id: r.id})"),
        ("postcount", "WHERE n.over_merged = true"),
    ],
)
def test_stdin_count_phases_bind_rows(tmp_path: Path, phase: str, needle: str) -> None:
    f = _rows_file(tmp_path, [{"id": "nar:aaaa-1111", "note": "why"}])
    out = _run("stdin", phase, rows_file=f).stdout
    assert out.splitlines()[0].startswith(":param rows => [")
    assert needle in out


def test_unknown_mode_and_phase_refuse(tmp_path: Path) -> None:
    assert _run("bogus").returncode != 0
    f = _rows_file(tmp_path, [{"id": "nar:a-1", "note": "n"}])
    assert _run("stdin", "bogus", rows_file=f).returncode != 0


# ===========================================================================
# The committed data file — the reviewed, final flagged set.
# ===========================================================================
def test_committed_flags_file_is_the_eight_final_rows() -> None:
    """Pins the committed set to the producer's final resolution (da#448).

    If the set changes, this test must be updated deliberately — the flagged nodes are a
    reviewed artifact, not something that should drift silently.
    """
    assert COMMITTED_FLAGS.exists()
    data = json.loads(COMMITTED_FLAGS.read_text(encoding="utf-8"))
    assert [r["id"] for r in data] == EIGHT_IDS
    assert all(r["note"].strip() for r in data)
    # The builder accepts it and emits one map per id.
    out = _run("param-rows", rows_file=COMMITTED_FLAGS)
    assert out.returncode == 0, out.stderr
    assert out.stdout.count("{id: 'nar:") == len(EIGHT_IDS)
    assert _run("count", rows_file=COMMITTED_FLAGS).stdout.strip() == str(len(EIGHT_IDS))

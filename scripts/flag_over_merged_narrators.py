#!/usr/bin/env python3
"""Build the Cypher that flags over-merged Narrator nodes on the LIVE graph.

For graph-flag-over-merged.yml (deploy#603) — the immediate "flag-NOW" leg of the
honest-leaderboard work (main#928 / main#958 / da#443).

da#447 gave every canonical Narrator row an ``over_merged: bool`` (+ ``over_merge_note``):
true means the node FUSES multiple distinct transmitters, so its betweenness/centrality is
inflated and it must not be read as one historical narrator. deploy-data-load.yml carries
those properties into Neo4j on every reload (the durable transport). A reload is ~7.5h, so
this backs the immediate path: annotate the live stg graph directly from the producer's
resolved id set, no reload.

WHY PYTHON, NOT A SHELL ssh BLOCK. The producer's notes are verbatim prose that contains
apostrophes and double-quotes (``collectors'``, ``("father of Abd Allah")``) and must be
written BYTE-IDENTICAL to what the durable reload writes (so the immediate flag and the
reload stay consistent). That rules out the whitelist-refuse approach the sibling
``graph_load_provenance.sh`` uses for its short controlled strings — a note cannot be
"fixed" to drop an apostrophe. So the notes are ESCAPED into single-quoted Cypher string
literals (``\\`` -> ``\\\\`` then ``'`` -> ``\\'``, with control chars encoded), which
preserves them exactly. Robust JSON parsing + escaping is python's job, not sed's — and
being a script under scripts/ it is ruff/mypy-linted and behaviourally unit-tested
(scripts/tests/test_flag_over_merged_narrators.py); an ssh ``with: script:`` body is
neither (deploy#555).

INJECTION MODEL. Ids are WHITELISTED (a canonical id is ``nar:<uuid>`` — no quote or
backslash can be one), so they are interpolated directly. Notes are ESCAPED, not
whitelisted, because they must survive verbatim; the escaping makes a ``'`` inside a note
``\\'`` so it cannot terminate the literal and break out of the statement. The QUERIES are
parameterized on ``$rows`` (a ``[{id, note}]`` list bound via cypher-shell ``:param``); the
statement text is fixed and the only operator/producer bytes that reach the session are the
escaped literals this script builds.

MODES:
  set-query        the parameterized UNWIND ... SET (references $rows).
  precheck-query   per-id OPTIONAL MATCH -> "<id> matched=<n>" (which ids resolve).
  postcount-query  count of $rows ids now carrying over_merged=true.
  param-rows       env: ROWS_FILE   ->  :param rows => [{id: '..', note: '..'}, ..]
  count            env: ROWS_FILE   ->  the integer row count.
  stdin <phase>    env: ROWS_FILE   ->  the full cypher-shell stdin stream for
                   <phase> = precheck | flag | postcount (the :param line then the query).

All statements are single statements terminated by ``;`` so a non-interactive cypher-shell
(which auto-commits each stdin statement — cypher-shell 5.x removed ``:auto``, deploy#540)
runs them in the implicit transaction a plain SET/UNWIND needs.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import NoReturn

# Default to the committed flag set; the test overrides ROWS_FILE to a fixture.
ROWS_FILE = os.environ.get("ROWS_FILE") or str(
    Path(__file__).resolve().parent / "over_merged_narrator_flags.json"
)

# A canonical Narrator id is `nar:<uuid>` — this charset admits no quote, backslash, space,
# or brace, so a validated id is safe to interpolate directly into a Cypher literal.
_ID_RE = re.compile(r"^nar:[A-Za-z0-9:_-]+$")

# Fixed, parameter-driven statements — no producer/operator bytes here. `$rows` is bound
# from the :param line param_rows() builds.
_SET_QUERY = (
    "UNWIND $rows AS r MATCH (n:Narrator {id: r.id}) "
    "SET n.over_merged = true, n.over_merge_note = r.note RETURN count(n) AS flagged;"
)
# One row per configured id, so a missing id is NAMED, not summed away (the read-back must
# say WHICH ids matched 0 — silent-zero discipline). OPTIONAL MATCH yields count 0 on a
# miss, 1 on a hit.
_PRECHECK_QUERY = (
    "UNWIND $rows AS r OPTIONAL MATCH (n:Narrator {id: r.id}) "
    "RETURN r.id + ' matched=' + toString(count(n)) AS idmatch;"
)
_POSTCOUNT_QUERY = (
    "UNWIND $rows AS r MATCH (n:Narrator {id: r.id}) "
    "WHERE n.over_merged = true RETURN count(n) AS flagged;"
)


def _die(msg: str) -> NoReturn:
    print(f"ERROR: [flag-over-merged] {msg}", file=sys.stderr)
    raise SystemExit(1)


def _cypher_str(value: str) -> str:
    """Escape a string into a single-line, single-quoted Cypher string literal.

    Only ``\\`` and ``'`` are structurally special inside a single-quoted Cypher literal;
    escaping them (backslash first, then quote) makes ANY note safe while preserving it
    verbatim. Newlines/tabs are encoded so the whole `:param` line stays one line for the
    cypher-shell stdin stream; any other control byte is refused (prose has none, and a raw
    control byte in a "verbatim" note is far likelier a corruption than intent).
    """
    out = value.replace("\\", "\\\\").replace("'", "\\'")
    out = out.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    if any(ord(c) < 0x20 for c in out):
        _die("note contains an unsupported control character; refusing to build a literal.")
    return f"'{out}'"


def _load_rows() -> list[tuple[str, str]]:
    path = Path(ROWS_FILE)
    if not path.is_file():
        _die(f"data file not found: {ROWS_FILE}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        _die(f"data file is not valid JSON ({ROWS_FILE}): {exc}")
    if not isinstance(data, list) or not data:
        _die(
            f"data file must be a non-empty JSON list of {{id, note}} objects ({ROWS_FILE}); "
            "an empty set flags nothing (fail-safe refusal)."
        )
    rows: list[tuple[str, str]] = []
    seen: set[str] = set()
    for i, item in enumerate(data):
        if not isinstance(item, dict) or "id" not in item or "note" not in item:
            _die(f"row {i} must be an object with exactly 'id' and 'note'.")
        rid, note = item["id"], item["note"]
        if not isinstance(rid, str) or not _ID_RE.match(rid):
            _die(
                f"row {i}: id {rid!r} is not a canonical nar:<uuid> id. Ids come from the "
                "producer's resolution (da#447); do not hand-derive them (da#371/#376 drift)."
            )
        if not isinstance(note, str) or not note.strip():
            _die(f"row {i}: note is empty — every flagged node must carry an over_merge_note.")
        if rid in seen:
            _die(f"duplicate id {rid} in {ROWS_FILE}.")
        seen.add(rid)
        rows.append((rid, note))
    return rows


def _param_rows() -> str:
    rows = _load_rows()
    # id is whitelisted (safe to interpolate); note is escaped (safe + verbatim).
    parts = [f"{{id: '{rid}', note: {_cypher_str(note)}}}" for rid, note in rows]
    return ":param rows => [" + ", ".join(parts) + "]"


def _stdin(phase: str) -> str:
    query = {
        "precheck": _PRECHECK_QUERY,
        "flag": _SET_QUERY,
        "postcount": _POSTCOUNT_QUERY,
    }.get(phase)
    if query is None:
        _die(f"stdin: unknown phase {phase!r} (want: precheck | flag | postcount)")
    return f"{_param_rows()}\n{query}"


def main(argv: list[str]) -> int:
    mode = argv[1] if len(argv) > 1 else ""
    if mode == "set-query":
        print(_SET_QUERY)
    elif mode == "precheck-query":
        print(_PRECHECK_QUERY)
    elif mode == "postcount-query":
        print(_POSTCOUNT_QUERY)
    elif mode == "param-rows":
        print(_param_rows())
    elif mode == "count":
        print(len(_load_rows()))
    elif mode == "stdin":
        print(_stdin(argv[2] if len(argv) > 2 else ""))
    else:
        print(
            "usage: flag_over_merged_narrators.py "
            "{set-query|precheck-query|postcount-query|param-rows|count|stdin <phase>}",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

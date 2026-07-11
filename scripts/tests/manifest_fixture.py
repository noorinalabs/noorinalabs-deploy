"""Build a `_backup_manifest.txt` fixture FROM `backup.sh`'S OWN printf — never from ours.

The manifest line is the exact place the last two bugs lived. `backup_is_complete`'s regex
could never match a real manifest, and it shipped through a green suite because every fixture
restated the format **in the test's own words** — so the consumer's parser and the producer's
writer were never actually introduced to each other.

    The paraphrase cannot falsify the thing it paraphrases: a fixture that restates the format
    is written by the same mind that wrote the parser, and encodes the same misreading.

This is the machinery `test_restore_failure_modes._backup_dump_filenames` already applies to
the dump FILENAMES ("the artifact does not name itself"), extended one field further, at
Nino Kavtaradze's request in the deploy#584 review.

BOTH halves are read out of the producer, and that matters:

* the **format string** gives the field names and their order;
* the **argument list** gives which shell variable fills each `%s`.

Taking only the format would let a producer-side reorder — `stores=%s complete=%s ...` —
silently put `true` in the `stores=` slot and keep every test green. Keying the values by the
producer's own variable names makes a reorder follow through into the fixture, and a *rename*
raise `KeyError` loudly rather than fabricate a plausible line.
"""

from __future__ import annotations

import re
from pathlib import Path

BACKUP_SH = Path(__file__).resolve().parent.parent / "backup.sh"

# `printf 'BACKUP_MANIFEST complete=%s ...\n' "$BACKUP_COMPLETE" "$BACKUP_STORES" ... > "$FILE"`
_PRINTF = re.compile(
    r"printf '(?P<fmt>BACKUP_MANIFEST [^']*)'\s*\\?\s*\n?\s*(?P<args>(?:\"\$\w+\"\s*)+)",
)


def manifest_format() -> tuple[str, list[str]]:
    """The producer's format string and the shell variables that fill it, in order."""
    m = _PRINTF.search(BACKUP_SH.read_text())
    assert m is not None, (
        "could not find backup.sh's `printf 'BACKUP_MANIFEST ...'`. If the producer changed "
        "how it writes the manifest, the fixtures must follow it — do not hand-write one."
    )
    return m.group("fmt"), re.findall(r'"\$(\w+)"', m.group("args"))


def build_manifest(
    *,
    complete: bool = True,
    run_ts: str = "20260711-030100",
    stores: str = "postgres,user-postgres,neo4j",
    category: str = "daily",
) -> bytes:
    """Render `backup.sh`'s manifest line, using `backup.sh`'s own printf."""
    fmt, args = manifest_format()
    values = {
        "BACKUP_COMPLETE": "true" if complete else "false",
        "BACKUP_STORES": stores,
        "TIMESTAMP": run_ts,
        "BACKUP_CATEGORY": category,
    }
    out = fmt.replace("\\n", "\n")
    for var in args:
        assert var in values, (
            f"backup.sh's manifest printf now takes ${var}, which this fixture does not know "
            "how to fill. The producer changed; teach the fixture rather than guessing."
        )
        out = out.replace("%s", values[var], 1)
    assert "%s" not in out, "unfilled %s — the format takes more args than backup.sh passes it"
    return out.encode()

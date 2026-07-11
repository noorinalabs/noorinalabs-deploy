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

# `MANIFEST_FILE="${LOCAL_BACKUP_PATH}/_backup_manifest-${TIMESTAMP}.txt"`
_MANIFEST_FILE = re.compile(r'MANIFEST_FILE="\$\{LOCAL_BACKUP_PATH\}/(?P<name>[^"]+)"')

# The default run id used across the fixtures. `backup.sh` builds it with
# `date -u +%Y%m%d-%H%M%S`, so it is strict and self-delimiting — which is why every parser in
# the consumers anchors on THIS and never on the store name (deploy#589).
DEFAULT_RUN_TS = "20260711-030100"


def manifest_format() -> tuple[str, list[str]]:
    """The producer's format string and the shell variables that fill it, in order."""
    m = _PRINTF.search(BACKUP_SH.read_text())
    assert m is not None, (
        "could not find backup.sh's `printf 'BACKUP_MANIFEST ...'`. If the producer changed "
        "how it writes the manifest, the fixtures must follow it — do not hand-write one."
    )
    return m.group("fmt"), re.findall(r'"\$(\w+)"', m.group("args"))


def manifest_filename(run_ts: str = DEFAULT_RUN_TS) -> str:
    """The manifest's FILE NAME, read out of `backup.sh` — never restated here.

    The name is not decoration; it is the deploy#587 fix. ``_backup_manifest.txt`` is a FIXED
    name inside a directory that holds MULTIPLE RUNS — the B2 path is ``<category>/<DATE>`` (a
    DAY), the dumps in it are ``isnad-<store>-<TS>`` (a RUN), ``rclone copy`` adds and never
    deletes, and ``backup.sh`` deliberately uploads partials. So the fixed-name manifest was
    overwritten by **whichever run uploaded last**, and a good run followed by a partial one
    ERASED ITS OWN ATTESTATION.

    This is the other half of what ``build_manifest`` already does for the manifest's CONTENTS:
    the artifact does not name itself in the test's words. A fixture that hard-codes
    ``_backup_manifest.txt`` would keep passing against a producer that had stopped writing it.
    """
    m = _MANIFEST_FILE.search(BACKUP_SH.read_text())
    assert m is not None, (
        "could not find backup.sh's `MANIFEST_FILE=` assignment. If the producer changed how "
        "it names the manifest, the fixtures must follow it — do not hand-write one."
    )
    name = m.group("name")
    assert "${TIMESTAMP}" in name, (
        f"backup.sh names its manifest {name!r} — the RUN ID is not in the name, so a "
        "day-directory holding several runs has ONE manifest and the last run to upload "
        "OVERWRITES the attestation of every run before it. A good run followed by a partial "
        "one then erases its own attestation (deploy#587). Completeness is a property of a "
        "RUN, so the manifest must be named for its run."
    )
    return name.replace("${TIMESTAMP}", run_ts)


def build_manifest(
    *,
    complete: bool = True,
    run_ts: str = DEFAULT_RUN_TS,
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

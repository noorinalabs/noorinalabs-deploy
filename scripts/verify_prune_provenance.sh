#!/usr/bin/env bash
# =============================================================================
# verify_prune_provenance.sh — the provenance gate for graph-prune-narrators.
#
# Exits 0 and prints `PRUNE_PROVENANCE canonical_ids=<n>` only when the keep-set
# on disk carries a trustworthy, non-circular declaration of how many canonical
# ids it should hold. Exits non-zero, printing why, otherwise.
#
# WHY THIS IS A SCRIPT AND NOT TEN LINES OF THE WORKFLOW'S ssh BLOCK
#
# It was ten lines of the ssh block. Weronika Zielinska mutated that block to
# forge a manifest on the box when B2 had none — three lines, `[ -s "$M" ] ||
# printf 'CANONICAL_MANIFEST …' > "$M"` — and the whole test suite still reported
# `10 passed`, because those tests grep the workflow's text. The property that
# justifies merging this ahead of da#428 — that the prune is INERT without
# provenance — was pinned by a layer that cannot see it being removed.
#
# A guard asserted by substring is not a guard. So the gate lives here, where a
# test can EXECUTE it against fixtures and watch it refuse. Being under scripts/
# it is also the first line of this workflow's shell to get a linter at all:
# actionlint does not shellcheck an ssh-action `with: script:` body (deploy#555).
#
# THE TWO ARTIFACTS, AND WHY THE SPLIT IS THE WHOLE POINT
#
# Requiring "a manifest" is not enough, because WHO WROTE IT decides whether its
# count means anything.
#
#   curated/_resolve_run.txt   written by RESOLVE — the process that produced the
#                              parquet. It alone holds an in-memory tally taken
#                              BEFORE the write, so its count does NOT agree with
#                              a short file. It also alone knows whether the run
#                              finished.
#
#     RESOLVE_RUN canonical_ids=<n> run_status=complete git_sha=<sha>
#
#   curated/_manifest.txt      written by publish_parquet.py — a SEPARATE process
#                              that starts after resolve has exited, reads
#                              data/curated/ off disk and uploads it. It has no
#                              pyarrow import and no access to any tally
#                              (verified at da origin/main). Any count it computed
#                              could only be a READ-BACK of the parquet it is about
#                              to upload — which agrees perfectly with a truncated
#                              one. So it is FORBIDDEN from declaring a count. It
#                              declares only what a publisher can honestly attest:
#                              the bytes it moved.
#
#     CANONICAL_MANIFEST file=<name> md5=<32hex> bytes=<n>
#
# The circularity is not eliminated by asking for provenance; it is RELOCATED to
# whoever computes the number. Splitting the contract this way makes a read-back
# implementation STRUCTURALLY IMPOSSIBLE rather than merely discouraged: the only
# artifact permitted to carry a count is written by the only process that can take
# one honestly. If `_manifest.txt` ever carries `canonical_ids=`, this script
# REFUSES — a publisher that thinks it knows the count has read the file back, and
# a count derived from the artifact is exactly the thing that cannot guard it.
#
# Contract: da#428.
# =============================================================================
set -euo pipefail

CURATED_DIR="${CURATED_DIR:?CURATED_DIR must be set (the pulled curated/ directory)}"
PARQUET_NAME="${PARQUET_NAME:-narrators_canonical.parquet}"
# Optional operator corroboration. Blank = skipped. It is NOT the gate — see the
# workflow — and there is deliberately no input here that can skip the gate.
EXPECTED_CANONICAL_IDS="${EXPECTED_CANONICAL_IDS:-}"

RESOLVE_RUN="${CURATED_DIR}/_resolve_run.txt"
MANIFEST="${CURATED_DIR}/_manifest.txt"
PARQUET="${CURATED_DIR}/${PARQUET_NAME}"

die() {
    echo "ERROR: [provenance] $1" >&2
    shift
    for _l in "$@"; do echo "  ${_l}" >&2; done
    echo "  NO node was deleted." >&2
    exit 1
}

# token_value <line> <key> [charclass]
# Whole-TOKEN extraction: split the line on spaces and match an exact `key=value`
# token. A key can therefore never be hijacked by a longer key ending in its name
# (da#419) — `edges_deleted=` can never answer for `deleted=`.
#
# The charclass is a PARAMETER, defaulting to digits, because `run_status=complete`
# is a WORD. The parser this replaces hard-coded `[0-9a-f]` and so could not read a
# word value at all: it would have returned empty for run_status, and an empty read
# that nobody notices is precisely the silent-zero this workflow exists to refuse.
token_value() {
    printf '%s\n' "$1" \
        | tr ' ' '\n' \
        | sed -n "s/^$2=\\($3$3*\\)\$/\\1/p" \
        | head -n1
}

# The CANONICAL_MANIFEST line for OUR parquet. A manifest may describe several
# objects; match the `file=` token exactly, never a substring.
#
# The `file` value is a PRODUCER-CHOSEN identifier (a filename), so its charclass is
# [[:graph:]] — the POSIX "printable non-space" class — NOT a range. A range like
# `[!-~]` is a byte-order assumption that GNU sed evaluates by LOCALE COLLATION: under
# a language locale such as en_US.UTF-8 (every deploy VPS) the collated range excludes
# ordinary filename bytes (`.`, `_`), the whole match returns empty, and the gate
# false-refuses a valid keep-set with "has no md5" — with no deletion, but also no
# prune (deploy#599). [[:graph:]] is locale-safe and is the WIDEST printable class, so
# it does not "quietly narrow" the way an enumerated subset would (deploy#589); a
# filename byte that somehow fell outside it would fail CLOSED, never silently through.
manifest_line() {
    grep '^CANONICAL_MANIFEST ' "${MANIFEST}" 2>/dev/null | while IFS= read -r _l; do
        if [ "$(token_value "${_l}" file '[[:graph:]]')" = "${PARQUET_NAME}" ]; then
            printf '%s\n' "${_l}"
            return 0
        fi
    done | head -n1
}

resolve_line() {
    grep '^RESOLVE_RUN ' "${RESOLVE_RUN}" 2>/dev/null | head -n1
}

# --- 1. Both artifacts must exist. Absent = inert. ---------------------------
[ -s "${RESOLVE_RUN}" ] || die \
    "curated/_resolve_run.txt is absent or empty." \
    "The keep-set carries no declaration from the process that WROTE it, so nothing" \
    "independent of the parquet says how many canonical ids it should hold. A" \
    "truncated-but-valid parquet is then indistinguishable from a complete one:" \
    "da#426 measured that shape deleting 83.9% of the graph with missing=0 and every" \
    "derived gate green. Publish this ref with a resolve-run record (da#428)."

[ -s "${MANIFEST}" ] || die \
    "curated/_manifest.txt is absent or empty." \
    "Without it the bytes on this box cannot be tied to the bytes that were published."

[ -s "${PARQUET}" ] || die "${PARQUET_NAME} is absent or empty."

# --- 2. The publisher must NOT declare a count. ------------------------------
# publish_parquet.py runs after resolve has exited and reads the parquet off disk.
# It has no tally. If it emits a count, that count is a read-back — it agrees with
# a short file by construction, and would sail through every gate below wearing the
# words "producer-signed provenance". Refuse the whole artifact rather than trust it.
if grep -q '^CANONICAL_MANIFEST .*canonical_ids=' "${MANIFEST}"; then
    die "curated/_manifest.txt declares canonical_ids — it must not." \
        "The publisher is a separate process from resolve and holds no tally, so any" \
        "count it emits is a READ-BACK of the parquet it is uploading, which agrees with" \
        "a truncated file. Only curated/_resolve_run.txt may carry a count. Fix the" \
        "producer (da#428); do not relax this check."
fi

# --- 3. The run must have FINISHED. -----------------------------------------
# No count equality of any kind can see "resolve stopped early": a run that halts
# after writing a coherent partial file is internally consistent, and its tally —
# taken before the write — would match what it managed to write. Only the producer
# can attest that it reached the end, so it must, explicitly.
RESOLVE_LINE="$(resolve_line)"
RUN_STATUS="$(token_value "${RESOLVE_LINE}" run_status '[a-z_]')"
[ -n "${RUN_STATUS}" ] || die \
    "curated/_resolve_run.txt does not declare run_status." \
    "Expected: RESOLVE_RUN canonical_ids=<n> run_status=complete git_sha=<sha>"
if [ "${RUN_STATUS}" != "complete" ]; then
    die "the resolve run that produced this keep-set did NOT complete (run_status=${RUN_STATUS})." \
        "Its canonical set is partial BY THE PRODUCER'S OWN ACCOUNT. Every id in it is real," \
        "so 'missing' would be 0 and every derived gate would go green while the prune" \
        "deleted the narrators the run never got to name."
fi

# --- 4. The producer's tally. ------------------------------------------------
RESOLVE_CANON_IDS="$(token_value "${RESOLVE_LINE}" canonical_ids '[0-9]')"
[ -n "${RESOLVE_CANON_IDS}" ] || die \
    "curated/_resolve_run.txt does not declare canonical_ids."
if [ "${RESOLVE_CANON_IDS}" -le 0 ]; then
    die "resolve declares canonical_ids=${RESOLVE_CANON_IDS} — a prune against a zero-id keep-set would delete every narrator."
fi

# --- 5. Byte integrity. ------------------------------------------------------
# A SEPARATE property from completeness. This catches a corrupt or truncated
# TRANSFER; it cannot catch a short WRITE, because a short parquet has a perfectly
# good md5 of itself. The count equality (done by the caller, against the tally
# above) catches that. Neither substitutes for the other.
MF_MD5="$(token_value "$(manifest_line)" md5 '[0-9a-f]')"
[ -n "${MF_MD5}" ] || die \
    "curated/_manifest.txt has no md5 for ${PARQUET_NAME}." \
    "Expected: CANONICAL_MANIFEST file=${PARQUET_NAME} md5=<32hex> bytes=<n>"
GOT_MD5="$(md5sum "${PARQUET}" | cut -d' ' -f1)"
if [ "${GOT_MD5}" != "${MF_MD5}" ]; then
    die "${PARQUET_NAME} md5 mismatch — the bytes here are NOT the bytes that were published." \
        "manifest: ${MF_MD5}" \
        "on disk : ${GOT_MD5}"
fi

# --- 6. Optional operator corroboration. -------------------------------------
# Not the gate. It covers the one failure the producer's own record cannot: an
# operator who meant run A and typed run B's parquet_ref. B's record and B's parquet
# agree with each other perfectly, so everything above passes.
if [ -n "${EXPECTED_CANONICAL_IDS}" ]; then
    case "${EXPECTED_CANONICAL_IDS}" in
        *[!0-9]*) die "expected_canonical_ids='${EXPECTED_CANONICAL_IDS}' is not an integer." ;;
    esac
    if [ "${RESOLVE_CANON_IDS}" -ne "${EXPECTED_CANONICAL_IDS}" ]; then
        die "the resolve run and the operator disagree about which run this is." \
            "resolve declared    : ${RESOLVE_CANON_IDS} canonical ids" \
            "operator expected   : ${EXPECTED_CANONICAL_IDS}" \
            "Either parquet_ref points at a different run than you meant, or your" \
            "expectation is stale. We do not get to guess which."
    fi
fi

echo "PRUNE_PROVENANCE canonical_ids=${RESOLVE_CANON_IDS} run_status=${RUN_STATUS} md5=${GOT_MD5}"

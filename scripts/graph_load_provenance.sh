#!/usr/bin/env bash
# =============================================================================
# graph_load_provenance.sh — the GRAPH-side load-provenance binding (deploy#580).
#
# THE HOLE THIS CLOSES
#
# graph-prune-narrators.yml prunes the graph down to the canonical set of whatever
# `parquet_ref` the operator types. Every gate in it is sound RELATIVE TO THAT REF:
# the resolve record declares the count, the count binds the parquet, the md5 binds
# the bytes, `missing` rejects a foreign keep-set.
#
# Not one of them can ask whether it was the RIGHT ref.
#
# The residual case is a GENUINE BUT OLDER, SMALLER run — not corrupt, not foreign,
# a real earlier resolve run:
#
#   * its _resolve_run.txt is valid and its tally agrees with its parquet (they were
#     published together);
#   * its md5 matches;
#   * `missing` is ~0, because that run's ids are a SUBSET of what the graph now holds;
#   * the completeness equality passes, because the parquet really does hold exactly
#     what its producing run declared;
#   * and if the operator supplies expected_canonical_ids they will supply THAT RUN'S
#     count, which agrees — because it is that run's real count.
#
# Every automated check goes green, and the prune deletes every narrator loaded since.
#
# `missing` is STRUCTURALLY BLIND here and no fix to it can help: it bounds
# `canonical − graph`, and NONE of those elements can be deleted. The over-deletion
# lives entirely in `graph − canonical` — which is exactly the set the tool exists to
# delete. NO GATE DERIVED FROM THE PARQUET CAN SEE THIS, because the parquet is
# internally perfect. The artifact cannot vouch for itself, and per the deploy#574
# review the operator has no independently-sourced number either (the pipeline's only
# count is a read-back of the artifact — da `src/resolve/__init__.py:344`).
#
# A gate derived from the GRAPH can see it, and is the only thing that can:
#
#   1. deploy-data-load.yml STAMPS the parquet_ref it loaded onto the graph.
#   2. graph-prune-narrators.yml READS IT BACK and refuses unless the ref it is
#      pruning against is the ref the graph was actually loaded from.
#
# This is the only check in the system independent of BOTH the parquet and the
# operator. That independence is the entire value; do not "simplify" it by deriving
# any part of it from the keep-set.
#
# WHY ONE FILE OWNS ALL THREE MODES
#
# `stamp` writes the properties, `read-query` projects them, `verify` parses the
# projection. A rename or reorder in any one of those silently narrows the next
# (deploy#589 — a paraphrase in the PRODUCT does not break the parser, it QUIETLY
# NARROWS it, and the guard downstream stops guarding while every test stays green).
# Keeping the writer, the projector and the parser within twenty lines of each other,
# under one test file that runs all three against each other, is what makes that drift
# a red test instead of a silent hole.
#
# WHY A SCRIPT AND NOT TEN LINES OF THE WORKFLOW'S ssh BLOCK
#
# Because a guard that lives in an ssh `script:` block can only ever be pinned by
# grepping the workflow's TEXT, and this team has already proved twice what that is
# worth: a forged manifest in the gate (10 passed) and the identical forge relocated
# to the caller (still green). A guard asserted by substring is not a guard. Living
# under scripts/ this is EXECUTED by scripts/tests/test_load_provenance.py against
# fixtures, and shellcheck-linted in CI — which an ssh `with: script:` body is not
# (deploy#555).
#
# ---------------------------------------------------------------------------------
# MODES
#
#   stamp        env: PARQUET_REF LOAD_STATUS IMAGE LOAD_ARGS
#                out: the Cypher MERGE that records what this load loaded.
#
#   read-query   out: the Cypher that projects the stamp as ONE machine-readable line:
#                     GRAPH_LOAD_PROVENANCE parquet_ref=<ref> load_status=<s> ...
#
#   verify       env: PRUNE_PARQUET_REF PROVENANCE_OUTPUT
#                rc 0 only if the graph was loaded from PRUNE_PARQUET_REF by a load
#                that COMPLETED. Otherwise non-zero, with a diagnostic naming BOTH refs.
#
# LOAD_STATUS is stamped `in_progress` BEFORE the loader runs and `complete` only
# after it commits. A load that dies mid-write therefore leaves the graph stamped
# `in_progress`, and this gate refuses to prune it AGAINST ANY REF — including its
# own. That is deliberate and it is the fail-closed direction: a half-loaded graph
# holds narrators no keep-set names, and pruning it deletes exactly those. Re-run the
# load to clear it. Backups have never actually run (deploy#558/#559/#560), so a
# wrong-ref prune is UNRECOVERABLE and every ambiguity here resolves to "refuse".
# =============================================================================
set -euo pipefail

# The one place the stamp's shape is defined. `stamp` writes these, `read-query`
# projects them, `verify` parses them.
NODE_LABEL="LoadProvenance"
NODE_SCOPE="graph"
LINE_KEYWORD="GRAPH_LOAD_PROVENANCE"
STATUS_IN_PROGRESS="in_progress"
STATUS_COMPLETE="complete"

die() {
    echo "ERROR: [load-provenance] $1" >&2
    shift
    for _l in "$@"; do echo "  ${_l}" >&2; done
    echo "  NO node was deleted." >&2
    exit 1
}

# ---------------------------------------------------------------------------
# safe_literal <name> <value> <allowed-charclass>
#
# Every value below is interpolated into a single-quoted Cypher string literal, so a
# value containing `'` or `\` is a CYPHER INJECTION into a statement that runs with
# write access to the graph. `parquet_ref` and `image` are BOTH free-text
# workflow_dispatch inputs; treating them as trusted because "only maintainers can
# dispatch" is exactly the reasoning that makes an injection bug ship.
#
# So the values are whitelisted, not escaped: no quote, no backslash, no newline, no
# brace can reach the statement at all. Refuse rather than sanitise — a rejected input
# is a typo the operator fixes in ten seconds; a sanitised one is a guess about intent.
# ---------------------------------------------------------------------------
safe_literal() {
    _name="$1"
    _val="$2"
    _allowed="$3"
    [ -n "${_val}" ] || die "${_name} is empty — refusing to stamp an unidentifiable load."
    # shellcheck disable=SC2254  # ${_allowed} is an intentional bracket-expression body.
    case "${_val}" in
        *[!${_allowed}]*)
            die "${_name}='${_val}' contains a character outside [${_allowed}]." \
                "It is interpolated into a single-quoted Cypher literal, so a quote or a" \
                "backslash here is an injection into a statement with write access to the" \
                "graph. Fix the input; this check is not the thing to relax."
            ;;
    esac
}

# validate_parquet_ref <ref> — the STRUCTURAL rules, on top of the charset.
#
# A trailing slash is REJECTED rather than trimmed. `staged/x/` and `staged/x` are the
# same B2 prefix but two different strings, and this gate is a STRING EQUALITY against
# what the load stamped. Canonicalising here and not in the prune's input would make a
# correct prune refuse itself; canonicalising in both is one more thing to keep in
# lockstep. Rejecting at the point of the stamp means only one spelling can ever be
# stamped, so the equality is safe without any normalisation anywhere.
validate_parquet_ref() {
    _ref="$1"
    safe_literal parquet_ref "${_ref}" 'A-Za-z0-9._/-'
    case "${_ref}" in
        /*) die "parquet_ref must NOT start with '/' — it is a bucket-relative prefix." ;;
        */) die "parquet_ref must NOT end with '/'." \
            "The prune gate is a string equality against this exact value, so 'staged/x/'" \
            "and 'staged/x' would be two different runs. Only one spelling may be stamped." ;;
        noorinalabs-pipeline/*)
            die "parquet_ref must NOT include the bucket name." \
                "Pass only the prefix under noorinalabs-pipeline (e.g. staged/narrator-resolve/<ver>)."
            ;;
    esac
    case "${_ref}" in
        ..|../*|*/../*|*/..) die "parquet_ref must not contain a '..' path segment." ;;
    esac
}

# ---------------------------------------------------------------------------
# token_value <line> <key>
#
# Whole-TOKEN extraction: split the line on spaces and match an exact `key=value`
# token, so no key can be hijacked by a longer key ending in its name (da#419).
#
# The value charclass is `[!-~]` — every printable non-space character. That is
# deliberately NOT an enumeration of what a parquet_ref may contain: enumerating a
# charset for a value someone else writes is how a parser silently NARROWS when the
# producer changes (deploy#589). Here the token is whatever sits between two spaces;
# an unexpected character is still captured, still compared, and still refused on
# mismatch. Fail-closed, not fail-blind.
#
# NO PIPE INTO AN EARLY-EXITING CONSUMER. `... | head -n1` would make the producer
# take SIGPIPE, and `pipefail` promotes that 141 to the pipeline's rc EVEN WHEN THE
# FINAL MATCH SUCCEEDED (deploy#591). sed reads its input to the end and the first
# line is taken with a parameter expansion instead.
# ---------------------------------------------------------------------------
token_value() {
    _tv_out="$(tr ' ' '\n' <<<"$1" | sed -n "s/^$2=\\([!-~][!-~]*\\)\$/\\1/p")"
    printf '%s\n' "${_tv_out%%$'\n'*}"
}

# ---------------------------------------------------------------------------
# MODE: stamp — the Cypher that records what this load loaded.
# ---------------------------------------------------------------------------
mode_stamp() {
    PARQUET_REF="${PARQUET_REF:?PARQUET_REF must be set}"
    LOAD_STATUS="${LOAD_STATUS:?LOAD_STATUS must be set}"
    IMAGE="${IMAGE:?IMAGE must be set}"
    LOAD_ARGS="${LOAD_ARGS:?LOAD_ARGS must be set}"

    validate_parquet_ref "${PARQUET_REF}"

    # A whitelist, not a charset: `complete` is the token the prune gate unlocks on, and
    # a typo'd status must be a refusal here rather than an unrecognised value that the
    # gate later reads as "not complete" for the wrong reason.
    case "${LOAD_STATUS}" in
        "${STATUS_IN_PROGRESS}" | "${STATUS_COMPLETE}") : ;;
        *) die "LOAD_STATUS='${LOAD_STATUS}' is not one of: ${STATUS_IN_PROGRESS} ${STATUS_COMPLETE}." ;;
    esac

    # An OCI ref: registry/path:tag or @sha256:<digest>.
    safe_literal image "${IMAGE}" 'A-Za-z0-9._/:@-'
    # The loader subcommand — 'load' or 'load --nodes-only'. A space is legal HERE (it is
    # stored, never projected into the space-delimited provenance line below).
    safe_literal load_args "${LOAD_ARGS}" 'A-Za-z0-9 ._-'

    # `scope` is the MERGE key: one provenance node per graph, upserted per load. Do not
    # MERGE on parquet_ref — that would accumulate one node per ref and the read-back
    # would have to pick a winner, which is precisely the ambiguity this gate exists to
    # remove. The graph was loaded from exactly one ref, and it says which.
    cat <<CYPHER
MERGE (p:${NODE_LABEL} {scope: '${NODE_SCOPE}'})
SET p.parquet_ref = '${PARQUET_REF}',
    p.load_status = '${LOAD_STATUS}',
    p.image = '${IMAGE}',
    p.load_args = '${LOAD_ARGS}',
    p.stamped_at = toString(datetime())
CYPHER
}

# ---------------------------------------------------------------------------
# MODE: read-query — project the stamp as ONE machine-readable line.
#
# The line is SPACE-DELIMITED, so every projected property must be space-free.
# `load_args` ('load --nodes-only') is deliberately NOT projected: it is stored for
# forensics, and projecting it would put a space inside a token and silently corrupt
# the parse of everything after it.
#
# `coalesce(..., '<unset>')` rather than letting a null collapse the whole concatenation
# to null: a node with a missing property must produce a line that FAILS the checks
# below for a nameable reason, not vanish and read as "no stamp at all". Both refuse —
# but only one of them tells the operator what is actually wrong.
# ---------------------------------------------------------------------------
mode_read_query() {
    cat <<CYPHER
MATCH (p:${NODE_LABEL} {scope: '${NODE_SCOPE}'})
RETURN '${LINE_KEYWORD}'
     + ' parquet_ref=' + coalesce(p.parquet_ref, '<unset>')
     + ' load_status=' + coalesce(p.load_status, '<unset>')
     + ' stamped_at=' + coalesce(p.stamped_at, '<unset>')
     + ' image=' + coalesce(p.image, '<unset>') AS provenance
CYPHER
}

# ---------------------------------------------------------------------------
# MODE: verify — the refusal.
# ---------------------------------------------------------------------------
mode_verify() {
    PRUNE_PARQUET_REF="${PRUNE_PARQUET_REF:?PRUNE_PARQUET_REF must be set (the ref being pruned against)}"
    # May legitimately be empty (an unstamped graph). Empty is a REFUSAL, not a pass.
    PROVENANCE_OUTPUT="${PROVENANCE_OUTPUT-}"

    # First line carrying the keyword. `sed -n` exits 0 on no-match (unlike `grep`, which
    # exits 1 and would CRASH this script under `set -e` on the very input it exists to
    # reject — the no-stamp case). Look up the command's contract; do not generalise.
    PROV_LINE="$(sed -n "/${LINE_KEYWORD} /{p;q;}" <<<"${PROVENANCE_OUTPUT}")"

    # `cypher-shell --format plain` renders a string column QUOTED, so the row arrives as
    #   "GRAPH_LOAD_PROVENANCE parquet_ref=… image=…"
    # Strip that TRANSPORT ENVELOPE — one leading and one trailing double quote on the
    # LINE — before tokenising. Doing it on the line, not on the extracted values, keeps
    # this independent of WHICH field happens to sit first or last in the projection: a
    # reorder in read-query must not silently start contaminating a value with a quote.
    # No legal value can contain `"` (safe_literal excludes it at the stamp), and if the
    # transport ever stops quoting, both strips are no-ops.
    PROV_LINE="${PROV_LINE#\"}"
    PROV_LINE="${PROV_LINE%\"}"

    GRAPH_REF="$(token_value "${PROV_LINE}" parquet_ref)"
    if [ -z "${PROV_LINE}" ] || [ -z "${GRAPH_REF}" ]; then
        die "the graph carries NO load-provenance stamp." \
            "Nothing in this graph says which resolve run it was loaded from, so there is no" \
            "way to tell a correct keep-set from a genuine-but-older one — and an older run's" \
            "keep-set passes EVERY other gate here (its record is valid, its md5 matches, its" \
            "ids are a subset of the graph so 'missing' is 0) while the prune deletes every" \
            "narrator loaded since. Run deploy-data-load.yml, which stamps the ref it loads." \
            "  ref you asked to prune against: ${PRUNE_PARQUET_REF}"
    fi

    GRAPH_STATUS="$(token_value "${PROV_LINE}" load_status)"
    GRAPH_STAMPED_AT="$(token_value "${PROV_LINE}" stamped_at)"

    if [ "${GRAPH_STATUS}" != "${STATUS_COMPLETE}" ]; then
        die "the last load of this graph did NOT complete (load_status=${GRAPH_STATUS:-<unreadable>})." \
            "The graph therefore holds an unknown fraction of ${GRAPH_REF}, and NO keep-set" \
            "names the narrators a half-finished load left behind — so a prune deletes exactly" \
            "those, whatever ref you point it at. Re-run the load; it stamps 'complete' only" \
            "after the loader commits." \
            "  graph loaded from : ${GRAPH_REF} (stamped ${GRAPH_STAMPED_AT})" \
            "  ref you asked to prune against: ${PRUNE_PARQUET_REF}"
    fi

    if [ "${GRAPH_REF}" != "${PRUNE_PARQUET_REF}" ]; then
        die "WRONG REF — this graph was not loaded from the ref you are pruning against." \
            "  graph was LOADED from        : ${GRAPH_REF}   (stamped ${GRAPH_STAMPED_AT})" \
            "  you asked to PRUNE against   : ${PRUNE_PARQUET_REF}" \
            "" \
            "Every other gate in this workflow is derived from the keep-set and would go GREEN" \
            "on a genuine but older, smaller run: its resolve record is valid, its md5 matches," \
            "its ids are a SUBSET of what this graph holds so 'missing' is 0, and the" \
            "completeness equality passes because the parquet really does hold what its run" \
            "declared. The prune would then delete every narrator loaded since that run." \
            "" \
            "Either you meant the ref above, or the graph needs re-loading from the ref you" \
            "typed. We do not get to guess which."
    fi

    echo "LOAD_PROVENANCE_OK parquet_ref=${GRAPH_REF} load_status=${GRAPH_STATUS} stamped_at=${GRAPH_STAMPED_AT}"
}

case "${1:-}" in
    stamp) mode_stamp ;;
    read-query) mode_read_query ;;
    verify) mode_verify ;;
    *)
        echo "usage: $0 {stamp|read-query|verify}" >&2
        echo "  stamp       env: PARQUET_REF LOAD_STATUS IMAGE LOAD_ARGS  -> Cypher MERGE" >&2
        echo "  read-query                                                -> Cypher RETURN" >&2
        echo "  verify      env: PRUNE_PARQUET_REF PROVENANCE_OUTPUT      -> rc 0 iff same ref" >&2
        exit 2
        ;;
esac

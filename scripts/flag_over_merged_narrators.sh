#!/usr/bin/env bash
# =============================================================================
# flag_over_merged_narrators.sh — build the Cypher that flags over-merged
# Narrator nodes on the LIVE graph, for graph-flag-over-merged.yml (deploy#603).
#
# WHAT IT IS FOR
#
# da#447 gave every canonical Narrator row an `over_merged: bool` (+
# `over_merge_note: str`): true means the node FUSES multiple distinct narrators,
# so its betweenness/centrality is inflated and it must NOT be read as one
# historical transmitter. deploy-data-load.yml carries BOTH properties into Neo4j
# on every reload (the durable transport). But a reload is a ~7.5h op, so this
# script backs the immediate "flag-NOW" path: annotate the live stg graph.
#
# BOOLEAN ONLY — the live op sets `over_merged = true`, NOT `over_merge_note`.
# The note is free-text prose (it contains `"`, `->`, `%`, per-node distinct), and
# its right home is the TYPED parquet load — the durable reload writes each node's
# own note. Building free text into cypher through a shell whitelist is both
# fragile (the charset would refuse the real note) and wrong (a live op cannot
# carry 8 DISTINCT notes without per-node plumbing, and a single shared note would
# diverge from the reload). The consumer disclosure is the badge + i18n explainer,
# which degrades gracefully when the note is null (ig#1183), so `over_merged=true`
# alone is a complete, correct interim disclosure; the notes appear on reload.
# Ids (`nar:`+uuid) ARE charset-safe, so the boolean SET stays clean and injection-
# free (see INJECTION MODEL). This is the deploy#603 revision to boolean-only.
#
# WHY A SCRIPT AND NOT TEN LINES OF THE WORKFLOW'S ssh BLOCK
#
# Same reason graph_load_provenance.sh / verify_prune_provenance.sh are scripts
# and not inline `script:` bodies: a guard that lives in an ssh `with: script:`
# string can only ever be pinned by grepping the workflow TEXT — it is NOT
# linted by shellcheck (deploy#555) and cannot be unit-tested. The ids flagged
# here are interpolated into a Cypher literal that runs with WRITE access to the
# graph, so the injection guard MUST be executable and tested. It lives here,
# EXECUTED by scripts/tests/test_flag_over_merged_narrators.py and shellcheck-
# linted by compose-validate.yml's shellcheck job.
#
# INJECTION MODEL (mirrors graph_load_provenance.sh safe_literal)
#
# Each id is interpolated into a SINGLE-QUOTED Cypher string literal, so an id
# containing `'` or `\` would be a Cypher injection into a write statement. Ids are
# WHITELISTED, not escaped: a canonical id is `nar:<uuid>` (charset A-Za-z0-9:_-),
# so no quote, backslash, newline or brace can reach the statement at all. Refuse
# rather than sanitise — a rejected input is a typo the operator fixes; a sanitised
# one is a guess.
#
# The QUERY is parameterized on `$ids` (cypher-shell `:param`), and the `:param`
# value literal is what this script builds under the whitelist. So the statement
# text is fixed and parameter-driven, and the only operator-supplied bytes that
# reach the session are ids that passed the charset gate.
#
# ---------------------------------------------------------------------------------
# MODES
#
#   set-query        out: the parameterized SET statement (references $ids).
#   precount-query   out: count of Narrator nodes whose id is in $ids.
#   postcount-query  out: count of those now carrying over_merged=true.
#   param-ids        env: IDS_FILE   out: `:param ids => ['nar:..', ..]`
#   id-count         env: IDS_FILE   out: the integer count of configured ids.
#   stdin <phase>    env: IDS_FILE   out: the full cypher-shell stdin stream for
#                    <phase> = precount | flag | postcount (the `:param` line
#                    followed by the statement).
#
# All statements are SINGLE statements terminated by `;` so a non-interactive
# cypher-shell (which auto-commits each stdin statement — cypher-shell 5.x removed
# `:auto`, deploy#540) runs them in the implicit transaction a plain SET needs.
# =============================================================================
set -euo pipefail

# Default to the committed id set; the test overrides IDS_FILE to a fixture.
IDS_FILE="${IDS_FILE:-"$(cd "$(dirname "$0")" && pwd)/over_merged_narrator_ids.txt"}"

die() {
    echo "ERROR: [flag-over-merged] $1" >&2
    shift
    for _l in "$@"; do echo "  ${_l}" >&2; done
    exit 1
}

# ---------------------------------------------------------------------------
# safe_literal <name> <value> <allowed-charclass>
# Whitelist every byte that will land inside a single-quoted Cypher literal.
# ---------------------------------------------------------------------------
safe_literal() {
    _name="$1"
    _val="$2"
    _allowed="$3"
    [ -n "${_val}" ] || die "${_name} is empty — refusing to build a Cypher literal from nothing."
    # shellcheck disable=SC2254  # ${_allowed} is an intentional bracket-expression body.
    case "${_val}" in
        *[!${_allowed}]*)
            die "${_name}='${_val}' contains a character outside [${_allowed}]." \
                "It is interpolated into a single-quoted Cypher literal that runs with write" \
                "access to the graph, so a quote or backslash here is an injection. Fix the" \
                "input; this check is not the thing to relax."
            ;;
    esac
}

# read_ids — echo the configured canonical ids, one per line, each whitelisted.
# Blank lines and #-comments are ignored; leading/trailing whitespace trimmed.
# A canonical id is `nar:<uuid>` — charset A-Za-z0-9:_- (no quote can hide here).
read_ids() {
    [ -f "${IDS_FILE}" ] || die "id file not found: ${IDS_FILE}"
    while IFS= read -r _line || [ -n "${_line}" ]; do
        # trim leading whitespace
        _line="${_line#"${_line%%[![:space:]]*}"}"
        # trim trailing whitespace
        _line="${_line%"${_line##*[![:space:]]}"}"
        case "${_line}" in
            '' | \#*) continue ;;
        esac
        safe_literal id "${_line}" 'A-Za-z0-9:_-'
        printf '%s\n' "${_line}"
    done <"${IDS_FILE}"
}

mode_id_count() {
    # Capture FIRST, not `read_ids | grep`: a bad id makes read_ids die, and this
    # plain assignment propagates that rc so `set -e` aborts HERE with the whitelist
    # diagnostic — a pipe would mask it and report a count instead.
    _ids_out="$(read_ids)"
    if [ -z "${_ids_out}" ]; then
        printf '0\n'
        return 0
    fi
    printf '%s\n' "${_ids_out}" | grep -c '^'
}

mode_param_ids() {
    # Capture FIRST so an id failing the whitelist aborts here (set -e) with the
    # injection diagnostic as the sole error — running read_ids inside the loop's
    # `<<<"$(read_ids)"` subshell would swallow that exit and mis-report "no ids".
    _ids_out="$(read_ids)"
    [ -n "${_ids_out}" ] || die "no canonical ids configured in ${IDS_FILE}." \
        "Wire in the producer's resolved over-merged ids (da#447) before dispatching;" \
        "the set is empty, so there is nothing to flag (fail-safe refusal)."
    _lit=""
    # Herestring keeps the loop in THIS shell so _lit accumulates; every id in
    # _ids_out is already whitelisted.
    while IFS= read -r _id; do
        [ -n "${_id}" ] || continue
        if [ -n "${_lit}" ]; then _lit="${_lit}, "; fi
        _lit="${_lit}'${_id}'"
    done <<<"${_ids_out}"
    printf ':param ids => [%s]\n' "${_lit}"
}

# The statement text is FIXED and parameter-driven — no operator bytes here.
# SC2016: `$ids` is a Cypher parameter and MUST stay literal (single-quoted so the
# shell never expands it); cypher-shell binds it from the `:param` line.
mode_set_query() {
    # shellcheck disable=SC2016
    printf '%s\n' \
        'MATCH (n:Narrator) WHERE n.id IN $ids SET n.over_merged = true RETURN count(n) AS flagged;'
}
mode_precount_query() {
    # shellcheck disable=SC2016
    printf '%s\n' \
        'MATCH (n:Narrator) WHERE n.id IN $ids RETURN count(n) AS matched;'
}
mode_postcount_query() {
    # shellcheck disable=SC2016
    printf '%s\n' \
        'MATCH (n:Narrator) WHERE n.id IN $ids AND n.over_merged = true RETURN count(n) AS flagged;'
}

mode_stdin() {
    case "${1:-}" in
        precount)
            mode_param_ids
            mode_precount_query
            ;;
        flag)
            mode_param_ids
            mode_set_query
            ;;
        postcount)
            mode_param_ids
            mode_postcount_query
            ;;
        *) die "stdin: unknown phase '${1:-}' (want: precount | flag | postcount)" ;;
    esac
}

case "${1:-}" in
    set-query) mode_set_query ;;
    precount-query) mode_precount_query ;;
    postcount-query) mode_postcount_query ;;
    param-ids) mode_param_ids ;;
    id-count) mode_id_count ;;
    stdin) mode_stdin "${2:-}" ;;
    *)
        echo "usage: $0 {set-query|precount-query|postcount-query|param-ids|id-count|stdin <phase>}" >&2
        echo "  param-ids/id-count/stdin   env: IDS_FILE" >&2
        exit 2
        ;;
esac

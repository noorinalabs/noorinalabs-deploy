#!/usr/bin/env bash
# =============================================================================
# user-postgres content queries — the ONE place these five SQL strings live.
# Sourced by BOTH scripts/backup.sh (writes the content manifest, querying a
# scratch restore of the dump it just produced) and scripts/restore_verify.sh
# (reads the manifest back, and recomputes the same queries against the
# restored-verify stack). (deploy#687)
#
# WHY A SHARED FILE AND NOT TWO COPIES
# -------------------------------------
# The manifest is only trustworthy if the value written at backup time and the
# value recomputed at verify time are guaranteed to come from byte-identical SQL.
# Two independent copies of "the same" query text can drift — a whitespace edit,
# a column reorder, a forgotten `SET timezone` — and the two sides would then
# disagree for a reason that has nothing to do with the data. Sourcing one file
# from both scripts makes "the SQL cannot drift" a structural property rather
# than a policy to remember, same discipline as scripts/compose_project.sh being
# shared between backup.sh and restore.sh.
#
# WHY EACH QUERY PINS `SET timezone='UTC';` ITSELF
# ---------------------------------------------------
# `<row>::text` renders `timestamptz` columns through the session's `timezone`
# GUC. A divergent TZ/PGTZ between the scratch container (backup.sh) and the
# restore-verify stack (restore_verify.sh) would stringify the same physical
# instant differently and manufacture a false MISMATCH. Baking the SET into the
# constant itself — rather than asking every call site to remember to prefix it —
# means a caller cannot forget the pin.
#
# WHY `COALESCE(md5(string_agg(t,'|' ORDER BY t)), 'EMPTY')`
# -------------------------------------------------------------
# `string_agg` over zero rows is SQL NULL, which renders as an EMPTY STRING over
# `psql -tAc` — and restore_verify.sh's `_run_capture` treats an empty reading as
# an INSTRUMENT FAILURE, not a value (by design, correctly: an empty reading and
# a query that could not run look identical, and only one of them is a
# measurement). `audit_log` is legitimately empty on stg today, so both sides
# must produce the SAME non-empty, comparable token for a zero-row table rather
# than special-casing it through the empty-reading guard. `md5=EMPTY` is that
# token — a literal sentinel, not a blank field.
#
# WHY COUNT AND MD5 IN ONE QUERY, JOINED WITH '|'
# ---------------------------------------------------
# One round trip per table, one string to parse (`<count>|<md5-or-EMPTY>`),
# split on the first `|`. The string_agg's OWN separator is also `|`, but that
# is never visible in the output — string_agg's result here is always a 32-hex
# md5 digest (or the literal `EMPTY`), never the joined-and-visible row text, so
# the two `|` uses can never collide.
#
# CONTRACT: every constant here is a single SELECT statement (plus its leading
# SET), safe to pass whole to `psql -tAc "$QUERY"` against either the isolated
# scratch container (backup.sh) or a real user-postgres service (restore_verify.sh).
# =============================================================================

readonly UPG_CONTENT_USERS="SET timezone='UTC'; SELECT count(*) || '|' || COALESCE(md5(string_agg(t,'|' ORDER BY t)), 'EMPTY') FROM (SELECT u::text AS t FROM users u) s;"
readonly UPG_CONTENT_OAUTH_ACCOUNTS="SET timezone='UTC'; SELECT count(*) || '|' || COALESCE(md5(string_agg(t,'|' ORDER BY t)), 'EMPTY') FROM (SELECT o::text AS t FROM oauth_accounts o) s;"
readonly UPG_CONTENT_SESSIONS="SET timezone='UTC'; SELECT count(*) || '|' || COALESCE(md5(string_agg(t,'|' ORDER BY t)), 'EMPTY') FROM (SELECT sess::text AS t FROM sessions sess) s;"
readonly UPG_CONTENT_AUDIT_LOG="SET timezone='UTC'; SELECT count(*) || '|' || COALESCE(md5(string_agg(t,'|' ORDER BY t)), 'EMPTY') FROM (SELECT a::text AS t FROM audit_log a) s;"
readonly UPG_CONTENT_ALEMBIC_VERSION="SET timezone='UTC'; SELECT count(*) || '|' || COALESCE(md5(string_agg(t,'|' ORDER BY t)), 'EMPTY') FROM (SELECT v::text AS t FROM alembic_version v) s;"

# The fixed table set every content manifest declares, in the order the
# manifest lines are written. Read out of THIS array by both producer
# (backup.sh, to know which queries to run) and consumer (restore_verify.sh,
# to know which lines to expect and what the restored public schema must
# contain) — one list, not two independently-typed ones.
# shellcheck disable=SC2034  # consumed by the scripts that source this file, not by this one
readonly UPG_CONTENT_TABLES=(users oauth_accounts sessions audit_log alembic_version)

# upg_content_query_for <table> — echoes the shared constant for <table> on stdout.
# The one place that maps a table name to its query variable, so a caller never
# has to hand-write a `case` of its own that could fall out of step with
# UPG_CONTENT_TABLES above.
upg_content_query_for() {
    case "$1" in
        users) printf '%s' "$UPG_CONTENT_USERS" ;;
        oauth_accounts) printf '%s' "$UPG_CONTENT_OAUTH_ACCOUNTS" ;;
        sessions) printf '%s' "$UPG_CONTENT_SESSIONS" ;;
        audit_log) printf '%s' "$UPG_CONTENT_AUDIT_LOG" ;;
        alembic_version) printf '%s' "$UPG_CONTENT_ALEMBIC_VERSION" ;;
        *) return 1 ;;
    esac
}

# --- Calibration-only: MINUS1 legs -------------------------------------------
# Same shape as restore_verify.sh's PG_HADITH_MD5_MINUS1 (`... ORDER BY t OFFSET
# 1`). calibrate() requires each of these to DIFFER from its full UPG_CONTENT_*
# md5 on the reference stack before any MATCH involving that table is believed —
# a comparator that cannot detect one missing row makes every "MATCH" it reports
# meaningless. `alembic_version` deliberately has no MINUS1 leg: it is a
# single-row schema-version marker, not a mutable content table, so "minus one
# row" would degenerate to "zero rows" rather than exercise the comparator the
# way the other four do.
# shellcheck disable=SC2034  # consumed by restore_verify.sh's calibrate(), which sources this file
readonly UPG_USERS_MD5_MINUS1="SET timezone='UTC'; SELECT COALESCE(md5(string_agg(t,'|' ORDER BY t)), 'EMPTY') FROM (SELECT u::text AS t FROM users u ORDER BY t OFFSET 1) s;"
# shellcheck disable=SC2034
readonly UPG_OAUTH_ACCOUNTS_MD5_MINUS1="SET timezone='UTC'; SELECT COALESCE(md5(string_agg(t,'|' ORDER BY t)), 'EMPTY') FROM (SELECT o::text AS t FROM oauth_accounts o ORDER BY t OFFSET 1) s;"
# shellcheck disable=SC2034
readonly UPG_SESSIONS_MD5_MINUS1="SET timezone='UTC'; SELECT COALESCE(md5(string_agg(t,'|' ORDER BY t)), 'EMPTY') FROM (SELECT sess::text AS t FROM sessions sess ORDER BY t OFFSET 1) s;"
# shellcheck disable=SC2034
readonly UPG_AUDIT_LOG_MD5_MINUS1="SET timezone='UTC'; SELECT COALESCE(md5(string_agg(t,'|' ORDER BY t)), 'EMPTY') FROM (SELECT a::text AS t FROM audit_log a ORDER BY t OFFSET 1) s;"

#!/usr/bin/env bash
# =============================================================================
# noorinalabs-isnad-graph restore script
# Downloads backups from Backblaze B2, verifies checksums, and restores
# PostgreSQL and Neo4j databases.
#
# Required environment variables:
#   B2_KEY_ID       — Backblaze B2 application key ID
#   B2_APP_KEY      — Backblaze B2 application key
#   B2_BUCKET       — Backblaze B2 bucket name
#
# Optional environment variables:
#   POSTGRES_USER   — PostgreSQL user (default: isnad)
#   POSTGRES_DB     — PostgreSQL database (default: isnad_graph)
#   COMPOSE_FILE    — Docker Compose file (default: compose/docker-compose.prod.yml,
#                     resolved relative to /opt/noorinalabs-deploy/ — MUST match
#                     backup.sh so a restore rolls back the same stack backup captured;
#                     see deploy#498)
#   COMPOSE_PROJECT — Docker Compose PROJECT the stack runs in (default: noorinalabs).
#                     `-f` names the FILE, not the PROJECT: with neither this nor
#                     COMPOSE_PROJECT_NAME set, Compose infers the project from the compose
#                     file's DIRECTORY (`compose`) and every call lands on a project with no
#                     containers in it. A RESTORE aimed at the wrong project is worse than a
#                     backup that reads from one — it writes. See scripts/compose_project.sh
#                     (deploy#617).
#   RESTORE_DIR     — Local restore staging directory (default: /tmp/isnad-restore)
#   RESTORE_LOCAL_DIR — Restore from a backup directory already on disk instead of
#                     downloading from B2. Checksum verification and every restore
#                     path are unchanged; only the download is skipped. Used by
#                     scripts/restore_rehearsal.sh (which therefore needs no B2
#                     credentials) and by DR from a locally-held dump.
#
# Exit status:
#   0  every dump present in the backup restored cleanly
#   1  anything else — unverifiable artifact, empty backup, or a failed restore
#      Do not treat a zero exit as "the data is back" without also asserting on
#      restored content; do treat a non-zero exit as authoritative.
#
# Usage:
#   ./scripts/restore.sh latest                    # Restore most recent backup
#   ./scripts/restore.sh daily/2026-03-25          # Restore specific date
#   ./scripts/restore.sh --force daily/2026-03-25  # Skip confirmation prompt
#   ./scripts/restore.sh --list                    # List available backups
#   RESTORE_LOCAL_DIR=/path/to/backup ./scripts/restore.sh --force local
# =============================================================================
set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
POSTGRES_USER="${POSTGRES_USER:-isnad}"
POSTGRES_DB="${POSTGRES_DB:-isnad_graph}"
# Defaults match the live stg/prod values. See backup.sh for the same pair.
USER_POSTGRES_USER="${USER_POSTGRES_USER:-user_service}"
USER_POSTGRES_DB="${USER_POSTGRES_DB:-user_service}"
COMPOSE_FILE="${COMPOSE_FILE:-compose/docker-compose.prod.yml}"
RESTORE_DIR="${RESTORE_DIR:-/tmp/isnad-restore}"
RESTORE_LOCAL_DIR="${RESTORE_LOCAL_DIR:-}"

RCLONE_REMOTE="isnad"

# B2 credentials are only needed when the artifact is fetched from B2. A local-dir
# restore must not require them — demanding credentials to read a file already on
# disk would push the rehearsal back onto the B2 dependency it exists to avoid.
if [[ -z "$RESTORE_LOCAL_DIR" ]]; then
    : "${B2_KEY_ID:?B2_KEY_ID must be set (or set RESTORE_LOCAL_DIR to restore from disk)}"
    : "${B2_APP_KEY:?B2_APP_KEY must be set (or set RESTORE_LOCAL_DIR to restore from disk)}"
    : "${B2_BUCKET:?B2_BUCKET must be set (or set RESTORE_LOCAL_DIR to restore from disk)}"

    export RCLONE_CONFIG_ISNAD_TYPE="b2"
    export RCLONE_CONFIG_ISNAD_ACCOUNT="${B2_KEY_ID}"
    export RCLONE_CONFIG_ISNAD_KEY="${B2_APP_KEY}"
fi

FORCE=false
# A DR escape hatch, not a convenience. Default-deny: an incomplete backup is REFUSED
# unless the operator explicitly says they know and accept it. Safety direction over UX
# friction (PR#494) — during an incident nobody reads the header comment; they read the
# exit code and the last line, and the last line used to say the restore was complete.
ALLOW_PARTIAL=false

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
log() {
    local level="$1"
    shift
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) [${level}] $*"
}

# ---------------------------------------------------------------------------
# Compose project scoping (deploy#617)
# ---------------------------------------------------------------------------
# Sourced AFTER log(): the library's guards report through it. Provides COMPOSE_PROJECT,
# dc(), assert_stack_present() and neo4j_start(). Every compose call below goes through
# dc(). This script had the SAME defect backup.sh did — no `-p` anywhere — and it is the
# more dangerous half: a backup addressing a phantom project READS nothing, a restore
# addressing one WRITES. Once a stray neo4j exists in project `compose` (backup.sh's `up`
# created exactly that on stg), `ps -aq neo4j` RESOLVES it, restore.sh's "refusing to
# guess" volume guard is satisfied, and `neo4j-admin database load --overwrite-destination`
# loads the backup into the STRAY volume — reporting "Neo4j restore complete" while the
# real graph is untouched and the operator believes they have recovered.
COMPOSE_PROJECT_LIB="$(dirname "${BASH_SOURCE[0]}")/compose_project.sh"
if [[ ! -f "$COMPOSE_PROJECT_LIB" ]]; then
    log "ERROR" "Missing ${COMPOSE_PROJECT_LIB} — cannot resolve the compose project to restore into"
    exit 1
fi
# shellcheck source=scripts/compose_project.sh
source "$COMPOSE_PROJECT_LIB"

# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------
cleanup() {
    if [[ -d "$RESTORE_DIR" ]]; then
        rm -rf "$RESTORE_DIR"
        log "INFO" "Cleaned up restore staging directory"
    fi
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Preflight checks
# ---------------------------------------------------------------------------
REQUIRED_CMDS=(docker zstd sha256sum)
# rclone is only reachable on the B2 path; a local-dir restore must not demand it.
if [[ -z "$RESTORE_LOCAL_DIR" ]]; then
    REQUIRED_CMDS+=(rclone)
fi
for cmd in "${REQUIRED_CMDS[@]}"; do
    if ! command -v "$cmd" &>/dev/null; then
        log "ERROR" "Required command not found: ${cmd}"
        exit 1
    fi
done

# ---------------------------------------------------------------------------
# Functions
# ---------------------------------------------------------------------------

# rclone lsf a category, separating "empty" from "I could not look".
#
# `|| echo "  (none)"` — what this used to be — is the SAME LIE this whole change exists to
# stop, by a third route: with a bad key or an unreachable bucket, `restore.sh --list` printed
#
#     === Daily ===
#       (none)
#
# and an operator reads that as "I have no backups." The instrument failed; the bucket may be
# full. A listing that cannot distinguish an empty bucket from a failed listing is not a
# listing, it is a guess (deploy#584 review, Nino Kavtaradze — the pattern, not the incident).
#
# ---------------------------------------------------------------------------
# THE TWO STREAMS EXIST BECAUSE ONE IS DATA AND ONE IS COMMENTARY
# ---------------------------------------------------------------------------
# The FIRST fix for the above used `2>&1`, and that is a NEW bug, not a fix:
#
#   2>/dev/null, || true    DISCARDS the diagnostic          <- the original bug
#   2>&1 into a variable    PROMOTES the diagnostic to DATA  <- the over-correction
#   2>"$err", read on rc    CAPTURES it                      <- correct
#
# `restore.sh` configures rclone through `RCLONE_CONFIG_ISNAD_*` and ships no `rclone.conf`,
# so rclone writes this to stderr on every SUCCESSFUL call:
#
#   NOTICE: Config file ".../rclone.conf" not found - using defaults
#
# `2>&1` folded that line into `$dirs`, and the code then read it as a backup DIRECTORY NAME.
# Against a healthy bucket holding one good backup, `resolve_latest` reported two INCOMPLETE
# backups that do not exist, and `--list` printed a log line to the operator as a backup name.
#
# Note the direction of the regression: the ORIGINAL bug fired only on FAILURE. This one fires
# on SUCCESS — because a tool writing to stderr when nothing is wrong is completely ordinary
# (warnings, deprecations, progress, config notices). `2>&1` corrupts the NORMAL path, which
# is the path nobody tests as hard. Found by both reviewers independently.
list_category() {
    local category="$1" out rc=0 err
    err="$(mktemp)"
    out="$(rclone lsf "${RCLONE_REMOTE}:${B2_BUCKET}/${category}/" --dirs-only 2>"$err")" || rc=$?
    if [[ "$rc" -ne 0 ]]; then
        log "ERROR" "Could not LIST ${category} (rclone rc=${rc}):"
        sed 's/^/    /' "$err" >&2
        log "ERROR" "  This is an INSTRUMENT failure — NOT a claim that you have no backups."
        rm -f "$err"
        return 1
    fi
    rm -f "$err"
    if [[ -z "$out" ]]; then
        echo "  (none)"
    else
        printf '%s\n' "$out"
    fi
}

# The DISTINCT run timestamps present among a directory's dumps.
#
# `backup.sh` names every dump `isnad-<store>-<TIMESTAMP>.dump[.zst]` with a single
# `%Y%m%d-%H%M%S` run id, so the filenames themselves carry which run each dump belongs to.
# A B2 day-directory accumulates runs (`rclone copy` adds and never deletes), so "how many
# runs are in here?" is a question the artifact can answer without a manifest.
#
# ---------------------------------------------------------------------------
# ANCHOR ON THE RUN ID. DO NOT PARSE THE STORE NAME. (deploy#589)
# ---------------------------------------------------------------------------
# This matched the store segment as `[a-z0-9]\{1,\}` — which CANNOT CONTAIN A HYPHEN. Rename
# `userpg` to `user-pg` in backup.sh and every dump of that store becomes INVISIBLE here:
#
#   isnad-user-pg-20260711-030100.dump   ->   (no match)
#
# A producer-side rename does not BREAK this parser — it QUIETLY NARROWS it. And what it
# stops seeing is exactly what the guard downstream needs to count: with a run rendered
# invisible, `count_runs` falls to 1, the refuse-on-ambiguity gate stops refusing, and THE
# TORN RESTORE COMES BACK — with nothing red anywhere. A paraphrase in the PRODUCT is worse
# than one in a test: the test paraphrases too, so it stays green.
#
# So do not parse the part that can change. The RUN ID is what we want and its format is
# strict and self-delimiting (`%Y%m%d-%H%M%S`), so anchor on THAT and let the store segment be
# anything at all. Sidecars still fall out for free: `.sha256` does not end in `.dump[.zst]`.
list_runs() {
    find "$1" \( -name 'isnad-*.dump' -o -name 'isnad-*.dump.zst' \) -type f -printf '%f\n' 2>/dev/null \
        | sed -n 's/^isnad-.*-\([0-9]\{8\}-[0-9]\{6\}\)\.dump\(\.zst\)\{0,1\}$/\1/p' \
        | sort -u
}

count_runs() {
    local n
    n="$(list_runs "$1" | grep -c . || true)"
    printf '%s\n' "${n:-0}"
}

# ---------------------------------------------------------------------------
# THE MANIFEST IS PER-RUN, SO THE SELECTION IS PER-RUN. (deploy#587)
# ---------------------------------------------------------------------------
# backup.sh writes `_backup_manifest-<TIMESTAMP>.txt` — one per RUN, never overwritten. It
# used to write a fixed `_backup_manifest.txt` into a DAY-directory that holds several runs,
# so the last run to upload overwrote the attestation of every run before it. A good run
# followed by a partial one erased its own attestation, and both consumers believed the
# survivor.
#
# The run ids of the manifests in a listing (one filename per line on stdin), NEWEST FIRST.
# `%Y%m%d-%H%M%S` sorts chronologically as text, so `sort -r` IS newest-first.
#
# ANCHOR ON THE RUN ID (deploy#589). `_backup_manifest-` is OUR OWN literal prefix and the run
# id's shape is strict and self-delimiting, so both halves of this pattern are things we
# control. Nothing here paraphrases a token another component is free to rename — which is the
# mistake `list_runs` made about the STORE segment, where a producer-side rename would not
# BREAK the parser but QUIETLY NARROW it.
#
# `sed -n` exits 0 when it matches nothing, so this pipeline does NOT trip `pipefail` on a
# directory with no manifests — but every CALLER still assigns it with an explicit `|| VAR=""`,
# because "legitimately matched nothing" is the ordinary case here and it must never be a crash.
manifest_runs() {
    sed -n 's/^_backup_manifest-\([0-9]\{8\}-[0-9]\{6\}\)\.txt$/\1/p' | sort -ru
}

# Does <manifest-text> ATTEST `complete=true`?
#
# WHOLE-TOKEN match on the FIRST `BACKUP_MANIFEST ` line. Both halves are load-bearing and both
# were bugs (deploy#584):
#
#   * `grep '^BACKUP_MANIFEST .*[[:space:]]complete=true'` COULD NEVER MATCH — `complete=` is
#     the FIRST token after `BACKUP_MANIFEST `, so the anchor's literal space consumes the only
#     space there is and `.*[[:space:]]` then demands a second one that never exists.
#   * FIRST LINE ONLY — unioning tokens across every line lets a corrupt artifact carrying BOTH a
#     `complete=false` and a `complete=true` line match. It fails OPEN, and a line-1 read is free.
#
# ---------------------------------------------------------------------------
# NO EARLY-EXIT PIPELINE. `pipefail` TURNS SIGPIPE INTO "DOES NOT ATTEST". (deploy#591 review)
# ---------------------------------------------------------------------------
# This was:
#
#   grep '^BACKUP_MANIFEST ' | head -n1 | tr ' ' '\n' | grep -qx 'complete=true'
#
# and under `set -euo pipefail` it can report a COMPLETE, INTACT, ATTESTING run as NOT
# ATTESTING. When the producer of a pipe stage outruns a consumer that exits early, the
# producer takes SIGPIPE and dies 141 — and **`pipefail` promotes that 141 to the rc of the
# whole pipeline even though the final `grep -qx` SUCCEEDED.** The scanner then prints
# `no_complete_backup` over a bucket holding a complete backup: THE #587 CONFIDENT LIE, by a
# fourth route, in the parser this very change ships. Found by Nurul Hakim.
#
# There were TWO early-exit consumers, not one, and fixing only the first leaves the shape:
#
#   measured, `set -euo pipefail`, first line attests complete=true:
#     grep | head -n1 | tr | grep -qx      100k lines -> rc=141   huge first line -> rc=141
#     grep -m1 | tr | grep -qx             100k lines -> rc=141   huge first line -> rc=141
#                                          ^ `grep -qx` exits on the match and SIGPIPEs `tr`.
#
# So the fix is not to swap `head` for `-m1`; it is to have NO PIPELINE. A herestring is a file
# descriptor, not a pipe: `grep -m1` may stop reading it early and nothing dies. The token test
# is then plain bash. Latent today (it needs a manifest ~700x what the producer writes) and it
# fails closed — but a guard with a known path that silently disarms it is not a correct guard,
# it is an incomplete one, and here the shape can simply be DELETED rather than guarded.
manifest_attests_complete() {
    local first="" tok
    local IFS=$' \t\n'
    first="$(grep -m1 '^BACKUP_MANIFEST ' <<< "$1")" || return 1
    # Deliberate word-splitting: the manifest's fields are space-separated, and an exact token
    # match cannot be defeated by a key that ends in another key's name (`xcomplete=true`).
    # shellcheck disable=SC2086
    for tok in $first; do
        if [[ "$tok" == "complete=true" ]]; then
            return 0
        fi
    done
    return 1
}

# Did every store REQUIRED for run <ts> actually ARRIVE in <listing>?
#
# THE ATTESTATION SAYS WHAT WAS DUMPED. THE LISTING SAYS WHAT ARRIVED. You need both, and they
# are different questions: backup.sh writes the manifest locally and then `rclone copy`s the
# directory, so a copy interrupted after the manifest object lands leaves `complete=true`
# sitting above a half-finished upload (deploy#584).
#
# THE REQUIRED SET IS THIS SCRIPT'S OWN — deliberately NOT read from the manifest's `stores=`
# field, for the same reason REQUIRED_STORES below is not. A set taken from the artifact lets a
# partial backup declare itself complete-for-what-it-happens-to-hold: perfect circularity.
#
# <listing> is a space-delimited set of basenames, padded with a space at BOTH ends, so that a
# `*" name "*` test cannot match a substring of a longer name.
run_dumps_arrived() {
    local ts="$1" have="$2"
    [[ "$have" == *" isnad-pg-${ts}.dump "* ]]     || return 1
    [[ "$have" == *" isnad-userpg-${ts}.dump "* ]] || return 1
    [[ "$have" == *" isnad-neo4j-${ts}.dump.zst "* || "$have" == *" isnad-neo4j-${ts}.dump "* ]] || return 1
    return 0
}

# ---------------------------------------------------------------------------
# ...AND WHY THE OTHER EARLY-EXIT PIPELINES IN THIS FILE ARE LEFT ALONE. (deploy#588)
# ---------------------------------------------------------------------------
# (This block belongs beside `manifest_attests_complete` and deliberately does NOT sit there:
# test_restore_sh_completeness_predicate_matches_a_real_manifest slices the shipped text from
# `manifest_attests_complete() {` to `# Did every store`, and asserts the banned early-exit
# consumers appear nowhere in it. QUOTING them in a comment inside that window reds the suite.
# The guard is textual and cannot tell code from prose — that is a fair price for naming the
# shape that must never come back, so the prose moved instead of the guard being weakened.)
#
# The rule above is NOT "no `| head -1` anywhere". This file still runs, deliberately, the
# early-exit pipelines annotated at their call sites below — a `docker compose ps` wait-loop,
# a one-service container-id lookup, and the per-run dump `find`s.
#
# They are SAFE, and the reason is the PRODUCER, not the consumer — so read this before you
# copy the shape somewhere the producer is not bounded.
#
# SIGPIPE requires a producer STILL WRITING after its consumer has quit. A producer whose
# entire output fits in the 64 KiB pipe buffer has already written everything and EXITED
# before the consumer even reads: there is no writer left to signal, and `pipefail` has no
# 141 to promote. Measured, under `set -euo pipefail`, consumer exiting on the FIRST line:
#
#   printf 'a\nb\nc\n' | <early-exit consumer>   -> rc=0     <- bounded: no SIGPIPE
#   seq 1 5000000      | <early-exit consumer>   -> rc=141   <- unbounded: SIGPIPE, promoted
#
# Every producer here is ENVIRONMENT-BOUNDED — a container list, one compose project's service
# states, a directory listing filtered to a single run's dumps. A few hundred bytes, bounded by
# the shape of the deployment and not by anything an operator, an attacker, or a corruption can
# inflate. They cannot reach the 64 KiB threshold.
#
# The MANIFEST was the one producer that could: it is ARTIFACT CONTENT, pulled from a bucket,
# and an operator or a corrupted upload can make it arbitrarily large. That is the whole
# difference, and it is why the manifest parser — and only the manifest parser — had to lose
# its pipeline (deploy#591).
#
# So the test is not "is there a pipe?" but "CAN ANYTHING MAKE THIS PRODUCER BIG?". If the
# answer is yes, or you cannot say, do not use an early-exit consumer: use a herestring.

list_backups() {
    log "INFO" "Available backups in ${B2_BUCKET}:"
    local failed=0
    echo ""
    echo "=== Daily ==="
    list_category daily || failed=1
    echo ""
    echo "=== Weekly ==="
    list_category weekly || failed=1
    # Exit non-zero on an instrument failure. `--list` that cannot see the bucket must not
    # exit 0 with a confident-looking empty report.
    return "$failed"
}

# Does the backup directory at <category>/<date> CONTAIN A RESTORABLE RUN?
#
# Not "does this DIRECTORY attest complete" — a directory is a DAY, and completeness is a
# property of a RUN (deploy#587). This enumerates the per-run manifests backup.sh writes
# (`_backup_manifest-<TS>.txt`) and looks for the newest run that BOTH attests `complete=true`
# AND whose dumps all arrived. A day with no manifest at all is NOT complete: every pre-#559
# backup predates user-postgres coverage entirely, so treating "no manifest" as "probably fine"
# would hand `latest` the exact artifact class this change exists to stop.
#
# THREE outcomes, not two:
#   0 = a restorable run exists (its run id is PRINTED)
#   1 = looked, and there is none (no manifest, none attests, or none arrived intact)
#   2 = COULD NOT LOOK  <- not a value of the predicate. A separate answer.
#
# This read was `rclone cat … 2>/dev/null || true`, with the rc DISCARDED, so a transient
# 401 / throttle / network blip collapsed into "does not attest" — and `resolve_latest` then
# told an operator mid-incident "No COMPLETE backup found in B2 bucket" over a bucket full of
# good ones. That is the SAME user-visible lie the unmatched regex produced, reached by a
# different route; fixing the regex and leaving this would have been half a fix to the
# identical symptom (deploy#584 review, Nino Kavtaradze).
#
# rc separates them, measured on both backends — which DISAGREE:
#   cat existing    -> rc=0 (B2)  rc=0 (local)
#   cat nonexistent -> rc=0 EMPTY (B2)  rc=3 (local)   <- absent. Not an error.
#   cat, bad key    -> rc=1 (B2)  rc=1 (local)         <- CANNOT EVALUATE
# On success it PRINTS THE CHOSEN RUN ID on stdout — so every `log` below goes to stderr.
# `log()` writes to STDOUT (see above), and this function's stdout is now captured by its
# caller; an unredirected diagnostic would be read back as a run id.
backup_is_complete() {
    local path="$1"
    local listing lrc=0 lerr

    # --- WHAT ARRIVED. The listing is the first of the two instruments. -----------
    #
    # `|| lrc=$?` and NOT a bare assignment: under `set -euo pipefail` an assignment whose
    # command substitution fails IS a failing simple command, and errexit fires AT THE
    # ASSIGNMENT — the rc check below would be dead code on every failing path (deploy#563).
    #
    # rc=3 is `not found` on rclone's LOCAL backend; on B2 a nonexistent prefix is rc=0 with
    # EMPTY output. Neither is an instrument failure — the backends simply disagree, and this
    # is the same disagreement measured in verify_b2_backup_artifact.sh. Anything else means
    # WE COULD NOT LOOK, which is a third outcome and not a value of the predicate.
    lerr="$(mktemp)"
    listing="$(rclone lsf "${RCLONE_REMOTE}:${B2_BUCKET}/${path}/" 2>"$lerr")" || lrc=$?
    if [[ "$lrc" -ne 0 && "$lrc" -ne 3 ]]; then
        log "ERROR" "Could not LIST ${path} (rclone rc=${lrc}):" >&2
        sed 's/^/    /' "$lerr" >&2
        log "ERROR" "  This is NOT a claim that the backup is incomplete — it is a claim that I could not look." >&2
        rm -f "$lerr"
        return 2
    fi
    rm -f "$lerr"

    # A directory with NO run manifests is an ordinary artifact — every pre-deploy#559 backup
    # predates the manifest entirely.
    #
    # ---------------------------------------------------------------------------
    # WHAT ACTUALLY PROTECTS THIS — and it is NOT what the first version of this comment said.
    # ---------------------------------------------------------------------------
    # I wrote that a matching-nothing pipeline here "kills the script outright". That is FALSE,
    # twice over, and Nino Kavtaradze measured both (deploy#591 review):
    #
    #   1. THE NO-MATCH CONTRACT IS PER-COMMAND, NOT A UNIX CONVENTION. `manifest_runs` is
    #      `sed -n …p | sort -ru`, and BOTH EXIT 0 on no match — so this pipeline cannot fail
    #      on an empty result at all. Only `grep` has the "no match is exit 1" contract:
    #
    #        grep                        no-match -> 1  -> would crash
    #        sed -n '/x/p'               no-match -> 0  -> cannot
    #        find                        no-match -> 0  -> cannot
    #        find, unreadable directory           -> 1  -> crashes ON AN ERROR, never a no-match
    #
    #   2. ERREXIT DOES NOT REACH IN HERE ANYWAY. `shopt inherit_errexit` is UNSET, and this
    #      function is invoked inside a command substitution (`run_ts="$(backup_is_complete …)"`),
    #      so errexit is not inherited by the subshell — REGARDLESS of the call site's context.
    #      That is a GLOBAL SHELL OPTION, not a property of the caller: anyone who adds
    #      `shopt -s inherit_errexit` arms every `$( )` in this file at once.
    #
    # So `|| runs=""` is DEFENCE IN DEPTH, not the load-bearing guard: it keeps this correct if
    # someone sets `inherit_errexit`, or edits `manifest_runs` to use `grep`. The guard that IS
    # load-bearing is the TOP-LEVEL `RESTORE_RUN_TS="$(select_local_run …)" || RESTORE_RUN_TS=""`
    # — there the substitution's non-zero rc IS the assignment's rc, at top level, under
    # errexit, and removing the `||` kills restore.sh on every artifact with no manifest. That
    # is the deploy#584 shape, and it is the one the tests pin.
    local runs=""
    runs="$(printf '%s\n' "$listing" | manifest_runs)" || runs=""
    [[ -n "$runs" ]] || return 1

    # Declared and assigned SEPARATELY (SC2155). `local x="$(cmd)"` takes the exit status of
    # `local`, not of the command substitution — the same class of dead guard as deploy#563's
    # `OUT="$(fn)"; RC=$?`, and worth avoiding even where the rc is not consulted.
    local have
    have=" $(printf '%s' "$listing" | tr '\n' ' ') "
    local ts manifest mrc chosen=""
    local torn=()

    # NEWEST RUN THAT BOTH ATTESTS AND ARRIVED. Not merely the newest that attests: a
    # `complete=true` above a half-finished upload is the artifact an interrupted `rclone copy`
    # leaves behind, and it is not restorable.
    while IFS= read -r ts; do
        [[ -z "$ts" ]] && continue

        mrc=0
        manifest="$(rclone cat "${RCLONE_REMOTE}:${B2_BUCKET}/${path}/_backup_manifest-${ts}.txt" 2>/dev/null)" || mrc=$?
        if [[ "$mrc" -ne 0 && "$mrc" -ne 3 ]]; then
            # "I COULD NOT READ IT" IS NOT "IT DOES NOT ATTEST". A transient 401, a throttle or
            # a network blip on the manifest object must NOT collapse into "incomplete" — that
            # is how `resolve_latest` came to tell an operator mid-incident "No COMPLETE backup
            # found in B2 bucket" over a bucket full of good ones (deploy#584).
            log "ERROR" "Could not READ the manifest for run ${ts} at ${path} (rclone rc=${mrc})." >&2
            log "ERROR" "  This is NOT a claim that the backup is incomplete — it is a claim that I could not look." >&2
            return 2
        fi
        [[ -n "$manifest" ]] || continue

        manifest_attests_complete "$manifest" || continue

        if ! run_dumps_arrived "$ts" "$have"; then
            torn+=("$ts")
            continue
        fi

        chosen="$ts"
        break
    done <<< "$runs"

    if [[ ${#torn[@]} -gt 0 ]]; then
        # Loud, even when an older run saves us. An upload that landed its attestation and not
        # its dumps is a real fault, and it must not be silent just because it was survivable.
        log "WARNING" "  ${path}: run(s) ${torn[*]} attest complete=true but their dumps did NOT all arrive" >&2
    fi

    [[ -n "$chosen" ]] || return 1
    printf '%s\n' "$chosen"
}

# The newest run in a LOCAL directory that we should restore, or nothing.
#
# Same selection as backup_is_complete, over a downloaded artifact rather than the bucket.
#
# TWO PASSES, and the second one matters:
#
#   1. the newest run that ATTESTS complete AND whose dumps ALL ARRIVED — the run we want;
#   2. failing that, the newest run that attests ANYTHING AT ALL.
#
# Pass 2 is not a weakening. Binding to a run is what stops a TORN restore (an isnad Postgres
# from one run beside a Neo4j from another); it is a DIFFERENT question from whether the run is
# complete. So we still bind — and the required-store gate downstream then names exactly what is
# missing and REFUSES, or, under `--allow-partial`, restores what is there. Both are honest.
# Selecting NO run here would instead drop us into the no-manifest fallback, which is strictly
# less safe.
select_local_run() {
    local dir="$1"
    local listing="" runs="" have ts manifest fallback=""

    listing="$(find "$dir" -maxdepth 1 -type f -printf '%f\n' 2>/dev/null)" || listing=""
    runs="$(printf '%s\n' "$listing" | manifest_runs)" || runs=""
    [[ -n "$runs" ]] || return 1
    have=" $(printf '%s' "$listing" | tr '\n' ' ') "

    while IFS= read -r ts; do
        [[ -z "$ts" ]] && continue
        manifest="$(cat "${dir}/_backup_manifest-${ts}.txt" 2>/dev/null)" || continue
        [[ -n "$manifest" ]] || continue

        # The newest run we could READ is the fallback, whatever it attests.
        [[ -n "$fallback" ]] || fallback="$ts"

        manifest_attests_complete "$manifest" || continue
        run_dumps_arrived "$ts" "$have" || continue

        printf '%s\n' "$ts"
        return 0
    done <<< "$runs"

    [[ -n "$fallback" ]] || return 1
    printf '%s\n' "$fallback"
}

resolve_latest() {
    # The most recent COMPLETE backup — not merely the newest directory NAME.
    #
    # A partial backup lands in B2 date-stamped and checksums cleanly; at rest it is
    # indistinguishable from a complete one. Selecting by name alone means that the night
    # the user-postgres dump fails, `latest` picks precisely the artifact missing the only
    # store no pipeline artifact can rebuild — and the previous night's good backup is
    # sitting right there, unselected.
    #
    # Skipped directories are named, loudly. An operator who is told nothing about why the
    # newest backup was passed over will assume the tool is broken and reach for the one it
    # refused.
    local latest="" latest_date="0000-00-00"
    local skipped=()

    # --- THE LISTING MUST NOT FAIL OPEN. -----------------------------------------
    #
    # This was `dirs=$(rclone lsf … 2>/dev/null || true)`. A bad key, a wrong bucket, or a
    # network fault made `dirs` EMPTY — so the loop body never ran, `backup_is_complete` was
    # NEVER CALLED, and control fell straight through to "No COMPLETE backup found in B2
    # bucket." The three-outcome guard below could not fire on the single most likely
    # instrument failure, because a bad credential dies at the FIRST rclone call, upstream of
    # it (deploy#584 review, Nino Kavtaradze):
    #
    #   FIXING A GUARD DOES NOT HELP IF THE CALL THAT FEEDS IT FAILS OPEN.
    #
    # An empty listing and a failed listing are the same empty string, and only one of them is
    # a measurement — the same sentence as the `lsf` silent zero the scanner is built around,
    # which is why the scanner's step-1 probe captures this rc and this one did not.
    for category in daily weekly; do
        local dirs lrc=0 lerr
        lerr="$(mktemp)"
        dirs="$(rclone lsf "${RCLONE_REMOTE}:${B2_BUCKET}/${category}/" --dirs-only 2>"$lerr")" || lrc=$?
        if [[ "$lrc" -ne 0 ]]; then
            log "ERROR" "Cannot resolve 'latest': could not LIST ${category} (rclone rc=${lrc}):" >&2
            sed 's/^/    /' "$lerr" >&2
            log "ERROR" "  This is an INSTRUMENT failure, NOT a verdict on your backups." >&2
            log "ERROR" "  Check credentials/connectivity and retry, or name a backup explicitly." >&2
            rm -f "$lerr"
            exit 1
        fi
        rm -f "$lerr"
        while IFS= read -r dir; do
            [[ -z "$dir" ]] && continue
            dir="${dir%/}"
            if [[ "$dir" > "$latest_date" ]]; then
                # CAPTURE its stdout. `backup_is_complete` now PRINTS the run id it chose, and
                # `resolve_latest`'s own stdout IS its return value — an uncaptured run id would
                # be concatenated onto the path the caller reads.
                #
                # Declare and assign SEPARATELY: `local x="$(...)" || rc=$?` can never see a
                # failure, because the exit status of a `local` builtin is `local`'s, not the
                # substitution's — the same class of dead guard as deploy#563's `OUT="$(fn)"; RC=$?`.
                local brc=0
                local run_ts=""
                run_ts="$(backup_is_complete "${category}/${dir}")" || brc=$?
                case "$brc" in
                    0)
                        latest_date="$dir"
                        latest="${category}/${dir}"
                        log "INFO" "  ${category}/${dir}: restorable run ${run_ts}" >&2
                        ;;
                    1)
                        skipped+=("${category}/${dir}")
                        ;;
                    *)
                        # COULD NOT READ the manifest. We must NOT quietly skip: that would
                        # demote a possibly-good backup to "incomplete" on a transient 401 or
                        # network blip, and then — if it was the only one — report "No
                        # COMPLETE backup found" over a bucket full of good backups, to an
                        # operator who is already mid-incident. Refuse to resolve, and say
                        # why. An operator who knows the instrument failed can retry, fix
                        # credentials, or name a directory explicitly; one who is told the
                        # backups are incomplete cannot.
                        log "ERROR" "Cannot resolve 'latest': the manifest for ${category}/${dir} is UNREADABLE." >&2
                        log "ERROR" "  This is an INSTRUMENT failure, not a verdict on the backup." >&2
                        log "ERROR" "  Check credentials/connectivity and retry, or name a backup explicitly." >&2
                        exit 1
                        ;;
                esac
            fi
        done <<< "$dirs"
    done

    if [[ ${#skipped[@]} -gt 0 ]]; then
        log "WARNING" "Skipped ${#skipped[@]} INCOMPLETE backup(s) when resolving 'latest':" >&2
        for _s in "${skipped[@]}"; do
            log "WARNING" "    incomplete: ${_s}" >&2
        done
        log "WARNING" "To restore one of these anyway, name it explicitly and pass --allow-partial." >&2
    fi

    if [[ -z "$latest" ]]; then
        log "ERROR" "No COMPLETE backup found in B2 bucket." >&2
        log "ERROR" "If only incomplete backups exist, name one explicitly and pass --allow-partial." >&2
        exit 1
    fi

    echo "$latest"
}

verify_checksums() {
    local dir="$1"
    local all_ok=true
    local verified=0

    log "INFO" "Verifying checksums..."

    # Collect via find rather than a bare glob. The previous `for f in "$dir"/*.sha256`
    # with a `[[ -f ]] || continue` guard iterated ONCE over the literal, unexpanded
    # pattern when the directory held no checksum files — the guard skipped it and the
    # function returned success having verified nothing. A backup with zero checksums,
    # or an entirely empty backup, therefore "passed" verification (deploy#560).
    local checksum_files=()
    while IFS= read -r -d '' f; do
        checksum_files+=("$f")
    done < <(find "$dir" -maxdepth 1 -type f -name '*.sha256' -print0)

    # Presence and validity are separate questions, and they get separate messages.
    # Folding them together made an all-mismatch artifact report "no checksum files
    # found", which sends the operator looking for the wrong problem.
    if [[ ${#checksum_files[@]} -eq 0 ]]; then
        log "ERROR" "No checksum files found in ${dir} — refusing to restore an unverifiable artifact"
        exit 1
    fi

    for checksum_file in "${checksum_files[@]}"; do
        local base_file="${checksum_file%.sha256}"
        if [[ ! -f "$base_file" ]]; then
            # A checksum naming a file the backup does not contain means the artifact
            # is incomplete. That is a hard failure. Warning-and-continuing here let an
            # incomplete backup reach the restore path.
            log "ERROR" "File referenced by checksum is missing: $(basename "$base_file")"
            all_ok=false
            continue
        fi
        local expected actual
        expected=$(awk '{print $1}' "$checksum_file")
        actual=$(sha256sum "$base_file" | awk '{print $1}')
        if [[ "$expected" == "$actual" ]]; then
            log "INFO" "Checksum OK: $(basename "$base_file")"
            verified=$((verified + 1))
        else
            log "ERROR" "Checksum MISMATCH: $(basename "$base_file")"
            all_ok=false
        fi
    done

    if [[ "$all_ok" == "false" ]]; then
        log "ERROR" "Checksum verification failed — aborting restore"
        exit 1
    fi

    # Belt and braces: checksum files existed and none failed, so `verified` must be
    # positive. If it somehow is not, we verified nothing and must not say we passed.
    if [[ "$verified" -eq 0 ]]; then
        log "ERROR" "Verified 0 files despite ${#checksum_files[@]} checksum file(s) present — refusing to restore"
        exit 1
    fi

    log "INFO" "Checksum verification passed (${verified} file(s) verified)"
}

terminate_pg_connections() {
    local service="$1" pg_user="$2" pg_db="$3"
    log "INFO" "Terminating active PostgreSQL connections to ${pg_db} (service=${service})..."
    dc exec -T "$service" \
        psql -U "$pg_user" -d postgres -c \
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '${pg_db}' AND pid <> pg_backend_pid();" \
        2>/dev/null || true
}

# restore_postgres <dump-file> <compose-service> <user> <db> <label>
restore_postgres() {
    local dump_file="$1" service="$2" pg_user="$3" pg_db="$4" label="$5"
    local rc out
    log "INFO" "Restoring ${label} from $(basename "$dump_file")..."

    terminate_pg_connections "$service" "$pg_user" "$pg_db"

    out="$(mktemp)"

    # `--exit-on-error` is deliberately NOT passed: without it pg_restore keeps going
    # past a failing object and still exits non-zero, so an operator mid-incident
    # recovers as much data as the dump allows AND gets an honest verdict. Measured
    # (deploy#560): a dump whose ALTER OWNER references a missing role exits 1 both
    # with and without the flag; a truncated dump exits 1 with "could not read from
    # input file: end of file".
    #
    # The previous implementation swallowed that exit code, logging
    #   "pg_restore finished with warnings (this is often normal with --clean)"
    # and returning success. That comment's premise is false: pg_restore exits 0 when
    # only warnings occurred. A non-zero exit means errors were ignored or the input
    # was unreadable. Verified: restoring a truncated dump printed
    # "could not read from input file: end of file", restored zero rows, and the
    # script reported "PostgreSQL: restored / === Restore complete ===" with exit 0.
    if dc exec -T "$service" \
        pg_restore -U "$pg_user" -d "$pg_db" --clean --if-exists \
        < "$dump_file" > "$out" 2>&1; then
        rc=0
    else
        rc=$?
    fi

    # Surface pg_restore's own diagnostics regardless of outcome.
    sed 's/^/    /' "$out"

    if [[ $rc -ne 0 ]]; then
        log "ERROR" "${label}: pg_restore FAILED (exit ${rc}) — the database may be partially restored"
        rm -f "$out"
        return 1
    fi

    # Defence in depth. If a future edit adds a flag that makes pg_restore exit 0 while
    # still discarding objects, this catches it: pg_restore announces the count itself.
    if grep -qE 'errors ignored on restore: [0-9]+' "$out"; then
        local ignored
        ignored=$(grep -oE 'errors ignored on restore: [0-9]+' "$out" | grep -oE '[0-9]+$' | tail -1)
        log "ERROR" "${label}: pg_restore ignored ${ignored} error(s) — treating as a failed restore"
        rm -f "$out"
        return 1
    fi

    rm -f "$out"
    log "INFO" "${label} restore complete"
    return 0
}

restore_neo4j() {
    local dump_file="$1"

    # Decompress if needed
    if [[ "$dump_file" == *.zst ]]; then
        log "INFO" "Decompressing Neo4j dump..."
        local decompressed="${dump_file%.zst}"
        zstd -d "$dump_file" -o "$decompressed"
        dump_file="$decompressed"
    fi

    log "INFO" "Stopping Neo4j for restore..."
    dc stop neo4j

    # Wait for Neo4j to stop.
    #
    # `| grep -q` is an early-exit consumer and this is SAFE: `docker compose ps` over one
    # project emits a few hundred bytes, which fits the pipe buffer entire, so the producer has
    # exited before grep quits and there is no SIGPIPE for `pipefail` to promote. The producer
    # is ENVIRONMENT-BOUNDED, not artifact content — see "WHY THE OTHER EARLY-EXIT PIPELINES IN
    # THIS FILE ARE LEFT ALONE" above before copying this shape somewhere it is not.
    local max_wait=30 waited=0
    while dc ps --format '{{.Service}}:{{.State}}' 2>/dev/null | grep -q "neo4j:running"; do
        if [[ $waited -ge $max_wait ]]; then
            log "ERROR" "Neo4j did not stop within ${max_wait}s"
            neo4j_start || log "ERROR" "  ...and it could not be restarted either — check manually"
            return 1
        fi
        sleep 1
        waited=$((waited + 1))
    done

    # Resolve the data volume from THIS compose project's neo4j container, not by
    # grepping every volume on the host.
    #
    # The previous implementation ran
    #     docker volume ls --format '{{.Name}}' | grep -E '(neo4j_data|neo4j-data)$' | head -1
    # which matches across ALL compose projects on the box. Loading a dump into the
    # first alphabetical match is how a restore rehearsal — or a restore aimed at a
    # scratch stack — silently overwrites the production graph volume. `--overwrite-
    # destination` makes that unrecoverable. Resolving through COMPOSE_FILE keeps the
    # blast radius inside the stack the caller actually named. (deploy#560)
    #
    # `| head -1` is safe here for the same reason as the wait-loop above: a one-service
    # container-id list is environment-bounded and fits the pipe buffer, so nothing is still
    # writing when `head` quits.
    local neo4j_cid
    neo4j_cid=$(dc ps -aq neo4j 2>/dev/null | head -1)
    if [[ -z "$neo4j_cid" ]]; then
        log "ERROR" "Cannot resolve a neo4j container in project '${COMPOSE_PROJECT}' (${COMPOSE_FILE}) — refusing to guess a data volume"
        neo4j_start || log "ERROR" "  ...and there is no neo4j container to start in that project either (deploy#617)"
        return 1
    fi

    NEO4J_VOLUME=$(docker inspect \
        --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}' \
        "$neo4j_cid")
    if [[ -z "$NEO4J_VOLUME" ]]; then
        log "ERROR" "neo4j container ${neo4j_cid} has no named volume mounted at /data"
        neo4j_start || log "ERROR" "  ...and neo4j could not be restarted — check manually"
        return 1
    fi
    log "INFO" "Resolved Neo4j data volume: ${NEO4J_VOLUME} (project ${COMPOSE_PROJECT}, ${COMPOSE_FILE})"

    # `neo4j-admin database load <db> --from-path=DIR` locates the archive by NAME:
    # it looks for DIR/<db>.dump, i.e. DIR/neo4j.dump. backup.sh stores the archive as
    # isnad-neo4j-<timestamp>.dump, so pointing --from-path at the staging directory
    # fails with "No matching archives found" — the Neo4j restore path could never
    # have succeeded against a real backup artifact. Stage the archive under the name
    # the loader demands, in a directory containing nothing else. (deploy#560)
    local dump_dir="${RESTORE_DIR}/neo4j-load"
    rm -rf "$dump_dir"
    mkdir -p "$dump_dir"
    cp "$dump_file" "${dump_dir}/neo4j.dump"

    log "INFO" "Loading Neo4j dump (staged as ${dump_dir}/neo4j.dump)..."
    # `--user 0:0 --entrypoint neo4j-admin` is required, not cosmetic.
    #
    # The neo4j:5-community entrypoint drops privileges to neo4j(7474) even when the
    # container starts as root. The dropped user then cannot read a /backups bind
    # mount it does not own, and `neo4j-admin database load` dies with
    # "AccessDeniedException: /backups". Bypassing the entrypoint keeps the command
    # running as root, which can read the staging dir and write /data. On restart the
    # entrypoint chowns /data back to neo4j(7474), so the store stays usable.
    # Measured on neo4j:5-community during deploy#560:
    #   entrypoint path -> effective uid 7474 -> AccessDeniedException, exit 1
    #   bypass path     -> effective uid 0    -> "Dump completed successfully", exit 0
    if docker run --rm \
        --user 0:0 \
        --entrypoint neo4j-admin \
        -v "${NEO4J_VOLUME}:/data" \
        -v "${dump_dir}:/backups" \
        neo4j:5-community \
        database load neo4j --from-path=/backups/ --overwrite-destination 2>&1; then
        log "INFO" "Neo4j restore complete"
    else
        log "ERROR" "Neo4j restore failed"
        neo4j_start || log "ERROR" "  ...and neo4j could not be restarted — check manually"
        return 1
    fi

    # `start`, never `up`. `up` CREATES a container when the project has none, and in THIS
    # function that is not merely untidy: the container it would invent is the one whose
    # volume the load above targets. Restoring into a project you accidentally created is
    # how a restore reports success while the real graph sits untouched (deploy#617).
    log "INFO" "Restarting Neo4j..."
    if ! neo4j_start; then
        log "ERROR" "Neo4j restored but could NOT be restarted — the graph is DOWN, check manually"
        return 1
    fi

    # Wait for healthy. Same early-exit pipeline, same environment-bounded producer, safe for
    # the same reason as the stop-loop above.
    local max_health=120 health_waited=0
    while ! dc ps --format '{{.Service}}:{{.Health}}' 2>/dev/null | grep -q "neo4j:healthy"; do
        if [[ $health_waited -ge $max_health ]]; then
            # The dump loaded; Neo4j not coming back healthy is a separate, real
            # failure. Report it as one rather than falling off the end with success.
            log "ERROR" "Neo4j did not become healthy within ${max_health}s after restore"
            return 1
        fi
        sleep 5
        health_waited=$((health_waited + 5))
    done

    return 0
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
BACKUP_PATH=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --force)
            FORCE=true
            shift
            ;;
        --allow-partial)
            ALLOW_PARTIAL=true
            shift
            ;;
        --list)
            list_backups
            exit 0
            ;;
        --help|-h)
            echo "Usage: $0 [--force] [--allow-partial] [--list] <backup-path|latest>"
            echo ""
            echo "  latest              Restore the most recent COMPLETE backup"
            echo "  daily/2026-03-25    Restore a specific backup"
            echo "  --force             Skip confirmation prompt"
            echo "  --allow-partial     Restore a backup that is MISSING one or more stores."
            echo "                      Without this, an incomplete backup is REFUSED: restoring"
            echo "                      some stores and not others reports success while silently"
            echo "                      leaving the rest at their pre-restore contents, and"
            echo "                      user-postgres (accounts, sessions, audit_log) cannot be"
            echo "                      rebuilt from any artifact. DR use only."
            echo "  --list              List available backups"
            exit 0
            ;;
        *)
            BACKUP_PATH="$1"
            shift
            ;;
    esac
done

if [[ -z "$BACKUP_PATH" ]]; then
    echo "Usage: $0 [--force] [--allow-partial] [--list] <backup-path|latest>"
    echo "Run '$0 --list' to see available backups."
    exit 1
fi

# ---------------------------------------------------------------------------
# Assert the stack we are about to restore INTO is actually there (deploy#617)
# ---------------------------------------------------------------------------
# Before we resolve anything, download gigabytes, or overwrite a single byte. A restore
# aimed at a project with no containers does not fail cleanly: `exec` has nothing to feed
# the dump to, and the Neo4j leg — absent this check and the `up`->`start` change — would
# CREATE a container and load the dump into ITS fresh volume, then report success.
#
# Deliberately AFTER `--list` and `--help` (which need no stack) and BEFORE the
# confirmation prompt: an operator who is about to type YES should already know the target
# exists. `--list` still works on a box where the stack is down, which is a real DR case.
if ! assert_stack_present postgres user-postgres neo4j; then
    log "ERROR" "Refusing to restore into a stack that is not present."
    log "ERROR" "  Nothing has been downloaded and nothing has been written."
    exit 1
fi

# ---------------------------------------------------------------------------
# Resolve backup path
# ---------------------------------------------------------------------------
if [[ "$BACKUP_PATH" == "latest" ]]; then
    if [[ -n "$RESTORE_LOCAL_DIR" ]]; then
        log "ERROR" "'latest' resolves against B2 and is meaningless with RESTORE_LOCAL_DIR set"
        exit 1
    fi
    BACKUP_PATH=$(resolve_latest)
    log "INFO" "Resolved 'latest' to: ${BACKUP_PATH}"
fi

# ---------------------------------------------------------------------------
# Confirmation prompt
# ---------------------------------------------------------------------------
if [[ "$FORCE" == "false" ]]; then
    echo ""
    echo "========================================================"
    echo "  WARNING: This will OVERWRITE the current databases."
    echo ""
    echo "  Backup source: $(if [[ -n "$RESTORE_LOCAL_DIR" ]]; then echo "${RESTORE_LOCAL_DIR} (local dir)"; else echo "${RCLONE_REMOTE}:${B2_BUCKET}/${BACKUP_PATH}/"; fi)"
    echo "  Compose file:  ${COMPOSE_FILE}"
    echo "  PostgreSQL DB: ${POSTGRES_DB}"
    echo "  Neo4j:         will be stopped and restored"
    echo "========================================================"
    echo ""
    read -r -p "Type YES to confirm restore: " confirm
    if [[ "$confirm" != "YES" ]]; then
        log "INFO" "Restore cancelled by user"
        exit 0
    fi
fi

# ---------------------------------------------------------------------------
# Download backup
# ---------------------------------------------------------------------------
mkdir -p "$RESTORE_DIR"

if [[ -n "$RESTORE_LOCAL_DIR" ]]; then
    # Local-artifact source. Two uses: (1) the restore rehearsal
    # (scripts/restore_rehearsal.sh) exercises this very script without needing B2
    # credentials or a network round-trip; (2) disaster recovery from a dump an
    # operator already holds on disk. The artifact still goes through the same
    # checksum verification and the same restore paths — the only thing skipped is
    # the download.
    if [[ ! -d "$RESTORE_LOCAL_DIR" ]]; then
        log "ERROR" "RESTORE_LOCAL_DIR does not exist: ${RESTORE_LOCAL_DIR}"
        exit 1
    fi
    log "INFO" "Using local backup artifact: ${RESTORE_LOCAL_DIR} (B2 download skipped)"
    cp -a "$RESTORE_LOCAL_DIR"/. "$RESTORE_DIR"/
else
    log "INFO" "Downloading backup from ${RCLONE_REMOTE}:${B2_BUCKET}/${BACKUP_PATH}/..."
    if ! rclone copy "${RCLONE_REMOTE}:${B2_BUCKET}/${BACKUP_PATH}/" "$RESTORE_DIR/" --log-level INFO; then
        log "ERROR" "Failed to download backup from B2"
        exit 1
    fi
fi

# ---------------------------------------------------------------------------
# Verify checksums
# ---------------------------------------------------------------------------
verify_checksums "$RESTORE_DIR"

# ---------------------------------------------------------------------------
# Restore databases
# ---------------------------------------------------------------------------

# Find dump files.
#
# `isnad-pg-*` must not also match `isnad-userpg-*`, and it does not: the prefixes
# diverge at the character after "isnad-". Keep it that way if the names ever change —
# restoring the user-service dump into the isnad database would be silent and severe.
#
# ---------------------------------------------------------------------------
# SELECT THE ATTESTED RUN — not "whatever `find` hands back first"
# ---------------------------------------------------------------------------
# These three were `find ... | head -1`. `find` emits READDIR order, which is neither
# chronological nor stable — and a backup directory can legitimately hold dumps from MORE
# THAN ONE RUN. B2's day-directory accumulates: the path is `<category>/<DATE>` (a day),
# the dumps inside it are `isnad-<store>-<TIMESTAMP>` (a run), and `rclone copy` ADDS and never
# deletes. backup.sh deliberately uploads partials ("a partial backup beats none"), so a failed
# 02:00 run and a good 14:00 run land side by side — routinely, by design. (Each run now carries
# its OWN `_backup_manifest-<TS>.txt`; the single fixed-name manifest this code was first
# written against was overwritten by whichever run uploaded last — deploy#587.)
#
# Measured over 48 two-run day-directories, `find | head -1` selected the OLDER dump in
# 14 of them. And because each store was selected by an INDEPENDENT `find`, the three could
# come from DIFFERENT RUNS: isnad Postgres from the failed 02:00 attempt, Neo4j from the
# good 14:00 one — a TORN RESTORE across datastores, referentially inconsistent, with the
# required-store gate satisfied (all three present), `verify_checksums` green (a checksum
# binds a file to ITSELF; it cannot see that the file is from the wrong run) and
# `complete=true` on the manifest. Every gate we have would have passed it.
#
# Each run carries its own `_backup_manifest-<TS>.txt`, and every dump of that run is named
# `isnad-<store>-<TS>.*`. So bind the selection to a RUN: the producer names the run, the
# consumer restores THAT RUN or none of it. A store missing from the selected run then falls
# through to the required-store gate below, which is exactly where it belongs.
#
# `select_local_run` picks the newest run that ATTESTS complete AND whose dumps ALL ARRIVED,
# falling back to the newest run that attests anything (see its comment). Before deploy#587
# this read a single FIXED-NAME manifest, so in a day-directory holding a good run and a later
# partial one it bound to whichever uploaded LAST — and under `--allow-partial` it would
# restore only the partial run's pg dump while the good run's three dumps sat beside it.
#
# `|| RESTORE_RUN_TS=""` is THE LOAD-BEARING GUARD — this one, and not the `|| runs=""` inside
# the functions (see `backup_is_complete` for why those are only defence in depth).
#
# The mechanism is the ASSIGNMENT'S OWN rc, not anything about pipefail or no-match contracts:
# `select_local_run` RETURNS 1 when the directory has no manifest — every pre-deploy#559 backup,
# and the rehearsal's own fixtures — and a command substitution's exit status IS the exit status
# of the assignment that reads it. At TOP LEVEL, under `set -e`, that is a failing simple command
# and errexit kills restore.sh outright. The recovery path would die on exactly the artifacts the
# fallback below exists to serve.
#
# Removing the `||` here is the deploy#584 crash, reproduced: it was caught then by the restore
# rehearsal and by NO unit test. It is pinned by a unit test now, and by a mutation.
RESTORE_RUN_TS=""
RESTORE_RUN_TS="$(select_local_run "$RESTORE_DIR")" || RESTORE_RUN_TS=""

#
# The `find … | head -1` pipelines below are early-exit consumers and are SAFE: `find` over one
# restore directory, filtered to a single run's dump name, is environment-bounded — a few
# hundred bytes, inside the pipe buffer, producer exited before `head` quits. The manifest was
# the only producer here that artifact content could inflate, and it is the only one that lost
# its pipeline (deploy#591). See "WHY THE OTHER EARLY-EXIT PIPELINES IN THIS FILE ARE LEFT
# ALONE" (just after `run_dumps_arrived`) before copying the shape.
if [[ -n "$RESTORE_RUN_TS" ]]; then
    log "INFO" "Manifest attests run ${RESTORE_RUN_TS} — selecting that run's dumps"
    PG_DUMP=$(find "$RESTORE_DIR" -name "isnad-pg-${RESTORE_RUN_TS}.dump" -type f | head -1)
    USER_PG_DUMP=$(find "$RESTORE_DIR" -name "isnad-userpg-${RESTORE_RUN_TS}.dump" -type f | head -1)
    NEO4J_DUMP=$(find "$RESTORE_DIR" \( -name "isnad-neo4j-${RESTORE_RUN_TS}.dump.zst" \
        -o -name "isnad-neo4j-${RESTORE_RUN_TS}.dump" \) -type f | head -1)
elif [[ "$(count_runs "$RESTORE_DIR")" -gt 1 ]]; then
    # --- SORTING MADE THE SELECTION REPRODUCIBLE. IT DID NOT MAKE IT COHERENT. ---
    #
    # The fallback below is three INDEPENDENT `sort -r | head -1`s, and with no manifest
    # timestamp to bind them there is nothing making the three agree on a RUN. Against a
    # directory holding a complete 03:01 run plus a failed 08:00 rerun that left a stray pg
    # dump, it selected:
    #
    #   pg=…-080000   userpg=…-030100   neo4j=…-030100      <- TORN
    #
    # Postgres from the failed rerun, Neo4j from the attested run. That is the SAME defect as
    # the original `find | head -1` — it merely tears the same way every time now. And every
    # guard still passes it: required-store sees all three present, and each checksum verifies
    # the file against ITSELF (deploy#584 review, Nino Kavtaradze).
    #
    # Determinism is not coherence. With more than one run in the directory and no manifest to
    # say which one is real, there is no honest answer — so REFUSE and let the operator name
    # it. Guessing is what produced the tear.
    log "ERROR" "This backup directory holds dumps from MORE THAN ONE RUN, and has no manifest"
    log "ERROR" "  timestamp to say which run is the real one:"
    list_runs "$RESTORE_DIR" | sed 's/^/      /'
    log "ERROR" "  Selecting per-store would mix runs — an isnad Postgres from one run beside a"
    log "ERROR" "  Neo4j from another restores a referentially INCONSISTENT stack, and every"
    log "ERROR" "  checksum still passes (a hash binds a file to ITSELF; it cannot see the file"
    log "ERROR" "  is from the wrong run)."
    log "ERROR" "  Remove the dumps from the runs you do not want, and re-run."
    exit 1
else
    # No manifest (a pre-deploy#559 artifact, or a hand-assembled directory under
    # --allow-partial), and exactly ONE run present — so per-store selection cannot mix runs.
    # We cannot bind to a run, so at least be DETERMINISTIC: the timestamp
    # is `%Y%m%d-%H%M%S`, which sorts chronologically as text, so `sort -r | head -1` is the
    # NEWEST — never an arbitrary one. Strictly better than readdir order in every case.
    #
    # And these `head -1`s cannot SIGPIPE for a reason stronger than the bounded-producer one
    # above: `sort` is a FULL CONSUMER. It must read all of `find`'s output before it can emit
    # its first line, so `find` has already closed by the time `head` quits — there is no writer
    # left to signal at ANY input size. (Nurul: bounded producer; Aisha: full consumer in the
    # middle. Both hold; the second needs no size argument.)
    log "WARNING" "No manifest timestamp — falling back to newest-by-name (cannot bind to a run)"
    PG_DUMP=$(find "$RESTORE_DIR" -name 'isnad-pg-*.dump' -type f | sort -r | head -1)
    USER_PG_DUMP=$(find "$RESTORE_DIR" -name 'isnad-userpg-*.dump' -type f | sort -r | head -1)
    NEO4J_DUMP=$(find "$RESTORE_DIR" \( -name 'isnad-neo4j-*.dump.zst' -o -name 'isnad-neo4j-*.dump' \) -type f | sort -r | head -1)
fi

# A backup containing no dumps at all is not a backup. Previously the `find`s coming
# back empty produced warnings, "=== Restore complete ===", and exit 0 — an empty
# directory restored "successfully" (deploy#560).
if [[ -z "$PG_DUMP" && -z "$USER_PG_DUMP" && -z "$NEO4J_DUMP" ]]; then
    log "ERROR" "Backup contains no PostgreSQL dump and no Neo4j dump — nothing to restore"
    exit 1
fi

PG_RESULT="skipped (no dump)"
USER_PG_RESULT="skipped (no dump)"
NEO4J_RESULT="skipped (no dump)"
FAILED=0

# ---------------------------------------------------------------------------
# REQUIRED-STORE GATE — an ABSENT dump is as fatal as a FAILED one
# ---------------------------------------------------------------------------
# This script used to set FAILED=1 only when a dump was PRESENT and its restore failed. A
# dump that was absent entirely logged a WARNING, recorded "skipped (no dump)", and never
# touched FAILED — so the script printed "=== Restore complete ===" and exited 0 on a
# backup that was missing user-postgres. The all-empty case was caught, and the
# one-store-missing case was not: a guard on each side of the hole and none in it.
#
# It was reachable in production, by an artifact THIS CHANGE creates: backup.sh
# deliberately uploads a partial when a leg fails ("a partial backup beats none"), that
# directory lands in B2 date-stamped and checksums cleanly, and `latest` picks the newest
# directory NAME. So the night the user-postgres dump fails, `restore.sh latest` selects
# exactly the artifact missing the one store no pipeline artifact can rebuild — and says
# "Restore complete".
#
# The scenario is the one this PR exists to prevent: the dump fails, BackupFailed fires
# CORRECTLY, nobody restores because nothing is broken yet, the next night succeeds. Weeks
# later a prune eats the graph, someone restores `latest`, gets the graph and the isnad DB
# back, is told the restore is complete — and has silently lost every account, session and
# audit record. The alerting cannot help: it watched the backup, and the backup honestly
# said it failed. THE RESTORE IS THE THING THAT LIED.
#
# backup.sh already refuses to call a partial backup a success — USER_PG_OK is INSIDE its
# non-zero-exit condition, for exactly this reason. The identical argument applies to the
# consumer and had not been carried across: the producer refused to call a partial backup a
# success while the consumer called a partial RESTORE one. And this script's own summary
# comment says "a restore that did not restore must not be reported as success" — the
# comment was right and the code disagreed with it.
#
# THE EXPECTED SET IS THIS SCRIPT'S OWN, and is deliberately NOT read from the artifact.
# If it were, a partial backup would declare itself complete-for-what-it-happens-to-hold —
# the same circularity as a read-back count, and just as invisible. The backup's manifest
# is a SECOND signal (checked above), never the definition of what is required.
REQUIRED_STORES=("isnad-pg:PostgreSQL (isnad)" "isnad-userpg:PostgreSQL (user-service)" "isnad-neo4j:Neo4j")
MISSING_STORES=()
[[ -z "$PG_DUMP" ]]      && MISSING_STORES+=("PostgreSQL (isnad)")
[[ -z "$USER_PG_DUMP" ]] && MISSING_STORES+=("PostgreSQL (user-service) — accounts, sessions, audit_log")
[[ -z "$NEO4J_DUMP" ]]   && MISSING_STORES+=("Neo4j")

if [[ ${#MISSING_STORES[@]} -gt 0 ]]; then
    if [[ "$ALLOW_PARTIAL" == "true" ]]; then
        log "WARNING" "--allow-partial: proceeding with ${#MISSING_STORES[@]} store(s) MISSING from this backup:"
        for _m in "${MISSING_STORES[@]}"; do
            log "WARNING" "    MISSING: ${_m}"
        done
        log "WARNING" "The stores above will NOT be restored. Their current contents are left as they are."
    else
        log "ERROR" "=== Restore REFUSED — this backup is incomplete ==="
        for _m in "${MISSING_STORES[@]}"; do
            log "ERROR" "    MISSING: ${_m}"
        done
        log "ERROR" "A complete backup contains all ${#REQUIRED_STORES[@]} dumped stores (see docs/DATASTORES.md)."
        log "ERROR" "Restoring only some of them would report success while silently leaving the others"
        log "ERROR" "at their pre-restore contents — and user-postgres cannot be rebuilt from any artifact."
        log "ERROR" ""
        log "ERROR" "If you are in a DR scenario and this really is the only backup you have, re-run with"
        log "ERROR" "--allow-partial to restore what is present. NOTHING has been restored."
        exit 1
    fi
fi

if [[ -n "$PG_DUMP" ]]; then
    if restore_postgres "$PG_DUMP" postgres "$POSTGRES_USER" "$POSTGRES_DB" "PostgreSQL (isnad)"; then
        PG_RESULT="restored"
    else
        PG_RESULT="FAILED"
        FAILED=1
    fi
fi

if [[ -n "$USER_PG_DUMP" ]]; then
    if restore_postgres "$USER_PG_DUMP" user-postgres "$USER_POSTGRES_USER" "$USER_POSTGRES_DB" "PostgreSQL (user-service)"; then
        USER_PG_RESULT="restored"
    else
        USER_PG_RESULT="FAILED"
        FAILED=1
    fi
fi

if [[ -n "$NEO4J_DUMP" ]]; then
    if restore_neo4j "$NEO4J_DUMP"; then
        NEO4J_RESULT="restored"
    else
        NEO4J_RESULT="FAILED"
        FAILED=1
    fi
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
if [[ "$FAILED" -eq 1 ]]; then
    log "ERROR" "=== Restore FAILED ==="
else
    log "INFO" "=== Restore complete ==="
fi
log "INFO" "Source: $(if [[ -n "$RESTORE_LOCAL_DIR" ]]; then echo "$RESTORE_LOCAL_DIR (local)"; else echo "${RCLONE_REMOTE}:${B2_BUCKET}/${BACKUP_PATH}/"; fi)"
log "INFO" "PostgreSQL (isnad):        ${PG_RESULT}"
log "INFO" "PostgreSQL (user-service): ${USER_PG_RESULT}"
log "INFO" "Neo4j:                     ${NEO4J_RESULT}"

# The exit status is the whole point: a restore that did not restore must not be
# reported as success. Callers (restore_rehearsal.sh, operators, CI) gate on this.
if [[ "$FAILED" -eq 1 ]]; then
    exit 1
fi

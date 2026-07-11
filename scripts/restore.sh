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
list_runs() {
    find "$1" \( -name 'isnad-*.dump' -o -name 'isnad-*.dump.zst' \) -type f -printf '%f\n' 2>/dev/null \
        | sed -n 's/^isnad-[a-z0-9]\{1,\}-\([0-9]\{8\}-[0-9]\{6\}\)\.dump\(\.zst\)\{0,1\}$/\1/p' \
        | sort -u
}

count_runs() {
    local n
    n="$(list_runs "$1" | grep -c . || true)"
    printf '%s\n' "${n:-0}"
}

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

# Does the backup directory at <category>/<date> declare itself COMPLETE?
# Reads `_backup_manifest.txt` (written by backup.sh, deploy#559). A directory with no
# manifest is NOT complete: every pre-#559 backup predates user-postgres coverage entirely,
# so treating "no manifest" as "probably fine" would hand `latest` the exact artifact class
# this change exists to stop.
#
# THREE outcomes, not two:
#   0 = attests complete
#   1 = read it, and it does NOT attest (or there is no manifest)
#   2 = COULD NOT READ IT  <- not a value of the predicate. A separate answer.
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
backup_is_complete() {
    local path="$1"
    local manifest rc=0
    manifest="$(rclone cat "${RCLONE_REMOTE}:${B2_BUCKET}/${path}/_backup_manifest.txt" 2>/dev/null)" || rc=$?
    if [[ "$rc" -ne 0 && "$rc" -ne 3 ]]; then
        log "ERROR" "Could not READ the manifest at ${path} (rclone rc=${rc})."
        log "ERROR" "  This is NOT a claim that the backup is incomplete — it is a claim that I could not look."
        return 2
    fi
    [[ -n "$manifest" ]] || return 1
    # WHOLE-TOKEN match. The predicate this replaces was:
    #
    #   grep -q '^BACKUP_MANIFEST .*[[:space:]]complete=true\([[:space:]]\|$\)'
    #
    # and it COULD NEVER MATCH. `complete=` is the FIRST token backup.sh writes after
    # `BACKUP_MANIFEST `, so the literal space in the anchor consumes the only space there
    # is, and `.*[[:space:]]complete=true` then demands a SECOND one that never exists.
    #
    # So `backup_is_complete` returned false for EVERY backup, `resolve_latest` skipped all
    # of them, and `restore.sh latest` could not select an artifact at all — it would have
    # reported "No COMPLETE backup found in B2 bucket" over a bucket full of good backups.
    # It failed CLOSED, so nothing was ever at risk; but the recovery path was inert, and it
    # shipped in deploy#577 with a green suite.
    #
    # It survived because #577's tests are TEXTUAL: they assert this function is CALLED
    # (correctly — that was the deploy#577 review's own lesson) and never once run its
    # predicate against a manifest `backup.sh` actually produces. Found by deploy#584, whose
    # fixture was a real manifest.
    #
    # Splitting on spaces and matching an exact `complete=true` token is order-independent
    # and cannot be defeated by a key that ends in another key's name.
    #
    # `head -n1`: attest on the FIRST manifest line only. Unioning tokens across every line
    # lets a corrupt artifact carrying both a `complete=false` and a `complete=true` line
    # match — it fails OPEN, and reading line 1 costs nothing.
    printf '%s\n' "$manifest" \
        | grep '^BACKUP_MANIFEST ' \
        | head -n1 \
        | tr ' ' '\n' \
        | grep -qx 'complete=true'
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
                local brc=0
                backup_is_complete "${category}/${dir}" || brc=$?
                case "$brc" in
                    0)
                        latest_date="$dir"
                        latest="${category}/${dir}"
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
    docker compose -f "$COMPOSE_FILE" exec -T "$service" \
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
    if docker compose -f "$COMPOSE_FILE" exec -T "$service" \
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
    docker compose -f "$COMPOSE_FILE" stop neo4j

    # Wait for Neo4j to stop
    local max_wait=30 waited=0
    while docker compose -f "$COMPOSE_FILE" ps --format '{{.Service}}:{{.State}}' 2>/dev/null | grep -q "neo4j:running"; do
        if [[ $waited -ge $max_wait ]]; then
            log "ERROR" "Neo4j did not stop within ${max_wait}s"
            docker compose -f "$COMPOSE_FILE" up -d neo4j
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
    local neo4j_cid
    neo4j_cid=$(docker compose -f "$COMPOSE_FILE" ps -aq neo4j 2>/dev/null | head -1)
    if [[ -z "$neo4j_cid" ]]; then
        log "ERROR" "Cannot resolve a neo4j container for ${COMPOSE_FILE} — refusing to guess a data volume"
        docker compose -f "$COMPOSE_FILE" up -d neo4j
        return 1
    fi

    NEO4J_VOLUME=$(docker inspect \
        --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}' \
        "$neo4j_cid")
    if [[ -z "$NEO4J_VOLUME" ]]; then
        log "ERROR" "neo4j container ${neo4j_cid} has no named volume mounted at /data"
        docker compose -f "$COMPOSE_FILE" up -d neo4j
        return 1
    fi
    log "INFO" "Resolved Neo4j data volume: ${NEO4J_VOLUME} (from ${COMPOSE_FILE})"

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
        docker compose -f "$COMPOSE_FILE" up -d neo4j
        return 1
    fi

    log "INFO" "Restarting Neo4j..."
    docker compose -f "$COMPOSE_FILE" up -d neo4j

    # Wait for healthy
    local max_health=120 health_waited=0
    while ! docker compose -f "$COMPOSE_FILE" ps --format '{{.Service}}:{{.Health}}' 2>/dev/null | grep -q "neo4j:healthy"; do
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
# the dumps inside it are `isnad-<store>-<TIMESTAMP>` (a run), `rclone copy` ADDS and never
# deletes, and the fixed-name `_backup_manifest.txt` is overwritten by whichever run
# uploaded last. backup.sh deliberately uploads partials ("a partial backup beats none"),
# so a failed 02:00 run and a good 14:00 run land side by side — routinely, by design.
#
# Measured over 48 two-run day-directories, `find | head -1` selected the OLDER dump in
# 14 of them. And because each store was selected by an INDEPENDENT `find`, the three could
# come from DIFFERENT RUNS: isnad Postgres from the failed 02:00 attempt, Neo4j from the
# good 14:00 one — a TORN RESTORE across datastores, referentially inconsistent, with the
# required-store gate satisfied (all three present), `verify_checksums` green (a checksum
# binds a file to ITSELF; it cannot see that the file is from the wrong run) and
# `complete=true` on the manifest. Every gate we have would have passed it.
#
# The manifest already carries the run id — `timestamp=<TS>` — and every dump of that run
# is named `isnad-<store>-<TS>.*`. So bind the selection to it: the producer names the run,
# the consumer restores THAT RUN or none of it. A store missing from the attested run then
# falls through to the required-store gate below, which is exactly where it belongs.
# The trailing `|| true` is LOAD-BEARING, and its absence was caught by the restore rehearsal
# rather than by any unit test. This script runs under `set -euo pipefail`. An artifact with
# no manifest — every pre-deploy#559 backup, and the rehearsal's own fixtures — makes `grep`
# match nothing, which under `pipefail` fails the WHOLE pipeline, which makes this bare
# assignment a FAILING SIMPLE COMMAND, at which point errexit kills restore.sh outright. The
# recovery path would have died on exactly the artifacts the fallback below exists to serve.
# Same shape as deploy#563's `OUT="$(fn)"; RC=$?`, which is dead on every failing path.
RESTORE_RUN_TS="$(grep '^BACKUP_MANIFEST ' "${RESTORE_DIR}/_backup_manifest.txt" 2>/dev/null \
    | tr ' ' '\n' | sed -n 's/^timestamp=\(..*\)$/\1/p' | head -1 || true)"

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

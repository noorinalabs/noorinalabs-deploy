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

list_backups() {
    log "INFO" "Available backups in ${B2_BUCKET}:"
    echo ""
    echo "=== Daily ==="
    rclone lsf "${RCLONE_REMOTE}:${B2_BUCKET}/daily/" --dirs-only 2>/dev/null || echo "  (none)"
    echo ""
    echo "=== Weekly ==="
    rclone lsf "${RCLONE_REMOTE}:${B2_BUCKET}/weekly/" --dirs-only 2>/dev/null || echo "  (none)"
}

resolve_latest() {
    # Find the most recent backup across daily and weekly
    local latest=""
    local latest_date="0000-00-00"

    for category in daily weekly; do
        local dirs
        dirs=$(rclone lsf "${RCLONE_REMOTE}:${B2_BUCKET}/${category}/" --dirs-only 2>/dev/null || true)
        while IFS= read -r dir; do
            [[ -z "$dir" ]] && continue
            dir="${dir%/}"
            if [[ "$dir" > "$latest_date" ]]; then
                latest_date="$dir"
                latest="${category}/${dir}"
            fi
        done <<< "$dirs"
    done

    if [[ -z "$latest" ]]; then
        log "ERROR" "No backups found in B2 bucket"
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
    log "INFO" "Terminating active PostgreSQL connections to ${POSTGRES_DB}..."
    docker compose -f "$COMPOSE_FILE" exec -T postgres \
        psql -U "$POSTGRES_USER" -d postgres -c \
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '${POSTGRES_DB}' AND pid <> pg_backend_pid();" \
        2>/dev/null || true
}

restore_postgres() {
    local dump_file="$1"
    local rc out
    log "INFO" "Restoring PostgreSQL from $(basename "$dump_file")..."

    terminate_pg_connections

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
    if docker compose -f "$COMPOSE_FILE" exec -T postgres \
        pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists \
        < "$dump_file" > "$out" 2>&1; then
        rc=0
    else
        rc=$?
    fi

    # Surface pg_restore's own diagnostics regardless of outcome.
    sed 's/^/    /' "$out"

    if [[ $rc -ne 0 ]]; then
        log "ERROR" "pg_restore FAILED (exit ${rc}) — the database may be partially restored"
        rm -f "$out"
        return 1
    fi

    # Defence in depth. If a future edit adds a flag that makes pg_restore exit 0 while
    # still discarding objects, this catches it: pg_restore announces the count itself.
    if grep -qE 'errors ignored on restore: [0-9]+' "$out"; then
        local ignored
        ignored=$(grep -oE 'errors ignored on restore: [0-9]+' "$out" | grep -oE '[0-9]+$' | tail -1)
        log "ERROR" "pg_restore ignored ${ignored} error(s) — treating as a failed restore"
        rm -f "$out"
        return 1
    fi

    rm -f "$out"
    log "INFO" "PostgreSQL restore complete"
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
        --list)
            list_backups
            exit 0
            ;;
        --help|-h)
            echo "Usage: $0 [--force] [--list] <backup-path|latest>"
            echo ""
            echo "  latest              Restore the most recent backup"
            echo "  daily/2026-03-25    Restore a specific backup"
            echo "  --force             Skip confirmation prompt"
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
    echo "Usage: $0 [--force] [--list] <backup-path|latest>"
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

# Find dump files
PG_DUMP=$(find "$RESTORE_DIR" -name 'isnad-pg-*.dump' -type f | head -1)
NEO4J_DUMP=$(find "$RESTORE_DIR" \( -name 'isnad-neo4j-*.dump.zst' -o -name 'isnad-neo4j-*.dump' \) -type f | head -1)

# A backup containing neither dump is not a backup. Previously both `find`s coming
# back empty produced two WARNINGs, "=== Restore complete ===", and exit 0 — an
# empty directory restored "successfully" (deploy#560).
if [[ -z "$PG_DUMP" && -z "$NEO4J_DUMP" ]]; then
    log "ERROR" "Backup contains no PostgreSQL dump and no Neo4j dump — nothing to restore"
    exit 1
fi

PG_RESULT="skipped (no dump)"
NEO4J_RESULT="skipped (no dump)"
FAILED=0

if [[ -n "$PG_DUMP" ]]; then
    if restore_postgres "$PG_DUMP"; then
        PG_RESULT="restored"
    else
        PG_RESULT="FAILED"
        FAILED=1
    fi
else
    log "WARNING" "No PostgreSQL dump found in backup"
fi

if [[ -n "$NEO4J_DUMP" ]]; then
    if restore_neo4j "$NEO4J_DUMP"; then
        NEO4J_RESULT="restored"
    else
        NEO4J_RESULT="FAILED"
        FAILED=1
    fi
else
    log "WARNING" "No Neo4j dump found in backup"
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
log "INFO" "PostgreSQL: ${PG_RESULT}"
log "INFO" "Neo4j:      ${NEO4J_RESULT}"

# The exit status is the whole point: a restore that did not restore must not be
# reported as success. Callers (restore_rehearsal.sh, operators, CI) gate on this.
if [[ "$FAILED" -eq 1 ]]; then
    exit 1
fi

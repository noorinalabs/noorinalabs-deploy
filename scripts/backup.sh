#!/usr/bin/env bash
# =============================================================================
# noorinalabs-isnad-graph automated backup script
# Dumps PostgreSQL and Neo4j databases, compresses, checksums, and uploads
# to Backblaze B2 via rclone. Manages retention (7 daily + 4 weekly).
#
# Required environment variables:
#   B2_KEY_ID       — Backblaze B2 application key ID
#   B2_APP_KEY      — Backblaze B2 application key
#   B2_BUCKET       — Backblaze B2 bucket name
#
# Optional environment variables:
#   POSTGRES_USER   — PostgreSQL user (default: isnad)
#   POSTGRES_DB     — PostgreSQL database (default: isnad_graph)
#   USER_POSTGRES_USER — user-service PostgreSQL user (default: noorina_user)
#   USER_POSTGRES_DB   — user-service database (default: noorina_users)
#
# Datastore coverage (deploy#559). The full enumeration, including WHY each store is
# or is not dumped, lives in docs/DATASTORES.md. Summary:
#
#   dumped here:  neo4j (graph), postgres (isnad relational + pgvector),
#                 user-postgres (accounts, sessions, RBAC, audit_log)
#   deliberately NOT dumped: redis, user-redis, prometheus, loki, kafka, caddy,
#                 grafana, neo4j_logs, loki_runtime, st_model_cache
#
# `user-postgres` is the only store here whose contents cannot be reconstructed from
# the published pipeline artifact. The artifact rebuilds the graph; it does not rebuild
# the users. It had NO backup coverage at all before deploy#559.
#   COMPOSE_FILE    — Docker Compose file (default: compose/docker-compose.prod.yml,
#                     resolved relative to /opt/noorinalabs-deploy/)
#   BACKUP_DIR      — Local backup staging root (default: /var/lib/noorinalabs-backups,
#                     a persistent path managed via tmpfiles.d to survive reboots —
#                     see deploy#121 Bug A for the /tmp/-namespace failure this avoids)
#   DAILY_RETAIN    — Number of daily backups to keep (default: 7)
#   WEEKLY_RETAIN   — Number of weekly backups to keep (default: 4)
#   DRY_RUN         — Set to "true" to show what would be pruned without deleting
#
# Usage:
#   ./scripts/backup.sh
#   DRY_RUN=true ./scripts/backup.sh
# =============================================================================
set -euo pipefail

# Restrict file permissions — backups contain sensitive database dumps
umask 077

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
POSTGRES_USER="${POSTGRES_USER:-isnad}"
POSTGRES_DB="${POSTGRES_DB:-isnad_graph}"
# Defaults match the live stg/prod values (`user_service` for both). compose declares
# both as `:?`-required, so in practice they always arrive via the systemd
# EnvironmentFile; the defaults exist so an operator running this by hand does not
# silently dump the wrong database.
USER_POSTGRES_USER="${USER_POSTGRES_USER:-user_service}"
USER_POSTGRES_DB="${USER_POSTGRES_DB:-user_service}"
COMPOSE_FILE="${COMPOSE_FILE:-compose/docker-compose.prod.yml}"
BACKUP_DIR="${BACKUP_DIR:-/var/lib/noorinalabs-backups}"
DAILY_RETAIN="${DAILY_RETAIN:-7}"
WEEKLY_RETAIN="${WEEKLY_RETAIN:-4}"
DRY_RUN="${DRY_RUN:-false}"

TIMESTAMP="$(date -u +%Y%m%d-%H%M%S)"
DATE_STAMP="$(date -u +%Y-%m-%d)"
DAY_OF_WEEK="$(date -u +%u)"  # 1=Monday, 7=Sunday

# Backup category: weekly on Sundays, daily otherwise
if [[ "$DAY_OF_WEEK" -eq 7 ]]; then
    BACKUP_CATEGORY="weekly"
else
    BACKUP_CATEGORY="daily"
fi

BACKUP_SUBDIR="${BACKUP_CATEGORY}/${DATE_STAMP}"
LOCAL_BACKUP_PATH="${BACKUP_DIR}/${BACKUP_SUBDIR}"

# rclone remote name (configured via env vars)
RCLONE_REMOTE="isnad"

# Required environment variables
: "${B2_KEY_ID:?B2_KEY_ID must be set}"
: "${B2_APP_KEY:?B2_APP_KEY must be set}"
: "${B2_BUCKET:?B2_BUCKET must be set}"

# Export rclone native env vars for credential-safe operation (no CLI flags)
export RCLONE_CONFIG_ISNAD_TYPE="b2"
export RCLONE_CONFIG_ISNAD_ACCOUNT="${B2_KEY_ID}"
export RCLONE_CONFIG_ISNAD_KEY="${B2_APP_KEY}"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
# Log file lives under LOCAL_BACKUP_PATH (the per-run dir) so cleanup() removes
# it along with the dump artifacts. Long-term log retention is the journal's
# job — under systemd, StandardOutput=journal already captures everything.
mkdir -p "$LOCAL_BACKUP_PATH"
LOG_FILE="${LOCAL_BACKUP_PATH}/backup-${TIMESTAMP}.log"

log() {
    local level="$1"
    shift
    local msg
    msg="$(date -u +%Y-%m-%dT%H:%M:%SZ) [${level}] $*"
    echo "$msg" | tee -a "$LOG_FILE"
}

# ---------------------------------------------------------------------------
# node-exporter textfile-collector metrics (deploy#565)
# ---------------------------------------------------------------------------
# Until deploy#565 this script emitted no metric at all, so the BackupFailure
# alert — which keyed off a success gauge nothing wrote — could never fire, and
# "no backup has ever run" read as healthy for months. The success gauge is the
# positive signal that makes staleness measurable; its ABSENCE is what
# BackupNeverSucceeded alerts on (infra/prometheus/alerts.yml § backup).
#
# The directory below is the one node-exporter is actually pointed at
# (`--collector.textfile.directory`, compose/docker-compose.prod.yml) and is the
# only path bind-mounted into the container.
TEXTFILE_DIR="/var/lib/node_exporter/textfile_collector"
SUCCESS_TEXTFILE="${TEXTFILE_DIR}/isnad_backup_success.prom"
FAILURE_TEXTFILE="${TEXTFILE_DIR}/isnad_backup_failure.prom"

# Emitted ONLY on a fully-successful run (every dump OK + upload OK). A partial
# backup must not advance the success timestamp: doing so would silence
# BackupStale while the thing it guards — a complete, restorable backup — did
# not happen.
emit_success_metric() {
    local now_epoch tmp
    now_epoch="$(date -u +%s)"

    install -d -m 0755 "$TEXTFILE_DIR"

    # Atomic write: temp + rename, so Prometheus never scrapes a half-written
    # file. The mktemp suffix means the temp name does NOT end in `.prom`, so
    # the collector ignores it until the rename lands.
    tmp="$(mktemp "${SUCCESS_TEXTFILE}.XXXXXX")"
    cat > "$tmp" <<EOF
# HELP isnad_backup_last_success_timestamp_seconds Unix timestamp of the most recent fully-successful isnad-backup run.
# TYPE isnad_backup_last_success_timestamp_seconds gauge
isnad_backup_last_success_timestamp_seconds ${now_epoch}
EOF
    # This script runs under `umask 077` (set above), which would leave the file
    # 0600 and root-owned. node-exporter reads it as an unprivileged uid through
    # a read-only bind mount, so without an explicit chmod the metric would be
    # written and never scraped — the same class of silent gap deploy#565 fixes.
    chmod 644 "$tmp"
    mv "$tmp" "$SUCCESS_TEXTFILE"

    # A success supersedes any prior failure marker. Removing it lets BackupFailed
    # resolve on the next scrape instead of lingering for its full 24h window.
    rm -f "$FAILURE_TEXTFILE"

    log "INFO" "Emitted success metric: ${SUCCESS_TEXTFILE} (ts=${now_epoch})"
}

# ---------------------------------------------------------------------------
# Cleanup handler
# ---------------------------------------------------------------------------
cleanup() {
    local exit_code=$?
    # Order matters: emit our last log lines BEFORE rm-ing the directory the
    # log file lives in, otherwise tee fails silently with "No such file or
    # directory" and we lose the cleanup confirmation.
    if [[ $exit_code -ne 0 ]]; then
        log "ERROR" "Backup script exited with code ${exit_code}"
    fi
    if [[ -n "${LOCAL_BACKUP_PATH:-}" && -d "$LOCAL_BACKUP_PATH" ]]; then
        log "INFO" "Cleaning up per-run staging directory: ${LOCAL_BACKUP_PATH}"
        # Remove only the per-run staging directory (LOCAL_BACKUP_PATH), never the
        # persistent BACKUP_DIR root. BACKUP_DIR is now /var/lib/noorinalabs-backups,
        # provisioned by tmpfiles.d and shared across runs — wiping it would also
        # destroy the tmpfiles.d-managed permissions/ownership we rely on.
        rm -rf "$LOCAL_BACKUP_PATH"
    fi
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Preflight checks
# ---------------------------------------------------------------------------
for cmd in docker rclone zstd sha256sum; do
    if ! command -v "$cmd" &>/dev/null; then
        log "ERROR" "Required command not found: ${cmd}"
        exit 1
    fi
done

if ! docker compose -f "$COMPOSE_FILE" ps --format json &>/dev/null; then
    log "ERROR" "Cannot reach Docker Compose services (is Docker running?)"
    exit 1
fi

log "INFO" "=== Backup started (${BACKUP_CATEGORY}) ==="
log "INFO" "Timestamp: ${TIMESTAMP}"
log "INFO" "Local staging: ${LOCAL_BACKUP_PATH}"
log "INFO" "Remote target: ${RCLONE_REMOTE}:${B2_BUCKET}/${BACKUP_SUBDIR}"

PG_OK=false
USER_PG_OK=false
NEO4J_OK=false

# ---------------------------------------------------------------------------
# 1. PostgreSQL dumps (isnad + user-service)
# ---------------------------------------------------------------------------
# dump_postgres <compose-service> <user> <db> <outfile> <label>
# Echoes nothing; returns 0 on a non-empty dump, 1 otherwise.
dump_postgres() {
    local service="$1" pg_user="$2" pg_db="$3" outfile="$4" label="$5"

    log "INFO" "Starting ${label} dump (service=${service} db=${pg_db})..."
    if ! docker compose -f "$COMPOSE_FILE" exec -T "$service" \
        pg_dump -U "$pg_user" -d "$pg_db" --format=custom \
        > "$outfile" 2>>"$LOG_FILE"; then
        log "ERROR" "${label} dump failed"
        rm -f "$outfile"
        return 1
    fi

    local size
    size=$(stat -c%s "$outfile" 2>/dev/null || stat -f%z "$outfile" 2>/dev/null)
    # A zero-byte dump is the failure mode that produces a backup you cannot restore
    # from and only discover during an incident. pg_dump can exit 0 having written
    # nothing if its stdout redirection failed.
    if [[ "${size:-0}" -le 0 ]]; then
        log "ERROR" "${label} dump produced empty file"
        rm -f "$outfile"
        return 1
    fi

    log "INFO" "${label} dump complete: $(du -h "$outfile" | cut -f1)"
    return 0
}

PG_DUMP_FILE="${LOCAL_BACKUP_PATH}/isnad-pg-${TIMESTAMP}.dump"
if dump_postgres postgres "$POSTGRES_USER" "$POSTGRES_DB" "$PG_DUMP_FILE" "PostgreSQL (isnad)"; then
    PG_OK=true
fi

# user-postgres holds accounts, roles, sessions, oauth_accounts, subscriptions,
# totp_secrets and the audit_log relocated out of Neo4j on 2026-06-30. None of it is
# reconstructible from the published pipeline artifact — that rebuilds the graph, not
# the users. It had no backup coverage of any kind before deploy#559.
USER_PG_DUMP_FILE="${LOCAL_BACKUP_PATH}/isnad-userpg-${TIMESTAMP}.dump"
if dump_postgres user-postgres "$USER_POSTGRES_USER" "$USER_POSTGRES_DB" \
    "$USER_PG_DUMP_FILE" "PostgreSQL (user-service)"; then
    USER_PG_OK=true
fi

# ---------------------------------------------------------------------------
# 2. Neo4j dump (stop → dump → restart)
# ---------------------------------------------------------------------------
NEO4J_DUMP_FILE="${LOCAL_BACKUP_PATH}/isnad-neo4j-${TIMESTAMP}.dump"
NEO4J_COMPRESSED="${NEO4J_DUMP_FILE}.zst"

log "INFO" "Stopping Neo4j for offline dump..."
docker compose -f "$COMPOSE_FILE" stop neo4j 2>>"$LOG_FILE"

# Wait for Neo4j container to fully stop
MAX_WAIT=30
WAITED=0
while docker compose -f "$COMPOSE_FILE" ps --format '{{.Service}}:{{.State}}' 2>/dev/null | grep -q "neo4j:running"; do
    if [[ $WAITED -ge $MAX_WAIT ]]; then
        log "ERROR" "Neo4j did not stop within ${MAX_WAIT}s"
        docker compose -f "$COMPOSE_FILE" up -d neo4j 2>>"$LOG_FILE"
        break
    fi
    sleep 1
    WAITED=$((WAITED + 1))
done

if [[ $WAITED -lt $MAX_WAIT ]]; then
    log "INFO" "Neo4j stopped (waited ${WAITED}s). Running dump..."

    # Resolve the data volume from THIS compose project's neo4j container rather than
    # by grepping every volume on the host. `docker volume ls | grep neo4j_data` matches
    # across all compose projects, so on a box running more than one stack the backup
    # could silently dump a different project's graph and be restored over the real one.
    # (deploy#559, same resolution as restore.sh restore_neo4j().)
    NEO4J_CID=$(docker compose -f "$COMPOSE_FILE" ps -aq neo4j 2>/dev/null | head -1)
    if [[ -z "$NEO4J_CID" ]]; then
        NEO4J_VOLUME=""
    else
        NEO4J_VOLUME=$(docker inspect \
            --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}' \
            "$NEO4J_CID")
    fi
    if [[ -z "$NEO4J_VOLUME" ]]; then
        log "ERROR" "Cannot resolve the Neo4j data volume for ${COMPOSE_FILE} — refusing to guess"
    else
        log "INFO" "Resolved Neo4j data volume: ${NEO4J_VOLUME}"
        # Use bare docker run (not compose run) to avoid service config conflicts.
        #
        # `--user 0:0 --entrypoint neo4j-admin` is required, not cosmetic. The
        # neo4j:5-community entrypoint drops privileges to neo4j(7474) even when the
        # container starts as root. BACKUP_DIR is 0700 root:root (tmpfiles.d), and this
        # script runs as root under systemd, so the dropped user cannot write the
        # /backups bind mount: `neo4j-admin` dies with "AccessDeniedException: /backups"
        # and the Neo4j leg of every backup fails. Measured on neo4j:5-community during
        # deploy#560: entrypoint path -> uid 7474 -> exit 1; bypass -> uid 0 ->
        # "Dump completed successfully". Same fix as restore.sh restore_neo4j().
        if docker run --rm \
            --user 0:0 \
            --entrypoint neo4j-admin \
            -v "${NEO4J_VOLUME}:/data" \
            -v "${LOCAL_BACKUP_PATH}:/backups" \
            neo4j:5-community \
            database dump neo4j --to-path=/backups/ 2>>"$LOG_FILE"; then

            # The dump command outputs to /backups/neo4j.dump — rename it
            if [[ -f "${LOCAL_BACKUP_PATH}/neo4j.dump" ]]; then
                mv "${LOCAL_BACKUP_PATH}/neo4j.dump" "$NEO4J_DUMP_FILE"
            fi

            if [[ -f "$NEO4J_DUMP_FILE" ]]; then
                log "INFO" "Neo4j dump complete: $(du -h "$NEO4J_DUMP_FILE" | cut -f1)"

                # Compress with zstd
                log "INFO" "Compressing Neo4j dump with zstd..."
                zstd -3 --rm "$NEO4J_DUMP_FILE" -o "$NEO4J_COMPRESSED" 2>>"$LOG_FILE"
                log "INFO" "Compressed: $(du -h "$NEO4J_COMPRESSED" | cut -f1)"
                NEO4J_OK=true
            else
                log "ERROR" "Neo4j dump file not found after dump command"
            fi
        else
            log "ERROR" "Neo4j dump command failed"
        fi
    fi

    # Always restart Neo4j
    log "INFO" "Restarting Neo4j..."
    docker compose -f "$COMPOSE_FILE" up -d neo4j 2>>"$LOG_FILE"

    # Wait for Neo4j to become healthy
    MAX_HEALTH_WAIT=120
    HEALTH_WAITED=0
    while ! docker compose -f "$COMPOSE_FILE" ps --format '{{.Service}}:{{.Health}}' 2>/dev/null | grep -q "neo4j:healthy"; do
        if [[ $HEALTH_WAITED -ge $MAX_HEALTH_WAIT ]]; then
            log "WARNING" "Neo4j did not become healthy within ${MAX_HEALTH_WAIT}s — check manually"
            break
        fi
        sleep 5
        HEALTH_WAITED=$((HEALTH_WAITED + 5))
    done

    if [[ $HEALTH_WAITED -lt $MAX_HEALTH_WAIT ]]; then
        log "INFO" "Neo4j healthy (waited ${HEALTH_WAITED}s)"
    fi
else
    log "WARNING" "Skipped Neo4j dump due to stop timeout"
fi

# ---------------------------------------------------------------------------
# 3. Generate SHA256 checksums
# ---------------------------------------------------------------------------
log "INFO" "Generating SHA256 checksums..."
for f in "$LOCAL_BACKUP_PATH"/isnad-*; do
    [[ -f "$f" ]] || continue
    sha256sum "$f" | sed "s|${LOCAL_BACKUP_PATH}/||" > "${f}.sha256"
    log "INFO" "Checksum: $(basename "${f}.sha256")"
done

# ---------------------------------------------------------------------------
# 4. Upload to Backblaze B2
# ---------------------------------------------------------------------------
if [[ "$PG_OK" == "false" && "$USER_PG_OK" == "false" && "$NEO4J_OK" == "false" ]]; then
    log "ERROR" "All dumps failed — nothing to upload"
    exit 1
fi

if [[ "$PG_OK" == "false" || "$USER_PG_OK" == "false" || "$NEO4J_OK" == "false" ]]; then
    # Upload what we have — a partial backup beats none — but the run still exits
    # non-zero below, so the systemd OnFailure marker fires and the operator is told.
    log "WARNING" "Partial backup — uploading available dumps only"
fi

# A user-postgres failure is called out separately because it is the one store whose
# contents cannot be rebuilt from the pipeline artifact.
if [[ "$USER_PG_OK" == "false" ]]; then
    log "ERROR" "user-postgres dump FAILED — accounts, sessions and audit_log are NOT in this backup"
fi

log "INFO" "Uploading to B2: ${RCLONE_REMOTE}:${B2_BUCKET}/${BACKUP_SUBDIR}/"
if rclone copy "$LOCAL_BACKUP_PATH" "${RCLONE_REMOTE}:${B2_BUCKET}/${BACKUP_SUBDIR}/" \
    --log-level INFO 2>>"$LOG_FILE"; then
    log "INFO" "Upload complete"
else
    log "ERROR" "Upload to B2 failed"
    exit 1
fi

# ---------------------------------------------------------------------------
# 5. Retention pruning (deterministic, date-stamped directory based)
# ---------------------------------------------------------------------------
log "INFO" "Running retention pruning..."

prune_old_backups() {
    local category="$1"
    local retain_days="$2"
    local cutoff_epoch
    cutoff_epoch=$(date -u -d "${retain_days} days ago" +%s 2>/dev/null) || \
    cutoff_epoch=$(date -u -v-"${retain_days}"d +%s 2>/dev/null)

    # List date-stamped directories under the category
    local dirs
    dirs=$(rclone lsf "${RCLONE_REMOTE}:${B2_BUCKET}/${category}/" --dirs-only 2>/dev/null || true)

    while IFS= read -r dir; do
        [[ -z "$dir" ]] && continue
        # Strip trailing slash
        dir="${dir%/}"

        # Parse date from directory name (YYYY-MM-DD)
        if [[ "$dir" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
            local dir_epoch
            dir_epoch=$(date -u -d "$dir" +%s 2>/dev/null) || \
            dir_epoch=$(date -u -j -f "%Y-%m-%d" "$dir" +%s 2>/dev/null) || continue

            if [[ "$dir_epoch" -lt "$cutoff_epoch" ]]; then
                if [[ "$DRY_RUN" == "true" ]]; then
                    log "INFO" "[DRY RUN] Would prune: ${category}/${dir}/"
                else
                    log "INFO" "Pruning: ${category}/${dir}/"
                    rclone purge "${RCLONE_REMOTE}:${B2_BUCKET}/${category}/${dir}/" 2>>"$LOG_FILE" || \
                        log "WARNING" "Failed to prune ${category}/${dir}/"
                fi
            fi
        fi
    done <<< "$dirs"
}

prune_old_backups "daily" "$DAILY_RETAIN"
prune_old_backups "weekly" "$((WEEKLY_RETAIN * 7))"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
log "INFO" "=== Backup summary ==="
log "INFO" "PostgreSQL (isnad):        $(if $PG_OK; then echo "OK"; else echo "FAILED"; fi)"
log "INFO" "PostgreSQL (user-service): $(if $USER_PG_OK; then echo "OK"; else echo "FAILED"; fi)"
log "INFO" "Neo4j:                     $(if $NEO4J_OK; then echo "OK"; else echo "FAILED"; fi)"
log "INFO" "Category:   ${BACKUP_CATEGORY}"
log "INFO" "Remote:     ${RCLONE_REMOTE}:${B2_BUCKET}/${BACKUP_SUBDIR}/"
log "INFO" "=== Backup finished ==="

# A partial backup is a failed backup: exit non-zero WITHOUT emitting the
# success gauge, so isnad-backup.service's `OnFailure=` fires the failure marker
# and BackupStale keeps counting from the last COMPLETE backup (deploy#565).
#
# `USER_PG_OK` belongs in this condition, not just alongside it. The success gauge is
# what the alerts treat as "a complete, restorable backup exists". If a run that failed
# to dump user-postgres still emitted it, the one store that cannot be rebuilt from the
# pipeline artifact could be missing from every backup we hold and the dashboards would
# stay green — the exact silence deploy#565 closed, reopened through the leg deploy#559
# adds.
if [[ "$PG_OK" == "false" || "$USER_PG_OK" == "false" || "$NEO4J_OK" == "false" ]]; then
    log "ERROR" "Partial backup — success metric NOT emitted"
    exit 1
fi

emit_success_metric

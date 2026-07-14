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
#   BACKUP_PREFIX   — Environment namespace for the REMOTE path ("stg" | "prod").
#                     stg and prod share one bucket (BACKUP_B2_BUCKET is a single
#                     repo-level secret), so without this both write into
#                     `daily/<date>/` and prod's retention purge DELETES stg's
#                     backups — and vice versa. Deliberately has NO DEFAULT: an
#                     empty default silently restores the shared prefix, and a
#                     default of "prod" would be catastrophic on stg. An
#                     un-deployed host must fail LOUDLY, not collide quietly.
#                     Injected by the deploy workflows via `extra_env`. deploy#632.
#
# Optional environment variables:
#   POSTGRES_USER      — isnad PostgreSQL user (default: isnad)
#   POSTGRES_DB        — isnad PostgreSQL database (default: isnad_graph)
#   USER_POSTGRES_USER — user-service PostgreSQL user (default: user_service)
#   USER_POSTGRES_DB   — user-service database (default: user_service)
#   COMPOSE_FILE       — Docker Compose file (default: compose/docker-compose.prod.yml,
#                        resolved relative to /opt/noorinalabs-deploy/)
#   COMPOSE_PROJECT    — Docker Compose PROJECT the stack runs in (default: noorinalabs).
#                        Passed as an explicit `-p` on every compose call; `-f` alone names
#                        the file, NOT the project. See scripts/compose_project.sh
#                        (deploy#617) — an unflagged call addresses a phantom project.
#   BACKUP_DIR         — Local backup staging root (default: /var/lib/noorinalabs-backups,
#                        a persistent path managed via tmpfiles.d to survive reboots —
#                        see deploy#121 Bug A for the /tmp/-namespace failure this avoids)
#   DAILY_RETAIN       — Number of daily backups to keep (default: 7)
#   WEEKLY_RETAIN      — Number of weekly backups to keep (default: 4)
#   DRY_RUN            — Set to "true" to show what would be pruned without deleting
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
: "${BACKUP_PREFIX:?BACKUP_PREFIX must be set (stg|prod) — the B2 remote path is namespaced per environment so that prod cannot purge the stg backups. See deploy#632.}"

# A prefix carrying a slash or a `..` would escape its namespace and reach into the
# other environment — the exact thing the prefix exists to prevent. Refuse rather
# than sanitize: there is no legitimate value outside this charset.
if [[ ! "$BACKUP_PREFIX" =~ ^[a-z0-9][a-z0-9-]*$ ]]; then
    echo "FATAL: BACKUP_PREFIX='${BACKUP_PREFIX}' is not a bare name ([a-z0-9][a-z0-9-]*)." >&2
    echo "       A prefix containing '/' or '..' escapes its environment namespace." >&2
    exit 1
fi

# The REMOTE path is namespaced; the LOCAL one is not. BACKUP_DIR is already
# per-host, so a prefix there would buy nothing and would churn restore.sh's
# local-run selection for no reason. Only the shared bucket needs the namespace.
REMOTE_SUBDIR="${BACKUP_PREFIX}/${BACKUP_SUBDIR}"

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
# Compose project scoping (deploy#617)
# ---------------------------------------------------------------------------
# Sourced AFTER log(): the library's guards report through it. Provides
# COMPOSE_PROJECT, dc(), assert_stack_present() and neo4j_start(). Every compose
# call below goes through dc() — `-f` names the file, `-p` names the project, and
# without the latter this script addresses a project with nothing in it.
COMPOSE_PROJECT_LIB="$(dirname "${BASH_SOURCE[0]}")/compose_project.sh"
if [[ ! -f "$COMPOSE_PROJECT_LIB" ]]; then
    log "ERROR" "Missing ${COMPOSE_PROJECT_LIB} — cannot resolve the compose project to back up"
    exit 1
fi
# shellcheck source=scripts/compose_project.sh
source "$COMPOSE_PROJECT_LIB"

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
# Overridable ONLY so a test can observe what the run leaves on the box. The default is the
# real path and the unit grants it (ReadWritePaths=/var/lib/node_exporter); nothing in
# production sets this. It matters because the interesting assertion here is about the
# textfiles themselves — the success gauge AND the failure marker `emit_success_metric`
# deletes (:171) — and with the path hardcoded, a sandboxed run dies at `install -d` with
# "Permission denied" and exits non-zero for a reason that has nothing to do with the
# behaviour under test. That is a measurement of the sandbox, not of the script.
TEXTFILE_DIR="${TEXTFILE_DIR:-/var/lib/node_exporter/textfile_collector}"
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
    #
    # The parent is named EXPLICITLY (`-p`), not implied by a template that
    # happens to carry a path. Both land in the same directory, but only one of
    # them is checkable: in a hardened script every mktemp names its parent, so
    # the guard in test_scratch_under_hardening.py needs no escape hatch to let
    # this line through — and an escape hatch is exactly what let deploy#613
    # back in (#624 review, Nino Kavtaradze).
    tmp="$(mktemp -p "$(dirname "$SUCCESS_TEXTFILE")" "$(basename "$SUCCESS_TEXTFILE").XXXXXX")"
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

# Verify the B2 credential BEFORE stopping Neo4j and dumping gigabytes. Discovering at
# upload time that the key cannot write means we took an outage for nothing.
#
# The preflight classifies by capability probe rather than by rclone's error text,
# because rclone's text does not identify the failure: a read-only key's 401 surfaces as
# "failed to create bucket", and a missing bucket is indistinguishable from a
# wrongly-scoped key. See scripts/b2_preflight.sh. (deploy#559)
PREFLIGHT="$(dirname "${BASH_SOURCE[0]}")/b2_preflight.sh"
if [[ -f "$PREFLIGHT" ]]; then
    # shellcheck source=scripts/b2_preflight.sh
    source "$PREFLIGHT"

    # `PREFLIGHT_OUT="$(preflight_b2 2>&1)"` followed by `PREFLIGHT_RC=$?` looks right and
    # is dead code on every failing path. This script runs under `set -euo pipefail`, and
    # an assignment whose command substitution exits non-zero IS a failing simple command:
    # errexit fires AT THE ASSIGNMENT. Nothing below it runs. The operator saw exactly one
    # line, from the EXIT trap — "Backup script exited with code 1" — and never the verdict
    # or its remediation. The guard that exists so nobody chases rclone's lying
    # "failed to create bucket" was, in the only place it runs, silent.
    #
    # Measured (bash 5.2.21): failing preflight under `set -e`
    #   before: exit 1, stdout has no "verdict=" and no "B2 preflight failed"
    #   after:  exit 1, stdout carries the full verdict + remediation
    #
    # `|| PREFLIGHT_RC=$?` puts the assignment in a condition context, which suspends
    # errexit for it. The inner `set +e` additionally protects the probes: without it, a
    # caller with `shopt -s inherit_errexit` would kill preflight_b2 at the first failing
    # `rclone` call, before it could compute a verdict at all.
    PREFLIGHT_RC=0
    PREFLIGHT_OUT="$(set +e; preflight_b2 2>&1)" || PREFLIGHT_RC=$?
    printf '%s\n' "$PREFLIGHT_OUT" | tee -a "$LOG_FILE"
    if [[ "$PREFLIGHT_RC" -ne 0 ]]; then
        log "ERROR" "B2 preflight failed — refusing to dump databases we cannot upload"
        exit 1
    fi
else
    log "ERROR" "Missing ${PREFLIGHT} — cannot verify B2 credentials before dumping"
    exit 1
fi

# Assert we are addressing the REAL project and not a phantom one.
#
# This REPLACES `docker compose ps --format json &>/dev/null`, which exited 0 on an empty or
# entirely nonexistent project: it asked whether `ps` RAN, not whether the stack EXISTS.
# That zero is what waved the deploy#617 run through to the dumps, where both pg_dumps
# failed against a project with no containers and the Neo4j leg CREATED one.
#
# ---------------------------------------------------------------------------
# WHY `postgres user-postgres` AND NOT ALSO `neo4j`. (deploy#618 review, Weronika Zielinska)
# ---------------------------------------------------------------------------
# The first version of this gate demanded all three, and that QUIETLY REPEALED the contract
# 270 lines below it — "a partial backup beats none" (`:5xx`, which refuses to upload only
# when ALL THREE dumps fail). On any night Neo4j is down, an all-three gate takes ZERO
# backups instead of two, and one of the two it discards is `user-postgres`: the only store
# here that CANNOT be rebuilt from the published pipeline artifact (deploy#559). Worse, this
# script can itself leave Neo4j stopped (see the `neo4j_start` failure branch), so one bad
# night would have silently disarmed every backup after it.
#
# The Postgres pair is the reliable witness of project identity because NOTHING IN THIS PATH
# CAN CREATE IT. Only `neo4j` was ever `up`'d, which is why a stray, empty `neo4j` — and no
# Postgres — is precisely the state stg was found in. A "refuse only when zero services
# resolve" rule would have passed that state and dumped the stray empty graph, which is the
# silently-empty success this whole change exists to prevent. Requiring both Postgres
# services refuses the phantom project in every observed state, stray Neo4j included.
#
# A missing Neo4j is a PER-LEG failure, handled at the Neo4j leg: skip the dump, record
# NEO4J_OK=false, upload the Postgres stores with `complete=false`, exit non-zero.
#
# ORDER: after the B2 preflight, before the first dump. The credential check stays the first
# thing that can stop a run (deploy#559/#613 — "discovering at upload time that the key
# cannot write means we took an outage for nothing"), and this now stands between it and any
# destructive step. Nothing has been stopped or dumped at this point; both are pure
# preflights, and both must pass.
if ! assert_stack_present postgres user-postgres; then
    log "ERROR" "Refusing to back up a stack that is not present."
    exit 1
fi

log "INFO" "=== Backup started (${BACKUP_CATEGORY}) ==="
log "INFO" "Timestamp: ${TIMESTAMP}"
log "INFO" "Local staging: ${LOCAL_BACKUP_PATH}"
log "INFO" "Remote target: ${RCLONE_REMOTE}:${B2_BUCKET}/${REMOTE_SUBDIR}"

PG_OK=false
USER_PG_OK=false
NEO4J_OK=false

# Did THIS RUN leave the graph down? (deploy#617 review — Aisha Idrissi)
#
# Deliberately SEPARATE from NEO4J_OK, and the distinction is the whole point:
#
#   NEO4J_OK   — is the ARTIFACT good?  (did the dump succeed and get uploaded)
#   NEO4J_DOWN — is the HOST  good?     (did we put the graph back the way we found it)
#
# They are independent, and the interesting case is exactly the one where they disagree: a
# run that dumps all three stores perfectly and then FAILS TO RESTART NEO4J has produced a
# complete, restorable backup AND taken production down.
#
# Before this flag, that run reported plain SUCCESS. NEO4J_OK was set true at the dump; the
# restart-failure branch logged "the graph is DOWN" and set nothing; the final gate read
# only the three _OK flags, so it did not fire; the script exited 0. systemd therefore
# marked the unit SUCCEEDED, `OnFailure=isnad-backup-failure-marker.service` never fired,
# and emit_success_metric's `rm -f` of the failure textfile meant THE BACKUP THAT TOOK THE
# GRAPH DOWN CLEARED ITS OWN FAILURE MARKER. The operator's only remaining signal was a
# generic ServiceDown page with nothing to say the backup had caused it.
#
# The fix is NOT to fold this into NEO4J_OK. That would suppress the success gauge — hiding
# a backup that is genuinely complete and restorable, and letting BackupStale fire falsely,
# the alert-fatigue trap deploy#559/#565 closed. BOTH facts must reach the box, because both
# are TRUE: *the last complete backup is at T* (the gauge) and *the last run broke something
# and needs a human* (non-zero exit -> OnFailure -> failure marker).
NEO4J_DOWN=false

# ---------------------------------------------------------------------------
# 1. PostgreSQL dumps (isnad + user-service)
# ---------------------------------------------------------------------------
# dump_postgres <compose-service> <user> <db> <outfile> <label>
# Echoes nothing; returns 0 on a non-empty dump, 1 otherwise.
dump_postgres() {
    local service="$1" pg_user="$2" pg_db="$3" outfile="$4" label="$5"

    log "INFO" "Starting ${label} dump (service=${service} db=${pg_db})..."
    if ! dc exec -T "$service" \
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

# ---------------------------------------------------------------------------
# IS THERE A NEO4J TO DUMP AT ALL? (deploy#618 review)
# ---------------------------------------------------------------------------
# A missing or stopped Neo4j is a PER-LEG failure, not a reason to abandon the run. The
# project-identity gate above deliberately does not demand `neo4j` — demanding it would take
# ZERO backups on a night Neo4j is down, instead of the two this script was designed to
# still take, and one of those two is `user-postgres`, which no artifact can rebuild
# (deploy#559). "A partial backup beats none" is the contract; an arity choice in a preflight
# must not silently repeal it.
#
# So: skip the leg, record it as FAILED (not skipped-and-forgiven), and let the manifest and
# the exit code carry the truth. NEO4J_OK stays false, the manifest attests complete=false,
# `restore.sh` refuses that artifact without `--allow-partial`, and the run exits non-zero so
# the systemd OnFailure marker fires and BackupStale keeps counting from the last COMPLETE
# backup. Nothing here silently forgives a missing store.
# `|| NEO4J_PROBE_RC=$?`, never a bare `if service_is_running neo4j`: we need to tell rc=1
# (the graph is genuinely down) from rc=2 (we could not find out) — see compose_project.sh.
# The `||` also keeps errexit off our backs; a bare assignment from a failing command would
# abort the run here (deploy#563).
NEO4J_PROBE_RC=0
service_is_running neo4j || NEO4J_PROBE_RC=$?

if [[ "$NEO4J_PROBE_RC" -eq 2 ]]; then
    # THE INSTRUMENT FAILED — WE KNOW NOTHING ABOUT THE GRAPH. (deploy#624 review)
    #
    # running_services() has already printed the honest diagnostic (no writable scratch;
    # check ProtectSystem=strict and free disk). Do NOT talk over it with a claim about
    # Neo4j. The old code did exactly that — it said "Neo4j is NOT running — Start Neo4j and
    # re-run", while the graph was UP and the disk was FULL, which makes re-running the one
    # action that makes things worse.
    #
    # This is reachable precisely BECAUSE the scratch now lives on BACKUP_DIR: the gate above
    # runs before the Postgres dumps, this runs after them, and the volume they just filled is
    # the volume the scratch needs.
    NEO4J_OK=false
    log "ERROR" "Could not determine whether Neo4j is running — the check itself failed (see above)."
    log "ERROR" "  This says NOTHING about the graph: do not start, stop, or re-run on the"
    log "ERROR" "  strength of it. Fix the scratch problem named above (free disk first)."
    log "ERROR" "  The Neo4j leg is SKIPPED; the Postgres stores are still dumped."
elif [[ "$NEO4J_PROBE_RC" -ne 0 ]]; then
    log "ERROR" "Neo4j is NOT running in project '${COMPOSE_PROJECT}' — SKIPPING the Neo4j dump."
    log "ERROR" "  The graph leg of this backup will be MISSING and the run will exit non-zero."
    log "ERROR" "  The Postgres stores are still dumped: a partial backup beats none, and"
    log "ERROR" "  user-postgres cannot be rebuilt from any artifact (deploy#559)."
    log "ERROR" "  Start Neo4j and re-run to get a COMPLETE backup."
    NEO4J_OK=false
else
    log "INFO" "Stopping Neo4j for offline dump..."
    dc stop neo4j 2>>"$LOG_FILE"

    # Wait for Neo4j container to fully stop
    MAX_WAIT=30
    WAITED=0
    while dc ps --format '{{.Service}}:{{.State}}' 2>/dev/null | grep -q "neo4j:running"; do
        if [[ $WAITED -ge $MAX_WAIT ]]; then
            log "ERROR" "Neo4j did not stop within ${MAX_WAIT}s"
            # `|| log …` used to SWALLOW the return code here: the restart failed, the run
            # logged a line, and exited 0 with the graph down. Set the flag instead.
            if ! neo4j_start; then
                NEO4J_DOWN=true
                log "ERROR" "Neo4j restart failed after a stop timeout — the graph is DOWN. Start it by hand."
            fi
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
        # `|| NEO4J_CID=""` is not belt-and-braces here — it is the difference between a
        # degraded backup and a DOWN DATABASE (deploy#622, Aisha Idrissi).
        #
        # This is a bare assignment under `set -euo pipefail`, and we are in the window AFTER
        # Neo4j has been STOPPED. A daemon blip, a socket timeout, a `docker` that exits
        # non-zero for any reason at all makes this a failing simple command — errexit fires AT
        # THE ASSIGNMENT, the restart below is NEVER REACHED, and the graph stays down. The
        # same deploy#563 shape assert_stack_present is careful about; these two lines were not.
        NEO4J_CID="$(dc ps -aq neo4j 2>/dev/null | head -1)" || NEO4J_CID=""
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

        # Always restart Neo4j — the one we STOPPED. `neo4j_start` uses `start`, never `up`:
        # `up` would CREATE a neo4j (and fresh, empty volumes) in a project that has none,
        # which is exactly the stray container the deploy#617 run left on stg. A backup script
        # must not be able to bring a database into existence.
        log "INFO" "Restarting Neo4j..."
        if neo4j_start; then
            # Wait for Neo4j to become healthy
            MAX_HEALTH_WAIT=120
            HEALTH_WAITED=0
            while ! dc ps --format '{{.Service}}:{{.Health}}' 2>/dev/null | grep -q "neo4j:healthy"; do
                if [[ $HEALTH_WAITED -ge $MAX_HEALTH_WAIT ]]; then
                    # A graph that never came back HEALTHY is a graph that is down, and this
                    # branch used to say so at WARNING and then exit 0 — the same structural
                    # hole as the restart-failure branch below, one state further on.
                    NEO4J_DOWN=true
                    log "ERROR" "Neo4j did not become healthy within ${MAX_HEALTH_WAIT}s — the graph is DOWN. Check it by hand."
                    break
                fi
                sleep 5
                HEALTH_WAITED=$((HEALTH_WAITED + 5))
            done

            if [[ $HEALTH_WAITED -lt $MAX_HEALTH_WAIT ]]; then
                log "INFO" "Neo4j healthy (waited ${HEALTH_WAITED}s)"
            fi
        else
            # The health-wait is INSIDE the success branch on purpose. Hung off the end, it
            # would run its first `ps`, see no healthy neo4j, and — with HEALTH_WAITED still 0 —
            # fall into `[[ 0 -lt 120 ]]` and log "Neo4j healthy (waited 0s)" over a database
            # that is not running at all. That is the exact sentence the stg run printed while a
            # stray, empty container came up beside the real one.
            NEO4J_DOWN=true
            log "ERROR" "Neo4j did NOT restart — the graph is DOWN. Start it by hand; do not wait for this script."
        fi
    else
        log "WARNING" "Skipped Neo4j dump due to stop timeout"
    fi
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
# 3b. Declare what this backup actually contains (deploy#559 review)
# ---------------------------------------------------------------------------
# A partial backup is uploaded deliberately ("a partial backup beats none"), and once it
# lands in B2 it is date-stamped, checksums cleanly, and is INDISTINGUISHABLE AT REST from
# a complete one. `restore.sh latest` picks the newest directory NAME. So the night the
# user-postgres dump fails, the artifact `latest` selects is precisely the one missing the
# only store that cannot be rebuilt from the pipeline artifact.
#
# Completeness is a property of the artifact, so the artifact must declare it. This is the
# same shape as `_resolve_run.txt` in da#428: the PRODUCER attests to what it actually
# wrote, because it is the only party that knows, and the consumer refuses anything that
# does not attest completeness.
#
# Note what this manifest is NOT: it is not the source of the EXPECTED set. restore.sh
# carries its own list of required stores. If the expected set came from the artifact, a
# partial backup would declare itself complete-for-what-it-happens-to-hold — the same
# circularity that a read-back count has, and it would be just as invisible.
#
# ---------------------------------------------------------------------------
# ONE MANIFEST PER RUN. NOT ONE PER DIRECTORY. (deploy#587)
# ---------------------------------------------------------------------------
# This was `_backup_manifest.txt` — a FIXED name — and completeness is not a property of a
# directory. It is a property of a RUN:
#
#   the B2 path is `<category>/<DATE>`            -> a DAY
#   the dumps inside are `isnad-<store>-<TS>`     -> a RUN
#
# `rclone copy` ADDS and never deletes, and this script deliberately uploads partials ("a
# partial backup beats none", below), so a day-directory routinely holds MORE THAN ONE RUN —
# and the fixed-name manifest was overwritten by WHICHEVER RUN UPLOADED LAST, good or not.
#
# So a GOOD run followed by a PARTIAL one ERASED THE GOOD RUN'S ATTESTATION. The 03:01 nightly
# succeeds completely; an 08:00 retry fails halfway and uploads its `complete=false` manifest
# over the good one. Both consumers then read the surviving attestation and believe it:
# `verify_b2_backup_artifact.sh` reported `status=incomplete reason=no_complete_backup` over a
# bucket containing a complete backup, and `restore.sh latest` walked past that day and
# restored one TWO DAYS OLDER — while the good run sat intact in the day it skipped.
#
# Both fail in the safe direction — a false alarm, an older restore — and that is precisely
# the problem: it is a false alarm ON THE ONE CHECK THAT MUST BE BELIEVED. Alert fatigue on a
# backup alert is how deploy#559 happened.
#
# Naming the manifest for its run makes the attestation the granularity that completeness
# actually has. Runs accumulate side by side, each one carrying its own verdict, and nothing
# overwrites anything. The consumers enumerate them and pick the newest run that BOTH attests
# `complete=true` AND whose dumps all arrived (deploy#584's two-instrument check, at run
# granularity: the attestation says what was DUMPED, the listing says what ARRIVED).
MANIFEST_FILE="${LOCAL_BACKUP_PATH}/_backup_manifest-${TIMESTAMP}.txt"
BACKUP_STORES=""
$PG_OK && BACKUP_STORES="${BACKUP_STORES}postgres,"
$USER_PG_OK && BACKUP_STORES="${BACKUP_STORES}user-postgres,"
$NEO4J_OK && BACKUP_STORES="${BACKUP_STORES}neo4j,"
BACKUP_STORES="${BACKUP_STORES%,}"

if $PG_OK && $USER_PG_OK && $NEO4J_OK; then
    BACKUP_COMPLETE=true
else
    BACKUP_COMPLETE=false
fi

printf 'BACKUP_MANIFEST complete=%s stores=%s timestamp=%s category=%s\n' \
    "$BACKUP_COMPLETE" "$BACKUP_STORES" "$TIMESTAMP" "$BACKUP_CATEGORY" > "$MANIFEST_FILE"
sha256sum "$MANIFEST_FILE" | sed "s|${LOCAL_BACKUP_PATH}/||" > "${MANIFEST_FILE}.sha256"
log "INFO" "Manifest: complete=${BACKUP_COMPLETE} stores=${BACKUP_STORES}"

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

log "INFO" "Uploading to B2: ${RCLONE_REMOTE}:${B2_BUCKET}/${REMOTE_SUBDIR}/"
if rclone copy "$LOCAL_BACKUP_PATH" "${RCLONE_REMOTE}:${B2_BUCKET}/${REMOTE_SUBDIR}/" \
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

    # List date-stamped directories under the category — SCOPED TO THIS ENVIRONMENT.
    #
    # This is the destructive one (deploy#632). Unprefixed, it listed every date dir
    # in a bucket SHARED by stg and prod, and the `rclone purge` below deleted any
    # older than the cutoff — so prod's nightly retention would have deleted STG's
    # backups, and stg's would have deleted PROD's. Each environment can now only
    # ever see, and therefore only ever delete, its own.
    local dirs
    dirs=$(rclone lsf "${RCLONE_REMOTE}:${B2_BUCKET}/${BACKUP_PREFIX}/${category}/" --dirs-only 2>/dev/null || true)

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
                    log "INFO" "[DRY RUN] Would prune: ${BACKUP_PREFIX}/${category}/${dir}/"
                else
                    log "INFO" "Pruning: ${BACKUP_PREFIX}/${category}/${dir}/"
                    rclone purge "${RCLONE_REMOTE}:${B2_BUCKET}/${BACKUP_PREFIX}/${category}/${dir}/" 2>>"$LOG_FILE" || \
                        log "WARNING" "Failed to prune ${BACKUP_PREFIX}/${category}/${dir}/"
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
log "INFO" "Remote:     ${RCLONE_REMOTE}:${B2_BUCKET}/${REMOTE_SUBDIR}/"
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

# The ARTIFACT is complete (we are past the gate above) but THIS RUN left the graph DOWN.
# Both facts are true and both must reach the box, so the order here is load-bearing:
#
#   emit_success_metric FIRST — the backup really is complete and restorable, and the gauge
#   is what the alerts read as "a complete backup exists". Suppressing it would hide a good
#   artifact and make BackupStale fire falsely (deploy#559/#565's alert-fatigue trap).
#
#   THEN exit non-zero — so systemd marks the unit failed and `OnFailure=` fires the failure
#   marker. Because OnFailure runs AFTER the script exits, the marker is written after
#   emit_success_metric's `rm -f`, and both signals survive.
#
# The run that produced this state is precisely the one the old code called a success: every
# dump good, the restart dead, the graph offline, exit 0, and the success metric wiping the
# failure textfile on its way out (deploy#617 review — Aisha Idrissi).
if [[ "$NEO4J_DOWN" == "true" ]]; then
    log "ERROR" "Backup artifact is COMPLETE and uploaded — but this run left Neo4j DOWN."
    log "ERROR" "  The success metric WAS emitted: the backup at ${TIMESTAMP} is good and restorable."
    log "ERROR" "  Exiting non-zero anyway so OnFailure= fires: production needs a human NOW."
    exit 1
fi

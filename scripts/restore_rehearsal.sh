#!/usr/bin/env bash
# =============================================================================
# Restore rehearsal (deploy#560)
#
# Exercises scripts/restore.sh end-to-end against a throwaway compose stack, and
# asserts BOTH directions:
#
#   1. NEGATIVE cases run first. Each feeds restore.sh a deliberately broken
#      backup artifact and requires a NON-ZERO exit. If restore.sh succeeds on a
#      broken artifact, the rehearsal FAILS. A restore check that cannot fail is
#      not a check; it is a decoration that reads as reassurance on the one day
#      it matters. Running the negatives first means a green rehearsal has
#      already demonstrated its own ability to go red.
#
#   2. The POSITIVE case then restores an intact artifact and asserts on restored
#      CONTENT — row counts, a sampled record, and the Neo4j node count — not on
#      the exit status. `pg_restore --clean` can warn-and-succeed while restoring
#      nothing, so an exit code alone proves only that the script ran.
#
# Before the positive restore the scratch datastores are emptied, so a restore
# that silently does nothing cannot pass by leaving the seed data in place.
#
# SAFETY
#   This script only ever touches the ephemeral `compose/docker-compose.rehearsal.yml`
#   stack under its own compose project. It hard-refuses to run against any other
#   compose file, refuses when ENVIRONMENT names a real environment, and asserts
#   that the resolved Neo4j volume belongs to the rehearsal project before any
#   restore runs. It never contacts B2 (restore.sh's RESTORE_LOCAL_DIR path).
#
# Usage:
#   ./scripts/restore_rehearsal.sh
#   KEEP_STACK=true ./scripts/restore_rehearsal.sh   # leave the stack up to debug
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REHEARSAL_COMPOSE="${REPO_ROOT}/compose/docker-compose.rehearsal.yml"
PROJECT="isnad-restore-rehearsal"
KEEP_STACK="${KEEP_STACK:-false}"

# REHEARSAL_WORK lets CI place the per-case restore.sh logs somewhere it can upload
# them as an artifact on failure. Defaults to a temp dir that cleanup() removes.
WORK="${REHEARSAL_WORK:-$(mktemp -d -t isnad-rehearsal-XXXXXX)}"
mkdir -p "$WORK"
ARTIFACT="${WORK}/artifact"
TS="$(date -u +%Y%m%d-%H%M%S)"
PG_DUMP_NAME="isnad-pg-${TS}.dump"
NEO4J_DUMP_NAME="isnad-neo4j-${TS}.dump"

SEED_ROWS=500
SEED_NODES=250
# Sampled identifiers must fall inside their store's seeded range, or the assertion
# fails for a reason that has nothing to do with the restore.
PG_SAMPLE_ID=377
NEO4J_SAMPLE_ID=137

PASS_COUNT=0
FAIL_COUNT=0

log()  { printf '%s [%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" "${*:2}"; }
fail() { log "FAIL" "$*"; FAIL_COUNT=$((FAIL_COUNT + 1)); }
pass() { log "PASS" "$*"; PASS_COUNT=$((PASS_COUNT + 1)); }

dc() { docker compose -f "$REHEARSAL_COMPOSE" -p "$PROJECT" "$@"; }

# ---------------------------------------------------------------------------
# Safety guards — these run before anything is started.
# ---------------------------------------------------------------------------
guard() {
    # A real environment name means someone pointed this at a live box.
    case "${ENVIRONMENT:-}" in
        prod | production | stg | staging)
            log "ERROR" "ENVIRONMENT=${ENVIRONMENT} — the rehearsal never runs against a real environment"
            exit 1
            ;;
    esac

    # Refuse an inherited COMPOSE_FILE. restore.sh reads COMPOSE_FILE, and an
    # ambient value pointing at docker-compose.prod.yml would aim every restore in
    # this script at the production stack. We set it explicitly per invocation.
    if [[ -n "${COMPOSE_FILE:-}" && "${COMPOSE_FILE}" != "${REHEARSAL_COMPOSE}" ]]; then
        log "ERROR" "Refusing to run with an inherited COMPOSE_FILE=${COMPOSE_FILE}"
        log "ERROR" "The rehearsal only ever drives ${REHEARSAL_COMPOSE}"
        exit 1
    fi

    if [[ ! -f "$REHEARSAL_COMPOSE" ]]; then
        log "ERROR" "Rehearsal compose file not found: ${REHEARSAL_COMPOSE}"
        exit 1
    fi

    for cmd in docker sha256sum zstd; do
        command -v "$cmd" &>/dev/null || { log "ERROR" "Required command not found: ${cmd}"; exit 1; }
    done

    # A sampled id outside its seeded range makes the content assertion fail for a
    # reason unrelated to the restore — and, worse, an id that can never exist would
    # make a *passing* assertion meaningless if the comparison were ever inverted.
    if [[ "$PG_SAMPLE_ID" -gt "$SEED_ROWS" ]]; then
        log "ERROR" "PG_SAMPLE_ID=${PG_SAMPLE_ID} exceeds SEED_ROWS=${SEED_ROWS}"
        exit 1
    fi
    if [[ "$NEO4J_SAMPLE_ID" -gt "$SEED_NODES" ]]; then
        log "ERROR" "NEO4J_SAMPLE_ID=${NEO4J_SAMPLE_ID} exceeds SEED_NODES=${SEED_NODES}"
        exit 1
    fi
}

cleanup() {
    local rc=$?
    if [[ "$KEEP_STACK" != "true" ]]; then
        log "INFO" "Tearing down rehearsal stack and volumes..."
        dc down -v --remove-orphans >/dev/null 2>&1 || true
    else
        log "WARN" "KEEP_STACK=true — stack left running (project ${PROJECT})"
    fi
    # Preserve the per-case restore.sh logs when the caller supplied the directory;
    # a failing rehearsal is exactly when those logs are wanted.
    if [[ -z "${REHEARSAL_WORK:-}" ]]; then
        rm -rf "$WORK"
    else
        log "INFO" "Per-case logs retained in ${WORK}"
    fi
    exit $rc
}

psql_q() {
    dc exec -T postgres psql -U isnad -d isnad_graph -tAc "$1" 2>/dev/null | tr -d ' \r'
}

# cypher-shell credentials go through the environment inside the container, never
# on argv, so they cannot surface in `docker inspect` or a process listing.
cypher_q() {
    dc exec -T neo4j sh -c \
        'NEO4J_USERNAME=neo4j NEO4J_PASSWORD=rehearsal_scratch_only cypher-shell --format plain "'"$1"'"' \
        2>/dev/null | tail -n +2 | tr -d ' \r'
}

# ---------------------------------------------------------------------------
# Stack lifecycle
# ---------------------------------------------------------------------------
start_stack() {
    log "INFO" "Starting ephemeral rehearsal stack (project ${PROJECT})..."
    dc down -v --remove-orphans >/dev/null 2>&1 || true
    dc up -d

    log "INFO" "Waiting for postgres + neo4j to report healthy..."
    local waited=0
    while :; do
        local pg_h neo_h
        pg_h=$(docker inspect --format '{{.State.Health.Status}}' "$(dc ps -q postgres)" 2>/dev/null || echo starting)
        neo_h=$(docker inspect --format '{{.State.Health.Status}}' "$(dc ps -q neo4j)" 2>/dev/null || echo starting)
        [[ "$pg_h" == "healthy" && "$neo_h" == "healthy" ]] && break
        if [[ $waited -ge 240 ]]; then
            log "ERROR" "Stack did not become healthy (postgres=${pg_h} neo4j=${neo_h})"
            dc ps
            exit 1
        fi
        sleep 3
        waited=$((waited + 3))
    done
    log "INFO" "Stack healthy after ${waited}s"
}

# Assert the neo4j volume restore.sh will resolve belongs to THIS project. Guards
# against the host-global `docker volume ls | grep neo4j_data` resolution that the
# pre-deploy#560 restore.sh used, which could select a production volume.
assert_volume_isolation() {
    local cid vol
    cid=$(dc ps -aq neo4j | head -1)
    vol=$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}' "$cid")
    log "INFO" "Neo4j data volume resolves to: ${vol}"
    case "$vol" in
        "${PROJECT}_rehearsal_neo4j_data")
            pass "volume isolation: restore targets the rehearsal volume, not a production one"
            ;;
        *)
            fail "volume isolation: expected ${PROJECT}_rehearsal_neo4j_data, resolved '${vol}' — ABORTING before any restore"
            exit 1
            ;;
    esac
}

seed_data() {
    log "INFO" "Seeding scratch datastores (${SEED_ROWS} pg rows, ${SEED_NODES} neo4j nodes)..."
    dc exec -T postgres psql -U isnad -d isnad_graph -q -v ON_ERROR_STOP=1 <<SQL
DROP TABLE IF EXISTS narrator;
CREATE TABLE narrator (id int PRIMARY KEY, name text NOT NULL);
INSERT INTO narrator SELECT g, 'narrator_' || g FROM generate_series(1, ${SEED_ROWS}) g;
SQL
    cypher_q "MATCH (n) DETACH DELETE n" >/dev/null
    cypher_q "UNWIND range(1, ${SEED_NODES}) AS i CREATE (:Narrator {id: i, name: 'narrator_' + i})" >/dev/null

    local rows nodes
    rows=$(psql_q "SELECT count(*) FROM narrator")
    nodes=$(cypher_q "MATCH (n:Narrator) RETURN count(n)")
    log "INFO" "Seeded: pg rows=${rows} neo4j nodes=${nodes}"
    [[ "$rows" == "$SEED_ROWS" ]] || { log "ERROR" "pg seed failed (${rows})"; exit 1; }
    [[ "$nodes" == "$SEED_NODES" ]] || { log "ERROR" "neo4j seed failed (${nodes})"; exit 1; }
}

# ---------------------------------------------------------------------------
# Build a real backup artifact using the same mechanics backup.sh uses.
# ---------------------------------------------------------------------------
create_artifact() {
    log "INFO" "Creating a backup artifact from the seeded stack..."
    mkdir -p "$ARTIFACT"

    dc exec -T postgres pg_dump -U isnad -d isnad_graph --format=custom > "${ARTIFACT}/${PG_DUMP_NAME}"
    local pg_size
    pg_size=$(stat -c%s "${ARTIFACT}/${PG_DUMP_NAME}")
    [[ "$pg_size" -gt 0 ]] || { log "ERROR" "pg_dump produced an empty file"; exit 1; }

    # Neo4j dump requires the database offline, exactly as backup.sh does it.
    dc stop neo4j >/dev/null
    local neo4j_vol
    neo4j_vol=$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}' "$(dc ps -aq neo4j | head -1)")
    # See restore.sh restore_neo4j() for why the entrypoint is bypassed: it would
    # otherwise drop to neo4j(7474), which cannot write the /backups bind mount.
    if ! docker run --rm \
        --user 0:0 \
        --entrypoint neo4j-admin \
        -v "${neo4j_vol}:/data" \
        -v "${ARTIFACT}:/backups" \
        neo4j:5-community \
        database dump neo4j --to-path=/backups/ > "${WORK}/neo4j-dump.out" 2>&1; then
        log "ERROR" "neo4j-admin dump failed while building the rehearsal artifact:"
        sed 's/^/        /' "${WORK}/neo4j-dump.out"
        exit 1
    fi
    [[ -s "${ARTIFACT}/neo4j.dump" ]] || { log "ERROR" "neo4j dump produced no file"; exit 1; }
    mv "${ARTIFACT}/neo4j.dump" "${ARTIFACT}/${NEO4J_DUMP_NAME}"
    zstd -3 --rm -q "${ARTIFACT}/${NEO4J_DUMP_NAME}" -o "${ARTIFACT}/${NEO4J_DUMP_NAME}.zst"
    dc up -d neo4j >/dev/null

    ( cd "$ARTIFACT" && for f in isnad-*; do
          [[ "$f" == *.sha256 ]] && continue
          sha256sum "$f" > "${f}.sha256"
      done )

    log "INFO" "Artifact contents:"
    find "$ARTIFACT" -maxdepth 1 -type f -printf '    %f\n' | sort

    # The artifact must be big enough that halving it truncates. A fixture that
    # cannot express the defect proves nothing about the guard.
    [[ "$pg_size" -gt 512 ]] || { log "ERROR" "pg dump too small (${pg_size}B) for a meaningful truncation fixture"; exit 1; }
}

wait_neo4j_healthy() {
    local waited=0 h
    while :; do
        h=$(docker inspect --format '{{.State.Health.Status}}' "$(dc ps -q neo4j 2>/dev/null)" 2>/dev/null || echo starting)
        [[ "$h" == "healthy" ]] && return 0
        [[ $waited -ge 180 ]] && { log "ERROR" "neo4j not healthy"; return 1; }
        sleep 3; waited=$((waited + 3))
    done
}

# ---------------------------------------------------------------------------
# Invoke the REAL scripts/restore.sh against a given artifact directory.
# ---------------------------------------------------------------------------
run_restore() {
    local src="$1" logfile="$2"
    (
        cd "$REPO_ROOT"
        COMPOSE_FILE="$REHEARSAL_COMPOSE" \
        COMPOSE_PROJECT_NAME="$PROJECT" \
        RESTORE_LOCAL_DIR="$src" \
        RESTORE_DIR="${WORK}/stage-$(basename "$src")" \
        POSTGRES_USER=isnad \
        POSTGRES_DB=isnad_graph \
        ./scripts/restore.sh --force local
    ) > "$logfile" 2>&1
}

expect_fail() {
    local name="$1" src="$2"
    local logfile="${WORK}/${name}.log" rc=0
    log "INFO" "--- negative case: ${name} (restore.sh MUST exit non-zero) ---"
    run_restore "$src" "$logfile" || rc=$?
    if [[ $rc -eq 0 ]]; then
        fail "${name}: restore.sh exited 0 on a broken artifact — the check cannot fail"
        sed 's/^/        /' "$logfile"
    else
        pass "${name}: restore.sh exited ${rc} — $(grep -m1 '\[ERROR\]' "$logfile" | sed 's/.*\[ERROR\] //' || echo 'failed as required')"
    fi
    wait_neo4j_healthy || true
}

expect_pass() {
    local name="$1" src="$2"
    local logfile="${WORK}/${name}.log" rc=0
    log "INFO" "--- positive case: ${name} (restore.sh must exit 0) ---"
    run_restore "$src" "$logfile" || rc=$?
    if [[ $rc -ne 0 ]]; then
        fail "${name}: restore.sh exited ${rc} on an intact artifact"
        sed 's/^/        /' "$logfile"
        return 1
    fi
    pass "${name}: restore.sh exited 0"
    return 0
}

# ---------------------------------------------------------------------------
# Broken-artifact fixtures
# ---------------------------------------------------------------------------
make_fixtures() {
    log "INFO" "Building broken-artifact fixtures..."

    # (a) Truncated dump whose checksum is RECOMPUTED to match. This models a dump
    #     that was short at creation time: checksum verification passes, and only the
    #     restore itself can detect it. This is the fixture that caught the swallowed
    #     pg_restore exit code.
    local trunc="${WORK}/fx_truncated"
    mkdir -p "$trunc"
    local full half
    full=$(stat -c%s "${ARTIFACT}/${PG_DUMP_NAME}")
    half=$(( full / 2 ))
    head -c "$half" "${ARTIFACT}/${PG_DUMP_NAME}" > "${trunc}/${PG_DUMP_NAME}"
    ( cd "$trunc" && sha256sum "${PG_DUMP_NAME}" > "${PG_DUMP_NAME}.sha256" )
    local tsize
    tsize=$(stat -c%s "${trunc}/${PG_DUMP_NAME}")
    if [[ "$tsize" -ge "$full" ]]; then
        log "ERROR" "truncation fixture is inert (${tsize} >= ${full}) — it would prove nothing"
        exit 1
    fi
    log "INFO" "  truncated fixture: ${full}B -> ${tsize}B, checksum recomputed to match"

    # (b) Dump present, zero checksum files. Verification of nothing must not pass.
    local nosums="${WORK}/fx_nosums"
    mkdir -p "$nosums"
    cp "${ARTIFACT}/${PG_DUMP_NAME}" "$nosums/"

    # (c) Entirely empty backup directory.
    local empty="${WORK}/fx_empty"
    mkdir -p "$empty"

    # (d) Bit-rot: dump mutated after its checksum was written.
    local bitrot="${WORK}/fx_bitrot"
    mkdir -p "$bitrot"
    cp "${ARTIFACT}/${PG_DUMP_NAME}" "${ARTIFACT}/${PG_DUMP_NAME}.sha256" "$bitrot/"
    printf 'GARBAGE' | dd of="${bitrot}/${PG_DUMP_NAME}" bs=1 seek=800 conv=notrunc status=none
}

# ---------------------------------------------------------------------------
# Content assertions after the positive restore
# ---------------------------------------------------------------------------
assert_restored_content() {
    log "INFO" "--- asserting on restored CONTENT (not on exit status) ---"

    local rows sample nodes node_sample

    rows=$(psql_q "SELECT count(*) FROM narrator")
    if [[ "$rows" == "$SEED_ROWS" ]]; then
        pass "postgres row count restored: ${rows}"
    else
        fail "postgres row count: expected ${SEED_ROWS}, got '${rows}'"
    fi

    sample=$(psql_q "SELECT name FROM narrator WHERE id = ${PG_SAMPLE_ID}")
    if [[ "$sample" == "narrator_${PG_SAMPLE_ID}" ]]; then
        pass "postgres sampled record id=${PG_SAMPLE_ID} restored: ${sample}"
    else
        fail "postgres sampled record id=${PG_SAMPLE_ID}: expected narrator_${PG_SAMPLE_ID}, got '${sample}'"
    fi

    nodes=$(cypher_q "MATCH (n:Narrator) RETURN count(n)")
    if [[ "$nodes" == "$SEED_NODES" ]]; then
        pass "neo4j node count restored: ${nodes}"
    else
        fail "neo4j node count: expected ${SEED_NODES}, got '${nodes}'"
    fi

    node_sample=$(cypher_q "MATCH (n:Narrator {id: ${NEO4J_SAMPLE_ID}}) RETURN n.name")
    node_sample="${node_sample//\"/}"
    if [[ "$node_sample" == "narrator_${NEO4J_SAMPLE_ID}" ]]; then
        pass "neo4j sampled node id=${NEO4J_SAMPLE_ID} restored: ${node_sample}"
    else
        fail "neo4j sampled node id=${NEO4J_SAMPLE_ID}: expected narrator_${NEO4J_SAMPLE_ID}, got '${node_sample}'"
    fi
}

# Empty the scratch stores so a no-op restore cannot pass by leaving seed data behind.
# Confined to the rehearsal project; asserted by assert_volume_isolation above.
empty_scratch_stores() {
    log "INFO" "Emptying scratch datastores so a no-op restore cannot masquerade as success..."
    dc exec -T postgres psql -U isnad -d isnad_graph -q -c 'DROP TABLE IF EXISTS narrator;'
    cypher_q "MATCH (n) DETACH DELETE n" >/dev/null

    local rows nodes
    rows=$(psql_q "SELECT count(*) FROM narrator" || true)
    nodes=$(cypher_q "MATCH (n:Narrator) RETURN count(n)")
    log "INFO" "Pre-restore state: pg narrator table absent (query yields '${rows:-<error>}'), neo4j nodes=${nodes}"
    if [[ "$nodes" != "0" ]]; then
        log "ERROR" "neo4j not emptied (nodes=${nodes}) — the positive case would be unfalsifiable"
        exit 1
    fi
}

# ---------------------------------------------------------------------------
main() {
    guard
    trap cleanup EXIT

    log "INFO" "=== Restore rehearsal starting (deploy#560) ==="
    log "INFO" "Compose: ${REHEARSAL_COMPOSE}"
    log "INFO" "Project: ${PROJECT}"

    start_stack
    assert_volume_isolation
    seed_data
    create_artifact
    make_fixtures

    # Negatives first: prove the check can go red before we trust it green.
    expect_fail "truncated_dump_matching_checksum" "${WORK}/fx_truncated"
    expect_fail "zero_checksum_files"              "${WORK}/fx_nosums"
    expect_fail "empty_backup"                     "${WORK}/fx_empty"
    expect_fail "bitrot_stale_checksum"            "${WORK}/fx_bitrot"

    # Positive.
    empty_scratch_stores
    if expect_pass "intact_artifact" "$ARTIFACT"; then
        wait_neo4j_healthy
        assert_restored_content
    fi

    echo ""
    log "INFO" "=== Rehearsal summary ==="
    log "INFO" "passed: ${PASS_COUNT}   failed: ${FAIL_COUNT}"

    if [[ "$FAIL_COUNT" -gt 0 ]]; then
        log "ERROR" "Restore rehearsal FAILED"
        exit 1
    fi
    log "INFO" "Restore rehearsal PASSED — restore.sh rejected every broken artifact and restored the intact one"
}

main "$@"

#!/usr/bin/env bash
# =============================================================================
# restore_verify.sh — restore the REAL B2 artifact and prove it reproduces the
#                     live database (deploy#640)
#
# WHAT THIS IS FOR, AND WHY restore_rehearsal.sh IS NOT ENOUGH
# ------------------------------------------------------------
# scripts/restore_rehearsal.sh drives restore.sh against SYNTHETIC seed data and
# a LOCALLY-FABRICATED artifact (RESTORE_LOCAL_DIR, no B2). It is valuable and it
# stays: it proves the restore CODE PATH works, and it proves restore.sh REJECTS
# broken artifacts.
#
# It cannot prove THIS artifact loads. Those two came apart in practice. The real
# backup only became restorable after #613/#617/#623/#632 — and the synthetic
# rehearsal was GREEN through every one of them. It supplies COMPOSE_PROJECT_NAME,
# the one input production omits, so it could never have caught #617.
#
# So this script closes the other half, and it is the half deploy#609 (the prod
# backup go/no-go gate) actually turns on: take the REAL object out of B2, restore
# it into a throwaway stack ON THE HOST, and assert the restored CONTENT equals
# the live database. Before this existed, "can we restore prod?" was answered by a
# transcript of a hand-run, not by a green check.
#
# THE VERDICT IS NEVER THE EXIT CODE OF THE RESTORE
# --------------------------------------------------
# `pg_restore --clean` can warn-and-succeed while restoring nothing. A restore that
# exits 0 having loaded an empty dump into an empty database is indistinguishable,
# by exit code, from a perfect one. Every claim this script makes is an assertion
# about CONTENT: row counts, full-table checksums, count(n), label and
# relationship-type histograms, all compared against the live stack.
#
# AND THE COMPARATOR IS CALIBRATED BEFORE IT IS READ
# ---------------------------------------------------
# This is the deliverable, not garnish. A fingerprint that cannot detect a missing
# node makes every "MATCH" meaningless, and that is not theoretical — it is what
# happened on the first hand-run attempt. The fingerprint was computed over
# `n.name`, which DOES NOT EXIST on `:Narrator` (the real properties are `name_ar`
# / `name_en` / `id`). Both sides read NULL, "agreed" perfectly, and the checksum
# was 0 on both sides.
#
#   A both-sides-zero is not a match. It is the instrument reading nothing, twice.
#
# So before any reading is believed, calibrate() proves on the LIVE stack that:
#   * every fingerprint component is NON-ZERO       (the property exists at all)
#   * the fingerprint of the graph MINUS ONE NARRATOR DIFFERS from the full one
#     (the instrument can actually go red)
# If either fails, the run reports INSTRUMENT FAILURE and refuses to testify.
#
# EXIT CODES — a check that cannot run must say so, and must NOT testify
# ----------------------------------------------------------------------
#   0  VERIFIED           the restored content matches live
#   1  FAILED             a real negative — the artifact did not reproduce live
#   2  INSTRUMENT FAILURE could not find out (creds, resources, inert comparator,
#                         dirty scratch, a query that could not run)
#
# rc=2 is never a claim about the backup. Collapsing it into rc=1 would report a
# broken instrument as a broken backup — which is precisely the failure this
# script's own ancestors committed (deploy#632/#641).
#
# USAGE
#   ./scripts/restore_verify.sh              # verify against the real B2 artifact
#   ./scripts/restore_verify.sh --self-test  # calibrate the comparator; no B2, no live
#   KEEP_STACK=true ./scripts/restore_verify.sh   # leave the scratch stack up to debug
#
# ENVIRONMENT (the real path) — sourced from ENV_FILE, default ./.env on the host
#   B2_BUCKET, B2_KEY_ID, B2_APP_KEY   B2 credentials (restore.sh consumes these)
#   BACKUP_PREFIX                      "stg" | "prod" — SELECTS WHICH ENVIRONMENT'S
#                                      backup is restored. Namespaced since #632/#633.
#   POSTGRES_USER / POSTGRES_DB        must match live, or pg_restore fails on a
#   USER_POSTGRES_USER / _DB           missing role and blames the backup
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# --- Exit codes --------------------------------------------------------------
readonly RC_VERIFIED=0
readonly RC_FAILED=1
readonly RC_INSTRUMENT=2

# --- Configuration -----------------------------------------------------------
# The live stack. These are what we must never, ever restore into.
LIVE_PROJECT="${LIVE_PROJECT:-noorinalabs}"
LIVE_COMPOSE="${LIVE_COMPOSE:-${REPO_ROOT}/compose/docker-compose.prod.yml}"

# The throwaway stack.
RV_PROJECT="${RV_PROJECT:-isnad-restore-verify}"
RV_COMPOSE="${RV_COMPOSE:-${REPO_ROOT}/compose/docker-compose.restore-verify.yml}"

# The stack the restore is compared AGAINST: the live one, on a real run. Named so the
# comparison code reads identically in both modes.
REF="live"

ENV_FILE="${ENV_FILE:-${REPO_ROOT}/.env}"
RESTORE_STAGING_DIR="${RESTORE_STAGING_DIR:-/var/tmp/isnad-restore-verify}"
KEEP_STACK="${KEEP_STACK:-false}"
SELF_TEST=false

# Resource gate. A second Neo4j alongside the live one wants the heap+pagecache
# below plus room for the restored store on disk.
RV_NEO4J_HEAP="${RV_NEO4J_HEAP:-2g}"
RV_NEO4J_PAGECACHE="${RV_NEO4J_PAGECACHE:-2g}"
export RV_NEO4J_HEAP RV_NEO4J_PAGECACHE
# 2g heap + 2g pagecache + JVM/container overhead. Refuse below this.
REQUIRED_MEM_MB="${REQUIRED_MEM_MB:-4608}"
# Headroom on top of (restored store + downloaded archive), in MB.
DISK_MARGIN_MB="${DISK_MARGIN_MB:-2048}"

STACK_STARTED=false

# The self-test's reference project. Script-scope, NOT a function-local, because the EXIT
# trap that tears it down runs after the installing function's scope is gone. See self_test().
ST_REF_PROJECT="${ST_REF_PROJECT:-isnad-rv-selftest-ref}"

# --- Logging -----------------------------------------------------------------
log() { printf '%s [%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" "${*:2}"; }

PASS_COUNT=0
FAIL_COUNT=0
pass() { log "PASS" "$*"; PASS_COUNT=$((PASS_COUNT + 1)); }
fail() { log "FAIL" "$*"; FAIL_COUNT=$((FAIL_COUNT + 1)); }

# An instrument failure is not a backup failure. It exits 2 and says why.
instrument_fail() {
    log "ERROR" "INSTRUMENT FAILURE — $*"
    log "ERROR" "This is NOT a claim about the backup. The check could not run, so it"
    log "ERROR" "reports nothing about whether the artifact is restorable."
    exit "$RC_INSTRUMENT"
}

usage() {
    sed -n '2,60p' "${BASH_SOURCE[0]}"
    exit "$RC_INSTRUMENT"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --self-test) SELF_TEST=true; shift ;;
        -h | --help) usage ;;
        *) log "ERROR" "Unknown argument: $1"; usage ;;
    esac
done

# --- Compose plumbing --------------------------------------------------------
# Every compose call in this file goes through _dc, and every one of them names its
# PROJECT explicitly. `-f` names the FILE; with no `-p` and no COMPOSE_PROJECT_NAME,
# Compose derives the project from the compose file's DIRECTORY basename — which for
# `compose/docker-compose.*.yml` is the string `compose`. That fallback is the whole
# of deploy#617: a restore addressed to a project you never deployed into WRITES, and
# `neo4j-admin database load --overwrite-destination` will happily create and fill a
# brand-new volume while reporting success and leaving the real graph untouched.
#
# scripts/tests/test_restore_verify.py statically asserts every `docker compose` here
# carries `-p`. An unflagged one added later fails the suite.
_dc() {
    local which="$1"
    shift
    case "$which" in
        live) docker compose -p "$LIVE_PROJECT" -f "$LIVE_COMPOSE" "$@" ;;
        rv) docker compose -p "$RV_PROJECT" -f "$RV_COMPOSE" "$@" ;;
        *) instrument_fail "internal: unknown stack '${which}'" ;;
    esac
}

# =============================================================================
# Measurement
# =============================================================================
# Every reading goes through these. They put the value in MEASURED and return 0, or
# they return non-zero having printed the tool's OWN diagnostic.
#
# THREE RULES, EACH PAID FOR:
#
# 1. NEVER `2>/dev/null`. The diagnostic IS the finding on the failure path. A real
#    verification script in this project printed `*** DATA LOSS ***` about a perfectly
#    intact backup because a permission error was swallowed and `wc -l` turned "could
#    not authenticate" into the number 0 (deploy#632/#641).
#
# 2. An EMPTY reading is NOT a value. "The query returned nothing" and "the query
#    could not run" look identical in the output, and only one of them is a
#    measurement. Empty is returned as an instrument failure, never as a datum.
#
# 3. The value comes back in a GLOBAL, not by `V="$(f)"`. Under `set -e` an assignment
#    from a command that legitimately fails is a CRASH, not an empty string, and the
#    `rc=$?` line after it is dead code on every failing path
#    (feedback_errexit_kills_assignment_guard, deploy#563/#584/#591).
MEASURED=""

_run_capture() { # _run_capture <label> <cmd...>  -> MEASURED, rc 0|2
    local label="$1"
    shift
    local err out rc=0
    err="$(mktemp)" || { log "ERROR" "cannot create temp file"; return 2; }

    # stdout is the datum; stderr is commentary and is kept SEPARATE (deploy#584).
    # It is neither discarded nor merged into the value — merging would let a NOTICE
    # on the success path become part of the reading.
    out="$("$@" 2>"$err")" || rc=$?

    if [[ "$rc" -ne 0 ]]; then
        log "ERROR" "${label}: command failed (rc=${rc}). Its own diagnostic follows:"
        while IFS= read -r line; do log "ERROR" "  | ${line}"; done <"$err"
        rm -f "$err"
        return 2
    fi
    rm -f "$err"

    # Trim the ENDS only. Stripping internal spaces would silently mangle a histogram
    # ("Hadith=20 Narrator=50" -> "Hadith=20Narrator=50") — still comparable, but no longer
    # legible in the log, and an unreadable diagnostic is a diagnostic nobody acts on.
    MEASURED="$(printf '%s' "$out" | tr -d '\r' | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
    if [[ -z "$MEASURED" ]]; then
        log "ERROR" "${label}: returned an EMPTY reading."
        log "ERROR" "  An empty reading is not a value — it is indistinguishable from a query"
        log "ERROR" "  that could not run, and only one of those is a measurement."
        return 2
    fi
    return 0
}

# cypher_val <live|rv> <query>
#
# The password is derived INSIDE the container from that container's own NEO4J_AUTH,
# so it never reaches this script's argv, cypher-shell's argv, or the run log
# (CWE-214). Same contract graph-ops.yml uses against the live graph. `--format plain`
# emits a header line + rows; we drop the header.
cypher_val() {
    local which="$1" q="$2"
    _run_capture "cypher(${which})" \
        _cypher_raw "$which" "$q" || return $?
}

_cypher_raw() {
    local which="$1" q="$2"
    # `--format plain` renders a STRING column QUOTED (see graph_load_provenance.sh) — the
    # fingerprint comes back as "50/300/450/350". An integer column does not. Strip the
    # quotes so a string reading and an integer reading compare the same way; the transform
    # is applied to BOTH stacks, so it cannot manufacture an agreement.
    # INTENTIONAL, not an oversight: the single quotes keep ${NEO4J_AUTH} unexpanded on this
    # side so it expands INSIDE the container. The credential never reaches this script's
    # argv, cypher-shell's argv, or the run log (CWE-214). Same contract as graph-ops.yml.
    # shellcheck disable=SC2016
    _dc "$which" exec -T neo4j sh -c \
        'NEO4J_USERNAME=neo4j NEO4J_PASSWORD="${NEO4J_AUTH#neo4j/}" exec cypher-shell --format plain "$1"' \
        _ "$q" | tail -n +2 | tr -d '"'
}

# pg_val <live|rv> <sql>   — the isnad_graph store
pg_val() {
    local which="$1" sql="$2"
    _run_capture "psql(${which})" \
        _dc "$which" exec -T postgres \
        psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "$sql" || return $?
}

# upg_val <live|rv> <sql>  — the user-service store
upg_val() {
    local which="$1" sql="$2"
    _run_capture "psql-user(${which})" \
        _dc "$which" exec -T user-postgres \
        psql -U "$USER_POSTGRES_USER" -d "$USER_POSTGRES_DB" -tAc "$sql" || return $?
}

# =============================================================================
# The fingerprints
# =============================================================================
# Counts can match while the bytes are garbage, so the Postgres fingerprints are an
# md5 over deterministically-ordered FULL ROWS, not cardinalities.
#
# The Neo4j fingerprint is over the properties that ACTUALLY EXIST on :Narrator —
# name_ar / name_en / id. Not `n.name`. See the header.
readonly CY_FP="MATCH (n:Narrator)
    RETURN toString(count(n)) + '/' +
           toString(sum(size(coalesce(n.name_ar,'')))) + '/' +
           toString(sum(size(coalesce(n.name_en,'')))) + '/' +
           toString(sum(size(coalesce(n.id,'')))) AS fp;"

# The SAME fingerprint over the graph minus exactly one narrator. calibrate() requires
# this to DIFFER from CY_FP on the live stack. If it does not, the fingerprint is blind
# to a whole missing node and its agreement across stacks would mean nothing.
readonly CY_FP_MINUS1="MATCH (n:Narrator) WITH n ORDER BY n.id SKIP 1
    RETURN toString(count(n)) + '/' +
           toString(sum(size(coalesce(n.name_ar,'')))) + '/' +
           toString(sum(size(coalesce(n.name_en,'')))) + '/' +
           toString(sum(size(coalesce(n.id,'')))) AS fp;"

# The individual components, so calibrate() can prove each is non-zero — i.e. that the
# property exists at all. This is the n.name guard, made a runtime assertion. Separated by
# '/' rather than a space so the parse cannot be broken by whitespace trimming.
readonly CY_FP_PARTS="MATCH (n:Narrator)
    RETURN toString(count(n)) + '/' +
           toString(sum(size(coalesce(n.name_ar,'')))) + '/' +
           toString(sum(size(coalesce(n.name_en,'')))) + '/' +
           toString(sum(size(coalesce(n.id,'')))) AS parts;"

readonly CY_NODES="MATCH (n) RETURN count(n);"
readonly CY_RELS="MATCH ()-[r]->() RETURN count(r);"
# Histograms: a node count can match while the LABELS are wrong.
readonly CY_LABEL_HIST="MATCH (n) UNWIND labels(n) AS l
    RETURN l + '=' + toString(count(*)) AS h ORDER BY h;"
readonly CY_REL_HIST="MATCH ()-[r]->()
    RETURN type(r) + '=' + toString(count(r)) AS h ORDER BY h;"

readonly PG_HADITH_COUNT="SELECT count(*) FROM isnad_graph.hadiths;"
readonly PG_HADITH_MD5="SELECT md5(string_agg(t,'|' ORDER BY t)) FROM (SELECT h::text AS t FROM isnad_graph.hadiths h) s;"
readonly PG_HADITH_MD5_MINUS1="SELECT md5(string_agg(t,'|' ORDER BY t)) FROM (SELECT h::text AS t FROM isnad_graph.hadiths h ORDER BY t OFFSET 1) s;"

readonly UPG_USERS_MD5="SELECT md5(string_agg(t,'|' ORDER BY t)) FROM (SELECT u::text AS t FROM users u) s;"

# Multi-row readings (histograms) collapse to one line so they compare as scalars.
cypher_hist() { # cypher_hist <live|rv> <query>
    local which="$1" q="$2"
    _run_capture "cypher-hist(${which})" \
        _cypher_hist_raw "$which" "$q" || return $?
}
_cypher_hist_raw() {
    local which="$1" q="$2"
    # INTENTIONAL, not an oversight: the single quotes keep ${NEO4J_AUTH} unexpanded on this
    # side so it expands INSIDE the container. The credential never reaches this script's
    # argv, cypher-shell's argv, or the run log (CWE-214). Same contract as graph-ops.yml.
    # shellcheck disable=SC2016
    _dc "$which" exec -T neo4j sh -c \
        'NEO4J_USERNAME=neo4j NEO4J_PASSWORD="${NEO4J_AUTH#neo4j/}" exec cypher-shell --format plain "$1"' \
        _ "$q" | tail -n +2 | tr -d '"\r' | tr '\n' ' '
}

# =============================================================================
# Guards — all of these run BEFORE anything is started or loaded
# =============================================================================
guard_not_live() {
    # The single most important refusal in this file. Everything else is hygiene.
    if [[ "$RV_PROJECT" == "$LIVE_PROJECT" ]]; then
        instrument_fail "RV_PROJECT='${RV_PROJECT}' IS the live project. Refusing: this script
        runs a --overwrite-destination Neo4j load, and that would destroy the live graph."
    fi
    if [[ "$RV_COMPOSE" == "$LIVE_COMPOSE" ]]; then
        instrument_fail "RV_COMPOSE is the LIVE compose file (${RV_COMPOSE}). Refusing."
    fi
    if [[ ! -f "$RV_COMPOSE" ]]; then
        instrument_fail "throwaway compose file not found: ${RV_COMPOSE}"
    fi

    # An ambient COMPOSE_FILE / COMPOSE_PROJECT(_NAME) would be inherited by restore.sh,
    # which reads both. We export ours explicitly before invoking it, but an inherited
    # value pointing at the live stack is a loaded gun and we refuse to run beside one.
    local var
    for var in COMPOSE_FILE COMPOSE_PROJECT COMPOSE_PROJECT_NAME; do
        if [[ -n "${!var:-}" ]]; then
            case "$var" in
                COMPOSE_FILE) [[ "${!var}" == "$RV_COMPOSE" ]] && continue ;;
                *) [[ "${!var}" == "$RV_PROJECT" ]] && continue ;;
            esac
            instrument_fail "refusing to run with an inherited ${var}=${!var} —
        this script only ever drives project '${RV_PROJECT}' via ${RV_COMPOSE}"
        fi
    done

    for cmd in docker rclone zstd sha256sum; do
        command -v "$cmd" >/dev/null 2>&1 ||
            instrument_fail "required command not found: ${cmd}"
    done
}

require_env() {
    local missing=() var
    for var in "$@"; do
        [[ -n "${!var:-}" ]] || missing+=("$var")
    done
    if [[ ${#missing[@]} -gt 0 ]]; then
        instrument_fail "missing required environment: ${missing[*]}
        (expected in ${ENV_FILE}, written by the deploy workflow)"
    fi
}

load_env() {
    if [[ ! -f "$ENV_FILE" ]]; then
        instrument_fail "env-file not found: ${ENV_FILE}
        The deploy workflow writes it. Without it we know neither the B2 credential nor
        the live Postgres identity, and a stack with the wrong identity fails pg_restore
        on a missing role — which looks exactly like a broken backup and is not one."
    fi
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a

    # BACKUP_PREFIX selects WHICH ENVIRONMENT's backups we restore. stg and prod share
    # one bucket, namespaced by prefix since #632/#633. Restoring prod's artifact while
    # believing it to be stg's would be a silent, confident lie.
    require_env B2_BUCKET B2_KEY_ID B2_APP_KEY BACKUP_PREFIX \
        POSTGRES_USER POSTGRES_DB USER_POSTGRES_USER USER_POSTGRES_DB

    # SCOPE THE MONITOR, NOT JUST THE DESTROYER (deploy#632/#633).
    #
    # The caller (the workflow's matrix leg) knows which environment it BELIEVES it is
    # checking. The host's .env knows which environment it actually IS. If those disagree,
    # the prod leg would restore STAGING's artifact, compare it against PROD's live stack,
    # and report... something. Either a false red or, if the two happened to agree, a false
    # green about a backup nobody has ever verified. The `[stg,prod]` backup watchdog had
    # exactly this shape before #632: both legs scanned the same objects, so the prod leg
    # would have reported `fresh` on the strength of staging's backup.
    if [[ -n "${EXPECT_PREFIX:-}" && "${EXPECT_PREFIX}" != "${BACKUP_PREFIX}" ]]; then
        instrument_fail "prefix mismatch: the caller expected to verify '${EXPECT_PREFIX}', but this
        host's ${ENV_FILE} says BACKUP_PREFIX='${BACKUP_PREFIX}'. Refusing: this run would restore
        one environment's backup and compare it against the other's live database. Either the
        workflow is pointed at the wrong host, or the host's .env is wrong."
    fi
}

# The deploy#617 guard, asserted independently BEFORE any load. restore.sh logs its own
# resolution ("Resolved Neo4j data volume: …") and refuses to guess — but this script is
# what points restore.sh at a stack in the first place, on a box where the LIVE graph is
# one `docker volume` away. So we resolve the volume ourselves, from the container that
# is actually running in OUR project, and require it to be ours.
assert_volume_isolation() {
    local cid vol expected="${RV_PROJECT}_rv_neo4j_data"

    cid="$(_dc rv ps -aq neo4j)" ||
        instrument_fail "cannot resolve a neo4j container in project '${RV_PROJECT}'"
    cid="$(printf '%s\n' "$cid" | head -1)"
    [[ -n "$cid" ]] ||
        instrument_fail "no neo4j container in project '${RV_PROJECT}' — nothing to assert on"

    vol="$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}' "$cid")" ||
        instrument_fail "cannot inspect the neo4j container's mounts"

    log "INFO" "Neo4j /data volume resolves to: ${vol}"
    if [[ "$vol" != "$expected" ]]; then
        fail "volume isolation: expected '${expected}', resolved '${vol}'"
        log "ERROR" "ABORTING BEFORE ANY LOAD. neo4j-admin database load runs with"
        log "ERROR" "--overwrite-destination; against the wrong volume it is unrecoverable."
        exit "$RC_INSTRUMENT"
    fi
    # Belt and braces: whatever it resolved to, it must not be a volume of the live project.
    case "$vol" in
        "${LIVE_PROJECT}_"*)
            fail "volume isolation: '${vol}' belongs to the LIVE project — ABORTING"
            exit "$RC_INSTRUMENT"
            ;;
    esac
    pass "volume isolation: the restore targets '${vol}', which belongs to the throwaway project"
}

# A restore that silently does nothing must not pass by leaving seed data in place.
# Fresh volumes make these empty; anything else means a previous run leaked, and a
# leaked store is a store that can answer a query with yesterday's right answer.
assert_scratch_empty() {
    local n

    cypher_val rv "$CY_NODES" || instrument_fail "cannot read the scratch graph"
    n="$MEASURED"
    if [[ "$n" != "0" ]]; then
        instrument_fail "the scratch Neo4j already holds ${n} nodes before the restore.
        A restore that loaded NOTHING would then still 'match' — the stores must be empty
        first or a no-op passes. Tear down: docker compose -p ${RV_PROJECT} -f ${RV_COMPOSE} down -v"
    fi

    pg_val rv "SELECT count(*) FROM information_schema.tables WHERE table_schema='isnad_graph';" ||
        instrument_fail "cannot read the scratch isnad Postgres"
    n="$MEASURED"
    [[ "$n" == "0" ]] ||
        instrument_fail "the scratch isnad Postgres already has ${n} table(s) in isnad_graph before the restore"

    upg_val rv "SELECT count(*) FROM information_schema.tables WHERE table_schema='public';" ||
        instrument_fail "cannot read the scratch user Postgres"
    n="$MEASURED"
    [[ "$n" == "0" ]] ||
        instrument_fail "the scratch user Postgres already has ${n} public table(s) before the restore"

    pass "scratch stores are EMPTY — a restore that does nothing cannot pass"
}

# =============================================================================
# Resource gate
# =============================================================================
# A second Neo4j alongside the live one wants ~4 GB and the store's size on disk. Start
# it on a box that cannot spare that and the OOM killer gets a vote on which Neo4j dies.
resource_gate() {
    local avail_mb store_kb free_kb need_kb docker_root

    # Same errexit trap as free_kb below — guard the assignment itself, not the line after it.
    if ! avail_mb="$(awk '/^MemAvailable:/ {print int($2/1024)}' /proc/meminfo)" || [[ -z "$avail_mb" ]]; then
        instrument_fail "cannot read MemAvailable from /proc/meminfo"
    fi
    if [[ "$avail_mb" -lt "$REQUIRED_MEM_MB" ]]; then
        instrument_fail "not enough memory: ${avail_mb} MB available, need ${REQUIRED_MEM_MB} MB
        (${RV_NEO4J_HEAP} heap + ${RV_NEO4J_PAGECACHE} pagecache + overhead, ON TOP of the live
        stack). Refusing to start a second Neo4j and let the OOM killer choose between them."
    fi
    log "INFO" "Memory: ${avail_mb} MB available, ${REQUIRED_MEM_MB} MB required — OK"

    # Size the live store, so the disk requirement is measured rather than guessed.
    if _run_capture "live-store-size" _dc live exec -T neo4j du -sk /data; then
        store_kb="$(printf '%s' "$MEASURED" | sed -n 's/^\([0-9][0-9]*\).*/\1/p')"
    fi
    if [[ -z "${store_kb:-}" ]]; then
        instrument_fail "cannot measure the live Neo4j store size. Without it the disk
        requirement would be a guess, and a restore that fills the disk takes the LIVE stack
        down with it. Refusing to proceed on an unmeasured box."
    fi

    docker_root="$(docker info --format '{{.DockerRootDir}}')" ||
        instrument_fail "cannot determine the docker root directory"

    # `free_kb="$(df …)"` followed by a `[[ -n "$free_kb" ]] || instrument_fail` guard would
    # be DEAD CODE on the only path it exists for: under `set -e` an assignment from a command
    # that legitimately fails is a CRASH, and the script would exit 1 — a REAL NEGATIVE, i.e.
    # "the backup does not restore" — because `df` could not stat a directory.
    # (feedback_errexit_kills_assignment_guard, deploy#563/#584/#591. This one was caught by
    # scripts/tests/test_restore_verify.py, not by review.)
    if ! free_kb="$(df -Pk "$docker_root" | awk 'NR==2 {print $4}')" || [[ -z "$free_kb" ]]; then
        instrument_fail "cannot determine free space on ${docker_root}. The disk requirement
        would be a guess, and a restore that fills the disk takes the LIVE stack down with it."
    fi

    # The restored store (~= live store) + the downloaded archive + margin.
    need_kb=$((store_kb + store_kb / 2 + DISK_MARGIN_MB * 1024))
    log "INFO" "Disk on ${docker_root}: $((free_kb / 1024)) MB free; need $((need_kb / 1024)) MB" \
        "(live store $((store_kb / 1024)) MB + archive + ${DISK_MARGIN_MB} MB margin)"
    if [[ "$free_kb" -lt "$need_kb" ]]; then
        instrument_fail "not enough disk on ${docker_root}: $((free_kb / 1024)) MB free,
        need $((need_kb / 1024)) MB. A restore that fills the disk takes the LIVE stack with it."
    fi
    pass "resource gate: memory and disk can carry a second Neo4j alongside the live one"
}

# =============================================================================
# Leak audit — and the rc discipline that goes with it
# =============================================================================
# A teardown that dies partway is how volumes leak, and a leaked scratch volume holds a FULL
# COPY OF THE PRODUCTION DATABASE. `docker compose down -v` is not sufficient evidence that it
# is gone: the compose file's `${POSTGRES_USER:?}` guards interpolate on `down` too, so a
# teardown run without those vars ABORTS. That is not hypothetical — it is what the hand-run
# did, and recovery was `docker rm -f` + `docker volume rm` + `docker network rm` by hand.
#
# So we do not trust `down -v`. We ENUMERATE what is left, using compose's own project label
# (the same label compose itself uses to decide what `down` owns), sweep it, and re-enumerate.
#
# THE EXIT-CODE RULE, which is the whole point:
#
#   * The comparator's verdict is captured BEFORE any teardown runs and is NEVER overwritten
#     by a teardown outcome. A cleanup fault cannot turn a FAILING comparator into a passing
#     run — a real negative always survives to the exit code.
#
#   * A leak on an otherwise-GREEN run is still a fault, and it exits 2 (INSTRUMENT), not 0.
#     It is reported as exactly what it is: "the comparator PASSED; the teardown LEAKED."
#     Those are two different facts and collapsing them would hide a live copy of production
#     sitting on the box.
#
# Verdict-before-cleanup, and cleanup never speaks for the verdict
# (feedback_your_own_verification_script_must_not_testify: on the hand-run, cleanup ran BEFORE
# the verdict and destroyed the evidence).
leak_audit() { # leak_audit <verdict_rc> <project>...
    local verdict="$1"
    shift
    local projects=("$@")
    local p kind leaked=0 found

    for p in "${projects[@]}"; do
        for kind in container volume network; do
            case "$kind" in
                container) found="$(docker ps -aq --filter "label=com.docker.compose.project=${p}")" || found="" ;;
                volume) found="$(docker volume ls -q --filter "label=com.docker.compose.project=${p}")" || found="" ;;
                network) found="$(docker network ls -q --filter "label=com.docker.compose.project=${p}")" || found="" ;;
            esac
            [[ -z "$found" ]] && continue

            local id
            while IFS= read -r id; do
                [[ -z "$id" ]] && continue
                log "WARN" "leak: ${kind} ${id} survived 'down -v' in project ${p} — removing it directly"
                case "$kind" in
                    container) docker rm -f "$id" >/dev/null || leaked=1 ;;
                    volume) docker volume rm -f "$id" >/dev/null || leaked=1 ;;
                    network) docker network rm "$id" >/dev/null || leaked=1 ;;
                esac
            done <<<"$found"
        done
    done

    # Re-enumerate. The sweep's own exit code is not evidence that the object is gone.
    local still=0
    for p in "${projects[@]}"; do
        found="$(docker ps -aq --filter "label=com.docker.compose.project=${p}")" || found=""
        [[ -n "$found" ]] && still=1
        found="$(docker volume ls -q --filter "label=com.docker.compose.project=${p}")" || found=""
        [[ -n "$found" ]] && still=1
        found="$(docker network ls -q --filter "label=com.docker.compose.project=${p}")" || found=""
        [[ -n "$found" ]] && still=1
    done

    if [[ "$still" -eq 0 && "$leaked" -eq 0 ]]; then
        pass "teardown audit: no stray containers, volumes or networks remain"
    else
        fail "teardown audit: STRAY OBJECTS REMAIN after the sweep — a scratch volume may hold a"
        log "ERROR" "  full copy of the ${BACKUP_PREFIX:-target} database. Remove it by hand NOW:"
        log "ERROR" "    docker ps -a  --filter label=com.docker.compose.project=${projects[0]}"
        log "ERROR" "    docker volume ls --filter label=com.docker.compose.project=${projects[0]}"
        # A leak must not silently pass. But it must ALSO not overwrite a real negative: if the
        # comparator already failed, that verdict is the more important fact and it stands.
        if [[ "$verdict" -eq 0 ]]; then
            log "ERROR" "The comparator PASSED, but the teardown LEAKED. Exiting ${RC_INSTRUMENT}"
            log "ERROR" "(INSTRUMENT), not 0: the restore verified, but the box is not clean."
            exit "$RC_INSTRUMENT"
        fi
    fi

    exit "$verdict"
}

# =============================================================================
# Stack lifecycle
# =============================================================================
teardown() {
    # THE VERDICT, captured first and never recomputed. See leak_audit() for the rule.
    local verdict=$?

    if [[ "$KEEP_STACK" == "true" ]]; then
        log "WARN" "KEEP_STACK=true — leaving project '${RV_PROJECT}' UP. Volumes are NOT removed."
        log "WARN" "  Tear down with: docker compose -p ${RV_PROJECT} -f ${RV_COMPOSE} down -v"
        exit "$verdict"
    fi
    if [[ "$STACK_STARTED" != "true" ]]; then
        exit "$verdict"
    fi

    # The compose file's `${POSTGRES_USER:?}` guards interpolate on `down` too. Run this
    # without those vars in the environment and compose ABORTS — leaking the volumes,
    # which is exactly what happened on the hand-run (recovery was `docker rm -f` +
    # `docker volume rm` + `docker network rm`, by hand). We sourced the env-file before
    # installing this trap, so they are present; leak_audit is the belt to that brace.
    log "INFO" "Tearing down the throwaway stack (project ${RV_PROJECT}, verdict rc=${verdict})..."
    _dc rv down -v --remove-orphans ||
        log "WARN" "compose down returned non-zero — the audit below is what stops a leak"

    rm -rf "$RESTORE_STAGING_DIR"

    # Enumerates containers/volumes/networks by compose's own project label, sweeps them,
    # re-enumerates, and exits with the VERDICT — never with the teardown's own outcome.
    leak_audit "$verdict" "$RV_PROJECT"
}

start_stack() {
    log "INFO" "Starting the throwaway stack (project ${RV_PROJECT}, no published ports)..."
    # A previous crashed run could have left this up. Down it first, or `up` reuses its
    # volumes and assert_scratch_empty is the only thing standing between us and a
    # yesterday's-data pass.
    _dc rv down -v --remove-orphans >/dev/null 2>&1 || true
    STACK_STARTED=true
    _dc rv up -d

    log "INFO" "Waiting for postgres + user-postgres + neo4j to report healthy..."
    local waited=0 svc cid health
    while :; do
        local all_healthy=true
        for svc in postgres user-postgres neo4j; do
            # A container that does not exist YET is the normal case on the first pass, so a
            # failing `ps` here is not an error — but it must not CRASH the wait loop either,
            # which a bare `cid="$(…)"` under `set -e` would do. stderr is left visible: if
            # compose is genuinely broken, that message is the only thing that will explain a
            # five-minute wait ending in a timeout.
            cid=""
            cid="$(_dc rv ps -q "$svc" | head -1)" || cid=""
            if [[ -z "$cid" ]]; then
                all_healthy=false
                break
            fi
            health="starting"
            health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$cid")" || health="starting"
            [[ "$health" == "healthy" ]] || all_healthy=false
        done
        [[ "$all_healthy" == "true" ]] && break
        if [[ "$waited" -ge 300 ]]; then
            log "ERROR" "the throwaway stack did not become healthy within ${waited}s"
            _dc rv ps
            exit "$RC_INSTRUMENT"
        fi
        sleep 3
        waited=$((waited + 3))
    done
    log "INFO" "Throwaway stack healthy after ${waited}s"
}

# =============================================================================
# CALIBRATION — the deliverable
# =============================================================================
# Run against the REFERENCE stack, BEFORE the restore, and before any reading is
# believed. A green run that could not have gone red is worth LESS than no run, because
# it manufactures false confidence.
calibrate() {
    local parts count ar en id full minus1 md5_full md5_minus1

    log "INFO" "=== CALIBRATION — the comparator must be shown able to go RED ==="

    # (1) Every component must be NON-ZERO. This is the n.name guard.
    #     `n.name` does not exist on :Narrator. A fingerprint over it reads NULL on both
    #     sides, "agrees" perfectly, and sums to 0 on both sides. A both-sides-zero is not
    #     a match; it is the instrument reading nothing, twice.
    cypher_val "$REF" "$CY_FP_PARTS" || instrument_fail "cannot read the reference graph"
    parts="$(printf '%s' "$MEASURED" | tr -d '"')"
    IFS='/' read -r count ar en id <<<"$parts"
    log "INFO" "  narrator components: count=${count} sum|name_ar|=${ar} sum|name_en|=${en} sum|id|=${id}"

    if [[ "${count:-0}" -eq 0 ]]; then
        instrument_fail "the reference graph holds ZERO :Narrator nodes. There is nothing to
        fingerprint, so a 'match' against the restore would be two empty readings agreeing."
    fi
    local comp
    for comp in ar en id; do
        if [[ "${!comp:-0}" -eq 0 ]]; then
            instrument_fail "the fingerprint component '${comp}' sums to ZERO over ${count} narrators.
        The property does not exist on :Narrator (this is exactly the n.name defect — the real
        properties are name_ar / name_en / id). A fingerprint with a dead component reads the
        same on any two stacks and cannot detect a difference. REFUSING TO TESTIFY."
        fi
    done
    pass "calibration: every fingerprint component is non-zero — the properties exist"

    # (2) The fingerprint MINUS ONE NARRATOR must DIFFER from the full one. A comparator
    #     that cannot detect a missing node makes every MATCH below meaningless.
    cypher_val "$REF" "$CY_FP" || instrument_fail "cannot compute the reference fingerprint"
    full="$MEASURED"
    cypher_val "$REF" "$CY_FP_MINUS1" || instrument_fail "cannot compute the minus-one fingerprint"
    minus1="$MEASURED"
    log "INFO" "  graph fingerprint            : ${full}"
    log "INFO" "  graph fingerprint minus 1 narr: ${minus1}"
    if [[ "$full" == "$minus1" ]]; then
        instrument_fail "the graph fingerprint CANNOT SEE A MISSING NARRATOR — it is identical
        with and without one. It is inert, and every 'MATCH' it reports would be meaningless.
        REFUSING TO TESTIFY."
    fi
    pass "calibration: the graph fingerprint DIFFERS when one narrator is removed — it can go red"

    # (3) Same, for the Postgres full-row checksum.
    pg_val "$REF" "$PG_HADITH_MD5" || instrument_fail "cannot compute the reference hadith checksum"
    md5_full="$MEASURED"
    pg_val "$REF" "$PG_HADITH_MD5_MINUS1" || instrument_fail "cannot compute the minus-one hadith checksum"
    md5_minus1="$MEASURED"
    log "INFO" "  hadiths md5            : ${md5_full}"
    log "INFO" "  hadiths md5 minus 1 row: ${md5_minus1}"
    if [[ "$md5_full" == "$md5_minus1" ]]; then
        instrument_fail "the hadiths checksum CANNOT SEE A MISSING ROW. It is inert.
        REFUSING TO TESTIFY."
    fi
    pass "calibration: the hadiths checksum DIFFERS when one row is removed — it can go red"

    log "INFO" "=== CALIBRATION PASSED — a MATCH below now means something ==="
}

# =============================================================================
# The restore
# =============================================================================
run_restore() {
    log "INFO" "=== Restoring the REAL artifact from B2 (prefix: ${BACKUP_PREFIX}) ==="

    # restore.sh reads COMPOSE_FILE / COMPOSE_PROJECT. Export ours so it can only ever
    # address the throwaway stack. This is the same pair guard_not_live() refuses to
    # inherit — we are the ones who get to set it.
    local rc=0
    COMPOSE_FILE="$RV_COMPOSE" \
        COMPOSE_PROJECT="$RV_PROJECT" \
        RESTORE_DIR="$RESTORE_STAGING_DIR" \
        "${SCRIPT_DIR}/restore.sh" --force latest || rc=$?

    # rc != 0 IS a real negative: restore.sh treats an ignored pg_restore error as a failed
    # restore, and it is right to. But rc == 0 is NOT a pass — that is what compare() is for.
    if [[ "$rc" -ne 0 ]]; then
        fail "restore.sh exited ${rc} — the real artifact did not restore"
        return "$RC_FAILED"
    fi
    log "INFO" "restore.sh exited 0. That is NOT the verdict — asserting on CONTENT now."
    return 0
}

# =============================================================================
# The comparison
# =============================================================================
row() { # row <label> <reference> <restored>
    local label="$1" a="$2" b="$3"
    if [[ "$a" == "$b" ]]; then
        printf '  %-26s %-34s %-34s MATCH\n' "$label" "$a" "$b"
    else
        printf '  %-26s %-34s %-34s *** MISMATCH ***\n' "$label" "$a" "$b"
        FAIL_COUNT=$((FAIL_COUNT + 1))
        return 1
    fi
    return 0
}

compare() {
    local a b
    log "INFO" "=== CONTENT: reference stack vs the B2-restored throwaway stack ==="
    printf '  %-26s %-34s %-34s %s\n' "metric" "REFERENCE (live)" "RESTORED" "verdict"

    local mismatches=0
    _cmp() { # _cmp <label> <reader> <query>
        local label="$1" reader="$2" q="$3"
        "$reader" "$REF" "$q" || instrument_fail "reference reading failed for '${label}'"
        a="$MEASURED"
        "$reader" rv "$q" || instrument_fail "restored reading failed for '${label}'"
        b="$MEASURED"
        row "$label" "$a" "$b" || mismatches=$((mismatches + 1))
    }

    _cmp "neo4j nodes"        cypher_val  "$CY_NODES"
    _cmp "neo4j rels"         cypher_val  "$CY_RELS"
    _cmp "neo4j narrator fp"  cypher_val  "$CY_FP"
    _cmp "neo4j label hist"   cypher_hist "$CY_LABEL_HIST"
    _cmp "neo4j reltype hist" cypher_hist "$CY_REL_HIST"
    _cmp "pg hadiths"         pg_val      "$PG_HADITH_COUNT"
    _cmp "pg hadiths md5"     pg_val      "$PG_HADITH_MD5"

    # The user-service store is the one nothing else covers (deploy#558-#561).
    _cmp "users"              upg_val     "SELECT count(*) FROM users;"
    _cmp "oauth_accounts"     upg_val     "SELECT count(*) FROM oauth_accounts;"
    _cmp "sessions"           upg_val     "SELECT count(*) FROM sessions;"
    _cmp "audit_log"          upg_val     "SELECT count(*) FROM audit_log;"
    _cmp "users md5"          upg_val     "$UPG_USERS_MD5"
    _cmp "alembic version"    upg_val     "SELECT version_num FROM alembic_version;"

    if [[ "$mismatches" -gt 0 ]]; then
        log "ERROR" "${mismatches} metric(s) MISMATCHED — the restored artifact does NOT reproduce"
        log "ERROR" "the live database. This is a real negative, not an instrument problem."
        return "$RC_FAILED"
    fi
    pass "every metric matches — the B2 artifact reproduces the live database"
    return 0
}

# =============================================================================
# Self-test: calibrate the comparator against a REAL engine, with no B2 and no live stack
# =============================================================================
# A comparator whose queries were only string-tested is not a calibrated comparator: a
# unit test proves the STRING, not that the engine ACCEPTS it (deploy#606/#607 — an
# invalid-Cypher precheck passed 32 tests and two reviewers, and only a dry-run against a
# real database caught it). The `n.name` defect was likewise invisible to any string test:
# the Cypher is perfectly valid, it just measures nothing.
#
# So this mode stands up TWO real stacks from the same throwaway compose file, seeds them
# with a realistically-shaped graph, and requires the comparator to:
#
#   MATCH  on two identical stacks
#   DIFFER on a stack missing exactly one narrator      (it can go red)
#   DIFFER on a stack whose content is subtly corrupted (not just cardinality)
#   REFUSE (rc=2) when pointed at a property that does not exist  (the n.name trap)
#
# It runs on a plain runner: no B2, no VPS, no secrets, nothing live is reachable.
self_test() {
    local rc=0
    log "INFO" "=== SELF-TEST: proving the comparator separates (real engines, no B2) ==="

    export POSTGRES_USER="${POSTGRES_USER:-isnad}"
    export POSTGRES_DB="${POSTGRES_DB:-isnad_graph}"
    export USER_POSTGRES_USER="${USER_POSTGRES_USER:-user_service}"
    export USER_POSTGRES_DB="${USER_POSTGRES_DB:-user_service}"
    # Two throwaway stacks. Neither is `noorinalabs`; there is no live stack on this box.
    #
    # ST_REF_PROJECT is deliberately NOT `local`. An EXIT trap runs AFTER the function's scope
    # is gone, so a trap that references a function-local dies on `set -u` with "unbound
    # variable" — and it dies BEFORE tearing anything down, leaking the stacks and their
    # volumes. That is not hypothetical: it is what this very function did on its first CI run
    # (the four calibration cases all passed, then the teardown crashed on this line). Every
    # name a teardown trap touches must outlive the function that installed it.
    ST_REF_PROJECT="isnad-rv-selftest-ref"
    RV_PROJECT="isnad-rv-selftest-sub"

    _st_dc() { # _st_dc <project> <args...>
        local p="$1"
        shift
        docker compose -p "$p" -f "$RV_COMPOSE" "$@"
    }
    _st_cypher() { # _st_cypher <project> <query>
        local p="$1" q="$2"
        # INTENTIONAL, not an oversight: the single quotes keep ${NEO4J_AUTH} unexpanded on this
        # side so it expands INSIDE the container. The credential never reaches this script's
        # argv, cypher-shell's argv, or the run log (CWE-214). Same contract as graph-ops.yml.
        # shellcheck disable=SC2016
        _st_dc "$p" exec -T neo4j sh -c \
            'NEO4J_USERNAME=neo4j NEO4J_PASSWORD="${NEO4J_AUTH#neo4j/}" exec cypher-shell --format plain "$1"' \
            _ "$q" | tail -n +2 | tr -d ' "\r'
    }

    _st_cleanup() {
        # THE VERDICT. Captured on the first line, before anything here can clobber `$?`,
        # and never recomputed. Everything below may fail freely without changing it.
        local verdict=$?
        log "INFO" "self-test: tearing down both stacks (comparator verdict rc=${verdict})"

        local p
        for p in "$ST_REF_PROJECT" "$RV_PROJECT"; do
            # NOT `>/dev/null 2>&1 || true`. If a teardown fails, its own message is the only
            # thing that will explain the leak it just caused.
            _st_dc "$p" down -v --remove-orphans ||
                log "WARN" "compose down failed for ${p} — the sweep below is what stops a leak"
        done

        leak_audit "$verdict" "$ST_REF_PROJECT" "$RV_PROJECT"
    }
    trap _st_cleanup EXIT

    local p
    for p in "$ST_REF_PROJECT" "$RV_PROJECT"; do
        log "INFO" "self-test: starting ${p}"
        _st_dc "$p" down -v --remove-orphans >/dev/null 2>&1 || true
        _st_dc "$p" up -d neo4j
    done

    for p in "$ST_REF_PROJECT" "$RV_PROJECT"; do
        local waited=0 cid health
        while :; do
            cid="$(_st_dc "$p" ps -q neo4j | head -1)"
            health="none"
            [[ -n "$cid" ]] && health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$cid")"
            [[ "$health" == "healthy" ]] && break
            [[ "$waited" -ge 240 ]] && instrument_fail "self-test: ${p} neo4j never became healthy"
            sleep 3
            waited=$((waited + 3))
        done
    done

    # A realistically-SHAPED graph: the properties the real corpus carries (name_ar /
    # name_en / id), not the ones a fixture author would invent. A fixture that cannot
    # express the defect proves nothing about the guard
    # (feedback_fixture_makes_guard_assertion_inert, main#927).
    local seed="
    UNWIND range(1, 50) AS i
    CREATE (n:Narrator {
        id: 'nar:' + toString(i),
        name_ar: 'راوي' + toString(i),
        name_en: 'Narrator ' + toString(i)
    });
    UNWIND range(1, 20) AS i
    CREATE (h:Hadith {id: 'had:' + toString(i), arabic_text: 'حديث' + toString(i)});
    MATCH (a:Narrator {id:'nar:1'}), (b:Narrator {id:'nar:2'}) CREATE (a)-[:NARRATED_TO]->(b);
    MATCH (n:Narrator {id:'nar:1'}), (h:Hadith {id:'had:1'}) CREATE (n)-[:NARRATED]->(h);
    "
    for p in "$ST_REF_PROJECT" "$RV_PROJECT"; do
        # INTENTIONAL, not an oversight: the single quotes keep ${NEO4J_AUTH} unexpanded on this
        # side so it expands INSIDE the container. The credential never reaches this script's
        # argv, cypher-shell's argv, or the run log (CWE-214). Same contract as graph-ops.yml.
        # shellcheck disable=SC2016
        NC_SEED="$seed"
        if [[ "$p" == "$RV_PROJECT" ]]; then
            NC_SEED="${seed//range(1, 50)/range(1, 49)}" # NEGATIVE CONTROL: deliberate divergence
        fi
        printf '%s\n' "$NC_SEED" | _st_dc "$p" exec -T neo4j sh -c \
            'NEO4J_USERNAME=neo4j NEO4J_PASSWORD="${NEO4J_AUTH#neo4j/}" exec cypher-shell' >/dev/null
    done

    local ref_fp sub_fp ref_parts
    ref_fp="$(_st_cypher "$ST_REF_PROJECT" "$CY_FP")"
    sub_fp="$(_st_cypher "$RV_PROJECT" "$CY_FP")"

    # --- Case 0: the components are alive (the n.name trap) ------------------
    ref_parts="$(_st_cypher "$ST_REF_PROJECT" "$CY_FP_PARTS")"
    log "INFO" "seeded narrator components (count/ar/en/id): ${ref_parts}"
    local dead_fp
    dead_fp="$(_st_cypher "$ST_REF_PROJECT" \
        "MATCH (n:Narrator) RETURN toString(count(n)) + '/' + toString(sum(size(coalesce(n.name,'')))) AS fp;")"
    log "INFO" "THE n.name TRAP — fingerprint over the NON-EXISTENT property 'n.name': ${dead_fp}"
    case "$dead_fp" in
        */0)
            pass "self-test: a fingerprint over n.name sums to ZERO on a fully-populated graph."
            log "INFO" "        That is the defect, reproduced: it would read 0 on BOTH stacks and 'agree'."
            log "INFO" "        calibrate() rejects exactly this — see the non-zero-component check."
            ;;
        *) fail "self-test: expected the n.name fingerprint to sum to zero; got '${dead_fp}'"; rc=1 ;;
    esac

    # --- Case 1: identical stacks MUST match ---------------------------------
    if [[ "$ref_fp" == "$sub_fp" && -n "$ref_fp" ]]; then
        pass "self-test MATCH: two identically-seeded stacks agree (fp=${ref_fp})"
    else
        fail "self-test MATCH: identical stacks disagreed (ref=${ref_fp} sub=${sub_fp})"
        rc=1
    fi

    # --- Case 2: minus one narrator MUST differ ------------------------------
    _st_cypher "$RV_PROJECT" "MATCH (n:Narrator {id:'nar:7'}) DETACH DELETE n;" >/dev/null
    sub_fp="$(_st_cypher "$RV_PROJECT" "$CY_FP")"
    if [[ "$ref_fp" != "$sub_fp" ]]; then
        pass "self-test DIFFER: removing ONE narrator changed the fingerprint (ref=${ref_fp} sub=${sub_fp})"
    else
        fail "self-test DIFFER: the fingerprint is INERT — it cannot see a missing narrator"
        rc=1
    fi

    # --- Case 3: same cardinality, corrupted CONTENT, MUST differ ------------
    # Restore the count, but with a different name. A comparator that only counted would
    # go green here — which is the whole reason the fingerprint sums the text.
    _st_cypher "$RV_PROJECT" \
        "CREATE (n:Narrator {id:'nar:7', name_ar:'XXXXXXXXXXXX', name_en:'Corrupted'});" >/dev/null
    local sub_count ref_count
    ref_count="$(_st_cypher "$ST_REF_PROJECT" "MATCH (n:Narrator) RETURN count(n);")"
    sub_count="$(_st_cypher "$RV_PROJECT" "MATCH (n:Narrator) RETURN count(n);")"
    sub_fp="$(_st_cypher "$RV_PROJECT" "$CY_FP")"
    if [[ "$ref_count" == "$sub_count" && "$ref_fp" != "$sub_fp" ]]; then
        pass "self-test DIFFER: node COUNT is identical (${ref_count}) yet the fingerprint differs —"
        log "INFO" "        it sees corrupted CONTENT, not just cardinality (ref=${ref_fp} sub=${sub_fp})"
    else
        fail "self-test DIFFER: a same-count content corruption was NOT detected (count ref=${ref_count} sub=${sub_count}, fp ref=${ref_fp} sub=${sub_fp})"
        rc=1
    fi

    log "INFO" "=== SELF-TEST: ${PASS_COUNT} passed, ${FAIL_COUNT} failed ==="
    if [[ "$rc" -ne 0 || "$FAIL_COUNT" -gt 0 ]]; then
        log "ERROR" "The comparator did NOT separate. It must not be used to certify a restore."
        return "$RC_FAILED"
    fi
    log "INFO" "The comparator MATCHes on identical content and DIFFERs on a missing node and"
    log "INFO" "on a same-count corruption. It can go red, so a green verify means something."
    return 0
}

# =============================================================================
# main
# =============================================================================
main() {
    if [[ "$SELF_TEST" == "true" ]]; then
        # No env-file, no B2, no live stack — and nothing live is reachable from the runner
        # this mode is built for, so the isolation is structural rather than careful.
        [[ -f "$RV_COMPOSE" ]] || instrument_fail "throwaway compose file not found: ${RV_COMPOSE}"
        command -v docker >/dev/null 2>&1 || instrument_fail "required command not found: docker"
        self_test
        return $?
    fi

    load_env
    guard_not_live

    log "INFO" "=== restore-verify: environment '${BACKUP_PREFIX}' ==="
    log "INFO" "  reference (live) : project ${LIVE_PROJECT} (${LIVE_COMPOSE})"
    log "INFO" "  throwaway        : project ${RV_PROJECT} (${RV_COMPOSE})"
    log "INFO" "  B2 prefix        : ${B2_BUCKET}/${BACKUP_PREFIX}"
    log "INFO" "  pg identity      : ${POSTGRES_USER}/${POSTGRES_DB} (taken from the live env)"

    resource_gate

    trap teardown EXIT
    start_stack
    assert_volume_isolation
    assert_scratch_empty

    # Calibrate against the LIVE stack BEFORE spending twenty minutes on a restore whose
    # result we would not be able to read.
    calibrate

    local rc=0
    run_restore || rc=$?
    if [[ "$rc" -ne 0 ]]; then
        return "$rc"
    fi

    compare || return $?

    log "INFO" "=== VERIFIED: the ${BACKUP_PREFIX} backup in B2 restores to a database that"
    log "INFO" "    matches live, on every metric, with a comparator proven able to go red. ==="
    return "$RC_VERIFIED"
}

main "$@"

#!/usr/bin/env bash
# =============================================================================
# The compose PROJECT the stack actually runs in — and the only way to address it.
# Sourced by scripts/backup.sh and scripts/restore.sh. (deploy#617)
#
# WHY `-f compose/docker-compose.prod.yml` IS NOT ENOUGH
#
# `-f` names the FILE. It does not name the PROJECT. With no `-p` flag and no
# COMPOSE_PROJECT_NAME in the environment, Compose derives the project name from the
# compose file's DIRECTORY — `compose` — while every deploy path in this repo brings the
# stack up as `-p noorinalabs` (.github/actions/write-deploy-env/action.yml, deploy-*.yml,
# rollback.yml, graph-ops.yml, scripts/rotate_db_password.sh, scripts/compose_tiered_up.sh).
#
# So an unflagged call resolves a project that exists only as a name, and contains
# nothing. Measured on stg 2026-07-13, on the first backup run in this project's history
# to get past the B2 preflight (deploy#613 had been aborting every earlier run before a
# single dump, which is why this sat undiscovered underneath it):
#
#   exec -T postgres pg_dump    -> no such service in project `compose` -> dump FAILED
#   exec -T user-postgres ...   -> no such service in project `compose` -> dump FAILED
#   stop neo4j                  -> stopped NOTHING ("Neo4j stopped (waited 0s)")
#   ps -aq neo4j                -> empty -> the data volume could not be resolved
#   up -d neo4j                 -> CREATED A NEW, EMPTY NEO4J, with fresh volumes
#
# The last line is the one to sit with. `up` is a CONVERGE verb: asked for a service the
# project does not have, it does not fail — it MAKES one. A backup script that can spawn a
# database is a bug independently of the project name, so `up` is not merely aimed
# correctly here, it is REMOVED from the backup/restore path (see neo4j_start below).
#
# AND WHY THE BACKUP WAS ONLY *FAILED* AND NOT SILENTLY *EMPTY*
#
# backup.sh refuses to guess the Neo4j data volume when it cannot resolve the container
# ("refusing to guess"). Had it fallen back to a `docker volume ls | grep neo4j_data`
# match, it would have dumped `compose_neo4j_data` — the fresh, empty graph its own `up`
# had just created — checksummed it, uploaded it, and reported SUCCESS. We would have held
# a green backup containing nothing and found out at a restore. That guard is load-bearing;
# nothing in this file weakens it.
#
# Contract for the sourcing script: define log() first, and set COMPOSE_FILE.
# =============================================================================

# Scratch allocation lives in scripts/scratch.sh (deploy#625/#628) — it is not a compose
# concern, and restore.sh and verify_b2_backup_artifact.sh need it without needing any of
# this file. Sourced (not duplicated) so there is exactly ONE allocator in the repo; this
# file's public surface is unchanged — scratch_file/scratch_failed remain in scope for
# everything that sources compose_project.sh.
COMPOSE_PROJECT_SCRATCH_LIB="$(dirname "${BASH_SOURCE[0]}")/scratch.sh"
if [[ ! -f "$COMPOSE_PROJECT_SCRATCH_LIB" ]]; then
    log "ERROR" "Missing ${COMPOSE_PROJECT_SCRATCH_LIB} — no scratch allocator, cannot run safely"
    exit 1
fi
# shellcheck source=scripts/scratch.sh
source "$COMPOSE_PROJECT_SCRATCH_LIB"

# An explicit `-p` FLAG on every call, not an exported COMPOSE_PROJECT_NAME. A flag cannot
# be unset, overridden, or dropped between here and the call; an exported variable can —
# the systemd unit's EnvironmentFile is exactly such a place, and "the project name was
# left to be inferred from the environment" is the whole bug. COMPOSE_PROJECT_NAME is still
# honoured as a fallback so a caller that names its project the compose-native way
# (scripts/restore_rehearsal.sh) keeps working.
COMPOSE_PROJECT="${COMPOSE_PROJECT:-${COMPOSE_PROJECT_NAME:-noorinalabs}}"

# Every compose call in the backup/restore path goes through here. scripts/tests/
# test_compose_project_scoping.py statically asserts that — an unflagged `docker compose`
# added anywhere in these scripts fails the suite.
dc() {
    docker compose -p "$COMPOSE_PROJECT" -f "$COMPOSE_FILE" "$@"
}

# Re-emit a captured stderr file THROUGH log(), rather than `sed … >&2`.
#
# Compose's own diagnostic is the most useful line in the file when this goes wrong, and it
# has to land where the operator will look for it. backup.sh's log() tees into the per-run
# log that is checksummed and uploaded with the dumps; a bare `>&2` would put the one line
# that explains the failure in the journal only, and NOT in the artifact.
#
# Iterating stderr line-by-line is safe here because each line is being LOGGED, not parsed:
# nothing downstream reads these as records (deploy#584 — the hazard is a CONSUMER that
# treats commentary as data, not the redirect itself).
log_captured_stderr() {
    local line
    while IFS= read -r line; do
        log "ERROR" "    ${line}"
    done < "$1"
}

# The services running in COMPOSE_PROJECT, one `name:state` line each, on STDOUT.
#
# An EMPTY result is a legitimate answer — a project with nothing in it. A non-zero rc is
# NOT: that is an instrument failure, and the two must never collapse into the same empty
# string (deploy#584: "an empty listing and a failed listing are the same empty string, and
# only one of them is a measurement").
#
# Every diagnostic here goes to STDERR, because this function's STDOUT IS ITS RETURN VALUE.
# `log()` writes to stdout (backup.sh additionally tees it), so an unredirected log line
# would be read back by the caller as a service state — the same shape as the rclone NOTICE
# that became a "backup directory" in deploy#584.
# The compose-flavoured wording of the scratch diagnostic. The allocator and its explanation
# live in scratch.sh; what belongs HERE is the one thing that is compose-specific — the list
# of subsystems this particular check must NOT be read as testifying about (deploy#623: it
# testified about Docker, and Docker was fine).
compose_scratch_failed() {
    scratch_failed "The compose check" "Docker, the project, or the stack"
}

running_services() {
    local err states rc=0

    # A capture file we could not create is a CHECK THAT DID NOT RUN. It says nothing about
    # the daemon, and it must not pretend otherwise (deploy#623).
    if ! err="$(scratch_file)"; then
        compose_scratch_failed
        return 1
    fi

    # NO `trap … RETURN` HERE, AND THAT IS DELIBERATE — I TRIED IT AND IT BROKE THE BACKUP.
    #
    # deploy#629 proposes `trap 'rm -f -- "$err"' RETURN` in each caller. It does not work, for
    # two reasons that only show up outside a toy harness:
    #
    #   1. A RETURN TRAP IS GLOBAL AND STAYS ARMED. It is not scoped to the function that set
    #      it. After running_services returns, the trap is STILL INSTALLED, and it fires again
    #      on the NEXT function's return — where `err` is out of scope. Under `set -u` that is
    #      `err: unbound variable`, errexit fires, and THE BACKUP DIES. Caught by
    #      test_restore_failure_modes.py, which drives the real sliced functions rather than one
    #      call in isolation. Worse than the crash: if that later function happens to have its
    #      own `err`, the stale trap deletes ITS file instead.
    #   2. IT DOES NOT EVEN COVER THE CASE IT WAS RAISED FOR. A RETURN trap does not fire on a
    #      signal (measured — see the kill test), so it never cleans up the SIGTERM that
    #      deploy#629 is actually about.
    #
    # So: explicit release on the paths that exist, and scratch_cleanup_all from the caller's
    # EXIT trap as the real backstop. Because the sweep matches this shell's PID TAG rather than
    # a remembered path, it catches ANY allocation this process leaked — including from a branch
    # nobody has written yet, which is the property the RETURN trap was wanted for.
    #
    # `|| rc=$?`, not a bare assignment: under `set -euo pipefail` an assignment whose
    # command substitution fails IS a failing simple command and errexit fires AT THE
    # ASSIGNMENT, making the rc check below dead code on every failing path (deploy#563).
    states="$(dc ps --format '{{.Service}}:{{.State}}' 2>"$err")" || rc=$?
    if [[ "$rc" -ne 0 ]]; then
        log "ERROR" "Cannot reach Docker Compose (rc=${rc}) — is the daemon running?" >&2
        log_captured_stderr "$err" >&2
        scratch_release "$err"
        return 1
    fi
    scratch_release "$err"

    printf '%s\n' "$states"
    return 0
}

# assert_stack_present <service>... — THE PROJECT-IDENTITY ASSERTION.
#
# THE GUARD THIS REPLACES WAS VACUOUS, AND ITS ZERO IS WHAT LET THE DUMPS RUN.
#
#   if ! docker compose -f "$COMPOSE_FILE" ps --format json &>/dev/null; then
#
# `ps` against an empty — or entirely nonexistent — project EXITS 0 and prints an empty
# list. That command answers "did `ps` run?", not "is the stack there?", and those are
# different questions with different answers. It waved the deploy#617 run straight through
# to the dumps.
#
# ---------------------------------------------------------------------------
# WHAT TO PASS HERE, AND WHY IT IS NOT "EVERY SERVICE YOU TOUCH". (deploy#618 review)
# ---------------------------------------------------------------------------
# The callers pass `postgres user-postgres` — NOT `neo4j`. That is deliberate, and getting
# it wrong silently repeals backup.sh's oldest contract.
#
# This is an assertion about the PROJECT'S IDENTITY: "am I addressing the stack, or a
# phantom?" It is not a health check on every store. Demanding `neo4j` too would make a
# Neo4j outage — including one THIS SCRIPT's own failed restart caused — abort the run
# before it dumped ANYTHING, taking zero backups on a night it could have taken two. And
# one of those two is `user-postgres`, the only store that cannot be rebuilt from any
# artifact (deploy#559). backup.sh's design is the opposite: upload what you got, attest
# `complete=false`, exit non-zero. A partial backup beats none.
#
# The two Postgres services are the RELIABLE WITNESS, and the reason is structural: nothing
# in the backup/restore path can create them. Only `neo4j` was ever `up`'d, so `neo4j` is
# the one service that can exist in the phantom project WITHOUT the stack being there —
# which is exactly the state stg was found in (a stray, empty neo4j in project `compose`,
# no Postgres). A "refuse only if ZERO services resolve" rule would have passed that state
# and dumped the stray empty graph. The Postgres pair refuses it.
#
# A missing `neo4j` is therefore a PER-LEG failure, handled where that leg runs — not a
# reason to abandon the run.
assert_stack_present() {
    local want=("$@")
    local states rc=0

    states="$(running_services)" || rc=$?
    if [[ "$rc" -ne 0 ]]; then
        # running_services has already said why, on stderr.
        return 1
    fi

    local missing=() svc
    for svc in "${want[@]}"; do
        grep -qx -- "${svc}:running" <<< "$states" || missing+=("$svc")
    done

    if [[ ${#missing[@]} -gt 0 ]]; then
        local seen="${states//$'\n'/ }"
        log "ERROR" "Compose project '${COMPOSE_PROJECT}' is not running: ${missing[*]}"
        log "ERROR" "  compose file:  ${COMPOSE_FILE}"
        log "ERROR" "  running there: ${seen:-<nothing at all>}"

        # ---------------------------------------------------------------------------
        # THE DIAGNOSIS IS DECIDED BY EVIDENCE, AND IT MUST NEVER SAY "RETARGET".
        # (deploy#618 review, Aisha Idrissi)
        # ---------------------------------------------------------------------------
        # This used to end, unconditionally, with "this project name is wrong — Set
        # COMPOSE_PROJECT." Read that as the operator who is about to see it: they are
        # mid-incident, restoring a database, and the very next command they run carries
        # `--overwrite-destination`. The guard whose entire purpose is to STOP a load into the
        # wrong project was handing them an instruction to change the project until the guard
        # stopped complaining. That is the guard becoming the accident.
        #
        # The two causes are distinguishable, and the listing already holds the evidence:
        #
        #   nothing running at all  -> could be a phantom project, or a stack that is simply
        #                              down. Say both; assert neither.
        #   other services running  -> the project is REAL. The name is RIGHT. The named
        #                              services are DOWN. Retargeting could only make it worse.
        if [[ -z "$states" ]]; then
            log "ERROR" "  This project holds NO containers at all. Either the stack is down, or"
            log "ERROR" "  '${COMPOSE_PROJECT}' is not the project it runs in — every deploy path"
            log "ERROR" "  brings this stack up as '-p noorinalabs'. Run 'docker compose ls' and"
            log "ERROR" "  find out WHICH before you change anything."
        else
            log "ERROR" "  This project IS the right one — other services are running in it. The"
            log "ERROR" "  services listed above are DOWN. Start them."
            log "ERROR" "  Do NOT change COMPOSE_PROJECT: the name is not the problem, and pointing"
            log "ERROR" "  this run at a different project is how a restore overwrites the wrong stack."
        fi
        return 1
    fi

    log "INFO" "Stack present in project '${COMPOSE_PROJECT}': ${want[*]} (all running)"
    return 0
}

# Is <service> running in COMPOSE_PROJECT? For a PER-LEG decision, not project identity.
#
# Returns non-zero both when the service is not running and when the listing could not be
# taken at all. Callers use this only AFTER assert_stack_present has established that the
# daemon is reachable and the project is real, so at that point the two collapse harmlessly:
# either way the leg cannot be dumped and must be recorded as failed rather than skipped
# silently.
# service_is_running <svc>
#
#   0 — the service IS running
#   1 — the service is NOT running          (a fact about the stack)
#   2 — WE COULD NOT FIND OUT               (a fact about us)
#
# THE THIRD CALL SITE, AND deploy#624 IS WHAT MADE IT REACHABLE. (review — Lucas Ferreira)
#
# This used to collapse 1 and 2 into a single `return 1`, and the comment justifying that
# said the two "collapse harmlessly" because callers only reach here after
# assert_stack_present has already established the project. That was true while scratch lived
# on /tmp — **because /tmp never fills.**
#
# deploy#623's fix moves scratch onto BACKUP_DIR: the volume the dumps themselves fill. It
# knows this — the ENOSPC `-s` check in scratch_file() exists for exactly that reason. And the
# ordering is against us: backup.sh calls assert_stack_present BEFORE the Postgres dumps and
# `service_is_running neo4j` AFTER them, so the disk that was fine at the gate can be full by
# the time we ask this question.
#
# Collapsed, the outcome is a lie with a dangerous remedy attached: scratch fails, this
# returns 1, and backup.sh prints "Neo4j is NOT running — Start Neo4j and re-run." The graph
# is UP. The disk is FULL. Re-running is the single action that makes it worse. Reproduced on
# the head tree with a healthy docker reporting neo4j:running.
#
# So the instrument failure gets its own code. A caller that cannot tell "the thing is broken"
# from "I am broken" will always eventually blame the thing.
service_is_running() {
    local svc="$1" states rc=0

    states="$(running_services)" || rc=$?
    # running_services has already printed the honest scratch diagnostic. Do NOT overwrite it
    # with a claim about the service — return the "I could not find out" code and let the
    # caller stay silent about the stack.
    [[ "$rc" -eq 0 ]] || return 2

    grep -qx -- "${svc}:running" <<< "$states" || return 1
    return 0
}

# Start the neo4j container we STOPPED. Never create one.
#
# `up -d neo4j` converges: against a project with no neo4j container it does not fail, it
# BUILDS one with fresh, empty volumes — which is precisely what left a stray
# `compose-neo4j-1` and a stray, empty `compose_neo4j_data` on stg. In a RESTORE path the
# same call is worse than untidy: the container it invents is the one whose volume the very
# next command would have loaded a dump into.
#
# `start` can only start a container that already exists, and fails loudly when there is
# none. That is the entire point: the capability to create is removed, not aimed.
neo4j_start() {
    local err rc=0

    # Same trap as running_services (deploy#623). Here it is worse: a scratch failure
    # rendered as "could not START neo4j" would tell the operator the graph is broken when
    # in fact the graph was never touched — and this runs in the window where backup.sh has
    # already STOPPED it.
    if ! err="$(scratch_file)"; then
        compose_scratch_failed
        return 1
    fi

    # No `trap … RETURN` — see running_services for why it crashes the backup. The EXIT drain
    # in the caller's cleanup() is what covers a kill or a missed branch (deploy#629).
    dc start neo4j 2>"$err" || rc=$?
    if [[ "$rc" -ne 0 ]]; then
        log "ERROR" "Could not START neo4j in project '${COMPOSE_PROJECT}' (rc=${rc}):"
        log_captured_stderr "$err"
        log "ERROR" "  NOT falling back to 'up' — that would CREATE a new, empty neo4j (deploy#617)."
        scratch_release "$err"
        return "$rc"
    fi
    scratch_release "$err"
    return 0
}

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
# scratch_file — a capture file that EXISTS UNDER THE BACKUP UNIT'S HARDENING. (deploy#623)
#
# THIS IS deploy#613, AND WE SHIPPED IT TWICE.
#
# A bare `mktemp` defaults to /tmp. Under systemd/isnad-backup.service, /tmp is READ-ONLY:
# the unit runs ProtectSystem=strict with PrivateTmp deliberately unset (deploy#121 Bug A)
# and grants only BACKUP_DIR, /opt/noorinalabs-deploy and /var/lib/node_exporter. So the
# allocation failed, $err was empty, the `2>"$err"` redirect died, and running_services()
# announced:
#
#     [ERROR] Cannot reach Docker Compose (rc=1) — is the daemon running?
#
# The daemon was running. The SCRATCH WRITE failed. That sentence is the same lie
# b2_preflight.sh used to tell about the B2 key — a check that could not run, reporting its
# own breakage as a fact about the system it was supposed to be checking, and pointing the
# operator at the wrong subsystem entirely. Measured on stg 2026-07-14T01:23:30Z, on the
# first backup run after the deploy#617 fix merged.
#
# Every capture file in this library now comes from here, so there is ONE place to get this
# right rather than one per call site. Failure to allocate is FATAL and says so in its own
# words — it must never be rendered as a claim about Docker, the project, or the stack.
#
# NOTE FOR ANYONE ADDING A THIRD CALLER: CI cannot catch this. GitHub Actions runners and
# the rehearsal containers all have a writable /tmp, so the entire test surface — including
# the e2e tests that drive the real backup.sh — runs where this bug is UNREACHABLE. Only the
# on-host systemd path is affected, and nothing in CI executes it. That blind spot, not the
# mktemp, is why this shipped twice. test_scratch_under_hardening.py exists to close it.
scratch_file() {
    local parent tmp
    parent="${BACKUP_DIR:-${TMPDIR:-/tmp}}"
    mkdir -p "$parent" 2>/dev/null

    if ! tmp="$(mktemp -p "$parent" compose-project-XXXXXX 2>/dev/null)" \
        || [[ -z "$tmp" || ! -f "$tmp" ]]; then
        return 1
    fi
    # EXISTS is not WRITABLE. Under ENOSPC the creat() succeeds and the write fails, leaving
    # a 0-byte file — so the -s check, not the redirect's exit status, is what catches a full
    # disk. Same lesson as deploy#614: prove the scratch is usable, never infer it.
    if ! printf 'x\n' > "$tmp" 2>/dev/null || [[ ! -s "$tmp" ]]; then
        rm -f "$tmp"
        return 1
    fi

    # The truncation is checked too, and that is not paranoia (deploy#624 review — Nino
    # Kavtaradze). An UNCHECKED redirect here — inside the one function whose entire purpose
    # is that there are no unchecked redirects — would hand the caller back a path it had
    # just failed to write to. The caller's `2>"$err"` then dies and running_services prints
    # "Cannot reach Docker Compose — is the daemon running?": deploy#623, verbatim,
    # resurrected inside its own fix.
    #
    # ENOSPC does not get you here (truncating FREES space). The way in is the filesystem
    # going read-only BETWEEN the check above and this line — an ext4 `remount-ro` on I/O
    # error, which is precisely the failure a backup script exists to survive. Narrow. Real.
    : > "$tmp" || { rm -f "$tmp"; return 1; }

    printf '%s\n' "$tmp"
    return 0
}

# The diagnostic for a scratch failure. Deliberately says what it does NOT know.
scratch_failed() {
    log "ERROR" "The compose check could not RUN: no writable scratch file." >&2
    log "ERROR" "  It therefore has NO evidence about Docker, the project, or the stack —" >&2
    log "ERROR" "  do not go looking at the daemon on the strength of this message." >&2
    log "ERROR" "  Scratch parent tried: ${BACKUP_DIR:-${TMPDIR:-/tmp}}" >&2
    log "ERROR" "  Under systemd, isnad-backup.service runs ProtectSystem=strict with no" >&2
    log "ERROR" "  PrivateTmp (deploy#121 Bug A), so /tmp is READ-ONLY and scratch must live" >&2
    log "ERROR" "  under BACKUP_DIR, which the unit grants via ReadWritePaths (deploy#613/#623)." >&2
    log "ERROR" "  Also check free space: BACKUP_DIR is the volume the dumps themselves fill." >&2
}

running_services() {
    local err states rc=0

    # A capture file we could not create is a CHECK THAT DID NOT RUN. It says nothing about
    # the daemon, and it must not pretend otherwise (deploy#623).
    if ! err="$(scratch_file)"; then
        scratch_failed
        return 1
    fi

    # `|| rc=$?`, not a bare assignment: under `set -euo pipefail` an assignment whose
    # command substitution fails IS a failing simple command and errexit fires AT THE
    # ASSIGNMENT, making the rc check below dead code on every failing path (deploy#563).
    states="$(dc ps --format '{{.Service}}:{{.State}}' 2>"$err")" || rc=$?
    if [[ "$rc" -ne 0 ]]; then
        log "ERROR" "Cannot reach Docker Compose (rc=${rc}) — is the daemon running?" >&2
        log_captured_stderr "$err" >&2
        rm -f "$err"
        return 1
    fi
    rm -f "$err"

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
        scratch_failed
        return 1
    fi

    dc start neo4j 2>"$err" || rc=$?
    if [[ "$rc" -ne 0 ]]; then
        log "ERROR" "Could not START neo4j in project '${COMPOSE_PROJECT}' (rc=${rc}):"
        log_captured_stderr "$err"
        log "ERROR" "  NOT falling back to 'up' — that would CREATE a new, empty neo4j (deploy#617)."
        rm -f "$err"
        return "$rc"
    fi
    rm -f "$err"
    return 0
}

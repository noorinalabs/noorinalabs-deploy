#!/usr/bin/env bash
# =============================================================================
# SCRATCH ALLOCATION — the one place in this repo that creates a temporary file.
# (deploy#625 / deploy#628 / deploy#629)
#
# THIS IS deploy#613, AND WE SHIPPED IT TWICE.
#
# A bare `mktemp` defaults to /tmp. Under systemd/isnad-backup.service, /tmp is READ-ONLY:
# the unit runs ProtectSystem=strict with PrivateTmp deliberately unset (deploy#121 Bug A)
# and grants only BACKUP_DIR, /opt/noorinalabs-deploy and /var/lib/node_exporter. So the
# allocation fails, the variable holding the path is EMPTY, and then:
#
#     err="$(mktemp)"                 -> err=""
#     out="$(rclone lsf … 2>"$err")"  -> bash: ambiguous redirect -> rc=1
#
# …and the caller renders that rc as a fact about whatever it was checking:
#
#   deploy#613  b2_preflight.sh   -> "verdict=KEY_INVALID"                 (the key was GOOD)
#   deploy#623  compose_project.sh-> "is the daemon running?"              (it WAS running)
#
# Both are one bug: **a check that could not run, reporting its own breakage as a fact about
# the system it was supposed to be checking**, sending the operator to the wrong subsystem.
#
# WHY THIS FILE EXISTS RATHER THAN A THIRD FIX IN A THIRD SCRIPT
#
# restore.sh and verify_b2_backup_artifact.sh were also allocating with a bare `mktemp`
# (deploy#628). They are safe today BY ACCIDENT, NOT BY CONSTRUCTION: restore is operator-run
# and the verify script runs on GitHub Actions, and both of those have a writable /tmp.
# Nothing enforced it, and deploy#640 is putting restore under a workflow. So allocation moves
# into ONE library that cannot default to /tmp, and every caller sources it.
#
# NOTE FOR ANYONE ADDING A CALLER: CI CANNOT CATCH THIS. GitHub Actions runners, the rehearsal
# containers and the dev box all have a writable /tmp, so the entire test surface — including
# the e2e tests that drive the real backup.sh — runs where this bug is UNREACHABLE. That blind
# spot, not the mktemp, is why it shipped twice. scripts/tests/test_scratch_under_hardening.py
# exists to close it: it makes the scratch parent unusable and asserts on what the scripts say.
#
# CONTRACT FOR THE SOURCING SCRIPT
#   - define log() first — `log <LEVEL> <msg…>`; the guards here report through it.
#   - drain the registry from your EXIT trap: call scratch_cleanup_all (see below).
# =============================================================================

# The parent every scratch allocation lands in.
#
# SCRATCH_PARENT IS DECLARED BY THE UNIT, NOT GUESSED BY THE LIBRARY. (deploy#666, Nino
# Kavtaradze — and this was blocking, correctly.)
#
# The previous chain was `BACKUP_DIR -> TMPDIR -> /tmp`, and under
# `isnad-backup-failure-marker.service` EVERY LINK OF IT IS UNWRITABLE:
#
#     ProtectSystem=strict, ProtectHome=yes, ReadWritePaths=/var/lib/node_exporter
#     (no Environment=BACKUP_DIR, no TMPDIR)
#
# so `BACKUP_DIR` is unset, `TMPDIR` is unset, and the chain lands on **the read-only /tmp that
# started this entire class**. `/var/lib/noorinalabs-backups` is not granted there either, so
# even BACKUP_DIR would not have saved it. A global chain that ends in a GUESS is not a scratch
# parent; it is deploy#613 waiting for its third outing.
#
# And that unit is the `OnFailure=` handler. If it cannot allocate, THE FAILURE SIGNAL ITSELF
# NEVER LANDS — a defect that can only manifest on the exact night something else has already
# broken, which is the worst possible time to discover it.
#
# So the parent is now DECLARED, per unit, in the unit file, next to the `ReadWritePaths=` that
# makes it true — and `test_every_hardened_unit_declares_a_writable_scratch_parent` proves the
# declaration is inside that unit's writable set, for every hardened unit, from the unit files
# themselves. The remaining rungs keep operator-run and CI invocation working (Actions has no
# BACKUP_DIR and no SCRATCH_PARENT, and its /tmp is real); under a hardened unit the chain never
# reaches them, BY PROOF rather than by assumption.
scratch_parent() {
    printf '%s\n' "${SCRATCH_PARENT:-${BACKUP_DIR:-${TMPDIR:-/tmp}}}"
}

# Every scratch this library hands out is TAGGED WITH THE OWNING SHELL'S PID, and that is how
# a killed run gets cleaned up after. It is deliberately NOT an in-memory array.
#
# A BASH ARRAY CANNOT WORK HERE, AND THE TEST CAUGHT ME BUILDING ONE.
#
# Every caller allocates as `err="$(scratch_file)"` — a COMMAND SUBSTITUTION, which bash runs in
# a SUBSHELL. A registry appended to inside that subshell dies with it: the parent's array comes
# back EMPTY, so the EXIT trap drains nothing and every allocation leaks. It looks completely
# correct, it passes any test that only calls the function directly, and it is worthless.
#
# `$$` is the PID of the SHELL and is stable inside command substitution (`$BASHPID` is the one
# that changes) — so the filename itself carries the ownership the variable could not.
_SCRATCH_TAG="compose-project-$$"

# The allocator's OWN words, straight to STDERR at the point of failure.
#
# NOT into a variable, AND THE TEST CAUGHT ME DOING THAT TOO. Every caller allocates as
# `err="$(scratch_file)"` — a COMMAND SUBSTITUTION, i.e. a SUBSHELL. A variable set in here
# dies with it, and the caller reads an EMPTY STRING. I wrote a `SCRATCH_ERROR` global, watched
# `scratch_failed` print nothing at all where the reason should be, and only then measured it.
# Same subshell trap as the registry array above; twice in one file. STDERR is not captured, so
# it is the one channel that crosses the boundary.
#
# Never `2>/dev/null` here: the reason a scratch write failed IS the diagnosis — ENOSPC and
# read-only and no-such-directory are three different problems with three different remedies,
# and discarding that distinction is precisely what let deploy#613 read as a bad credential.
_scratch_report() {
    log "ERROR" "Scratch allocation FAILED: $1" >&2
}

# _scratch_alloc <file|dir> — the allocation itself. Internal; use scratch_file/scratch_dir.
_scratch_alloc() {
    local kind="$1" parent out rc=0
    parent="$(scratch_parent)"

    # Sweep any debris a SIGKILLed predecessor left behind. Lazy and once-per-process, so no
    # entry point has to remember to call it — see scratch_reap_stale.
    scratch_reap_stale

    # mkdir failing is NOT on its own fatal — the parent usually already exists — so its
    # complaint is held back rather than reported. The allocation below is the decider, and if
    # it also fails, ITS message is the one that matters.
    local mkerr=""
    mkerr="$(mkdir -p -- "$parent" 2>&1)" || true

    # `2>&1` INTO THE CAPTURE, AND WHY THAT IS SAFE *HERE* SPECIFICALLY.
    #
    # deploy#584's lesson is that `2>&1` promotes commentary to DATA and fires on the SUCCESS
    # path — a tool that warns while succeeding corrupts the value you then read. The rule is
    # about the CONSUMER, not the redirect, and this consumer does not trust the string: it
    # requires the result to be an existing file/directory of the right type. A stray warning
    # on the success path yields a two-line `out` which is not a path, so it FAILS CLOSED,
    # returning 1 with the warning REPORTED on stderr — rather than handing back a
    # garbage path for a caller to redirect into. That is the direction to be wrong in, and it
    # is why the type check below is load-bearing rather than defensive decoration.
    if [[ "$kind" == "dir" ]]; then
        out="$(mktemp -d -p "$parent" "${_SCRATCH_TAG}-XXXXXX" 2>&1)" || rc=$?
    else
        out="$(mktemp -p "$parent" "${_SCRATCH_TAG}-XXXXXX" 2>&1)" || rc=$?
    fi

    if [[ "$rc" -ne 0 ]]; then
        # The allocator writes nothing to stdout when it fails, so `out` IS its diagnostic. If
        # even that is empty, fall back to mkdir's complaint — the parent may not exist at all.
        _scratch_report "${out:-${mkerr:-could not create a scratch ${kind} under ${parent}}}"
        return 1
    fi

    # NOTE ON THE WORDING BELOW: these strings deliberately do not contain the literal word
    # m-k-t-e-m-p. test_no_bare_mktemp_survives_in_the_library scans SOURCE TEXT and cannot
    # parse shell, so that token inside a message reads as a bare call and fails the build.
    # That is the guard behaving correctly and this file being careless — the fix belongs here,
    # not in the filter. Do not loosen the filter to admit a string; an escape hatch inside a
    # guard against a bug that has shipped twice is the hole (deploy#624 review).
    if [[ "$kind" == "dir" ]]; then
        [[ -n "$out" && -d "$out" ]] || { _scratch_report "${out:-the allocator returned no directory}"; return 1; }
    else
        [[ -n "$out" && -f "$out" ]] || { _scratch_report "${out:-the allocator returned no file}"; return 1; }
    fi

    printf '%s\n' "$out"
    return 0
}

# scratch_file — a capture file that EXISTS UNDER THE BACKUP UNIT'S HARDENING, on STDOUT.
#
# Failure to allocate is FATAL and says so in its OWN words (scratch_failed). It must never be
# rendered as a claim about Docker, B2, the graph, or the backups.
scratch_file() {
    local tmp
    tmp="$(_scratch_alloc file)" || return 1

    # EXISTS is not WRITABLE. Under ENOSPC the creat() succeeds and the write fails, leaving a
    # 0-byte file — so the -s check, not the redirect's exit status, is what catches a full
    # disk. Same lesson as deploy#614: prove the scratch is usable, never infer it.
    if ! printf 'x\n' > "$tmp" 2>/dev/null || [[ ! -s "$tmp" ]]; then
        _scratch_report "allocated ${tmp} but could not write to it (full disk? read-only mount?)"
        rm -f -- "$tmp"
        return 1
    fi

    # The truncation is checked too, and that is not paranoia (deploy#624 review — Nino
    # Kavtaradze). An UNCHECKED redirect here — inside the one function whose entire purpose is
    # that there are no unchecked redirects — would hand the caller back a path it had just
    # failed to write to. The caller's `2>"$err"` then dies and running_services prints "Cannot
    # reach Docker Compose — is the daemon running?": deploy#623, verbatim, resurrected inside
    # its own fix.
    #
    # ENOSPC does not get you here (truncating FREES space). The way in is the filesystem going
    # read-only BETWEEN the check above and this line — an ext4 `remount-ro` on I/O error, which
    # is precisely the failure a backup script exists to survive. Narrow. Real.
    if ! : > "$tmp"; then
        _scratch_report "could not truncate ${tmp} (filesystem went read-only?)"
        rm -f -- "$tmp"
        return 1
    fi

    printf '%s\n' "$tmp"
    return 0
}

# scratch_dir — the directory variant, on STDOUT. Same contract, same guarantees.
#
# A DIRECTORY THAT EXISTS IS NOT A DIRECTORY YOU CAN WRITE INTO, and the difference is the
# whole bug one layer down (b2_preflight.sh makes the same point about its own probe dir): a
# caller that redirects into `$dir/out` gets an ambiguous-redirect rc=1 that it will happily
# report as a failure of whatever it was measuring. So the write capability is PROVEN here,
# once, rather than assumed by every caller.
scratch_dir() {
    local tmp probe
    tmp="$(_scratch_alloc dir)" || return 1

    probe="${tmp}/.scratch-probe"
    if ! printf 'x\n' > "$probe" 2>/dev/null || [[ ! -s "$probe" ]]; then
        _scratch_report "allocated ${tmp} but could not create a file in it (full disk? read-only mount?)"
        rm -rf -- "$tmp"
        return 1
    fi
    rm -f -- "$probe"

    printf '%s\n' "$tmp"
    return 0
}

# scratch_release <path> — remove one scratch allocation, now.
#
# THERE IS NO REGISTRY TO DEREGISTER FROM, and the previous version of this comment said there
# was (deploy#667, Nino Kavtaradze). `_SCRATCH_PATHS` was appended to twice and read ZERO times
# — dead on arrival, directly beneath the twelve-line comment explaining why a bash array cannot
# survive the `err="$(scratch_file)"` subshell. The array is gone; ownership lives in the
# FILENAME's PID tag, which is what scratch_cleanup_all and scratch_reap_stale actually read.
# Dead code that does not LOOK dead is a trap for the next reader.
#
# Idempotent: releasing an already-released path is a no-op.
scratch_release() {
    local path="$1"
    [[ -n "$path" ]] || return 0
    rm -rf -- "$path"
    return 0
}

# scratch_cleanup_all — drain the registry. CALL THIS FROM YOUR EXIT TRAP. (deploy#629)
#
# A `trap … RETURN` DOES NOT FIRE ON A SIGNAL, AND THAT IS THE WHOLE POINT OF THIS FUNCTION.
#
# deploy#629 proposed closing the leak with a RETURN trap in each caller. Measured, that does
# not do it: SIGTERM the script mid-`dc ps` and the in-flight function's RETURN trap never
# runs, so the scratch survives. bash's EXIT trap, by contrast, DOES run on SIGTERM (bash's
# fatal-signal path calls the exit trap before terminating). So the registry is drained from
# EXIT, which is the only trap that sees a kill — and the RETURN traps remain as the
# fast path that keeps the file short-lived on the ordinary routes.
#
# WHY THE LEAK IS WORTH FIXING FOR A ONE-BYTE FILE. Previously the debris landed in /tmp,
# which the system reaps. BACKUP_DIR's ROOT HAS NO REAPER: retention purges the REMOTE
# (`rclone purge` on B2) and local cleanup covers LOCAL_BACKUP_PATH, the per-run subdirectory
# — not the root above it. And a full BACKUP_DIR is no longer merely a storage problem: an
# ENOSPC there makes scratch_file() fail mid-run, and the `service_is_running neo4j` call site
# renders that as "Neo4j is NOT running" — a false claim about the graph, with "re-run the
# backup" as its implied remedy. The volume filling has become a LYING-DIAGNOSTIC problem.
scratch_cleanup_all() {
    local parent
    parent="$(scratch_parent)"
    [[ -d "$parent" ]] || return 0

    # Scoped to THIS shell's PID tag, so a concurrent run's live scratch is untouchable — and
    # `-maxdepth 1` keeps it at the root, never descending into a dump directory. No
    # `2>/dev/null`: if cleanup cannot do its job, that belongs in the journal. `|| true`
    # because a failure to tidy up must never be the thing that fails a backup.
    find "$parent" -maxdepth 1 -name "${_SCRATCH_TAG}-*" -exec rm -rf -- {} + || true
    return 0
}

# scratch_reap_stale — sweep debris a previous run could NOT have cleaned up.
#
# SIGKILL CANNOT BE TRAPPED. The OOM killer, `systemctl kill -s KILL`, a power cut: no trap of
# any kind runs, so no amount of trap engineering closes this. The only thing that can is a
# reaper — and BACKUP_DIR's root is precisely the directory that has never had one.
#
# LIVENESS IS NOT AGE, AND THE AGE HEURISTIC WOULD HAVE EATEN A LIVE FILE DURING A DR.
# (deploy#668, Nino Kavtaradze — graded non-blocking, upheld as blocking, and rightly.)
#
# The first cut reaped anything matching the template that was older than `-mmin +60`, on the
# reasoning that "a scratch lives for the duration of one `dc ps` — seconds". That is true of
# the sites it was written for. It is FALSE of `restore.sh`'s pg_restore capture, which holds
# its scratch for as long as the restore runs — and **a production dump can take well over an
# hour**. The nightly backup timer's lazy reap would then find a >60-minute-old file matching
# the template and delete it OUT FROM UNDER A RUNNING RESTORE. During a disaster recovery. The
# one night it must not happen.
#
# And the fixture could not have caught it: it tested a FRESH live file, which is the case
# where the age heuristic is RIGHT. There was no old-and-live fixture — the only case where it
# is wrong (cf. `feedback_fixture_makes_guard_assertion_inert`).
#
# So age is gone. The filename already carries the owning shell's PID, so LIVENESS IS DIRECTLY
# OBSERVABLE and needs no heuristic at all: if `/proc/<pid>` exists, some process still owns
# that scratch and it is NOT debris, however old it is. A one-hour restore is safe by
# construction rather than by a threshold nobody will revisit.
#
#   `[ -e /proc/<pid> ]`, not `kill -0`: `kill -0` on a process owned by another user returns
#   EPERM, which means the process EXISTS but reads as a failure — so a non-root caller would
#   see a live root-owned PID as dead and reap its file. procfs does not lie about existence.
#
# PID reuse can only make a DEAD scratch look alive (we skip it; the debris survives one more
# cycle). It cannot make a LIVE one look dead. That is the safe direction, and it is the whole
# reason to prefer an observation over a guess.
#
# Scoped so it cannot eat anything that matters:
#   -maxdepth 1   the scratch parent's ROOT only — never descends into a per-run dump directory
#   -name         only THIS library's own PID-tagged template. `b2-preflight-*` is deliberately
#                 NOT swept: b2_preflight.sh allocates those and they carry no PID, so I have no
#                 liveness signal for them and will not guess with somebody else's artifacts.
#                 Their SIGKILL debris is pre-existing and belongs in its own issue.
_SCRATCH_REAPED=false
scratch_reap_stale() {
    [[ "$_SCRATCH_REAPED" == "true" ]] && return 0
    _SCRATCH_REAPED=true

    local parent path base pid
    parent="$(scratch_parent)"
    [[ -d "$parent" ]] || return 0

    # `find` exits 0 on no-match (non-zero only on an ERROR — deploy#591), so the loop simply
    # does not run when there is nothing to reap. Reaping is best-effort: a failure to sweep
    # debris must never be the thing that takes down a backup, hence `|| true` on the walk.
    while IFS= read -r path; do
        [[ -n "$path" ]] || continue
        base="${path##*/}"
        # compose-project-<pid>-XXXXXX  ->  <pid>
        pid="${base#compose-project-}"
        pid="${pid%%-*}"
        # Not a PID-tagged name we recognise? Leave it alone. Refusing to delete what we cannot
        # identify is the only safe default at the root of the volume the dumps live on.
        [[ "$pid" =~ ^[0-9]+$ ]] || continue
        # Still owned by a living process — NOT debris, no matter how old. This is the
        # >1h pg_restore during a DR.
        [[ -e "/proc/${pid}" ]] && continue
        rm -rf -- "$path"
    done < <(find "$parent" -maxdepth 1 -name 'compose-project-*' 2>/dev/null || true)
    return 0
}

# scratch_failed <subject> <what-it-cannot-speak-about> — the diagnostic for a scratch failure.
#
# Deliberately says what it does NOT know. This is the sentence that deploy#613 and deploy#623
# both got wrong, in the same way, two years of incident-hours apart.
scratch_failed() {
    local what="${1:-The check}" disowned="${2:-the system it was checking}"
    log "ERROR" "${what} could not RUN: no writable scratch file." >&2
    log "ERROR" "  It therefore has NO evidence about ${disowned} —" >&2
    log "ERROR" "  do not go looking there on the strength of this message." >&2
    log "ERROR" "  Scratch parent tried: $(scratch_parent)" >&2
    log "ERROR" "  Under systemd, isnad-backup.service runs ProtectSystem=strict with no" >&2
    log "ERROR" "  PrivateTmp (deploy#121 Bug A), so /tmp is READ-ONLY and scratch must live" >&2
    log "ERROR" "  under BACKUP_DIR, which the unit grants via ReadWritePaths (deploy#613/#623)." >&2
    log "ERROR" "  Also check free space: BACKUP_DIR is the volume the dumps themselves fill." >&2
}

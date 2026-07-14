#!/usr/bin/env bash
# =============================================================================
# B2 credential + bucket preflight for scripts/backup.sh (deploy#559)
#
# WHY THIS IS NOT ERROR-STRING PARSING
#
# rclone's failure text does not identify the failure. Measured against the live B2
# account on 2026-07-09 with a bucket-scoped key, these two situations produce a
# BYTE-IDENTICAL message:
#
#   (a) the bucket does not exist
#   (b) the bucket exists but the key is not scoped to it
#
#   CRITICAL: Failed to create file system for destination "isnad:<bucket>/":
#   you must use bucket(s) [{"3c86..." "noorinalabs-pipeline"}] with this application key
#
# B2 refuses at key-scope before it ever looks at the bucket. Separately, a read-only
# key attempting a write surfaces its 401 as "failed to create bucket" -- a message that
# names the wrong problem entirely and has already cost this project time
# (reference_pipeline_b2_publish_key). Any classifier keyed on those strings will
# misdiagnose the next person exactly the way rclone misdiagnosed us.
#
# So we classify by CAPABILITY PROBE, not by message:
#
#   1. `rclone lsd` -- can the key enumerate buckets at all, and is the target among
#      them? A bucket-scoped key lists only its own bucket, so visibility here answers
#      "is this key scoped to this bucket" definitively.
#   2. write a canary object     -> proves `writeFiles`
#   3. delete the canary object  -> proves `deleteFiles`, which backup.sh's retention
#      prune (`rclone purge`) requires and which a read-only key silently lacks
#
# Each probe's outcome is a fact. The error text is used only to enrich the human-facing
# diagnosis, never to decide it.
#
# A PROBE THAT COULD NOT RUN IS NOT EVIDENCE ABOUT THE KEY (deploy#613)
#
# The probes need somewhere to capture rclone's output. `mktemp -d` defaults to /tmp, and
# /tmp is READ-ONLY inside systemd/isnad-backup.service: it runs ProtectSystem=strict with
# PrivateTmp deliberately unset (deploy#121 Bug A), and /tmp is not in ReadWritePaths. The
# scratch allocation therefore failed on every scheduled run, its status went unchecked,
# the redirect to `${tmp}/lsd.out` degraded to `/lsd.out` on the read-only root, lsd_rc
# came back 1 -- and lsd_rc != 0 was read as "the key cannot list buckets", i.e.
# KEY_INVALID. That verdict is key-INDEPENDENT: the same freshly-provisioned,
# write-capable key that produced KEY_INVALID under the unit produced OK when run by hand
# with a writable /tmp (stg, 2026-07-13). Every backup this project has ever attempted
# died there, blaming a credential that was fine.
#
# So: scratch lives under BACKUP_DIR (which the unit DOES grant, and which the unit's own
# comment already assumed backup.sh would use), a failure to allocate it is FATAL, and
# "the probe never ran" is its own verdict -- PROBE_FAILED -- which can never be mistaken
# for a statement about the credential.
#
# NEVER set RCLONE_DUMP (or any equivalent). An ambient `RCLONE_DUMP=auth` once leaked
# base64 credentials past GitHub's log masking in a PUBLIC repo. This script prints no
# credential material: it reports presence and length only.
#
# Usage:
#   ./scripts/b2_preflight.sh              # requires B2_KEY_ID/B2_APP_KEY/B2_BUCKET/BACKUP_PREFIX
#   source ./scripts/b2_preflight.sh       # to reuse classify_b2_state()
#
# BACKUP_PREFIX ("stg"|"prod") is REQUIRED and has no default: the canary is written inside
# the environment's own namespace, never at the shared bucket root (deploy#638). An unset
# prefix is a refusal, not a fallback -- see the rationale in preflight_b2().
# =============================================================================
set -uo pipefail

RCLONE_REMOTE="${RCLONE_REMOTE:-isnad}"

# ---------------------------------------------------------------------------
# Pure classifier. No I/O, no network -- takes probe outcomes, returns a verdict code.
# Kept side-effect free so it can be unit-tested against the real captured outcomes.
#
#   $1 lsd_rc          exit status of `rclone lsd <remote>:` ("skip" if it never ran)
#   $2 bucket_visible  "yes" | "no"  -- was B2_BUCKET in the lsd listing
#   $3 write_rc        exit status of the canary write ("skip" if not attempted)
#   $4 delete_rc       exit status of the canary delete ("skip" if not attempted)
#
# Echoes one of:
#   OK | PROBE_FAILED | KEY_INVALID | BUCKET_UNREACHABLE | KEY_READ_ONLY | KEY_CANNOT_DELETE
# ---------------------------------------------------------------------------
classify_b2_state() {
    local lsd_rc="$1" bucket_visible="$2" write_rc="$3" delete_rc="$4"

    # The probe never ran, so we hold no evidence about the key. `lsd_rc` is an exit
    # status; anything that is not a number is not one. "skip" is the sentinel preflight_b2
    # passes when it could not allocate the scratch directory the probes write into -- the
    # same "skip" vocabulary write_rc/delete_rc already use for "not attempted".
    #
    # This branch MUST precede the KEY_INVALID one. Until deploy#613 it did not exist: a
    # preflight that could not run at all fell straight through into "lsd_rc != 0" and
    # condemned the credential. Absence of evidence was rendered as evidence of absence,
    # and it was wrong every single time -- see the header.
    if [[ ! "$lsd_rc" =~ ^[0-9]+$ ]]; then
        echo "PROBE_FAILED"
        return
    fi

    # The probe RAN and the key cannot enumerate buckets: it is invalid, revoked, or lacks
    # listBuckets. Nothing downstream is meaningful.
    if [[ "$lsd_rc" != "0" ]]; then
        echo "KEY_INVALID"
        return
    fi

    # The target bucket is not among the buckets this key can see. Either it does not
    # exist, or the key is scoped elsewhere. B2 cannot tell us which, and neither can
    # rclone -- both render identically. We report the ambiguity rather than guess.
    if [[ "$bucket_visible" != "yes" ]]; then
        echo "BUCKET_UNREACHABLE"
        return
    fi

    # Bucket is visible, so it EXISTS and the key is scoped to it. A write failure here
    # therefore cannot be a missing bucket, whatever rclone's message says. It is a
    # missing writeFiles capability -- a read-only key.
    if [[ "$write_rc" != "0" ]]; then
        echo "KEY_READ_ONLY"
        return
    fi

    # backup.sh prunes its own retention window with `rclone purge`. A key that can write
    # but not delete produces backups forever and never prunes: the failure appears
    # months later as a bill, not as an error.
    if [[ "$delete_rc" != "0" ]]; then
        echo "KEY_CANNOT_DELETE"
        return
    fi

    echo "OK"
}

# Human-facing diagnosis for a verdict. Deliberately names the misleading rclone text so
# the next operator is not sent chasing it.
explain_b2_state() {
    case "$1" in
        OK)
            echo "B2 credentials verified: bucket reachable, writable, and prunable."
            ;;
        PROBE_FAILED)
            echo "The B2 preflight could not RUN its capability probes. It therefore has NO evidence about the credential."
            echo "This is NOT a verdict on the key: do not rotate, revoke, or re-provision anything on the strength of it."
            echo "Cause: ${PROBE_FAILED_CAUSE:-the probes could not be executed}."
            echo "Scratch parent actually used: ${SCRATCH_PARENT_USED:-<none>} (BACKUP_DIR=${BACKUP_DIR:-<unset>}, TMPDIR=${TMPDIR:-<unset>})."
            echo "Two things commonly do this. (1) A read-only scratch parent: under systemd, isnad-backup.service runs"
            echo "ProtectSystem=strict with no PrivateTmp (deploy#121 Bug A), so /tmp is READ-ONLY and the scratch dir must live"
            echo "under BACKUP_DIR, which the unit grants via ReadWritePaths. (2) A FULL DISK: BACKUP_DIR is the volume the dumps"
            echo "themselves fill, so ENOSPC there is an expected fault -- and it can let a directory be created while a file"
            echo "written into it stays empty."
            echo "Remediation: check free space on the scratch parent first (df), then confirm BACKUP_DIR is set, exists, is"
            echo "writable by the unit, and is listed in the unit's ReadWritePaths."
            ;;
        KEY_INVALID)
            echo "B2 key cannot list buckets. It is invalid, revoked, or lacks the listBuckets capability."
            ;;
        BUCKET_UNREACHABLE)
            echo "Bucket '${B2_BUCKET}' is not visible to this key. EITHER the bucket does not exist, OR the key is scoped to a different bucket."
            echo "B2 refuses at key-scope before evaluating the bucket, so these two cases are indistinguishable from the error text."
            echo "rclone renders both as: 'you must use bucket(s) [...] with this application key'."
            echo "Remediation: confirm the bucket exists (terraform/backblaze), then confirm BACKUP_B2_KEY_ID is the key scoped to it."
            ;;
        KEY_READ_ONLY)
            echo "Bucket '${B2_BUCKET}' EXISTS and this key can list it, but the canary write FAILED."
            echo "This is a read-only key: it lacks the writeFiles capability. It is NOT a missing bucket."
            echo "rclone reports a read-only key's 401 as 'failed to create bucket'. That message names the wrong problem; ignore it."
            echo "Remediation: rotate BACKUP_B2_APP_KEY to the write-capable key (writeFiles + deleteFiles), e.g. b2_application_key.backups_rw."
            ;;
        KEY_CANNOT_DELETE)
            echo "Bucket '${B2_BUCKET}' is writable but the canary DELETE failed: the key lacks deleteFiles."
            echo "backup.sh's retention prune uses 'rclone purge' and will fail silently-late; backups will accumulate without bound."
            echo "Remediation: the backups key needs deleteFiles as well as writeFiles."
            ;;
        *)
            echo "Unknown B2 preflight verdict: $1"
            ;;
    esac
}

# ---------------------------------------------------------------------------
# Live probe. Performs the three capability probes and prints a verdict.
# Returns 0 only for OK.
# ---------------------------------------------------------------------------
preflight_b2() {
    local remote="${RCLONE_REMOTE}"
    local tmp scratch_parent lsd_out lsd_rc bucket_visible write_rc delete_rc verdict canary

    # Every probe below is EXPECTED to fail sometimes -- that is the entire point, and a
    # probe's non-zero exit is data, not an error. A caller running under `set -e` with
    # `shopt -s inherit_errexit` would otherwise kill this function at the first failing
    # `rclone` call, before `lsd_rc=$?` executes, so it would die without ever computing
    # a verdict. Disable errexit for the probe section explicitly rather than depending on
    # the caller's shell options. (deploy#563 review)
    set +e

    : "${B2_BUCKET:?B2_BUCKET must be set}"

    # WHAT THIS PROBE IS ACTUALLY FOR (deploy#638)
    #
    # The probe proves write+delete CAPABILITY. It has never proved anything else, and the
    # capability that matters is write+delete *WITHIN THIS ENVIRONMENT'S PREFIX* -- which is
    # what it should have been probing from the start. stg and prod share one bucket
    # (deploy#632); the canary used to be written at the bucket ROOT, outside every prefix.
    #
    # That was benign only by accident: the object name carries a UTC timestamp and the PID,
    # so a run only ever touched its own object and stg/prod canaries could not collide. The
    # hazard is the cleanup nobody has written yet. `.backup-preflight/` accrues orphans
    # whenever a run dies between the write and the delete, and the day someone tidies them
    # with `rclone purge .../.backup-preflight/` they purge a ROOT directory that the OTHER
    # environment is concurrently writing into.
    #
    # It is also the hard blocker on scoping the key (deploy#634): under a `namePrefix=stg/`
    # key a bucket-root write 401s, and backup.sh runs this preflight BEFORE it dumps
    # anything and refuses on failure. Scoping the key without moving the canary first would
    # land as a total backup outage on both hosts, reported as a credential fault -- the
    # single most misleading verdict this script exists to prevent.
    #
    # So BACKUP_PREFIX is REQUIRED here, exactly as backup.sh and restore.sh require it, and
    # for the same reason: there is no safe default. `""` silently restores the shared root
    # and `prod` on the stg box is catastrophic, so an unset prefix is a REFUSAL, not a
    # fallback to a legacy root-scoped path (deploy#633's stance). backup.sh sources this
    # file and has already required and charset-validated the prefix; the standalone
    # invocation an operator reaches for to double-check a verdict must not be laxer than
    # the scheduled one it is checking.
    # No apostrophes in this message: inside ${VAR:?word} the word undergoes QUOTE REMOVAL,
    # so a `'` is silently eaten and the operator reads "the environments own namespace".
    : "${BACKUP_PREFIX:?BACKUP_PREFIX must be set (stg|prod) — the canary is written inside this environment namespace so it cannot collide with, or be purged alongside, the other environment objects. See deploy#638.}"

    # Identical charset guard to backup.sh/restore.sh, and deliberately the same refusal
    # wording. A prefix carrying '/' or '..' climbs out of its namespace into the other
    # environment's -- and this script WRITES and DELETES, so it is exactly as dangerous
    # here as it is there.
    if [[ ! "$BACKUP_PREFIX" =~ ^[a-z0-9][a-z0-9-]*$ ]]; then
        echo "FATAL: BACKUP_PREFIX='${BACKUP_PREFIX}' is not a bare name ([a-z0-9][a-z0-9-]*)." >&2
        echo "       A prefix containing '/' or '..' escapes its environment namespace." >&2
        return 1
    fi

    # Guard the leak vector explicitly rather than trusting the ambient environment.
    if [[ -n "${RCLONE_DUMP:-}" ]]; then
        echo "[ERROR] RCLONE_DUMP is set (${RCLONE_DUMP:+<redacted>}). It can print credentials into logs. Refusing to run." >&2
        return 1
    fi

    # A probe that could not run yields NO evidence about the key. Every such route -- and
    # there are four -- funnels through here, so none of them can drift back into a
    # credential verdict the way the unchecked mktemp did (deploy#613, Lucas/Nino review).
    probe_failed() {
        PROBE_FAILED_CAUSE="$1"
        SCRATCH_PARENT_USED="$scratch_parent"
        verdict="$(classify_b2_state skip unknown skip skip)"
        echo "[preflight] scratch_parent=${scratch_parent} probe_failed=${1}"
        echo "[preflight] lsd_rc=skip bucket_visible=unknown write_rc=skip delete_rc=skip"
        echo "[preflight] verdict=${verdict}"
        explain_b2_state "$verdict"
    }

    # rclone absent is a probe that cannot run, not a bad key: `rclone lsd` would exit 127
    # and 127 is a number, so it would sail past PROBE_FAILED into KEY_INVALID. backup.sh
    # pre-checks rclone, but the standalone and `source` invocations this file documents do
    # not -- and standalone is precisely what an operator reaches for to double-check a
    # KEY_INVALID they do not believe.
    if ! command -v rclone > /dev/null 2>&1; then
        scratch_parent="${BACKUP_DIR:-${TMPDIR:-/tmp}}"
        probe_failed "rclone is not installed or not executable on PATH"
        return 1
    fi

    # Scratch space for the probes' output. NOT /tmp: read-only under the backup unit's
    # ProtectSystem=strict (deploy#613, see header). BACKUP_DIR is what the unit grants in
    # ReadWritePaths and what backup.sh already stages its dumps into. Falling back to
    # TMPDIR, then /tmp, keeps standalone and CI invocation (no BACKUP_DIR) working.
    scratch_parent="${BACKUP_DIR:-${TMPDIR:-/tmp}}"
    tmp=""
    mkdir -p "$scratch_parent" 2>/dev/null

    # FATAL, and never a credential verdict. An unchecked mktemp is what turned a read-only
    # /tmp into "your B2 key is invalid" on every backup this project ever ran.
    if ! tmp="$(mktemp -d -p "$scratch_parent" b2-preflight-XXXXXX)" \
        || [[ -z "$tmp" || ! -d "$tmp" ]]; then
        tmp=""
        probe_failed "the scratch directory could not be created under ${scratch_parent}"
        return 1
    fi

    # Single-quoted so $tmp expands when the trap FIRES, not when it is set. The previous
    # form interpolated the path into a string that bash re-parses at return time, so a
    # single quote anywhere in BACKUP_DIR broke the quoting of an `rm -rf` in a ROOT-run
    # unit. `rm -rf ''` stays unreachable either way -- the trap is installed only after the
    # [[ -z "$tmp" || ! -d "$tmp" ]] guard above has already returned.
    trap 'rm -rf -- "$tmp"' RETURN

    # The directory EXISTS. That is not the same as being able to put a file in it, and the
    # difference is the whole bug one layer down: if the redirect below cannot create its
    # target, bash never execs rclone, $? is 1, and 1 is a number -- so it would sail past
    # PROBE_FAILED straight into KEY_INVALID, condemning a key that was never consulted.
    #
    # This is not a theoretical fault, and moving scratch onto BACKUP_DIR RAISES it: that is
    # the one volume that fills with multi-GB dumps under a 7-daily/4-weekly retention, so
    # ENOSPC is its single most expected failure. Under ENOSPC the creat() can succeed while
    # the write fails, leaving a 0-byte file -- so testing the redirect's exit status alone
    # is not enough. -s (non-empty) is what actually catches it.
    if ! printf 'preflight-writable\n' > "${tmp}/.rw" 2>/dev/null || [[ ! -s "${tmp}/.rw" ]]; then
        probe_failed "the scratch directory ${tmp} exists but cannot hold a file (full disk, quota, or read-only filesystem)"
        return 1
    fi
    rm -f -- "${tmp}/.rw"

    # 1. Can the key enumerate buckets, and is ours among them?
    lsd_out="${tmp}/lsd.out"
    if ! : > "$lsd_out" 2>/dev/null; then
        probe_failed "the scratch directory ${tmp} would not accept the probe's output file"
        return 1
    fi
    rclone lsd "${remote}:" > "$lsd_out" 2>&1
    lsd_rc=$?

    bucket_visible="no"
    if [[ "$lsd_rc" -eq 0 ]] && awk '{print $NF}' "$lsd_out" | grep -qx -- "$B2_BUCKET"; then
        bucket_visible="yes"
    fi

    # 2/3. Canary write + delete, but only when the bucket is actually reachable --
    # probing a bucket we cannot see would return the ambiguous message and tell us
    # nothing we do not already know.
    write_rc="skip"
    delete_rc="skip"
    if [[ "$bucket_visible" == "yes" ]]; then
        canary=".backup-preflight/canary-$(date -u +%Y%m%d-%H%M%S)-$$"

        # An unchecked local write here is the WORST of this family, because its downstream
        # verdict is actively harmful. A failed/empty canary makes `rclone copyto` fail,
        # which classifies as KEY_READ_ONLY -- whose remediation text says "rotate
        # BACKUP_B2_APP_KEY to the write-capable key". So a FULL DISK would hand the
        # operator a written instruction to re-provision a perfectly good credential with
        # MORE capability than it needs. That is the exact door PROBE_FAILED exists to shut.
        if ! printf 'noorinalabs backup preflight canary\n' > "${tmp}/canary" 2>/dev/null \
            || [[ ! -s "${tmp}/canary" ]]; then
            probe_failed "the canary file could not be written to ${tmp} (full disk, quota, or read-only filesystem)"
            return 1
        fi

        # INSIDE the environment's prefix -- never at the bucket root (deploy#638; see the
        # rationale above the BACKUP_PREFIX guard). The write and the delete must address
        # the SAME path, or the delete probes an object that was never written and
        # KEY_CANNOT_DELETE becomes a lie about a key that deletes perfectly well.
        rclone copyto "${tmp}/canary" "${remote}:${B2_BUCKET}/${BACKUP_PREFIX}/${canary}" > "${tmp}/write.out" 2>&1
        write_rc=$?

        if [[ "$write_rc" -eq 0 ]]; then
            rclone deletefile "${remote}:${B2_BUCKET}/${BACKUP_PREFIX}/${canary}" > "${tmp}/delete.out" 2>&1
            delete_rc=$?
        fi
    fi

    verdict="$(classify_b2_state "$lsd_rc" "$bucket_visible" "$write_rc" "$delete_rc")"

    echo "[preflight] lsd_rc=${lsd_rc} bucket_visible=${bucket_visible} write_rc=${write_rc} delete_rc=${delete_rc}"
    echo "[preflight] verdict=${verdict}"
    explain_b2_state "$verdict"

    [[ "$verdict" == "OK" ]]
}

# Only run when executed, not when sourced.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    : "${B2_KEY_ID:?B2_KEY_ID must be set}"
    : "${B2_APP_KEY:?B2_APP_KEY must be set}"
    : "${B2_BUCKET:?B2_BUCKET must be set}"
    : "${BACKUP_PREFIX:?BACKUP_PREFIX must be set (stg|prod) — see deploy#638}"

    # Report shape, never value. BACKUP_PREFIX is not a credential: it is a path component
    # and printing it is what tells the operator WHICH environment they just probed.
    printf '[preflight] B2_KEY_ID len=%s  B2_APP_KEY len=%s  B2_BUCKET=%s  BACKUP_PREFIX=%s\n' \
        "${#B2_KEY_ID}" "${#B2_APP_KEY}" "$B2_BUCKET" "$BACKUP_PREFIX"

    export RCLONE_CONFIG_ISNAD_TYPE="b2"
    export RCLONE_CONFIG_ISNAD_ACCOUNT="${B2_KEY_ID}"
    export RCLONE_CONFIG_ISNAD_KEY="${B2_APP_KEY}"

    preflight_b2
fi

#!/usr/bin/env bash
# =============================================================================
# verify_b2_backup_artifact.sh — assert a RESTORABLE OBJECT EXISTS IN B2.
#
# Every backup alert we have reads a HOST-LOCAL textfile gauge. The single point of
# trust that an object ever landed in the bucket is `rclone copy; echo $?` — the
# uploader's own opinion of its upload. The whole alerting stack can be green while
# the bucket is empty (deploy#583).
#
# This is the only thing that looks IN THE BUCKET. It runs from CI, never from the box
# whose backup it is checking: a host that is down cannot report that its own backup is
# missing, and a Prometheus gauge scraped from that host cannot either. That is why the
# signal here is a failing CI job and not a metric — a metric would have to be scraped
# from the very machine whose absence we are trying to detect.
#
# ---------------------------------------------------------------------------
# THE SILENT ZERO THIS EXISTS TO CLOSE — and which it must not commit itself
# ---------------------------------------------------------------------------
#   `rclone lsf` on an EMPTY prefix exits 0 and prints NOTHING.
#
# An empty listing read as "fine" is exactly the defect this issue names, and it would be
# a perfect instance of the bug landing inside its own fix. So the outcomes are kept
# strictly apart and NEVER collapsed:
#
#   bucket unreachable      -> INSTRUMENT ERROR (exit 2). Credentials, network, wrong
#                              bucket. NOT a claim about backups: we know nothing, and
#                              "no backups" here would be a fabricated measurement in the
#                              same shape as "fine".
#   reachable, no dumps     -> ABSENT (exit 1). ALERT.
#   reachable, newest old   -> STALE  (exit 1). ALERT.
#   reachable, newest fresh -> FRESH  (exit 0).
#
# ---------------------------------------------------------------------------
# WHY THE REACHABILITY PROBE IS ON THE BUCKET AND NOT THE PREFIX
# ---------------------------------------------------------------------------
# Because the B2 backend and the local backend DISAGREE, and calibrating on the wrong one
# would have shipped a scanner that cannot tell "no backups" from "wrong path". Measured
# against real B2, 2026-07-11:
#
#                                  local backend      B2 backend
#   lsf on a NONEXISTENT prefix    rc=3 (error)       rc=0, empty output   <-- !!
#   lsd on a NONEXISTENT bucket    rc=3 (error)       rc=1 (error)
#   lsd on a bucket, BAD key       n/a                rc=1 (error)
#
# So on B2 there is NO error for a nonexistent prefix: a typo in the path is
# indistinguishable, by exit code, from an empty bucket. A prefix-level probe therefore
# cannot be the instrument-liveness control on the backend we actually run against — it
# would have been calibrated on a behaviour that does not exist in production.
#
# The BUCKET-level `lsd` does separate, on both backends, so that is the control. After it
# passes, a zero under the prefix is a real zero: we know the credentials work, the bucket
# exists, and the scanner can see.
#
# One residual ambiguity is stated rather than hidden: a valid bucket with a MIS-TYPED
# prefix reads as ABSENT. It fails loud, which is the right direction, but the diagnosis
# would be wrong — so the result line carries `bucket_objects`, and an ABSENT verdict with
# `bucket_objects > 0` means "the bucket has contents, just not where I looked", which is
# an operator's cue to check the prefix rather than hunt for a lost backup.
#
# ---------------------------------------------------------------------------
# WHY --self-test RUNS BEFORE EVERY REAL READING
# ---------------------------------------------------------------------------
# You cannot draw a "the listing works" control from INSIDE the scope: if the bucket is
# genuinely empty, the control has nothing to see either. A zero from a broken scanner and
# a zero from an empty bucket are the same string, and only one of them is a measurement.
#
# So the scanner is calibrated against local fixtures through the same code path, and must
# SEPARATE all four classes before any real verdict is trusted. A scanner that has not been
# shown to return all four cannot be believed when it returns one.
#
# Usage:
#   B2_ROOT="isnad:isnad-graph-backups" ./scripts/verify_b2_backup_artifact.sh
#   ./scripts/verify_b2_backup_artifact.sh --self-test
#
# Env:
#   B2_ROOT          rclone path to the BUCKET (reachability probe + scan root)
#   B2_PREFIX        optional sub-path under the bucket to scan (default: whole bucket)
#   MAX_AGE_HOURS    freshness bound (default 30 — one nightly run plus slack)
# =============================================================================
set -euo pipefail

# rclone prints ModTime in LOCAL time, and this script parses it with `date -u -d`.
# Unpinned, that is a systematic error equal to the box's UTC offset — measured on this
# workstation (UTC-4), an object uploaded seconds earlier reported `newest_age_hours=4`.
#
# The direction matters: on a box BEHIND UTC the age is inflated (a fresh backup reads
# stale — a false alarm, annoying). On a box AHEAD of UTC the age is DEFLATED, and a
# stale backup reads FRESH. That is a MISSED alarm on the one check that stands between
# us and an unrecoverable delete, and it would depend on nothing but the runner's clock
# configuration.
#
# Pinning TZ makes rclone emit UTC and `date -u` parse UTC, so the two agree by
# construction. The self-test asserts the resulting AGE, not merely the class, because a
# four-hour systematic error does not move a fresh fixture out of the fresh class — the
# classes were far enough apart to hide it, which is exactly how it survived the first
# calibration.
export TZ=UTC

MAX_AGE_HOURS="${MAX_AGE_HOURS:-30}"
B2_PREFIX="${B2_PREFIX:-}"

EXIT_OK=0
EXIT_ALERT=1
EXIT_INSTRUMENT=2

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) [$1] ${*:2}" >&2; }

# One machine-readable line. `status` is set explicitly on every path — it is never
# inferred from an absence of output, which is the whole point of this script.
result() {
    printf 'B2_BACKUP_ARTIFACT status=%s dumps=%s newest_age_hours=%s bucket_objects=%s newest=%s\n' \
        "$1" "$2" "$3" "$4" "${5:--}"
}

# scan <bucket-root> <prefix> -> result line; 0 fresh / 1 alert / 2 instrument error.
scan() {
    local root="$1" prefix="${2:-}"
    local rc=0 listing bucket_listing bucket_objects=0

    # --- 1. INSTRUMENT LIVENESS, on the bucket. -----------------------------
    # `|| rc=$?` and NOT a bare assignment: under `set -e` an assignment whose command
    # substitution fails IS a failing simple command and errexit fires AT THE ASSIGNMENT,
    # so the rc check below would be dead code on every failing path and an instrument
    # error would never be told apart from an absence. (Learned in deploy#563.)
    bucket_listing="$(rclone lsf --recursive "$root" 2>/dev/null)" || rc=$?
    if [[ $rc -ne 0 ]]; then
        result instrument_error 0 -1 -1 -
        return $EXIT_INSTRUMENT
    fi
    bucket_objects="$(printf '%s' "$bucket_listing" | grep -c . || true)"

    # --- 2. Scan the prefix we actually care about. -------------------------
    local base="$root"
    [[ -n "$prefix" ]] && base="${root}/${prefix}"
    rc=0
    listing="$(rclone lsf --recursive --format tp --separator ';' "$base" 2>/dev/null)" || rc=$?
    if [[ $rc -ne 0 ]]; then
        result instrument_error 0 -1 "$bucket_objects" -
        return $EXIT_INSTRUMENT
    fi

    # --- 3. Classify. An empty listing is an ALERT, never an OK. ------------
    local newest_epoch=0 newest_path="-" dumps=0
    local line ts path epoch
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        ts="${line%%;*}"
        path="${line#*;}"
        # A checksum or a manifest is not a restorable backup. A bucket holding only
        # `.sha256` and `_backup_manifest.txt` lists NON-empty and restores NOTHING — so
        # "is anything there?" is the wrong question. Count only dumps.
        case "$path" in
            *.dump | *.dump.zst) ;;
            *) continue ;;
        esac
        dumps=$((dumps + 1))
        epoch="$(date -u -d "$ts" +%s 2>/dev/null || echo 0)"
        if [[ "$epoch" -gt "$newest_epoch" ]]; then
            newest_epoch="$epoch"
            newest_path="$path"
        fi
    done <<< "$listing"

    if [[ "$dumps" -eq 0 ]]; then
        result absent 0 -1 "$bucket_objects" -
        return $EXIT_ALERT
    fi

    local now age_h
    now="$(date -u +%s)"
    age_h=$(( (now - newest_epoch) / 3600 ))

    if [[ "$age_h" -gt "$MAX_AGE_HOURS" ]]; then
        result stale "$dumps" "$age_h" "$bucket_objects" "$newest_path"
        return $EXIT_ALERT
    fi

    result fresh "$dumps" "$age_h" "$bucket_objects" "$newest_path"
    return $EXIT_OK
}

# --------------------------------------------------------------------------
# Self-test — calibrate before reading. Runs the SAME scan() against fixtures.
# --------------------------------------------------------------------------
self_test() {
    local tmp fails=0 out status rc
    tmp="$(mktemp -d)"
    # shellcheck disable=SC2064  # expand $tmp now, deliberately
    trap "rm -rf '$tmp'" RETURN

    _expect() { # <label> <root> <prefix> <want-status> <want-rc> [max-age-hours-reported]
        local label="$1" root="$2" prefix="$3" want="$4" want_rc="$5" want_age="${6:-}"
        rc=0
        out="$(scan "$root" "$prefix")" || rc=$?
        status="$(printf '%s' "$out" | sed -n 's/.*status=\([a-z_]*\).*/\1/p')"
        local ok=1
        [[ "$status" == "$want" && "$rc" -eq "$want_rc" ]] || ok=0

        # The AGE, not just the class. A systematic clock/TZ error does not move a fresh
        # fixture out of the fresh class — the classes are far apart, so the class check
        # alone is blind to it. Asserting the value is what makes the calibration able to
        # see a skew at all.
        if [[ -n "$want_age" && "$ok" -eq 1 ]]; then
            local got_age
            got_age="$(printf '%s' "$out" | sed -n 's/.*newest_age_hours=\(-\?[0-9]*\).*/\1/p')"
            if [[ "$got_age" -gt "$want_age" ]]; then
                log FAIL "self-test ${label}: reported age ${got_age}h for an object created NOW (max ${want_age}h). Clock/TZ skew — a stale backup could read fresh."
                fails=$((fails + 1))
                return
            fi
        fi

        if [[ "$ok" -eq 1 ]]; then
            log PASS "self-test ${label}: status=${status} rc=${rc}"
        else
            log FAIL "self-test ${label}: got status=${status} rc=${rc}; want status=${want} rc=${want_rc}"
            fails=$((fails + 1))
        fi
    }

    # FRESH — the POSITIVE control. Without a fixture the scanner can pass, every
    # refusal below is vacuous: a scanner that only ever says ALERT proves every bucket
    # is empty.
    mkdir -p "${tmp}/fresh"
    : > "${tmp}/fresh/isnad-pg-now.dump"
    _expect fresh "${tmp}/fresh" "" fresh "$EXIT_OK" 1

    # ABSENT — the silent zero itself: rclone exits 0 and prints nothing.
    mkdir -p "${tmp}/empty"
    _expect absent-empty "${tmp}/empty" "" absent "$EXIT_ALERT"

    # ABSENT — lists NON-empty, restores nothing. Checksums and a manifest, no dumps.
    mkdir -p "${tmp}/nodumps"
    : > "${tmp}/nodumps/isnad-pg-old.dump.sha256"
    : > "${tmp}/nodumps/_backup_manifest.txt"
    _expect absent-no-dumps "${tmp}/nodumps" "" absent "$EXIT_ALERT"

    # STALE — a real dump, far too old.
    mkdir -p "${tmp}/stale"
    : > "${tmp}/stale/isnad-pg-old.dump"
    touch -d "$(( MAX_AGE_HOURS + 24 )) hours ago" "${tmp}/stale/isnad-pg-old.dump"
    _expect stale "${tmp}/stale" "" stale "$EXIT_ALERT"

    # INSTRUMENT ERROR — an unreachable bucket must NOT be reported as "absent".
    # This is the one the B2/local divergence forced onto the BUCKET probe: on B2 a
    # nonexistent PREFIX returns rc=0 and empty, so only a bucket-level failure can carry
    # this signal on the backend we actually run against.
    _expect instrument-error "${tmp}/no-such-bucket" "" instrument_error "$EXIT_INSTRUMENT"

    if [[ "$fails" -ne 0 ]]; then
        log ERROR "self-test FAILED (${fails} case(s)). The scanner does not separate the"
        log ERROR "classes, so its verdict on the real bucket would mean nothing. Refusing"
        log ERROR "to report one — an uncalibrated instrument's zero is not a zero."
        return 1
    fi
    log INFO "self-test PASSED — scanner separates fresh / absent / stale / instrument-error."
    return 0
}

main() {
    if [[ "${1:-}" == "--self-test" ]]; then
        self_test
        exit $?
    fi

    : "${B2_ROOT:?B2_ROOT must be set (rclone path to the backups bucket)}"

    # Calibrate first, ALWAYS. A reading from an instrument that has not been shown to
    # separate the classes is not a reading.
    if ! self_test; then
        log ERROR "refusing to scan ${B2_ROOT} with an uncalibrated scanner."
        exit $EXIT_INSTRUMENT
    fi

    log INFO "scanning ${B2_ROOT}${B2_PREFIX:+/$B2_PREFIX} (max age ${MAX_AGE_HOURS}h)"
    local rc=0
    scan "$B2_ROOT" "$B2_PREFIX" || rc=$?

    case "$rc" in
        "$EXIT_OK")
            log INFO "FRESH — a restorable object exists in the bucket and is recent."
            ;;
        "$EXIT_ALERT")
            log ERROR "ALERT — no fresh restorable backup object in ${B2_ROOT}${B2_PREFIX:+/$B2_PREFIX}."
            log ERROR "  The host-local gauges may well be green: they say backup.sh RAN and rclone"
            log ERROR "  returned 0. They do not look in the bucket. This does."
            log ERROR "  If bucket_objects > 0 above, the bucket has contents but no dump under this"
            log ERROR "  prefix — check the prefix before concluding the backups are gone."
            ;;
        "$EXIT_INSTRUMENT")
            log ERROR "INSTRUMENT ERROR — could not list ${B2_ROOT} (credentials, network, bucket)."
            log ERROR "  This is NOT a claim that backups are missing. We do not know. Fix the scan."
            ;;
    esac
    exit "$rc"
}

main "$@"

#!/usr/bin/env bash
# =============================================================================
# Host provisioning assertions (deploy#558, deploy#551)
#
# Read-only. Asserts that the state cloud-init and scripts/converge_host.sh are
# supposed to establish is ACTUALLY established on this host. Run by
# .github/workflows/verify-deploy.yml over SSH after every deploy.
#
# Why this exists: `isnad-backup.timer` was never installed on stg or prod, and
# nothing noticed for months. The units, the tmpfiles.d entry and the cloud-init
# runcmd that installs them were all committed and correct. What was missing was
# anything that CHECKED. A timer a human installed by hand does not survive a
# reprovision, and we would not learn that until we needed a backup.
#
# The install is not the deliverable. This file is.
#
# Exit status: 0 all assertions pass; 1 one or more failed.
#
# Usage (as root, or via sudo):
#   sudo ./scripts/assert_host_state.sh
#   ssh deploy@host 'sudo bash -s' < scripts/assert_host_state.sh
# =============================================================================
set -uo pipefail

TIMER_UNIT="isnad-backup.timer"
SERVICE_UNIT="isnad-backup.service"
MARKER_UNIT="isnad-backup-failure-marker.service"
UNIT_DIR="/etc/systemd/system"
BACKUP_DIR="/var/lib/noorinalabs-backups"
DEPLOY_USER="deploy"
DEPLOY_HOME="/home/deploy"

# A timer that was installed more than this long ago should have fired at least
# once (it runs daily at 03:00 UTC). Never having run after that window means it
# is installed but not actually scheduling — the failure mode a bare
# `is-enabled` check misses entirely.
NEVER_RUN_GRACE_HOURS="${NEVER_RUN_GRACE_HOURS:-26}"

# --- clock sanity (deploy#585) ----------------------------------------------
# `rclone` PRESERVES the source file's mtime on upload, so a backup object's timestamp in
# B2 *is* this host's clock at dump time. Every freshness judgement about our backups is
# therefore downstream of this clock, and nothing asserted it.
#
# deploy#584 pinned the READER side (it now refuses a future-dated object as
# `instrument_error reason=future_timestamp`). But refusing a bad reading is not the same
# as knowing the clock is good — that is the gap this closes, on the WRITER.
#
# A minute is generous: deploy#584 already tolerates 5 minutes of jitter before calling a
# timestamp "future", so anything this check passes is comfortably inside that.
CLOCK_MAX_OFFSET_SECONDS="${CLOCK_MAX_OFFSET_SECONDS:-60}"

# The external reference: the `Date:` header of an HTTPS response. It needs nothing on the
# box beyond curl, and it is genuinely OUTSIDE this host's clock — unlike asking
# systemd-timesyncd for its own opinion of the offset, which is the daemon whose job it is
# to be right and therefore cannot be the witness that it is.
#
# Known limitation, stated rather than hidden: TLS validates certificate not-before /
# not-after against this same clock, so a catastrophic skew (years) makes curl fail rather
# than report. That degrades to `unknown reason=reference_unreachable`, which does NOT pass
# — so it fails safe, just with a less precise diagnosis than it could give.
CLOCK_REF_URLS="${CLOCK_REF_URLS:-https://www.cloudflare.com https://www.google.com}"
CLOCK_REF_TIMEOUT="${CLOCK_REF_TIMEOUT:-10}"

PASS=0
FAIL=0

pass() { printf '  [PASS] %s\n' "$*"; PASS=$((PASS + 1)); }
fail() { printf '  [FAIL] %s\n' "$*"; FAIL=$((FAIL + 1)); }
info() { printf '  [INFO] %s\n' "$*"; }

# Extract the epoch of an HTTP `Date:` header from a raw response-header block.
#
# `date -d <string>` is pure string parsing — it does NOT consult the system clock — so the
# reference survives even when the host clock is the very thing that is wrong.
#
# Three properties of a REAL header stream that a hand-written fixture would not have, and
# which the committed fixtures in scripts/tests/fixtures/ do (they are curl's verbatim
# output, not a retyping of it):
#   * HTTP/2 lower-cases header names — `date:`, not `Date:`.
#   * Cloudflare emits a `103 Early Hints` response BEFORE the `200`, so the block holds two
#     responses and only the second carries a date.
#   * Header lines end CRLF, so the value carries a trailing `\r`.
http_date_epoch() {
    local hdrs="$1" raw
    # `sed -n …p` exits 0 when it matches nothing; `grep` exits 1 and would be a CRASH under
    # a stricter `set -e` (deploy#563/#584/#591). No pipeline here at all: a herestring is a
    # file descriptor, not a pipe, so there is no early-exit consumer to SIGPIPE a producer
    # and no 141 for `pipefail` to promote over a succeeding stage.
    raw=$(sed -n 's/^[Dd]ate:[[:space:]]*//p' <<<"$hdrs")
    raw=${raw%%$'\n'*}   # first Date header, without a `head -n1` pipe
    raw=${raw%$'\r'}     # HTTP headers are CRLF-terminated
    [[ -n "$raw" ]] || return 1
    date -u -d "$raw" +%s 2>/dev/null || return 1
}

# The external reference's epoch, or rc=1 if none could be read.
#
# CLOCK_REF_EPOCH_CMD is the CALIBRATION SEAM. `self_test_clock` injects a reference a known
# distance from this host's clock and requires the classifier to go RED — in BOTH directions.
# A guard nobody has watched fail is not a guard.
reference_epoch() {
    if [[ -n "${CLOCK_REF_EPOCH_CMD:-}" ]]; then
        eval "$CLOCK_REF_EPOCH_CMD"
        return
    fi
    local url hdrs epoch
    local -a urls
    read -r -a urls <<<"$CLOCK_REF_URLS"
    for url in "${urls[@]}"; do
        hdrs=$(curl -sS -I --max-time "$CLOCK_REF_TIMEOUT" "$url" 2>&1) || continue
        epoch=$(http_date_epoch "$hdrs") || continue
        printf '%s\n' "$epoch"
        return 0
    done
    return 1
}

# The SIGNED offset (host minus reference) in seconds. rc=1 if the reference is unreadable.
#
# The host clock is sampled on BOTH sides of the reference read and the midpoint taken: the
# reference generates its timestamp somewhere inside that window, so a merely SLOW reference
# would otherwise register as "the host is behind" by the round-trip time.
measure_offset() {
    local t0 t1 ref
    t0=$(date -u +%s)
    ref=$(reference_epoch) || return 1
    t1=$(date -u +%s)
    # A reference that returned garbage (an HTML error page, an empty string) must not slip
    # into the arithmetic below, where it would silently become 0 and read as a colossal skew.
    [[ "$ref" =~ ^-?[0-9]+$ ]] || return 1
    printf '%s\n' "$(( (t0 + t1) / 2 - ref ))"
}

# The unit actually disciplining this clock, or `none`.
clock_sync_service() {
    local unit
    for unit in systemd-timesyncd.service chrony.service chronyd.service ntpsec.service; do
        if [[ "$(systemctl is-active "$unit" 2>/dev/null || true)" == "active" ]]; then
            printf '%s\n' "$unit"
            return 0
        fi
    done
    printf 'none\n'
}

# THE CLASSIFIER. A pure function of its inputs — no I/O — so `self_test_clock` can drive it
# with a clock IT chooses and watch it go red.
#
# Prints one `HOST_CLOCK` signal line. It is deliberately its OWN signal, not folded into the
# backup assertions: "the clock is wrong" and "the backup is missing" have different fixes and
# must never be confused. Every field is emitted on every path — if the check knows something,
# the line says it (deploy#591).
#
# rc: 0 = sane, 1 = the clock is BAD, 2 = could not measure (NOT a claim the clock is bad).
classify_clock() {
    local ntp_sync="$1" sync_service="$2" offset="$3" max="$4"
    local status reason

    if [[ "$offset" =~ ^-?[0-9]+$ ]] && (( offset > max )); then
        # AHEAD: dumps stamped in the FUTURE. Pre-deploy#584 this did not degrade, it
        # LATCHED — one bad-clock upload turned the bucket check into a permanent green light.
        status="skewed"; reason="ahead"
    elif [[ "$offset" =~ ^-?[0-9]+$ ]] && (( offset < -max )); then
        # BEHIND: the quieter one. Healthy nightly backups stamp OLD and read `stale` (a page
        # for nothing); or, with a generous MAX_AGE_HOURS, a genuinely stale bucket reads fresh.
        status="skewed"; reason="behind"
    elif [[ ! "$offset" =~ ^-?[0-9]+$ ]]; then
        status="unknown"; reason="reference_unreachable"
    elif [[ "$ntp_sync" != "yes" && "$ntp_sync" != "no" ]]; then
        status="unknown"; reason="ntp_state_unreadable"
    elif [[ "$ntp_sync" != "yes" ]]; then
        # The offset is fine RIGHT NOW. Nothing is holding it there. Today's small offset is
        # not a guarantee about tonight's 03:00 dump.
        status="unsynchronised"; reason="ntp_not_synchronised"
    elif [[ "$sync_service" == "none" ]]; then
        status="unsynchronised"; reason="no_sync_service"
    else
        status="sane"; reason="ok"
    fi

    printf 'HOST_CLOCK status=%s reason=%s offset_seconds=%s max_offset_seconds=%s ntp_sync=%s sync_service=%s\n' \
        "$status" "$reason" "$offset" "$max" "$ntp_sync" "$sync_service"

    case "$status" in
        sane) return 0 ;;
        unknown) return 2 ;;
        *) return 1 ;;
    esac
}

# Measure and classify. `ntp_sync` and `sync_service` are the CALLER's, so `self_test_clock`
# can pin them: a genuinely unsynchronised dev box must not make the offset calibration report
# a confusing "calibration failed" in place of the clean verdict the real check would give.
probe_clock() {
    local ntp_sync="$1" sync_service="$2" offset
    offset=$(measure_offset) || offset="unknown"
    classify_clock "$ntp_sync" "$sync_service" "$offset" "$CLOCK_MAX_OFFSET_SECONDS"
}

# THE CALIBRATION. Runs on the real host, before every real reading.
#
# This is the acceptance bar of deploy#585 turned into code that executes in production: skew
# the clock and WATCH THE CHECK FAIL, in both directions, every single deploy. It drives the
# WHOLE `probe_clock` path — not a separate copy of the classifier — by moving the REFERENCE a
# known distance from the host clock, which is arithmetically the same input the classifier
# would see from a host clock skewed the other way (offset = host − reference; only our own
# code is under test, `date +%s` is the kernel's).
#
# Case (1) is the positive control and is not optional: a classifier that only ever said
# "skewed" would pass (2) and (3) and prove exactly nothing.
self_test_clock() {
    local skew failed=0 out rc
    skew=$(( CLOCK_MAX_OFFSET_SECONDS * 10 ))

    _expect() {
        local label="$1" want_rc="$2" want_status="$3" want_reason="$4" got_rc="$5" got_out="$6"
        if [[ "$got_rc" == "$want_rc" && "$got_out" == *"status=${want_status}"* \
              && "$got_out" == *"reason=${want_reason}"* ]]; then
            printf 'self-test %s: OK (%s)\n' "$label" "$got_out" >&2
        else
            printf 'self-test %s: FAILED — expected rc=%s status=%s reason=%s, got rc=%s: %s\n' \
                "$label" "$want_rc" "$want_status" "$want_reason" "$got_rc" "$got_out" >&2
            failed=1
        fi
    }

    # (1) POSITIVE CONTROL — a reference that agrees with the host clock.
    rc=0
    out=$(CLOCK_REF_EPOCH_CMD='date -u +%s' probe_clock yes systemd-timesyncd.service) || rc=$?
    _expect "sane" 0 "sane" "ok" "$rc" "$out"

    # (2) The host clock reads AHEAD of true time — future-dated dumps, the LATCHING failure.
    rc=0
    out=$(CLOCK_REF_EPOCH_CMD="echo \$(( \$(date -u +%s) - $skew ))" \
          probe_clock yes systemd-timesyncd.service) || rc=$?
    _expect "skew-ahead" 1 "skewed" "ahead" "$rc" "$out"

    # (3) The host clock reads BEHIND true time — healthy backups reading stale.
    rc=0
    out=$(CLOCK_REF_EPOCH_CMD="echo \$(( \$(date -u +%s) + $skew ))" \
          probe_clock yes systemd-timesyncd.service) || rc=$?
    _expect "skew-behind" 1 "skewed" "behind" "$rc" "$out"

    # (4) "I could not look" is a THIRD outcome, never a verdict on the clock (deploy#584).
    rc=0
    out=$(CLOCK_REF_EPOCH_CMD='exit 1' probe_clock yes systemd-timesyncd.service) || rc=$?
    _expect "unmeasurable" 2 "unknown" "reference_unreachable" "$rc" "$out"

    return "$failed"
}

# Calibration-only entry point, so the guard can be watched failing by hand and by pytest:
#   ./scripts/assert_host_state.sh --self-test
if [[ "${1:-}" == "--self-test" ]]; then
    self_test_clock
    exit $?
fi

printf '=== Host state assertions (%s) ===\n' "$(hostname)"

# ---------------------------------------------------------------------------
# 1. Backup unit files are installed (deploy#558)
# ---------------------------------------------------------------------------
printf '\n-- backup units --\n'
for unit in "$TIMER_UNIT" "$SERVICE_UNIT" "$MARKER_UNIT"; do
    if [[ -f "${UNIT_DIR}/${unit}" ]]; then
        pass "unit file installed: ${unit}"
    else
        fail "unit file MISSING: ${UNIT_DIR}/${unit}"
    fi
done

# ---------------------------------------------------------------------------
# 2. The timer is enabled AND running (deploy#558)
# ---------------------------------------------------------------------------
# `enable` alone is not enough. cloud-init's runcmd executes during boot, AFTER
# timers.target has been reached, so `systemctl enable` arms the timer for the
# NEXT reboot and never starts it in the current one. Both IaC paths then printed
# "start the backup timer" as a manual runbook step. That step was never taken.
# is-active is the assertion that would have caught it.
printf '\n-- timer state --\n'
enabled=$(systemctl is-enabled "$TIMER_UNIT" 2>&1 || true)
active=$(systemctl is-active "$TIMER_UNIT" 2>&1 || true)

if [[ "$enabled" == "enabled" ]]; then
    pass "timer is-enabled=enabled"
else
    fail "timer is-enabled=${enabled} (expected 'enabled')"
fi

if [[ "$active" == "active" ]]; then
    pass "timer is-active=active"
else
    fail "timer is-active=${active} (expected 'active') — 'enable' without '--now' leaves it inactive until reboot"
fi

# ---------------------------------------------------------------------------
# 3. Last-run state (deploy#558)
# ---------------------------------------------------------------------------
printf '\n-- last run --\n'
last_trigger=$(systemctl show "$TIMER_UNIT" -p LastTriggerUSec --value 2>/dev/null || echo "")
next_elapse=$(systemctl show "$TIMER_UNIT" -p NextElapseUSecRealtime --value 2>/dev/null || echo "")

# systemd renders "never triggered" as an empty value or the literal "n/a".
never_run=false
if [[ -z "$last_trigger" || "$last_trigger" == "n/a" || "$last_trigger" == "0" ]]; then
    never_run=true
fi

if [[ -n "$next_elapse" && "$next_elapse" != "n/a" ]]; then
    pass "timer has a scheduled next elapse: ${next_elapse}"
else
    fail "timer has NO scheduled next elapse — it is not going to run"
fi

if [[ ! -f "${UNIT_DIR}/${TIMER_UNIT}" ]]; then
    # An absent unit has no last-run state to reason about. Crucially it must NOT be
    # granted the post-install grace window below: "installed 0h ago" and "not installed
    # at all" are different facts, and the first version of this script awarded a PASS
    # for the second one.
    fail "cannot evaluate last-run state: ${TIMER_UNIT} is not installed"
elif [[ "$never_run" == "true" ]]; then
    # Installed-but-never-fired is expected immediately after provisioning and is a
    # hard failure once the daily schedule has had time to come round.
    now=$(date +%s)
    mtime=$(stat -c %Y "${UNIT_DIR}/${TIMER_UNIT}" 2>/dev/null || echo "$now")
    unit_age_hours=$(( (now - mtime) / 3600 ))
    if [[ "$unit_age_hours" -ge "$NEVER_RUN_GRACE_HOURS" ]]; then
        fail "timer has NEVER triggered, and its unit file is ${unit_age_hours}h old (>= ${NEVER_RUN_GRACE_HOURS}h) — it should have fired by now"
    else
        info "timer has not triggered yet (unit installed ${unit_age_hours}h ago; grace ${NEVER_RUN_GRACE_HOURS}h)"
        pass "timer within its post-install grace window"
    fi
else
    info "timer last triggered: ${last_trigger}"
    # If it has run, the last run must have succeeded. A backup service that fails
    # every night while the timer reports 'active' is the exact false-green this
    # whole file exists to prevent.
    result=$(systemctl show "$SERVICE_UNIT" -p Result --value 2>/dev/null || echo "unknown")
    status=$(systemctl show "$SERVICE_UNIT" -p ExecMainStatus --value 2>/dev/null || echo "unknown")
    if [[ "$result" == "success" && "$status" == "0" ]]; then
        pass "last backup run succeeded (Result=${result} ExecMainStatus=${status})"
    else
        fail "last backup run FAILED (Result=${result} ExecMainStatus=${status}) — check: journalctl -u ${SERVICE_UNIT}"
    fi
fi

# ---------------------------------------------------------------------------
# 4. Persistent staging directory (deploy#121 Bug A regression guard)
# ---------------------------------------------------------------------------
printf '\n-- staging dir --\n'
if [[ -d "$BACKUP_DIR" ]]; then
    pass "staging dir exists: ${BACKUP_DIR}"
    mode=$(stat -c %a "$BACKUP_DIR")
    owner=$(stat -c %U:%G "$BACKUP_DIR")
    # The unit declares ReadWritePaths=$BACKUP_DIR; if the path is missing, systemd
    # fails namespace setup with 226/NAMESPACE before ExecStart ever runs.
    if [[ "$mode" == "700" ]]; then
        pass "staging dir mode 0700 (dumps are sensitive)"
    else
        fail "staging dir mode ${mode} (expected 700)"
    fi
    if [[ "$owner" == "root:root" ]]; then
        pass "staging dir owned by root:root"
    else
        fail "staging dir owned by ${owner} (expected root:root)"
    fi
else
    fail "staging dir MISSING: ${BACKUP_DIR} — the unit's ReadWritePaths= will fail namespace setup (226/NAMESPACE)"
fi

# ---------------------------------------------------------------------------
# 5. The deploy user owns its own home (deploy#551)
# ---------------------------------------------------------------------------
# prod's /home/deploy is root:root, dated to the 2026-05-01 rebuild. The first
# `deploy-data-load.yml` run creates ${LOAD_DATA_DIR} under it and fails at mkdir.
# stg only looks healthy because someone chowned it by hand — which is why #551
# exists. Assert it so a reprovision cannot quietly reintroduce it.
printf '\n-- deploy user home (deploy#551) --\n'
if [[ -d "$DEPLOY_HOME" ]]; then
    home_owner=$(stat -c %U:%G "$DEPLOY_HOME")
    if [[ "$home_owner" == "${DEPLOY_USER}:${DEPLOY_USER}" ]]; then
        pass "${DEPLOY_HOME} owned by ${DEPLOY_USER}:${DEPLOY_USER}"
    else
        fail "${DEPLOY_HOME} owned by ${home_owner} (expected ${DEPLOY_USER}:${DEPLOY_USER}) — the deploy user cannot write its own \$HOME"
    fi

    # Ownership is the proxy; writability is the property we actually depend on.
    if runuser -u "$DEPLOY_USER" -- test -w "$DEPLOY_HOME" 2>/dev/null; then
        pass "${DEPLOY_USER} can write ${DEPLOY_HOME}"
    else
        fail "${DEPLOY_USER} CANNOT write ${DEPLOY_HOME} (EACCES) — deploy-data-load.yml will fail at mkdir"
    fi
else
    fail "${DEPLOY_HOME} does not exist"
fi

# ---------------------------------------------------------------------------
# 6. The clock that stamps every backup (deploy#585)
# ---------------------------------------------------------------------------
# CALIBRATE FIRST. An uncalibrated instrument's green is not a green — so before reading the
# real clock, skew a clock we control and require the check to go RED in both directions. If
# it cannot separate ahead from behind from sane, it does not get to have an opinion about
# this host.
printf '\n-- clock sanity (deploy#585) --\n'
# The calibration drives the instrument-error path ON PURPOSE (case 4), so a PASSING run must
# not print its alarming innards — a check that cries wolf on success is one people learn to
# scroll past, and this one only matters on the day someone actually reads it (deploy#591).
# On FAILURE the diagnostic is the whole point, so it is held and then shown, never discarded.
selftest_rc=0
selftest_out=$(self_test_clock 2>&1) || selftest_rc=$?
if [[ "$selftest_rc" -eq 0 ]]; then
    pass "clock check calibrated — goes RED on both forward and backward skew"

    ntp_sync=$(timedatectl show -p NTPSynchronized --value 2>/dev/null || echo "unreadable")
    sync_service=$(clock_sync_service)

    clock_rc=0
    clock_line=$(probe_clock "$ntp_sync" "$sync_service") || clock_rc=$?
    # The signal, on its own line and machine-parseable. Distinct from every backup-freshness
    # signal: "the clock is wrong" and "the backup is missing" have different fixes.
    printf '  %s\n' "$clock_line"

    case "$clock_rc" in
        0)
            pass "host clock is sane (within ±${CLOCK_MAX_OFFSET_SECONDS}s of an external reference, NTP synchronised)"
            ;;
        2)
            # NOT a claim that the clock is wrong. We could not measure it — and an
            # unverifiable clock is still not a verified one, so it does not pass.
            fail "clock could NOT be verified against an external reference — this is an INSTRUMENT failure, not a verdict on the clock: ${clock_line}"
            ;;
        *)
            fail "host clock is BAD: ${clock_line} — every backup this host uploads is stamped with it (rclone preserves source mtime), so backup freshness cannot be trusted until this is fixed"
            ;;
    esac
else
    # The calibration itself failed. Report nothing about the real clock: a check that cannot
    # be shown to fail is not evidence that anything passed.
    printf '%s\n' "$selftest_out" >&2
    fail "clock check FAILED ITS OWN CALIBRATION — refusing to report a verdict on this host's clock. Re-run with: ${0} --self-test"
fi

# ---------------------------------------------------------------------------
printf '\n=== Summary: %d passed, %d failed ===\n' "$PASS" "$FAIL"
if [[ "$FAIL" -gt 0 ]]; then
    printf '::error::Host state assertions FAILED (%d) — see above. Remediate with: sudo /opt/noorinalabs-deploy/scripts/converge_host.sh\n' "$FAIL"
    exit 1
fi
printf 'All host state assertions passed.\n'

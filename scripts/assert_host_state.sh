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

PASS=0
FAIL=0

pass() { printf '  [PASS] %s\n' "$*"; PASS=$((PASS + 1)); }
fail() { printf '  [FAIL] %s\n' "$*"; FAIL=$((FAIL + 1)); }
info() { printf '  [INFO] %s\n' "$*"; }

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
printf '\n=== Summary: %d passed, %d failed ===\n' "$PASS" "$FAIL"
if [[ "$FAIL" -gt 0 ]]; then
    printf '::error::Host state assertions FAILED (%d) — see above. Remediate with: sudo /opt/noorinalabs-deploy/scripts/converge_host.sh\n' "$FAIL"
    exit 1
fi
printf 'All host state assertions passed.\n'

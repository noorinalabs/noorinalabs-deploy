#!/usr/bin/env bash
# =============================================================================
# Converge an already-provisioned host to the state cloud-init would establish.
# (deploy#558, deploy#551)
#
# WHY THIS EXISTS
#
# cloud-init runs exactly once, on first boot. Both stg and prod were provisioned
# before the runcmd that installs the backup units landed (deploy#329), so fixing
# the template fixes zero existing hosts. Every mechanism was committed and correct;
# no host had it. Hand-fixing is what produced the divergence in the first place —
# stg's /home/deploy is deploy-owned only because someone chowned it once, which is
# precisely why deploy#551 exists and why prod is still root:root.
#
# So the fix has to be code that runs on every deploy, is idempotent, and is asserted
# afterwards by scripts/assert_host_state.sh. This is that code.
#
# It is ADDITIVE ONLY. It installs unit files, creates a directory, enables a timer,
# and corrects the ownership of the deploy user's home. It removes nothing, wipes
# nothing, and touches no datastore.
#
# Usage (must be root):
#   sudo ./scripts/converge_host.sh
#   ssh deploy@host 'sudo bash -s' < scripts/converge_host.sh
# =============================================================================
set -euo pipefail

REPO_DIR="${REPO_DIR:-/opt/noorinalabs-deploy}"
BACKUP_DIR="/var/lib/noorinalabs-backups"
DEPLOY_USER="deploy"
DEPLOY_HOME="/home/deploy"

log() { printf '%s [%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" "${*:2}"; }

if [[ "$(id -u)" -ne 0 ]]; then
    log "ERROR" "converge_host.sh must run as root (use sudo)"
    exit 1
fi

# When piped over SSH (`sudo bash -s` < this file) there is no $0 to locate the repo
# from, so the unit sources are read from REPO_DIR. Fail loudly rather than silently
# skipping the install — a skipped install that exits 0 is how we got here.
if [[ ! -d "${REPO_DIR}/systemd" ]]; then
    log "ERROR" "${REPO_DIR}/systemd not found — cannot install units"
    exit 1
fi

# ---------------------------------------------------------------------------
# 1. deploy user's home (deploy#551)
# ---------------------------------------------------------------------------
# Only the top-level directory. A recursive chown would rewrite ownership of
# .ssh/authorized_keys and the docker config, which are already correct and whose
# modes matter.
log "INFO" "Checking ${DEPLOY_HOME} ownership..."
current_owner=$(stat -c %U:%G "$DEPLOY_HOME")
if [[ "$current_owner" != "${DEPLOY_USER}:${DEPLOY_USER}" ]]; then
    log "INFO" "chown ${DEPLOY_USER}:${DEPLOY_USER} ${DEPLOY_HOME} (was ${current_owner})"
    chown "${DEPLOY_USER}:${DEPLOY_USER}" "$DEPLOY_HOME"
else
    log "INFO" "${DEPLOY_HOME} already ${current_owner}"
fi

# ---------------------------------------------------------------------------
# 2. Backup staging directory (deploy#121 Bug A)
# ---------------------------------------------------------------------------
log "INFO" "Provisioning ${BACKUP_DIR} via tmpfiles.d..."
install -m 644 "${REPO_DIR}/systemd/tmpfiles.d/noorinalabs-backups.conf" /etc/tmpfiles.d/
systemd-tmpfiles --create /etc/tmpfiles.d/noorinalabs-backups.conf

# The failure-marker unit writes .prom files here. cloud-init creates the subdir on
# fresh hosts; `install -d` is idempotent and covers hosts that predate it.
install -d -m 0755 /var/lib/node_exporter
install -d -m 0755 -o "$DEPLOY_USER" -g "$DEPLOY_USER" /var/lib/node_exporter/textfile_collector

# ---------------------------------------------------------------------------
# 3. Backup units
# ---------------------------------------------------------------------------
log "INFO" "Installing backup systemd units..."
install -m 644 "${REPO_DIR}/systemd/isnad-backup.service" /etc/systemd/system/
install -m 644 "${REPO_DIR}/systemd/isnad-backup.timer" /etc/systemd/system/
install -m 644 "${REPO_DIR}/systemd/isnad-backup-failure-marker.service" /etc/systemd/system/

systemctl daemon-reload

# `--now` is the whole point. `systemctl enable` on its own only creates the
# timers.target want-symlink; the timer does not start until the next boot. cloud-init's
# runcmd executes during boot, after timers.target has already been reached, so an
# `enable`-only install leaves the timer inactive indefinitely and both IaC paths then
# instructed an operator to run `systemctl start` by hand. Nobody ever did.
log "INFO" "Enabling and starting isnad-backup.timer..."
systemctl enable --now isnad-backup.timer

log "INFO" "Converge complete. Current timer state:"
systemctl is-enabled isnad-backup.timer || true
systemctl is-active isnad-backup.timer || true
systemctl list-timers isnad-backup.timer --no-pager || true

log "INFO" "Verify with: sudo ${REPO_DIR}/scripts/assert_host_state.sh"

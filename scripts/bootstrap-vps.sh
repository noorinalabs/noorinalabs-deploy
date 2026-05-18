#!/usr/bin/env bash
# =============================================================================
# bootstrap-vps.sh — Residual VPS bootstrap for Noorina Labs
#
# !! ONLY run this on existing VPSes that pre-date cloud-init, or as an !!
# !! idempotent refresher for the items cloud-init does NOT cover yet:  !!
# !!   - aux-repo dir pre-provisioning under /opt/                      !!
# !!   - systemd backup timer (isnad-backup.{service,timer})            !!
# !!   - safe.directory exceptions for root-run git in /opt/            !!
# !!   - SSH key merge for operator-added admin keys on root            !!
# !!   - first-time .env stub for manual (non-CI) bring-ups             !!
#
# Fresh Terraform-provisioned VPSes are bootstrapped end-to-end by
# terraform/hetzner/modules/hetzner-vps/cloud-init.yaml.tpl, which now
# handles: Docker, docker-compose, docker-buildx, git, curl, fail2ban,
# ufw, unattended-upgrades, rclone, deploy user, SSH keys (root + deploy),
# GHCR auth, .env.user-service, root SSH password disable, firewall.
# That coverage obsoletes Steps 1, 2, and 6 of the legacy bootstrap (#163).
#
# Run as root:
#   ssh -i ~/.ssh/isnad_deploy root@isnad.noorinalabs.com
#   curl -sL https://raw.githubusercontent.com/noorinalabs/noorinalabs-deploy/main/scripts/bootstrap-vps.sh | bash
#
# Or copy this script to the VPS and run:
#   chmod +x bootstrap-vps.sh && ./bootstrap-vps.sh
# =============================================================================
set -euo pipefail

# Suppress all interactive debconf prompts during any apt operations this
# script may trigger transitively (e.g. via `apt-get update` if a re-run
# refreshes lists). Cloud-init's own apt: conf: handles --force-conf{def,old}
# for fresh boots; this guard remains for safety on re-runs. See #110.
export DEBIAN_FRONTEND=noninteractive

REPO_URL="https://github.com/noorinalabs/noorinalabs-deploy.git"
INSTALL_DIR="/opt/noorinalabs-deploy"
DEPLOY_USER="deploy"

# Auxiliary repo directories pre-provisioned under /opt/ (see Step 3).
# Single source of truth — also drives the safe.directory loop in Step 2.
# To add a new repo to the deploy pipeline: append it here, re-run this
# script (idempotent). cloud-init does NOT pre-create these dirs (gap to
# close — see #163 PR body); until then, this script is the canonical
# place that adds them.
AUX_REPOS=(
  "noorinalabs-isnad-graph"
  "noorinalabs-design-system"
)

echo "============================================="
echo "  Noorina Labs VPS bootstrap (residual)"
echo "============================================="
echo ""

# ── Preflight checks ────────────────────────────────────────────────────────
if [ "$(id -u)" -ne 0 ]; then
  echo "ERROR: This script must be run as root."
  exit 1
fi

# Sanity-check that the deploy user exists. Fresh cloud-init boots create
# it via the `users:` block; if it's missing, this VPS was NOT cloud-init'd
# AND the legacy steps (1+2) need re-introducing — bail with a clear
# message so the operator doesn't end up with a half-state.
if ! id "$DEPLOY_USER" &>/dev/null; then
  echo "ERROR: User '$DEPLOY_USER' does not exist on this host."
  echo "       This script expects cloud-init to have created the user (or"
  echo "       a prior bootstrap-vps.sh run that pre-dates #163's reduction)."
  echo "       Recreate manually: adduser --disabled-password --gecos '' deploy"
  echo "       Then re-run this script."
  exit 1
fi

# Similarly, Docker is a cloud-init prereq. If it's missing on a host that
# claims to have been cloud-init'd, that's an upstream cloud-init failure
# and bootstrap-vps.sh cannot recover the box on its own.
if ! command -v docker &>/dev/null; then
  echo "ERROR: docker is not installed. cloud-init should have installed it"
  echo "       (see terraform/hetzner/modules/hetzner-vps/cloud-init.yaml.tpl"
  echo "       packages: block). Investigate cloud-init.log on this host."
  exit 1
fi

# ── Step 1: Merge SSH authorized_keys into deploy user (append-with-dedup) ──
# Per deploy#112: original bootstrap did a destructive `cp` of root's
# authorized_keys over the deploy user's file, wiping the deploy CI key any
# time an operator added a personal admin key to /root/.ssh/authorized_keys
# and then re-ran bootstrap.
#
# Cloud-init seeds both /root/.ssh/authorized_keys AND
# /home/deploy/.ssh/authorized_keys with the same ${ssh_public_key} on
# first boot — so this merge is a no-op on a fresh cloud-init box. Its
# residual value is for operators who add personal admin keys to
# /root/.ssh/authorized_keys post-provision and want them propagated to
# deploy without losing the existing deploy authorized keys.
#
# Fix: append each root key to deploy's file only if its fingerprint isn't
# already present. Idempotent across any number of re-runs.
echo "==> [1/5] Merging /root/.ssh/authorized_keys → deploy (idempotent)..."
DEPLOY_AUTH_KEYS="/home/$DEPLOY_USER/.ssh/authorized_keys"
mkdir -p "/home/$DEPLOY_USER/.ssh"
touch "$DEPLOY_AUTH_KEYS"
chown -R "$DEPLOY_USER:$DEPLOY_USER" "/home/$DEPLOY_USER/.ssh"
chmod 700 "/home/$DEPLOY_USER/.ssh"
chmod 600 "$DEPLOY_AUTH_KEYS"

# Fingerprint a single authorized_keys line. Echoes the SHA256 fingerprint
# or empty on failure. Wrapped in `|| true` so a malformed line in an
# operator-edited file doesn't trip the script's `set -e`.
ssh_key_fp() {
  echo "$1" | ssh-keygen -lf - 2>/dev/null | awk '{print $2}' || true
}

if [ -f /root/.ssh/authorized_keys ]; then
  declare -A DEPLOY_FPS=()
  while IFS= read -r existing_line; do
    case "$existing_line" in ''|\#*) continue ;; esac
    fp=$(ssh_key_fp "$existing_line")
    [ -n "$fp" ] && DEPLOY_FPS["$fp"]=1
  done < "$DEPLOY_AUTH_KEYS"

  added=0
  skipped=0
  while IFS= read -r root_line; do
    case "$root_line" in ''|\#*) continue ;; esac
    fp=$(ssh_key_fp "$root_line")
    [ -z "$fp" ] && continue
    if [ -n "${DEPLOY_FPS[$fp]:-}" ]; then
      skipped=$((skipped + 1))
    else
      echo "$root_line" >> "$DEPLOY_AUTH_KEYS"
      DEPLOY_FPS["$fp"]=1
      added=$((added + 1))
    fi
  done < /root/.ssh/authorized_keys

  chown "$DEPLOY_USER:$DEPLOY_USER" "$DEPLOY_AUTH_KEYS"
  chmod 600 "$DEPLOY_AUTH_KEYS"
  echo "    Merged root authorized_keys → deploy: $added added, $skipped already present."
else
  echo "    /root/.ssh/authorized_keys not found — nothing to merge (expected on heavily-locked-down hosts)."
fi

# ── Step 2: git safe.directory exceptions ──────────────────────────────────
# Allow root to operate on repos owned by the deploy user (git 2.35+
# CVE-2022-24765 mitigation). Step 3's `git fetch && git reset --hard` runs
# as root inside $INSTALL_DIR, which gets chowned to $DEPLOY_USER; every
# subsequent re-run hits the ownership-mismatch refusal without these
# exceptions. Aux-repo dirs are listed too because the same hazard applies
# if/when this script evolves to git-operate on them. See deploy#113.
# Runs before Step 3 (first git call) — the `${AUX_REPOS[@]/#//opt/}`
# form prefixes each entry with /opt/.
echo "==> [2/5] Adding git safe.directory exceptions..."
for dir in "$INSTALL_DIR" "${AUX_REPOS[@]/#//opt/}"; do
  git config --global --add safe.directory "$dir"
done

# ── Step 3: Refresh the deploy repo (idempotent) ───────────────────────────
# Cloud-init's runcmd already clone-or-skips this on first boot. On
# subsequent runs we fetch + reset to keep an existing checkout current.
# Doubles as recovery if the working tree drifted from a manual edit.
echo "==> [3/5] Refreshing $INSTALL_DIR (idempotent)..."
if [ -d "$INSTALL_DIR/.git" ]; then
  echo "    Repository already exists, pulling latest..."
  cd "$INSTALL_DIR" && git fetch origin main && git reset --hard origin/main
else
  git clone "$REPO_URL" "$INSTALL_DIR"
fi
chown -R "$DEPLOY_USER:$DEPLOY_USER" "$INSTALL_DIR"

# ── Step 4: Pre-provision auxiliary repo directories under /opt/ ───────────
# /opt/ is root-owned by default, so the deploy user can't `git clone` into
# it without these dirs existing first with the right ownership. The deploy
# workflow (.github/workflows/deploy-isnad-graph.yml) clone-or-pulls into
# each of these.
#
# CLOUD-INIT GAP (#163): cloud-init.yaml.tpl does NOT currently create
# these dirs — until that gap is closed, this script is the canonical place
# they live. To add a new repo to the deploy pipeline: append it to
# AUX_REPOS at the top of this script, re-run (idempotent).
echo "==> [4/5] Pre-provisioning auxiliary repo directories under /opt/..."
for repo in "${AUX_REPOS[@]}"; do
  dir="/opt/$repo"
  if [ ! -d "$dir" ]; then
    mkdir -p "$dir"
    echo "    Created $dir"
  else
    echo "    Exists  $dir"
  fi
  chown "$DEPLOY_USER:$DEPLOY_USER" "$dir"
done
echo "    Auxiliary repo dirs ready."

# ── Step 5: Install backup systemd timer + persistent staging dir ──────────
# CLOUD-INIT GAP (#163): cloud-init does NOT install the systemd backup
# units. Until that gap is closed, the timer + tmpfiles.d staging dir +
# failure-marker unit are installed here.
#
# .env note: the CI deploy workflow (deploy-isnad-graph.yml) writes
# /opt/noorinalabs-deploy/.env fresh from GH Action secrets on every push,
# so we no longer write a CHANGE-ME stub here. For first-time MANUAL
# bring-ups (no CI yet), copy compose/env.example and edit by hand:
#   cp $INSTALL_DIR/compose/env.example $INSTALL_DIR/.env && chmod 600 $INSTALL_DIR/.env
echo "==> [5/5] Installing backup timer..."
if [ -f "$INSTALL_DIR/systemd/isnad-backup.service" ]; then
  install -m 644 "$INSTALL_DIR/systemd/isnad-backup.service"               /etc/systemd/system/
  install -m 644 "$INSTALL_DIR/systemd/isnad-backup.timer"                 /etc/systemd/system/
  install -m 644 "$INSTALL_DIR/systemd/isnad-backup-failure-marker.service" /etc/systemd/system/

  # Persistent staging dir via tmpfiles.d. Without this, the unit's
  # ReadWritePaths=/var/lib/noorinalabs-backups fails the mount-namespace
  # setup with status=226/NAMESPACE before ExecStart fires — original
  # deploy#121 Bug A failure mode against /tmp/isnad-backups.
  install -m 644 "$INSTALL_DIR/systemd/tmpfiles.d/noorinalabs-backups.conf" /etc/tmpfiles.d/
  systemd-tmpfiles --create /etc/tmpfiles.d/noorinalabs-backups.conf

  # node-exporter textfile-collector PARENT directory for failure-marker
  # *.prom files. cloud-init creates the textfile_collector SUBDIR
  # (/var/lib/node_exporter/textfile_collector); `install -d` is idempotent
  # so this remains safe on fresh cloud-init hosts.
  install -d -m 0755 /var/lib/node_exporter

  systemctl daemon-reload
  systemctl enable isnad-backup.timer
  echo "    Backup timer installed (daily at 03:00 UTC)."
  echo "    Failure marker unit installed (OnFailure= → /var/lib/node_exporter/*.prom + journal)."
  echo "    Persistent staging dir provisioned: /var/lib/noorinalabs-backups (mode 0700, root)."
  echo "    Start with: systemctl start isnad-backup.timer"
else
  echo "    Backup systemd files not found, skipping (did the repo refresh in Step 3 succeed?)."
fi

# ── Done ────────────────────────────────────────────────────────────────────
echo ""
echo "============================================="
echo "  Bootstrap (residual) complete!"
echo "============================================="
echo ""
echo "What this script did NOT do (cloud-init already handles it on fresh boxes):"
echo "  - Install Docker / docker-compose / docker-buildx / git / curl / rclone / jq"
echo "  - Create the 'deploy' user, configure sudo, add to docker group"
echo "  - Seed root + deploy authorized_keys with the per-env ssh_public_key"
echo "  - Install fail2ban, ufw, unattended-upgrades"
echo "  - Disable root SSH password login"
echo "  - Write /opt/noorinalabs-deploy/.env.user-service from Terraform vars"
echo "  - Write /home/deploy/.docker/config.json (GHCR auth) when ghcr_auth_b64 set"
echo ""
echo "Next steps:"
echo "  1. (manual non-CI bring-ups only) Stage .env:"
echo "       cp $INSTALL_DIR/compose/env.example $INSTALL_DIR/.env"
echo "       chmod 600 $INSTALL_DIR/.env && nano $INSTALL_DIR/.env"
echo "     CI deploy workflows write .env fresh from GH secrets on every push."
echo ""
echo "  2. Start the stack as the deploy user (services are GHCR-only — no local builds):"
echo "       su - $DEPLOY_USER"
echo "       cd $INSTALL_DIR"
echo "       docker compose -f compose/docker-compose.prod.yml --env-file .env pull"
echo "       docker compose -f compose/docker-compose.prod.yml --env-file .env up -d"
echo ""
echo "  3. Verify: curl http://localhost:8000/health"
echo ""
echo "  4. Start the backup timer: systemctl start isnad-backup.timer"
echo ""

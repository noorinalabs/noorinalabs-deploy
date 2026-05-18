#!/usr/bin/env bash
# =============================================================================
# bootstrap-vps.sh — First-time VPS setup for Noorina Labs
#
# Run as root on a fresh Hetzner VPS:
#   ssh -i ~/.ssh/isnad_deploy root@isnad.noorinalabs.com
#   curl -sL https://raw.githubusercontent.com/noorinalabs/noorinalabs-deploy/main/scripts/bootstrap-vps.sh | bash
#
# Or copy this script to the VPS and run:
#   chmod +x bootstrap-vps.sh && ./bootstrap-vps.sh
# =============================================================================
set -euo pipefail

# Suppress all interactive debconf prompts during apt operations. Without this,
# any package whose postinst asks a question (docker.io "restart Docker?",
# openssh-server, libc6, etc.) hangs the script indefinitely on the first
# upgrade-with-prompt. See #110 for the incident this fixes.
export DEBIAN_FRONTEND=noninteractive

REPO_URL="https://github.com/noorinalabs/noorinalabs-deploy.git"
INSTALL_DIR="/opt/noorinalabs-deploy"
DEPLOY_USER="deploy"

# Auxiliary repo directories pre-provisioned under /opt/ (see Step 4.5).
# Hoisted from Step 4.5 to module scope so the safe.directory loop in Step 3.5
# can reference the same list — keeping AUX_REPOS as the single source of truth
# for "directories root should be allowed to git-operate on after deploy
# takes ownership."
AUX_REPOS=(
  "noorinalabs-isnad-graph"
  "noorinalabs-design-system"
)

echo "============================================="
echo "  Noorina Labs VPS bootstrap"
echo "============================================="
echo ""

# ── Preflight checks ────────────────────────────────────────────────────────
if [ "$(id -u)" -ne 0 ]; then
  echo "ERROR: This script must be run as root."
  exit 1
fi

# ── Step 1: System packages ─────────────────────────────────────────────────
echo "==> [1/7] Installing system packages..."
apt-get update -qq
# --force-confold: keep operator-modified config files on upgrade (don't blow
#   away changes under /etc/docker/, /etc/ssh/, etc.).
# --force-confdef: use the package's default when there's no existing file to
#   preserve. Standard idempotent-upgrade pair.
apt-get install -y -qq \
  -o Dpkg::Options::="--force-confdef" \
  -o Dpkg::Options::="--force-confold" \
  docker.io docker-compose-v2 docker-buildx git curl > /dev/null
systemctl enable docker
systemctl start docker
echo "    Docker version: $(docker --version)"

# ── Step 2: Create deploy user ──────────────────────────────────────────────
echo "==> [2/7] Creating deploy user..."
if id "$DEPLOY_USER" &>/dev/null; then
  echo "    User '$DEPLOY_USER' already exists, skipping."
else
  adduser --disabled-password --gecos "" "$DEPLOY_USER"
  echo "    Created user '$DEPLOY_USER'."
fi
usermod -aG docker "$DEPLOY_USER"

# ── Step 3: Merge SSH authorized_keys into deploy user (append-with-dedup) ──
# Per deploy#112: this used to be a destructive `cp` of root's authorized_keys
# over the deploy user's file. That wiped the deploy CI key any time an
# operator added a personal admin key to /root/.ssh/authorized_keys and then
# re-ran bootstrap (as PR #109 instructs for new aux-repos). The next
# workflow_dispatch then failed with `ssh: handshake failed`.
#
# Fix: append each root key to deploy's file only if its fingerprint isn't
# already present. Idempotent across any number of re-runs; preserves the
# deploy CI key and any keys an operator has already added to deploy directly.
# Owner's manual unblock recipe (`cat key.pub | ssh prod 'tee -a ...'`) is
# the same shape — append, not overwrite.
echo "==> [3/7] Setting up SSH for deploy user..."
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
  # Build the set of fingerprints already authorized for deploy. We fingerprint
  # each line individually rather than passing the whole file to `ssh-keygen -lf`
  # because that bails on the first malformed line.
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
    # Malformed line in root's file — skip rather than copy garbage into
    # deploy's authorized_keys.
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
  echo "    WARNING: /root/.ssh/authorized_keys not found."
  echo "    You'll need to manually add your public key to $DEPLOY_AUTH_KEYS"
fi

# ── Step 3.5: git safe.directory exceptions ────────────────────────────────
# Allow root to operate on repos owned by the deploy user (git 2.35+
# CVE-2022-24765 mitigation). Step 4's `git fetch && git reset --hard` runs
# as root inside $INSTALL_DIR, which gets chowned to $DEPLOY_USER at the end
# of Step 4 on first-run; every subsequent re-run hits the ownership-mismatch
# refusal without these exceptions. Aux-repo dirs are listed too because the
# same hazard applies if/when bootstrap evolves to git-operate on them. See
# deploy#113. Runs after Step 1 (git installed) and before Step 4 (first git
# call) — the `${AUX_REPOS[@]/#//opt/}` form prefixes each entry with /opt/.
echo "==> [3.5/7] Adding git safe.directory exceptions..."
for dir in "$INSTALL_DIR" "${AUX_REPOS[@]/#//opt/}"; do
  git config --global --add safe.directory "$dir"
done

# ── Step 4: Clone the repository ────────────────────────────────────────────
echo "==> [4/7] Cloning repository to $INSTALL_DIR..."
if [ -d "$INSTALL_DIR/.git" ]; then
  echo "    Repository already exists, pulling latest..."
  cd "$INSTALL_DIR" && git fetch origin main && git reset --hard origin/main
else
  git clone "$REPO_URL" "$INSTALL_DIR"
fi
chown -R $DEPLOY_USER:$DEPLOY_USER "$INSTALL_DIR"

# ── Step 4.5: Pre-provision auxiliary repo directories ─────────────────────
# /opt/ is root-owned by default, so the deploy user can't `git clone` into it
# without these dirs existing first with the right ownership. The deploy
# workflow (.github/workflows/deploy-isnad-graph.yml) clone-or-pulls into each
# of these. To add a new repo to the deploy pipeline: append it to AUX_REPOS
# (declared at the top of this script alongside INSTALL_DIR — that placement
# also feeds the safe.directory loop), re-run this bootstrap script
# (idempotent), done. See deploy#108 for context.
echo "==> [4.5/7] Pre-provisioning auxiliary repo directories under /opt/..."
for repo in "${AUX_REPOS[@]}"; do
  dir="/opt/$repo"
  if [ ! -d "$dir" ]; then
    mkdir -p "$dir"
    echo "    Created $dir"
  else
    echo "    Exists  $dir"
  fi
  chown $DEPLOY_USER:$DEPLOY_USER "$dir"
done
echo "    Auxiliary repo dirs ready."

# ── Step 5: Create production .env ──────────────────────────────────────────
echo "==> [5/7] Creating production .env..."
ENV_FILE="$INSTALL_DIR/.env"
if [ -f "$ENV_FILE" ]; then
  echo "    .env already exists, skipping. Edit manually if needed:"
  echo "    nano $ENV_FILE"
else
  cat > "$ENV_FILE" << 'ENVEOF'
# =============================================================================
# noorinalabs-isnad-graph production environment
# Fill in all values marked CHANGE-ME before starting the stack.
# =============================================================================

# Neo4j [REQUIRED]
NEO4J_URI=bolt://neo4j:7687
NEO4J_USER=CHANGE-ME
NEO4J_PASSWORD=CHANGE-ME

# PostgreSQL [REQUIRED]
POSTGRES_USER=CHANGE-ME
POSTGRES_PASSWORD=CHANGE-ME
POSTGRES_DB=isnad_graph

# Redis [REQUIRED]
REDIS_URL=redis://:CHANGE-ME@redis:6379/0
REDIS_PASSWORD=CHANGE-ME

# Grafana [REQUIRED]
GRAFANA_ADMIN_USER=admin
GRAFANA_ADMIN_PASSWORD=CHANGE-ME
GRAFANA_ROOT_URL=https://isnad.noorinalabs.com/grafana

# CORS
CORS_ORIGINS=["https://isnad.noorinalabs.com"]

# Logging
LOG_LEVEL=INFO
LOG_FORMAT=json

# Rate Limiting
RATE_LIMIT_REQUESTS_PER_MINUTE=120
RATE_LIMIT_WINDOW_SECONDS=60

# Paths
DATA_RAW_DIR=./data/raw
DATA_STAGING_DIR=./data/staging
DATA_CURATED_DIR=./data/curated

# Backblaze B2 — backup credentials [REQUIRED for backup timer]
# Consumed by scripts/backup.sh:59-61 which uses `${VAR:?...}` fail-fast.
# Scope: read+write+delete on the isnad-graph-backups bucket only (separate
# from PIPELINE_B2_* ingest scope and TF_STATE_B2_* terraform backend scope).
# The deploy-{stg,prod}.yml workflows populate these from BACKUP_B2_* GH
# secrets (writing empty strings when unset, so backup.sh fails fast at
# preflight); manual operators bootstrapping a new VPS must fill these in
# by hand. See deploy#188 / #191.
#
# IMPORTANT: leave B2_KEY_ID and B2_APP_KEY EMPTY (not "CHANGE-ME") at
# bootstrap. The ${VAR:?} preflight in backup.sh treats unset and empty
# identically; a literal "CHANGE-ME" satisfies :? and would let backup.sh
# proceed to rclone, which then fails with an opaque B2 401. Empty values
# give the operator the actually-useful "B2_KEY_ID must be set" error.
B2_KEY_ID=
B2_APP_KEY=
B2_BUCKET=isnad-graph-backups
ENVEOF
  chmod 600 "$ENV_FILE"
  chown $DEPLOY_USER:$DEPLOY_USER "$ENV_FILE"
  echo ""
  echo "    ╔═══════════════════════════════════════════════════════╗"
  echo "    ║  IMPORTANT: Edit $ENV_FILE before starting!          ║"
  echo "    ║  Replace all CHANGE-ME values with real credentials. ║"
  echo "    ╚═══════════════════════════════════════════════════════╝"
  echo ""
  echo "    nano $ENV_FILE"
  echo ""
fi

# ── Step 6: Install rclone for backups ──────────────────────────────────────
echo "==> [6/7] Installing rclone for backups..."
if command -v rclone &>/dev/null; then
  echo "    rclone already installed: $(rclone version --check | head -1)"
else
  curl -sL https://rclone.org/install.sh | bash > /dev/null 2>&1
  echo "    rclone installed: $(rclone version --check 2>/dev/null | head -1 || echo 'installed')"
fi

# ── Step 7: Install backup systemd timer + persistent staging dir ───────────
echo "==> [7/7] Installing backup timer..."
if [ -f "$INSTALL_DIR/systemd/isnad-backup.service" ]; then
  # Install the unit + timer + the OnFailure=-target failure-marker unit.
  install -m 644 "$INSTALL_DIR/systemd/isnad-backup.service"               /etc/systemd/system/
  install -m 644 "$INSTALL_DIR/systemd/isnad-backup.timer"                 /etc/systemd/system/
  install -m 644 "$INSTALL_DIR/systemd/isnad-backup-failure-marker.service" /etc/systemd/system/

  # Provision the persistent staging directory via tmpfiles.d. Without this,
  # the unit's ReadWritePaths=/var/lib/noorinalabs-backups would fail the
  # mount-namespace setup with status=226/NAMESPACE before ExecStart fires
  # — the original deploy#121 Bug A failure mode against /tmp/isnad-backups.
  install -m 644 "$INSTALL_DIR/systemd/tmpfiles.d/noorinalabs-backups.conf" /etc/tmpfiles.d/
  systemd-tmpfiles --create /etc/tmpfiles.d/noorinalabs-backups.conf

  # Provision the node-exporter textfile-collector directory so the
  # failure-marker unit can drop *.prom files there. Idempotent: install -d
  # is a no-op when the directory already exists with the right mode.
  install -d -m 0755 /var/lib/node_exporter

  systemctl daemon-reload
  systemctl enable isnad-backup.timer
  echo "    Backup timer installed (daily at 03:00 UTC)."
  echo "    Failure marker unit installed (OnFailure= → /var/lib/node_exporter/*.prom + journal)."
  echo "    Persistent staging dir provisioned: /var/lib/noorinalabs-backups (mode 0700, root)."
  echo "    Start with: systemctl start isnad-backup.timer"
else
  echo "    Backup systemd files not found, skipping."
fi

# ── Done ────────────────────────────────────────────────────────────────────
echo ""
echo "============================================="
echo "  Bootstrap complete!"
echo "============================================="
echo ""
echo "Next steps:"
echo "  1. Edit the .env file with your production credentials:"
echo "     nano $ENV_FILE"
echo ""
echo "  2. Start the stack as the deploy user (services are GHCR-only — no local builds):"
echo "     su - $DEPLOY_USER"
echo "     cd $INSTALL_DIR"
echo "     docker login ghcr.io -u noorinalabs --password-stdin <<< \"\$GH_PACKAGES_TOKEN\""
echo "     docker compose -f compose/docker-compose.prod.yml --env-file .env pull"
echo "     docker compose -f compose/docker-compose.prod.yml --env-file .env up -d"
echo ""
echo "  3. Verify:"
echo "     curl http://localhost:8000/health"
echo ""
echo "  4. Add VPS_HOST variable in GitHub repo settings:"
echo "     Settings → Variables → New: VPS_HOST = isnad.noorinalabs.com"
echo ""
echo "  5. Start the backup timer:"
echo "     systemctl start isnad-backup.timer"
echo ""

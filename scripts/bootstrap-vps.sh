#!/usr/bin/env bash
# =============================================================================
# bootstrap-vps.sh — Residual VPS bootstrap for Noorina Labs
#
# !! ONLY run this on existing VPSes that pre-date cloud-init, or as an !!
# !! idempotent refresher for the items cloud-init does NOT cover yet:  !!
# !!   - aux-repo dir pre-provisioning under /opt/                      !!
# !!   - systemd backup timer (isnad-backup.{service,timer})            !!
# !!   - SSH key merge for operator-added admin keys on root            !!
# !!   - first-time .env stub for manual (non-CI) bring-ups             !!
#
# Privilege model (deploy#311, option (b) followup to #113): every git
# operation on a deploy-owned /opt/ repo runs as the deploy user via
# `sudo -u "$DEPLOY_USER" git ...`, NOT as root. This removes the
# CVE-2022-24765 ownership mismatch at its source — root never git-operates
# on a deploy-owned tree — so the `git config --global --add safe.directory`
# loop that #113/PR #309 added (option (a)) is no longer needed and has been
# removed. Running git as the deploy user is also symmetric with how the
# deploy user runs everything else at runtime.
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

# ── Swap-provisioning helpers (deploy#506) ──────────────────────────────────
# Two small, pure helpers used by Step 5. Kept INLINE (not a sourced library)
# because this script is delivered standalone via `curl … | bash` (RUNBOOK.md),
# where a sibling lib file would not be present. The constrained `--selftest`
# dispatch below lets scripts/tests exercise them in isolation without running
# the privileged bootstrap body.

# swap_size_to_mib SIZE
#   Echo SIZE expressed in whole MiB, for the `dd bs=1M count=…` fallback used
#   when the backing FS doesn't support fallocate. Accepts an integer with an
#   optional unit suffix: bare or G/g = GiB (8, 8G, 8g), M/m = MiB (8192M), and
#   K/k = KiB (2097152K). A KiB value that is not a whole number of MiB (the dd
#   fallback is an integer `count=`), any unrecognized unit, and any non-integer
#   value are rejected with a non-zero return rather than being silently
#   mis-sized by the `$(( ))` arithmetic (the fallocate path handles arbitrary
#   suffixes; this fallback must not guess). Generalized from the G-only #506
#   helper by deploy#507.
swap_size_to_mib() {
  local size num unit
  size="${1:-}"
  # Strip a single trailing unit char if present: `num` is the integer part and
  # `unit` is "" (bare) or that char. A bare value or garbage strips nothing, so
  # num=size and the numeric check below rejects any non-integer.
  num="${size%[GgMmKk]}"
  unit="${size#"$num"}"
  case "$num" in
    '' | *[!0-9]*)
      echo "swap_size_to_mib: unsupported SWAP_SIZE '${size}'" \
        "(need an integer with an optional G/M/K unit, e.g. 8, 8G, 8192M, 2097152K)" >&2
      return 1
      ;;
  esac
  case "$unit" in
    '' | G | g) echo "$((num * 1024))" ;;
    M | m) echo "$num" ;;
    K | k)
      # The dd fallback is `bs=1M count=N` (integer MiB), so a KiB value that is
      # not a whole number of MiB cannot be expressed exactly — reject it loudly
      # rather than truncate to a wrong (possibly 0) size.
      if [ "$((num % 1024))" -ne 0 ]; then
        echo "swap_size_to_mib: SWAP_SIZE '${size}' is not a whole number of MiB" \
          "(dd fallback needs integer MiB; use a KiB value divisible by 1024)" >&2
        return 1
      fi
      echo "$((num / 1024))"
      ;;
    *)
      # Unreachable given the strip above only removes G/g/M/m/K/k, but kept as a
      # defensive floor for a script that runs as root.
      echo "swap_size_to_mib: unsupported SWAP_SIZE '${size}'" \
        "(need an integer with an optional G/M/K unit, e.g. 8, 8G, 8192M, 2097152K)" >&2
      return 1
      ;;
  esac
}

# swap_fstab_entry_wanted SWAPFILE
#   Succeed (0) only when SWAPFILE actually exists, so the persistent
#   /etc/fstab entry is written only for a swapfile this script created or
#   (re)activated — never for a host whose active swap is a FOREIGN device (a
#   swap partition), where SWAPFILE was intentionally left absent. A fstab line
#   for an absent file makes systemd-fstab-generator fail the .swap unit
#   (ENOENT) on the next boot and bring the host up degraded.
swap_fstab_entry_wanted() {
  [ -e "${1:-}" ]
}

# Test-only dispatch: `bootstrap-vps.sh --selftest <helper> [args…]` runs a
# single named helper and exits, so scripts/tests can assert on it. Restricted
# to the two helpers above (never an arbitrary command) since this script runs
# as root. No effect on a normal run, where $1 is unset/empty.
if [ "${1:-}" = "--selftest" ]; then
  shift
  case "${1:-}" in
    swap_size_to_mib | swap_fstab_entry_wanted)
      "$@"
      exit "$?"
      ;;
    *)
      echo "bootstrap-vps.sh: unknown --selftest target '${1:-}'" >&2
      exit 2
      ;;
  esac
fi

# Suppress all interactive debconf prompts during any apt operations this
# script may trigger transitively (e.g. via `apt-get update` if a re-run
# refreshes lists). Cloud-init's own apt: conf: handles --force-conf{def,old}
# for fresh boots; this guard remains for safety on re-runs. See #110.
export DEBIAN_FRONTEND=noninteractive

REPO_URL="https://github.com/noorinalabs/noorinalabs-deploy.git"
INSTALL_DIR="/opt/noorinalabs-deploy"
DEPLOY_USER="deploy"

# Host swapfile size (Step 5). Both stg + prod are 15 GB RAM / 0-swap hosts;
# the #723 data-reload OOM-killed the graph-load loader because Neo4j (heap
# 5G + pagecache 3G + overhead) left too little headroom and there was no
# swap to absorb the spike. 8 GB is the default; override with SWAP_SIZE, which
# accepts an integer plus an optional G/M/K unit (bare = GiB), e.g. 8G, 8192M.
SWAP_SIZE="${SWAP_SIZE:-8G}"
SWAPFILE="/swapfile"

# Auxiliary repo directories pre-provisioned under /opt/ (see Step 3).
# Single source of truth. To add a new repo to the deploy pipeline: append
# it here, re-run this
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
# Per ADR 0006 (#164) cloud-init now seeds the two files with DIFFERENT keys:
# /home/deploy/.ssh/authorized_keys gets ${deploy_ssh_public_key} (the per-env
# DEPLOY key), and /root/.ssh/authorized_keys gets ${root_ssh_public_key} (the
# per-env ROOT key, owner-workstation-only). On a fresh cloud-init box the two
# keys are distinct by design — the role split is the whole point of ADR 0006.
#
# Residual value of this merge: operators who add PERSONAL admin keys to
# /root/.ssh/authorized_keys post-provision and want them reachable from the
# deploy user too. Idempotent append-with-fingerprint-dedup (#287).
#
# ADR 0006 role separation (#352): this merge must NOT copy the canonical
# per-env ROOT key into the deploy user. The whole point of the split is that
# the root key authorizes root ONLY and the deploy key authorizes deploy ONLY.
# Previously this loop copied EVERY /root/.ssh/authorized_keys line into deploy,
# including the cloud-init-seeded canonical root key — re-authorizing root for
# deploy and partially eroding the separation. We now exclude the canonical root
# key by fingerprint so ONLY operator-personal admin keys (added to root post-
# provision) flow into deploy.
#
# Identifying the canonical root key: the operator supplies it out-of-band so
# this on-box script can fingerprint-match and skip it. Set ONE of:
#   CANONICAL_ROOT_PUBKEY       — the pubkey line itself (e.g. "ssh-ed25519 AAA... root@prod")
#   CANONICAL_ROOT_PUBKEY_FILE  — path to a file containing that pubkey line
# This is the same ${root_ssh_public_key} cloud-init seeded for this env (see
# terraform/hetzner/modules/hetzner-vps/cloud-init.yaml.tpl write_files). When
# neither is set, the script PRESERVES the legacy behavior (merge everything)
# but prints a loud warning — on a role-separated box the operator should always
# pass it. Do not rely on this merge as part of the role-separation guarantee
# unless the canonical root key is supplied.
#
# Residual value of the merge (post-exclusion): operators who add PERSONAL admin
# keys to /root/.ssh/authorized_keys post-provision and want them reachable from
# the deploy user too. Idempotent append-with-fingerprint-dedup (#287).
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

# Resolve the canonical root key's fingerprint (the line to EXCLUDE from the
# merge per ADR 0006). Empty when the operator did not supply it.
CANONICAL_ROOT_FP=""
canonical_root_line=""
if [ -n "${CANONICAL_ROOT_PUBKEY:-}" ]; then
  canonical_root_line="$CANONICAL_ROOT_PUBKEY"
elif [ -n "${CANONICAL_ROOT_PUBKEY_FILE:-}" ]; then
  if [ -f "$CANONICAL_ROOT_PUBKEY_FILE" ]; then
    # First non-empty, non-comment line of the supplied pubkey file.
    canonical_root_line=$(grep -vE '^[[:space:]]*($|#)' "$CANONICAL_ROOT_PUBKEY_FILE" | head -n1)
  else
    echo "ERROR: CANONICAL_ROOT_PUBKEY_FILE='$CANONICAL_ROOT_PUBKEY_FILE' not found." >&2
    exit 1
  fi
fi
if [ -n "$canonical_root_line" ]; then
  CANONICAL_ROOT_FP=$(ssh_key_fp "$canonical_root_line")
  if [ -z "$CANONICAL_ROOT_FP" ]; then
    echo "ERROR: supplied canonical root pubkey is not a valid SSH public key line." >&2
    exit 1
  fi
fi

if [ -f /root/.ssh/authorized_keys ]; then
  declare -A DEPLOY_FPS=()
  while IFS= read -r existing_line; do
    case "$existing_line" in ''|\#*) continue ;; esac
    fp=$(ssh_key_fp "$existing_line")
    [ -n "$fp" ] && DEPLOY_FPS["$fp"]=1
  done < "$DEPLOY_AUTH_KEYS"

  if [ -z "$CANONICAL_ROOT_FP" ]; then
    echo "    WARNING (ADR 0006): canonical root pubkey not supplied"
    echo "    (set CANONICAL_ROOT_PUBKEY or CANONICAL_ROOT_PUBKEY_FILE). Merging"
    echo "    ALL root keys including the canonical root key — this re-authorizes"
    echo "    the root key for the deploy user and erodes per-role separation."
  fi

  added=0
  skipped=0
  excluded=0
  while IFS= read -r root_line; do
    case "$root_line" in ''|\#*) continue ;; esac
    fp=$(ssh_key_fp "$root_line")
    [ -z "$fp" ] && continue
    # ADR 0006: never propagate the canonical per-env root key into deploy.
    if [ -n "$CANONICAL_ROOT_FP" ] && [ "$fp" = "$CANONICAL_ROOT_FP" ]; then
      excluded=$((excluded + 1))
      continue
    fi
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
  echo "    Merged root authorized_keys → deploy: $added added, $skipped already present, $excluded canonical-root excluded (ADR 0006)."
else
  echo "    /root/.ssh/authorized_keys not found — nothing to merge (expected on heavily-locked-down hosts)."
fi

# ── Step 2: Refresh the deploy repo (idempotent, as the deploy user) ───────
# Cloud-init's runcmd already clone-or-skips this on first boot. On
# subsequent runs we fetch + reset to keep an existing checkout current.
# Doubles as recovery if the working tree drifted from a manual edit.
#
# deploy#311: every git op here runs as the deploy user via
# `sudo -u "$DEPLOY_USER" git ...`. $INSTALL_DIR ends up deploy-owned, so the
# deploy user is the natural owner of the working tree and git never hits the
# CVE-2022-24765 "dubious ownership" refusal — which is why this script no
# longer needs the `git config --global --add safe.directory` loop (the
# option-(a) fix from #113/PR #309).
#
# /opt/ is root-owned by default, so the deploy user cannot create
# $INSTALL_DIR itself on a first run. Pre-create + chown it first (same shape
# as the Step 3 aux-repo dirs below), then clone into it as the deploy user.
echo "==> [2/5] Refreshing $INSTALL_DIR as $DEPLOY_USER (idempotent)..."
mkdir -p "$INSTALL_DIR"
chown "$DEPLOY_USER:$DEPLOY_USER" "$INSTALL_DIR"
if [ -d "$INSTALL_DIR/.git" ]; then
  echo "    Repository already exists, pulling latest..."
  # Adopt any pre-existing root-owned checkout (e.g. from a legacy run) so the
  # deploy user can git-operate on it; -R because the .git tree may be mixed.
  chown -R "$DEPLOY_USER:$DEPLOY_USER" "$INSTALL_DIR"
  sudo -u "$DEPLOY_USER" git -C "$INSTALL_DIR" fetch origin main
  sudo -u "$DEPLOY_USER" git -C "$INSTALL_DIR" reset --hard origin/main
else
  sudo -u "$DEPLOY_USER" git clone "$REPO_URL" "$INSTALL_DIR"
fi
chown -R "$DEPLOY_USER:$DEPLOY_USER" "$INSTALL_DIR"

# ── Step 3: Pre-provision auxiliary repo directories under /opt/ ───────────
# /opt/ is root-owned by default, so the deploy user can't `git clone` into
# it without these dirs existing first with the right ownership. The deploy
# workflow (.github/workflows/deploy-isnad-graph.yml) clone-or-pulls into
# each of these.
#
# CLOUD-INIT GAP (#163): cloud-init.yaml.tpl does NOT currently create
# these dirs — until that gap is closed, this script is the canonical place
# they live. To add a new repo to the deploy pipeline: append it to
# AUX_REPOS at the top of this script, re-run (idempotent).
echo "==> [3/5] Pre-provisioning auxiliary repo directories under /opt/..."
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

# ── Step 4: Install backup systemd timer + persistent staging dir ──────────
# CLOUD-INIT GAP (#163): cloud-init does NOT install the systemd backup
# units. Until that gap is closed, the timer + tmpfiles.d staging dir +
# failure-marker unit are installed here.
#
# .env note: the CI deploy workflow (deploy-isnad-graph.yml) writes
# /opt/noorinalabs-deploy/.env fresh from GH Action secrets on every push,
# so we no longer write a CHANGE-ME stub here. For first-time MANUAL
# bring-ups (no CI yet), copy compose/env.example and edit by hand:
#   cp $INSTALL_DIR/compose/env.example $INSTALL_DIR/.env && chmod 600 $INSTALL_DIR/.env
echo "==> [4/5] Installing backup timer..."
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
  echo "    Backup systemd files not found, skipping (did the repo refresh in Step 2 succeed?)."
fi

# ── Step 5: Ensure a persistent host swapfile (deploy#504) ──────────────────
# CLOUD-INIT GAP: the 15 GB stg/prod hosts ship with 0 swap. The #723 prod
# data-reload rehearsal OOM-killed the graph-load loader on stg because Neo4j
# (heap 5G + pagecache 3G + JVM/page-cache overhead, capped at a 10G container
# limit) left too little headroom on a swapless box. The operator hand-added an
# 8 GB swapfile to unblock the rehearsal, but it was ephemeral (no /etc/fstab
# entry → lost on reboot). This step codifies that swapfile as IaC so it is
# provisioned on bootstrap AND persists across reboots — for stg today and prod
# before its data-load.
#
# Idempotent: if ANY swap is already active (e.g. the operator's live swapfile,
# or a swap partition), it is left untouched. Otherwise the file is created only
# if absent, then activated; the /etc/fstab entry is added only if not already
# present. Re-runs are safe. Size is overridable via SWAP_SIZE (default 8G).
echo "==> [5/5] Ensuring persistent host swap (size ${SWAP_SIZE}, override with SWAP_SIZE)..."
if [ "$(swapon --show --noheadings | wc -l)" -gt 0 ]; then
  echo "    Swap already active — leaving existing swap untouched:"
  swapon --show | sed 's/^/      /'
else
  if [ ! -e "$SWAPFILE" ]; then
    echo "    No active swap and $SWAPFILE absent — creating ${SWAP_SIZE} swapfile..."
    # Prefer fallocate; fall back to dd if the backing FS doesn't support it.
    if ! fallocate -l "$SWAP_SIZE" "$SWAPFILE" 2>/dev/null; then
      echo "    fallocate unsupported on this FS — falling back to dd..."
      # dd needs an explicit MiB count. swap_size_to_mib (deploy#506, multi-unit
      # since #507) converts the G/M/K SWAP_SIZE and aborts on an unsupported
      # unit or a sub-MiB KiB value rather than letting it silently break the
      # `$(( ))` arithmetic (8G -> 8192, 8192M -> 8192, 2097152K -> 2048).
      swap_mib="$(swap_size_to_mib "$SWAP_SIZE")"
      dd if=/dev/zero of="$SWAPFILE" bs=1M count="$swap_mib" status=none
    fi
    chmod 600 "$SWAPFILE"
    mkswap "$SWAPFILE" >/dev/null
  else
    echo "    $SWAPFILE already exists but is not active — (re)activating it..."
    chmod 600 "$SWAPFILE"
    # mkswap only if it lacks a swap signature (avoid clobbering live data).
    if ! blkid "$SWAPFILE" 2>/dev/null | grep -q 'TYPE="swap"'; then
      mkswap "$SWAPFILE" >/dev/null
    fi
  fi
  swapon "$SWAPFILE"
  echo "    Swap activated:"
  swapon --show | sed 's/^/      /'
fi

# Persist across reboots via /etc/fstab — but only for a swapfile that actually
# exists (deploy#506). If the host's active swap is a foreign device (a swap
# partition, not $SWAPFILE), the block above took the "leave existing swap
# untouched" branch and never created $SWAPFILE; appending a fstab line for that
# absent file would make systemd-fstab-generator fail the .swap unit (ENOENT) on
# the next boot and bring the host up degraded. The inner grep still guards
# re-runs against duplicate entries.
if swap_fstab_entry_wanted "$SWAPFILE"; then
  if ! grep -qE "^[[:space:]]*${SWAPFILE}[[:space:]]+none[[:space:]]+swap[[:space:]]" /etc/fstab; then
    echo "${SWAPFILE} none swap sw 0 0" >> /etc/fstab
    echo "    Added persistent /etc/fstab entry: ${SWAPFILE} none swap sw 0 0"
  else
    echo "    /etc/fstab already has a ${SWAPFILE} swap entry — not duplicating."
  fi
else
  echo "    Active swap is a foreign device (not ${SWAPFILE}) — skipping ${SWAPFILE} fstab entry."
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
echo "  - Seed root + deploy authorized_keys with the per-env per-role keys (ADR 0006: root key, deploy key)"
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

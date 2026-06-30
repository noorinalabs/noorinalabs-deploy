#cloud-config
# =============================================================================
# cloud-init template for Noorina Labs VPS provisioning
# Installs Docker, security hardening (fail2ban, ufw), and GHCR auth.
# (Caddy runs as a Docker container via compose, not via apt.)
#
# Services provisioned on this VPS:
#   - isnad-graph (FastAPI + React + Neo4j)
#   - user-service (FastAPI + PostgreSQL + Redis)
#
# Individual containers are managed by Docker Compose, not Terraform.
# This template bootstraps the VPS with prerequisites for all services.
# =============================================================================

package_update: true
package_upgrade: true

# Apt configuration applied to cloud-init's own `packages:` install + the
# package_upgrade step. Per #173 gap D: cloud-init defaults don't set
# DEBIAN_FRONTEND, so any package with an interactive postinst (needrestart's
# whiptail "Pending kernel upgrade" being the most common) creates noise in
# cloud-init.log and is a hang risk. --force-confold/confdef preserves
# operator-modified config files on upgrade; matches the apt invocation
# pattern in bootstrap-vps.sh since deploy#110.
apt:
  conf: |
    DPkg::Options {
      "--force-confdef";
      "--force-confold";
    };

packages:
  - docker.io
  - docker-compose-v2
  - docker-buildx
  - git
  - curl
  - fail2ban
  - ufw
  - unattended-upgrades
  - rclone
  - jq

# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------
users:
  - name: deploy
    groups: docker, sudo
    shell: /bin/bash
    sudo: ALL=(ALL) NOPASSWD:ALL
    ssh_authorized_keys:
      - ${deploy_ssh_public_key}

# ---------------------------------------------------------------------------
# Write configuration files
# ---------------------------------------------------------------------------
write_files:
  # Root authorized_keys — the per-env ROOT key, distinct from the deploy key
  # injected into the `users:` block above. Per ADR 0006 (#164) the root key is
  # owner-workstation-only and MUST NOT appear in any GH secret; only the deploy
  # key is the env-scoped DEPLOY_SSH_PRIVATE_KEY CI secret. Replaces the role
  # previously played by Hetzner's `ssh_keys` server argument (removed in #222
  # because per-env resources couldn't share one pubkey). Operators who want an
  # additional personal id_ed25519 on root can append it post-provision.
  # See docs/adr/0006-per-env-per-role-ssh-keys.md for the split rationale
  # (supersedes 0003) and docs/runbooks/ssh-key-rotation.md for rotation.
  - path: /root/.ssh/authorized_keys
    owner: root:root
    permissions: '0600'
    content: |
      ${root_ssh_public_key}

  # fail2ban jail for SSH brute force
  - path: /etc/fail2ban/jail.local
    content: |
      [sshd]
      enabled = true
      port = 22
      filter = sshd
      logpath = /var/log/auth.log
      maxretry = 5
      bantime = 3600
      findtime = 600

  # Unattended upgrades — security patches only
  - path: /etc/apt/apt.conf.d/50unattended-upgrades
    content: |
      Unattended-Upgrade::Allowed-Origins {
          "$${distro_id}:$${distro_codename}-security";
      };
      Unattended-Upgrade::AutoFixInterruptedDpkg "true";
      Unattended-Upgrade::Remove-Unused-Dependencies "true";

  # GHCR Docker auth config for deploy user.
  # defer: true so cloud-init waits until the `users:` module has created
  # `deploy` before chowning this file — without it, cloud-init's
  # write_files module runs before users, hits OSError('Unknown user "deploy"'),
  # aborts the entire write_files stage, and silently drops the remaining
  # entries (this file, .env.user-service, .cloud-init-provisioned). See #173 gap B.
  #
  # Conditional skip (deploy#28): when ghcr_auth_b64 is empty, omit this
  # write_files entry entirely rather than writing an empty `"auth": ""`
  # blob. An empty auth blob silently breaks `docker pull ghcr.io/...` for
  # private images on first boot (per ontology/repos/deploy.yaml line 159,
  # all app services are GHCR-only with no local-build fallback). Skipping
  # the file means docker pull fails LOUDLY with "no basic auth credentials"
  # instead of half-succeeding with a broken config — the operator sees the
  # missing-credential error and supplies the value, rather than chasing a
  # silently-misconfigured deploy. Public-only deploys (rare, e.g. infra-only
  # bring-up before app images publish) work fine without the file present.
%{ if ghcr_auth_b64 != "" ~}
  - path: /home/deploy/.docker/config.json
    owner: deploy:deploy
    permissions: '0600'
    defer: true
    content: |
      {
        "auths": {
          "ghcr.io": {
            "auth": "${ghcr_auth_b64}"
          }
        }
      }
%{ endif ~}

  # ---------------------------------------------------------------------------
  # User-service environment file
  # Docker Compose reads this to configure user-postgres, user-redis, and
  # user-service containers. Values are injected from Terraform variables.
  # defer: true — same reason as the .docker/config.json entry above. See #173 gap B.
  # ---------------------------------------------------------------------------
  - path: /opt/noorinalabs-deploy/.env.user-service
    owner: deploy:deploy
    permissions: '0600'
    defer: true
    content: |
      # user-service secrets — managed by Terraform cloud-init
      USER_POSTGRES_PASSWORD=${user_postgres_password}
      USER_REDIS_PASSWORD=${user_redis_password}
      USER_SERVICE_JWT_SECRET=${user_service_jwt_secret}
      # Non-admin QA test-user seed (deploy#508). Injected here so the seed
      # survives a user-postgres DB wipe; empty values disable the seed (the
      # bootstrap_test_user.py script no-ops). TEST_USER_EMAIL is an identifier,
      # TEST_USER_PASSWORD is a secret.
      TEST_USER_EMAIL=${test_user_email}
      TEST_USER_PASSWORD=${test_user_password}

  # Deploy directory marker
  - path: /opt/noorinalabs-deploy/.cloud-init-provisioned
    content: |
      Provisioned by cloud-init at $(date -u +%Y-%m-%dT%H:%M:%SZ)

# ---------------------------------------------------------------------------
# Commands to run on first boot
# ---------------------------------------------------------------------------
runcmd:
  # Enable and start Docker
  - systemctl enable docker
  - systemctl start docker

  # Firewall — allow SSH, HTTP, HTTPS only
  - ufw default deny incoming
  - ufw default allow outgoing
  - ufw allow 22/tcp
  - ufw allow 80/tcp
  - ufw allow 443/tcp
  - ufw --force enable

  # Start fail2ban
  - systemctl enable fail2ban
  - systemctl start fail2ban

  # Disable root SSH password login (key-only).
  # Ubuntu 24.04 unit name is `ssh.service`, not `sshd.service` (debian-style
  # naming). The old `systemctl restart sshd` silently failed with
  # "Unit sshd.service not found" — sshd_config edits then only took effect
  # on the next kernel-upgrade reboot. See #173 gap C.
  - sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config
  - sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
  - systemctl restart ssh

  # Clone deploy repo
  - git clone https://github.com/noorinalabs/noorinalabs-deploy.git /opt/noorinalabs-deploy || true
  - chown -R deploy:deploy /opt/noorinalabs-deploy

  # Set up deploy user home directory
  - mkdir -p /home/deploy/.docker
  - chown -R deploy:deploy /home/deploy/.docker

  # node-exporter textfile_collector input directory (deploy#161).
  # Owned by deploy:deploy because the alembic pre-deploy gate's SSH step
  # (.github/workflows/db-migrate.yml) writes .prom files here as the
  # `deploy` user. Mode 0755 so the node-exporter container — which runs
  # as image-default uid (nobody) inside its container — can read the
  # files via its read-only bind mount in compose/docker-compose.prod.yml.
  # No host-side `node_exporter` system user is required: node-exporter
  # is a container, not a system service. Provisioning this here (TF
  # cloud-init) rather than as a runbook step prevents silent drift on
  # fresh VPSes — a missed mkdir would leave the alert silently dark.
  - mkdir -p /var/lib/node_exporter/textfile_collector
  - chown deploy:deploy /var/lib/node_exporter/textfile_collector
  - chmod 0755 /var/lib/node_exporter/textfile_collector

  # Pre-provision aux-repo dirs under /opt/ (#163 followup gap 1/3, deploy#328).
  # /opt/ is root-owned by default, so the deploy workflow
  # (.github/workflows/deploy-isnad-graph.yml) `git clone` as the deploy user
  # fails without these dirs existing first with deploy:deploy ownership.
  # Mirrors scripts/bootstrap-vps.sh Step 4 (AUX_REPOS); a strictly-cloud-init'd
  # box now matches a bootstrapped one. Same mkdir+chown shape as the
  # node_exporter triplet above.
  - mkdir -p /opt/noorinalabs-isnad-graph
  - chown deploy:deploy /opt/noorinalabs-isnad-graph
  - mkdir -p /opt/noorinalabs-design-system
  - chown deploy:deploy /opt/noorinalabs-design-system

  # Install the systemd backup unit set (#163 followup gap 2/3, deploy#329).
  # Transcribed from scripts/bootstrap-vps.sh Step 5. The source files live
  # under /opt/noorinalabs-deploy/systemd/, so this MUST run after the
  # `git clone .../noorinalabs-deploy` step above (cloud-init runcmd is ordered)
  # — otherwise the install targets don't exist yet. Without this, backups
  # never run on a strictly-cloud-init'd box (silent failure; deploy#121 class).
  # The tmpfiles.d staging dir is created before daemon-reload so the unit's
  # ReadWritePaths=/var/lib/noorinalabs-backups namespace setup succeeds
  # (deploy#121 Bug A: 226/NAMESPACE before ExecStart). The failure-marker
  # unit's .prom output dir (/var/lib/node_exporter/textfile_collector) is
  # already provisioned by the node_exporter triplet above.
  - install -m 644 /opt/noorinalabs-deploy/systemd/isnad-backup.service /etc/systemd/system/
  - install -m 644 /opt/noorinalabs-deploy/systemd/isnad-backup.timer /etc/systemd/system/
  - install -m 644 /opt/noorinalabs-deploy/systemd/isnad-backup-failure-marker.service /etc/systemd/system/
  - install -m 644 /opt/noorinalabs-deploy/systemd/tmpfiles.d/noorinalabs-backups.conf /etc/tmpfiles.d/
  - systemd-tmpfiles --create /etc/tmpfiles.d/noorinalabs-backups.conf
  - systemctl daemon-reload
  - systemctl enable isnad-backup.timer

  # Allow root to git-operate on the deploy-owned /opt/ repos (#163 followup
  # gap 3/3, deploy#330). git 2.35+ (CVE-2022-24765) refuses to run as root
  # against a deploy-owned working tree with "detected dubious ownership"; any
  # root-run `git fetch`/`git reset` (e.g. a bootstrap-vps.sh re-run, recovery)
  # then fails. MUST come after the aux-repo dirs are created above so the paths
  # exist. `--system` (not `--global`, which is per-user $HOME) because root may
  # not be the only user invoking git against these from a script. Mirrors
  # scripts/bootstrap-vps.sh Step 2; git's --add dedups so re-runs are safe.
  - git config --system --add safe.directory /opt/noorinalabs-deploy
  - git config --system --add safe.directory /opt/noorinalabs-isnad-graph
  - git config --system --add safe.directory /opt/noorinalabs-design-system

  # Enable automatic security updates
  - systemctl enable unattended-upgrades
  - systemctl start unattended-upgrades

#cloud-config
# =============================================================================
# cloud-init template for Noorina Labs VPS provisioning
# Installs Docker, Caddy, security hardening (fail2ban, ufw), and GHCR auth.
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
      - ${ssh_public_key}

# ---------------------------------------------------------------------------
# Write configuration files
# ---------------------------------------------------------------------------
write_files:
  # Root authorized_keys — the canonical operator/CI deploy key. Replaces the
  # role previously played by Hetzner's `ssh_keys` server argument (removed
  # in #222 because per-env resources couldn't share one pubkey). Operators
  # who want their personal id_ed25519 on root can append it post-provision.
  # See docs/adr/0003-ssh-key-authorization-via-cloud-init.md for rationale.
  - path: /root/.ssh/authorized_keys
    owner: root:root
    permissions: '0600'
    content: |
      ${ssh_public_key}

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

  # Install Caddy via official apt repo.
  # DEBIAN_FRONTEND=noninteractive + NEEDRESTART_MODE=a are inlined per-command
  # because cloud-init's runcmd exec's each entry independently — `export`s in
  # an earlier entry don't survive to the next. Without these, caddy's apt
  # install triggers a whiptail "Pending kernel upgrade" dialog from
  # needrestart, which prints
  #   debconf: whiptail output the above errors, giving up!
  # and "Use of uninitialized value $ret in scalar chomp" in cloud-init logs.
  # The install does NOT block today (whiptail bails when it can't open a
  # terminal) but is an apt-hang risk under stricter dialog policies and is
  # noisy in cloud-init.log. Matches the pattern used in bootstrap-vps.sh
  # since deploy#110. See #173 gap D.
  - curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  - echo "deb [signed-by=/usr/share/keyrings/caddy-stable-archive-keyring.gpg] https://dl.cloudsmith.io/public/caddy/stable/deb/debian any-version main" > /etc/apt/sources.list.d/caddy-stable.list
  - DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a apt-get update -qq
  - DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a apt-get install -y -qq -o Dpkg::Options::="--force-confdef" -o Dpkg::Options::="--force-confold" caddy
  - systemctl stop caddy
  # Caddy will be run via Docker Compose, not systemd — disable the system service
  - systemctl disable caddy

  # Enable automatic security updates
  - systemctl enable unattended-upgrades
  - systemctl start unattended-upgrades

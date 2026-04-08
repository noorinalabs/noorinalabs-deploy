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

  # GHCR Docker auth config for deploy user
  - path: /home/deploy/.docker/config.json
    owner: deploy:deploy
    permissions: '0600'
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
  # ---------------------------------------------------------------------------
  - path: /opt/noorinalabs-deploy/.env.user-service
    owner: deploy:deploy
    permissions: '0600'
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

  # Disable root SSH password login (key-only)
  - sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config
  - sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
  - systemctl restart sshd

  # Clone deploy repo
  - git clone https://github.com/noorinalabs/noorinalabs-deploy.git /opt/noorinalabs-deploy || true
  - chown -R deploy:deploy /opt/noorinalabs-deploy

  # Set up deploy user home directory
  - mkdir -p /home/deploy/.docker
  - chown -R deploy:deploy /home/deploy/.docker

  # Pre-create Docker volumes for user-service data persistence
  - docker volume create user-postgres-data
  - docker volume create user-redis-data

  # Install Caddy via official apt repo
  - curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  - echo "deb [signed-by=/usr/share/keyrings/caddy-stable-archive-keyring.gpg] https://dl.cloudsmith.io/public/caddy/stable/deb/debian any-version main" > /etc/apt/sources.list.d/caddy-stable.list
  - apt-get update -qq
  - apt-get install -y -qq caddy
  - systemctl stop caddy
  # Caddy will be run via Docker Compose, not systemd — disable the system service
  - systemctl disable caddy

  # Enable automatic security updates
  - systemctl enable unattended-upgrades
  - systemctl start unattended-upgrades

#!/usr/bin/env bash
# rotate_db_password.sh — rotate ONE database/cache password on the running
# deploy VPS, health-gated, with automatic rollback (deploy#387).
#
# Implements the bespoke scheduled-workflow rotation mechanism the owner chose
# in ADR 0007 (deploy#388): stay on the GitHub-Environment-secrets baseline
# (Option A), no new secrets manager, P2/P3 DB-password rotation built on top.
# This script is the on-box UNIT MECHANIC; the runner-side orchestration (mint
# the value, persist it to the GH Environment secret) lives in
# `.github/workflows/rotate-db-passwords.yml`, which invokes this over SSH.
#
# Scope (ADR 0007 § Decision — per-secret / S1): exactly one of the four
# DB/cache password secrets, for the env whose stack this box runs:
#   POSTGRES_PASSWORD       REDIS_PASSWORD
#   USER_POSTGRES_PASSWORD  USER_REDIS_PASSWORD
#
# Why the two engines differ (the load-bearing detail):
#   - Postgres stores the role password IN its data volume (pg_authid).
#     `POSTGRES_PASSWORD` only seeds an *empty* volume on first init, so a
#     container recreate does NOT change the password. Rotation therefore runs
#     a live `ALTER ROLE … WITH PASSWORD` against the running server, then
#     recreates the *client* services so they reconnect with the new value.
#     The postgres healthcheck uses `pg_isready` (no auth), so it does not
#     break across the change.
#   - Redis takes `requirepass` from its `command:` arg (no persisted password
#     and — here — no config file, so `CONFIG REWRITE` has nowhere to write).
#     Recreating the redis container with the new `.env` value re-applies the
#     password by construction; no live command is needed. Its healthcheck
#     interpolates `REDISCLI_AUTH=${REDIS_PASSWORD}` at container-create time,
#     so the redis container MUST be recreated (not just its clients) or its
#     healthcheck would keep using the old value and flap.
#
# Security (CWE-214 — no secret on argv / in /proc/cmdline):
#   - The old password is read from the box's own `.env` (never passed over the
#     wire); the new password arrives via the environment (appleboy `envs:`),
#     never as a command-line argument.
#   - psql auth uses `-e PGPASSWORD` NAME-ONLY passthrough (docker reads the
#     value from this script's environment, it is not on docker's argv); the
#     `ALTER ROLE` SQL (which contains the new value) is fed on STDIN, not `-c`.
#   - The new value is validated to the URL-safe alphabet `[A-Za-z0-9_-]` so it
#     can never corrupt the compose `PG_DSN`/`REDIS_URL` interpolation
#     (the us#65 / ig#956 url-unsafe-password class of bug) and needs no SQL
#     quote-escaping.
#
# Rollback invariant (#387 acceptance): a failed rotation leaves the stack on
# the OLD credential — `.env` is restored, postgres is `ALTER ROLE`d back, redis
# clients are recreated against the restored `.env`, and the script exits non-
# zero WITHOUT the caller persisting the new GH secret. The caller only updates
# the GH Environment secret AFTER this script exits 0, keeping
# "GH secret == live box" intact.
#
# Inputs (environment variables — never positional args):
#   ROTATE_SECRET   required  one of the four secret names above
#   NEW_PASSWORD    required  the freshly-minted value (URL-safe alphabet)
#   ENV_FILE        optional  path to the compose .env (default: .env)
#   COMPOSE_CMD     optional  compose invocation (default: the prod stack);
#                             overridable for tests
#   DOCKER          optional  docker binary (default: docker); overridable
#   HEALTH_RETRIES  optional  health-poll attempts per container (default: 24)
#   HEALTH_INTERVAL optional  seconds between polls (default: 5)
#
# Exit: 0 = rotated + healthy; 1 = rolled back to old credential (or input
# error before any change). Designed to run from /opt/noorinalabs-deploy.

set -euo pipefail

ENV_FILE="${ENV_FILE:-.env}"
DOCKER="${DOCKER:-docker}"
COMPOSE_CMD="${COMPOSE_CMD:-$DOCKER compose -p noorinalabs -f compose/docker-compose.prod.yml --env-file $ENV_FILE}"
HEALTH_RETRIES="${HEALTH_RETRIES:-24}"
HEALTH_INTERVAL="${HEALTH_INTERVAL:-5}"

log()  { printf '%s %s\n' "$(date -u +%H:%M:%SZ)" "$*"; }
fail() { printf '::error::%s\n' "$*" >&2; }

# --- Input validation -------------------------------------------------------
: "${ROTATE_SECRET:?ROTATE_SECRET must be set (one of POSTGRES_PASSWORD, REDIS_PASSWORD, USER_POSTGRES_PASSWORD, USER_REDIS_PASSWORD)}"
: "${NEW_PASSWORD:?NEW_PASSWORD must be set}"

if ! printf '%s' "$NEW_PASSWORD" | grep -qE '^[A-Za-z0-9_-]{24,}$'; then
  fail "NEW_PASSWORD must be >=24 chars from the URL-safe alphabet [A-Za-z0-9_-] (got ${#NEW_PASSWORD} chars). This guards the compose PG_DSN/REDIS_URL interpolation (us#65/ig#956) and avoids SQL quoting."
  exit 1
fi

if [ ! -f "$ENV_FILE" ]; then
  fail "ENV_FILE '$ENV_FILE' not found — run from /opt/noorinalabs-deploy after a deploy has written .env."
  exit 1
fi

# Per-secret routing: engine, DB container, the .env vars needed to auth, which
# compose services to recreate so they pick up the new credential, and which
# containers to health-gate on. Kept in one place so adding a secret is a single
# case arm. Container names follow the `noorinalabs-<service>-1` compose default.
ENGINE=""
DB_CONTAINER=""
USER_VAR=""
DB_VAR=""
RECREATE_SERVICES=""
HEALTH_CONTAINERS=""
case "$ROTATE_SECRET" in
  POSTGRES_PASSWORD)
    ENGINE="postgres"; DB_CONTAINER="noorinalabs-postgres-1"
    USER_VAR="POSTGRES_USER"; DB_VAR="POSTGRES_DB"
    RECREATE_SERVICES="api postgres-exporter"
    HEALTH_CONTAINERS="noorinalabs-postgres-1 noorinalabs-api-1" ;;
  USER_POSTGRES_PASSWORD)
    ENGINE="postgres"; DB_CONTAINER="noorinalabs-user-postgres-1"
    USER_VAR="USER_POSTGRES_USER"; DB_VAR="USER_POSTGRES_DB"
    RECREATE_SERVICES="user-service user-postgres-exporter"
    HEALTH_CONTAINERS="noorinalabs-user-postgres-1 noorinalabs-user-service-1" ;;
  REDIS_PASSWORD)
    ENGINE="redis"; DB_CONTAINER="noorinalabs-redis-1"
    RECREATE_SERVICES="redis api"
    HEALTH_CONTAINERS="noorinalabs-redis-1 noorinalabs-api-1" ;;
  USER_REDIS_PASSWORD)
    ENGINE="redis"; DB_CONTAINER="noorinalabs-user-redis-1"
    RECREATE_SERVICES="user-redis user-service"
    HEALTH_CONTAINERS="noorinalabs-user-redis-1 noorinalabs-user-service-1" ;;
  *)
    fail "ROTATE_SECRET '$ROTATE_SECRET' is not a rotatable DB/cache password. Allowed: POSTGRES_PASSWORD, REDIS_PASSWORD, USER_POSTGRES_PASSWORD, USER_REDIS_PASSWORD."
    exit 1 ;;
esac

if ! grep -qE "^${ROTATE_SECRET}=" "$ENV_FILE"; then
  fail "$ROTATE_SECRET not present in $ENV_FILE — nothing to rotate (is the stack deployed?)."
  exit 1
fi

# --- Read the OLD value + auth context from the box's own .env --------------
# Sourcing the .env is exactly how docker compose consumes it; the file is the
# 0600 deploy artifact written by the write-deploy-env composite in the
# `NAME='...'` single-quote-escaped form, so the values round-trip safely. We
# read in a subshell-scoped way and only keep what we need.
set -a
# shellcheck disable=SC1090  # ENV_FILE is the runtime deploy artifact, not a static path
. "$ENV_FILE"
set +a

OLD_PASSWORD="${!ROTATE_SECRET-}"
if [ -z "${OLD_PASSWORD:-}" ]; then
  fail "$ROTATE_SECRET is empty in $ENV_FILE — cannot authenticate to rotate."
  exit 1
fi
if [ "$OLD_PASSWORD" = "$NEW_PASSWORD" ]; then
  fail "NEW_PASSWORD equals the current value — refusing a no-op rotation."
  exit 1
fi

DB_USER=""
DB_NAME=""
if [ "$ENGINE" = "postgres" ]; then
  DB_USER="${!USER_VAR-}"
  DB_NAME="${!DB_VAR-}"
  if [ -z "$DB_USER" ] || [ -z "$DB_NAME" ]; then
    fail "postgres rotation needs $USER_VAR and $DB_VAR in $ENV_FILE (got user='$DB_USER' db='$DB_NAME')."
    exit 1
  fi
  # Identifier sanity — the role name is interpolated into ALTER ROLE.
  if ! printf '%s' "$DB_USER" | grep -qE '^[A-Za-z_][A-Za-z0-9_]*$'; then
    fail "$USER_VAR='$DB_USER' is not a plain SQL identifier — refusing to build ALTER ROLE."
    exit 1
  fi
fi

log "==> Rotating $ROTATE_SECRET (engine=$ENGINE, container=$DB_CONTAINER) on $ENV_FILE"
log "    recreate services: $RECREATE_SERVICES"
log "    health gate:       $HEALTH_CONTAINERS"

# --- .env backup for rollback ----------------------------------------------
BAK="$(mktemp "${TMPDIR:-/tmp}/env.rotate.XXXXXX")"
cp -p "$ENV_FILE" "$BAK"
trap 'rm -f "$BAK" 2>/dev/null || true' EXIT

# Rewrite the single ROTATE_SECRET line in .env to `value`, preserving every
# other line and the `NAME='...'` single-quote-escaped form the deploy
# composite uses (so the next normal deploy stays consistent).
rewrite_env() {
  local value="$1" tmp esc
  esc="${value//\'/\'\\\'\'}"
  tmp="$(mktemp "${TMPDIR:-/tmp}/env.write.XXXXXX")"
  # awk replaces only the matching key line. The new value is passed through the
  # ENVIRONMENT (read via ENVIRON["val"]), never as `-v val=` — `-v` would put
  # the secret on awk's argv (visible in /proc/<pid>/cmdline). Only the secret
  # NAME (non-sensitive) stays on -v. (CWE-214 — Nino, PR #438 review.)
  val="$esc" awk -v key="$ROTATE_SECRET" '
    index($0, key "=") == 1 { printf "%s=\047%s\047\n", key, ENVIRON["val"]; next }
    { print }
  ' "$ENV_FILE" > "$tmp"
  mv "$tmp" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
}

# Apply a password to the LIVE engine. For postgres this is the real rotation
# (ALTER ROLE); for redis the recreate does it, so this is a no-op.
#   $1 = password to authenticate WITH   $2 = password to SET
apply_live() {
  local auth="$1" target="$2"
  if [ "$ENGINE" = "postgres" ]; then
    # SQL (incl. the target value) on STDIN; PGPASSWORD via NAME-ONLY -e
    # passthrough — neither reaches docker/psql argv.
    printf 'ALTER ROLE "%s" WITH PASSWORD '\''%s'\'';\n' "$DB_USER" "$target" \
      | PGPASSWORD="$auth" "$DOCKER" exec -i -e PGPASSWORD "$DB_CONTAINER" \
          psql -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME" >/dev/null
  fi
}

recreate() {
  # shellcheck disable=SC2086  # RECREATE_SERVICES is a deliberate word-split list
  $COMPOSE_CMD up -d --force-recreate $RECREATE_SERVICES
}

wait_healthy() {
  local c i st rstate ok
  for c in $HEALTH_CONTAINERS; do
    ok=0
    for ((i = 1; i <= HEALTH_RETRIES; i++)); do
      st="$("$DOCKER" inspect --format '{{.State.Health.Status}}' "$c" 2>/dev/null || echo missing)"
      if [ "$st" = "healthy" ]; then ok=1; break; fi
      # Containers without a HEALTHCHECK report "<no value>"/empty — accept them
      # if simply running (e.g. the exporters).
      if [ "$st" = "<no value>" ] || [ -z "$st" ]; then
        rstate="$("$DOCKER" inspect --format '{{.State.Status}}' "$c" 2>/dev/null || echo missing)"
        if [ "$rstate" = "running" ]; then ok=1; break; fi
      fi
      sleep "$HEALTH_INTERVAL"
    done
    if [ "$ok" -ne 1 ]; then
      log "    health gate FAILED for $c (last status: ${st:-empty})"
      return 1
    fi
    log "    healthy: $c"
  done
  return 0
}

rollback() {
  fail "Rotation of $ROTATE_SECRET failed health gate — rolling back to the OLD credential."
  cp -p "$BAK" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  if [ "$ENGINE" = "postgres" ]; then
    # The live DB is currently on NEW; set it back to OLD, authenticating with NEW.
    apply_live "$NEW_PASSWORD" "$OLD_PASSWORD" || fail "    WARNING: ALTER ROLE rollback failed — DB may still hold the new password; investigate before next deploy."
  fi
  # Recreate clients (+ redis) against the restored .env so they hold OLD again.
  recreate || fail "    WARNING: rollback recreate failed — run deploy-${ENV_FILE} to reconcile."
  log "==> Rollback complete: stack restored to the previous credential."
}

# --- Rotate -----------------------------------------------------------------
log "==> Applying new credential to the live engine..."
apply_live "$OLD_PASSWORD" "$NEW_PASSWORD"

log "==> Rewriting $ENV_FILE..."
rewrite_env "$NEW_PASSWORD"

log "==> Recreating dependent services..."
recreate

log "==> Health-gating ($HEALTH_RETRIES x ${HEALTH_INTERVAL}s)..."
if wait_healthy; then
  log "==> Rotation of $ROTATE_SECRET succeeded — stack healthy on the new credential."
  exit 0
else
  rollback
  exit 1
fi

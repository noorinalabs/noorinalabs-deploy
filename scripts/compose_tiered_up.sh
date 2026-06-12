#!/bin/sh
# compose_tiered_up.sh — tiered `docker compose up` for the prod rollout
# (deploy#429).
#
# THE BUG THIS FIXES
# ------------------
# The prod deploy previously ran a single
#   docker compose ... up -d --force-recreate --remove-orphans
# over the WHOLE stack. `compose up` brings services up in dependency order and
# (with healthcheck-gated `depends_on`) FAILS THE WHOLE COMMAND if any service
# is unhealthy. The Kafka broker (pipeline tier) has dependents — kafka-init /
# kafka-ui / kafka-exporter (`depends_on: condition: service_healthy`) — so when
# the broker crash-loops, that single `up` aborts with
#   "dependency failed to start: container noorinalabs-kafka-1 is unhealthy"
# and the services LATER in the start graph (caddy + frontend) are left in
# `Created`, never started. caddy never binds :80/:443 → every public endpoint
# returns 521 (Cloudflare origin-unreachable) → total edge outage — even though
# api/neo4j/postgres/redis came up healthy. The app tier does NOT depend on
# Kafka, so a broker hiccup must NEVER take the reverse proxy down.
#
# THE FIX — THREE TIERS
# ---------------------
#   tier 1  app+edge  ($GATED_SERVICES)   health-GATED  (`up -d --wait`)
#           A genuine app/edge failure (api/frontend/landing/user-service/caddy
#           unhealthy) fails the deploy HERE. These are the only services whose
#           health may gate the rollout.
#   tier 2  remainder (observability + already-up app deps)  NON-gating
#           Everything that is neither gated nor deferred. Brought up best-effort
#           so an observability hiccup also cannot down the edge.
#   tier 3  pipeline  ($DEFERRED_SERVICES)  NON-gating
#           Kafka + its dependents, brought up LAST and in isolation so a broker
#           crash-loop is reported (::warning::) but never fails the app deploy.
#
# The app deps (neo4j/postgres/redis/user-postgres/user-redis/
# user-service-migrate) are pulled in transitively by the tier-1 `--wait`
# closure; tier 2 re-touches them as a no-op (compose only recreates on a
# config/image change).
#
# INVOCATION
# ----------
# Called by `.github/actions/write-deploy-env` on the prod VPS (the repo is
# `git reset --hard origin/main` there, so this script is present):
#   COMPOSE_CMD="docker compose -p noorinalabs -f compose/docker-compose.prod.yml --env-file .env" \
#     GATED_SERVICES="caddy frontend landing api user-service" \
#     DEFERRED_SERVICES="kafka kafka-init kafka-ui kafka-exporter" \
#     sh scripts/compose_tiered_up.sh
#
# stg keeps the legacy single-`up` path (the composite only calls this script
# when GATED_SERVICES is set), so this change is prod-only.
#
# POSIX sh on purpose (mirrors scripts/kafka_logdir_preflight.sh): no bashisms,
# so it is unit-tested directly with a mocked COMPOSE_CMD — see
# scripts/tests/test_compose_tiered_up.py.
set -eu

# Base `docker compose ...` invocation. Overridable so the unit test can inject
# a mock that records its args and simulates `config --services` + failures.
COMPOSE_CMD="${COMPOSE_CMD:-docker compose -p noorinalabs -f compose/docker-compose.prod.yml --env-file .env}"
GATED_SERVICES="${GATED_SERVICES:-}"
DEFERRED_SERVICES="${DEFERRED_SERVICES:-}"

if [ -z "$GATED_SERVICES" ]; then
  echo "ERROR: GATED_SERVICES is empty — refusing to roll prod with no health-gated tier." >&2
  echo "       (The composite only invokes this script for the prod tiered path.)" >&2
  exit 1
fi

# Run the base compose command with the given args. COMPOSE_CMD is deliberately
# word-split into the program + its flags.
compose() {
  # shellcheck disable=SC2086
  $COMPOSE_CMD "$@"
}

is_deferred() {
  _needle="$1"
  for _d in $DEFERRED_SERVICES; do
    if [ "$_needle" = "$_d" ]; then
      return 0
    fi
  done
  return 1
}

# ── tier 1: app+edge — health-gated ──────────────────────────────────────────
# `--wait` makes a still-unhealthy app/edge service (incl. caddy, whose own
# health nothing else depends on) fail the deploy. The deferred pipeline tier is
# NOT in this set, so the broker's health can never gate or abort it.
echo "==> [tier 1/3] App+edge bring-up (health-gated --wait): $GATED_SERVICES"
# shellcheck disable=SC2086
compose up -d --force-recreate --wait $GATED_SERVICES

# ── tier 2: remainder (observability + app deps) — NON-gating ─────────────────
# Everything that is not deferred. Excludes the pipeline tier so a broker hiccup
# cannot abort observability bring-up. Non-gating: a failure here is reported but
# does not fail the app deploy.
_rest=""
for _svc in $(compose config --services); do
  if is_deferred "$_svc"; then
    continue
  fi
  _rest="$_rest $_svc"
done
echo "==> [tier 2/3] Observability/remainder bring-up (non-gating):$_rest"
# shellcheck disable=SC2086
compose up -d --remove-orphans $_rest \
  || echo "::warning::tier 2 (observability/remainder) bring-up returned non-zero — NOT failing the app deploy (deploy#429)."

# ── tier 3: pipeline — NON-gating ─────────────────────────────────────────────
# Kafka + dependents, brought up LAST and in isolation. A broker crash-loop here
# is reported but must NEVER down the reverse proxy / fail the app deploy.
if [ -n "$DEFERRED_SERVICES" ]; then
  echo "==> [tier 3/3] Pipeline bring-up (non-gating): $DEFERRED_SERVICES"
  # shellcheck disable=SC2086
  compose up -d $DEFERRED_SERVICES \
    || echo "::warning::tier 3 (pipeline) bring-up returned non-zero — NOT failing the app deploy (deploy#429). A broker hiccup must never down the edge."
fi

echo "==> Tiered bring-up complete."

#!/usr/bin/env bash
# Single-command integration test runner.
#
# Modes (set via RUN_MODE; default `hermetic`):
#
#   hermetic  — Full local docker-compose stack, builds user-service and
#               isnad-graph-api from sibling checkouts, seeds DB+Redis
#               directly. PR-CI default; what `integration-tests.yml` runs.
#
#   remote    — Skip stack-up. Point HTTP fixtures at env-supplied stg URLs
#               (ISNAD_BASE_URL, USER_SERVICE_BASE_URL). DB/Redis-direct
#               fixtures auto-skip in remote (no direct stg DB access from
#               outside the VPS), so the effective remote-runnable set today
#               is health + JWKS. Per-test refactors to use stg test-user
#               creds via secrets are downstream follow-up work — see #178.
#
# Usage:
#   ./run-tests.sh [pytest args]
#
# Env (hermetic):
#   KEEP_STACK=1  — leave stack up after tests for debugging.
#
# Env (remote — required):
#   ISNAD_BASE_URL          — e.g. https://isnad.stg.noorinalabs.com
#   USER_SERVICE_BASE_URL   — e.g. https://auth.stg.noorinalabs.com
#
# Env (remote — guardrail):
#   ENVIRONMENT             — must NOT equal "prod"; refused at startup.
#                             Defaults to "stg" if unset.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

RUN_MODE="${RUN_MODE:-hermetic}"

case "$RUN_MODE" in
    hermetic|remote) ;;
    *)
        echo "ERROR: RUN_MODE must be one of: hermetic, remote (got: $RUN_MODE)" >&2
        exit 2
        ;;
esac

if [[ "$RUN_MODE" == "remote" ]]; then
    # Hard refusal: never run integration tests against prod, even by accident.
    # The suite seeds and mutates state via /auth endpoints; running against
    # prod could create test users in the live user-postgres or trip
    # rate-limiters that page oncall.
    : "${ENVIRONMENT:=stg}"
    if [[ "$ENVIRONMENT" == "prod" || "$ENVIRONMENT" == "production" ]]; then
        echo "ERROR: refusing to run integration suite against ENVIRONMENT=$ENVIRONMENT." >&2
        echo "       Remote mode is stg-only. Unset ENVIRONMENT or set ENVIRONMENT=stg." >&2
        exit 3
    fi

    : "${ISNAD_BASE_URL:?ISNAD_BASE_URL is required in remote mode}"
    : "${USER_SERVICE_BASE_URL:?USER_SERVICE_BASE_URL is required in remote mode}"

    # Sanity-check both URLs respond before pytest spends real time. We use
    # /health on each — same probe verify-prod's smoke battery uses. Failure
    # here means the runner is fundamentally pointed at the wrong place;
    # bail before pytest emits a wall of timeouts.
    echo "--- Probing remote URLs ---"
    echo "  USER_SERVICE_BASE_URL=$USER_SERVICE_BASE_URL"
    echo "  ISNAD_BASE_URL=$ISNAD_BASE_URL"
    echo "  ENVIRONMENT=$ENVIRONMENT"

    mkdir -p reports

    echo "--- Building runner image ---"
    # docker compose `build` reads docker-compose.test.yml's test-runner
    # service and produces the image without bringing up depends_on (we
    # don't `up` anything in remote mode). The image gets a stable repo
    # tag we can `docker run` directly afterward without resurrecting
    # any compose state.
    docker build -f Dockerfile.runner -t noorinalabs-integration-test-runner:remote .

    echo "--- Running integration tests (remote mode) ---"
    # Run pytest in the same image we use hermetically, but pass URLs +
    # RUN_MODE through env. Mount tests + reports just like compose does.
    # No --network arg — runner container reaches the public stg URLs over
    # the host's default bridge.
    set +e
    docker run --rm \
        -e RUN_MODE=remote \
        -e ENVIRONMENT="$ENVIRONMENT" \
        -e ISNAD_BASE_URL="$ISNAD_BASE_URL" \
        -e USER_SERVICE_BASE_URL="$USER_SERVICE_BASE_URL" \
        -e USER_SERVICE_URL="$USER_SERVICE_BASE_URL" \
        -e ISNAD_GRAPH_URL="$ISNAD_BASE_URL" \
        -v "$SCRIPT_DIR/tests:/app/tests:ro" \
        -v "$SCRIPT_DIR/reports:/app/reports" \
        noorinalabs-integration-test-runner:remote \
        pytest -v --tb=short --junit-xml=/app/reports/junit.xml "$@"
    ec=$?
    set -e
    exit "$ec"
fi

# ─────────────────────────────────────────────────────────────────
# Hermetic path (default; unchanged behavior).
# ─────────────────────────────────────────────────────────────────

COMPOSE="docker compose -f docker-compose.test.yml --env-file .env.test"

dump_logs() {
    echo "--- Container logs (on failure) ---"
    $COMPOSE logs --no-color --tail=200 || true
}

cleanup() {
    ec=$?
    if [[ "$ec" -ne 0 ]]; then
        dump_logs
    fi
    if [[ "${KEEP_STACK:-0}" != "1" ]]; then
        echo "--- Tearing down test stack ---"
        $COMPOSE down -v --remove-orphans || true
    else
        echo "--- KEEP_STACK=1 — leaving stack up. Run '$COMPOSE down -v' to clean up. ---"
    fi
    exit "$ec"
}
trap cleanup EXIT

# Generate fresh JWT keys + TOTP encryption key for this run.
# The keys are written to files (not a .env file) because multi-line PEM
# values don't round-trip through dotenv format cleanly. We read the files
# with real newlines via $(cat ...) below.
echo "--- Generating fresh test secrets ---"
./scripts/generate_test_secrets.sh secrets

# Declare-then-export so `cat` exit codes surface as the script's exit
# (export's always-zero return would mask a missing secret file). #284 / SC2155.
JWT_PRIVATE_KEY=$(cat secrets/jwt.key)
JWT_PUBLIC_KEY=$(cat secrets/jwt.pub)
TOTP_ENCRYPTION_KEY=$(cat secrets/totp.key)
export JWT_PRIVATE_KEY JWT_PUBLIC_KEY TOTP_ENCRYPTION_KEY

mkdir -p reports

# Pull images with per-image retry-with-backoff (closes #214).
# `docker compose up -d --build` pulls images implicitly, but a single-image
# Docker Hub registry timeout (e.g., neo4j:5-community on 2026-05-01 P3W1
# wave-merge run 25196318721) hard-fails the entire run with no retry.
# Pulling explicitly first with bounded retry absorbs single-image transient
# Docker Hub jitter without changing the test suite or compose layout.
echo "--- Pulling images (with retry-with-backoff) ---"
pull_with_retry() {
    local image="$1"
    local delay=1
    for attempt in 1 2 3; do
        if docker pull "$image" >/dev/null 2>&1; then
            return 0
        fi
        if [[ $attempt -lt 3 ]]; then
            echo "  retry $attempt failed for $image — backing off ${delay}s" >&2
            sleep "$delay"
            delay=$((delay * 2))
        fi
    done
    echo "  ERROR: docker pull $image failed after 3 attempts" >&2
    return 1
}
# Identify pull targets via structured compose-config parse: a pull target
# is a service with `image:` and NO `build:`. Services with `build:` are
# locally built by the `up --build` step below — pulling them would hit
# Docker Hub for a non-existent tag.
#
# Why not `docker compose config --images` + a glob filter? Compose emits
# short-form Docker Hub references (`neo4j:5-community`, `postgres:16-alpine`)
# without a registry-path slash; a `*/*` filter silently skips them.
# JSON-parse-by-shape avoids that whole class of false-positive miss.
mapfile -t PULL_IMAGES < <(
    $COMPOSE config --format json 2>/dev/null | python3 -c "
import json, sys
config = json.load(sys.stdin)
seen = set()
for name, svc in config.get('services', {}).items():
    if 'image' in svc and 'build' not in svc:
        img = svc['image']
        if img not in seen:
            seen.add(img)
            print(img)
"
)
for image in "${PULL_IMAGES[@]}"; do
    [[ -z "$image" ]] && continue
    echo "  pull: $image"
    pull_with_retry "$image"
done

echo "--- Building and starting stack ---"
$COMPOSE up -d --build \
    user-postgres user-redis user-service \
    neo4j isnad-postgres isnad-redis isnad-graph-api

echo "--- Waiting for services to be healthy ---"
# shellcheck disable=SC2016 # The single-quoted body is intentionally a
# self-contained script passed to `timeout 180 bash -c '...'`. All $VAR
# references inside the body (unhealthy, line, d) are LOCAL to that
# subshell and MUST NOT expand in the parent shell. Switching to double
# quotes would interpolate `$unhealthy` at script-load time as empty,
# silently breaking the health-poll loop.
timeout 180 bash -c '
    while :; do
        unhealthy=$(docker compose -f docker-compose.test.yml --env-file .env.test ps --format json \
            | python3 -c "
import json, sys
bad = []
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    d = json.loads(line)
    if d.get(\"Health\") not in (\"healthy\", \"\"):
        bad.append(d[\"Service\"])
print(\" \".join(bad))
")
        if [[ -z "$unhealthy" ]]; then
            echo "All services healthy."
            break
        fi
        echo "  still waiting on: $unhealthy"
        sleep 3
    done
'

echo "--- Running network isolation probe ---"
# The isolation invariant is "isnad-graph-api cannot reach user-postgres".
# We probe from INSIDE isnad-graph-api (the container whose isolation we care
# about) and pass the boolean result into the test-runner as an env var.
if $COMPOSE exec -T isnad-graph-api python3 -c \
    "import socket; socket.gethostbyname('user-postgres')" >/dev/null 2>&1; then
    export ISOLATION_CHECK_RESULT="fail"
    echo "  WARNING: isnad-graph-api resolved user-postgres — isolation broken."
else
    export ISOLATION_CHECK_RESULT="pass"
    echo "  pass — isnad-graph-api cannot resolve user-postgres."
fi

echo "--- Running integration tests ---"
$COMPOSE run --rm --build \
    -e ISOLATION_CHECK_RESULT="$ISOLATION_CHECK_RESULT" \
    test-runner pytest -v --tb=short --junit-xml=/app/reports/junit.xml "$@"

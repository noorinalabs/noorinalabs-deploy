#!/usr/bin/env bash
# Single-command integration test runner.
# Usage: ./run-tests.sh [pytest args]
# Env:
#   KEEP_STACK=1  — leave stack up after tests for debugging.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

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

export JWT_PRIVATE_KEY="$(cat secrets/jwt.key)"
export JWT_PUBLIC_KEY="$(cat secrets/jwt.pub)"
export TOTP_ENCRYPTION_KEY="$(cat secrets/totp.key)"

mkdir -p reports

echo "--- Building and starting stack ---"
$COMPOSE up -d --build \
    user-postgres user-redis user-service \
    neo4j isnad-postgres isnad-redis isnad-graph-api

echo "--- Waiting for services to be healthy ---"
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

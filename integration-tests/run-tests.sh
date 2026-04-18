#!/usr/bin/env bash
# Single-command integration test runner.
# Usage: ./run-tests.sh [pytest args]
# Env:
#   KEEP_STACK=1  — leave stack up after tests for debugging.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

COMPOSE="docker compose -f docker-compose.test.yml --env-file .env.test"

cleanup() {
    if [[ "${KEEP_STACK:-0}" != "1" ]]; then
        echo "--- Tearing down test stack ---"
        $COMPOSE down -v --remove-orphans || true
    else
        echo "--- KEEP_STACK=1 — leaving stack up. Run '$COMPOSE down -v' to clean up. ---"
    fi
}
trap cleanup EXIT

# Generate fresh JWT keys + TOTP encryption key for this run.
echo "--- Generating fresh test secrets ---"
./scripts/generate_test_secrets.sh > .env.test.secrets
# shellcheck disable=SC1091
set -a
source .env.test
source .env.test.secrets
set +a

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

echo "--- Running integration tests ---"
$COMPOSE run --rm --build test-runner pytest -v --tb=short --junit-xml=/app/reports/junit.xml "$@"

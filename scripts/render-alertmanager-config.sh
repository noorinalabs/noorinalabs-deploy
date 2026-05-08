#!/usr/bin/env bash
# Renders infra/alertmanager/alertmanager.yml from the .tmpl source.
# Alertmanager does not expand env vars at startup (#127), so we pre-render
# the template with real secrets before the compose stack starts.
#
# Required env vars (set from GH Actions secrets in deploy workflows):
#   ALERTMANAGER_SLACK_WEBHOOK_URL      — Slack incoming webhook (warning channel)
#   ALERTMANAGER_PAGERDUTY_INTEGRATION_KEY — PagerDuty Events API v2 integration key
#
# Usage: scripts/render-alertmanager-config.sh [REPO_ROOT]
#   REPO_ROOT defaults to the directory containing this script's parent.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${1:-"$(cd "$SCRIPT_DIR/.." && pwd)"}"
TMPL="$REPO_ROOT/infra/alertmanager/alertmanager.yml.tmpl"
OUT="$REPO_ROOT/infra/alertmanager/alertmanager.yml"

: "${ALERTMANAGER_SLACK_WEBHOOK_URL:?ALERTMANAGER_SLACK_WEBHOOK_URL must be set}"
: "${ALERTMANAGER_PAGERDUTY_INTEGRATION_KEY:?ALERTMANAGER_PAGERDUTY_INTEGRATION_KEY must be set}"

# Single-quoting is correct: envsubst needs literal variable names; double-quoting
# would shell-expand them to empty values before envsubst sees the argument.
# shellcheck disable=SC2016
envsubst '$ALERTMANAGER_SLACK_WEBHOOK_URL $ALERTMANAGER_PAGERDUTY_INTEGRATION_KEY' \
  < "$TMPL" > "$OUT"

echo "Rendered $TMPL -> $OUT"

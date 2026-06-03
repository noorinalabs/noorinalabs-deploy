#!/usr/bin/env bash
# verify_prod_smoke.sh — Prod post-deploy smoke battery (<60s budget).
#
# Runs a tight set of route-reachability checks against the prod URLs.
# By design this script does NOT mint real auth tokens (no test creds in
# prod, per Bereket sign-off in deploy#87). The full login+refresh flow
# is exercised by the integration suite pre-deploy (see verify-stg).
#
# Checks:
#   1. isnad-graph /health    → HTTP 200, JSON .status in {healthy,degraded,ok}
#   2. user-service /health   → HTTP 200 (USER_SERVICE_BASE_URL/health,
#                                  direct on the carved-out users.* vhost — #245/#414)
#   3. landing /              → HTTP 200
#   4. /api/v1/narrators?limit=1 → HTTP 401 + JSON-shaped body (proves
#                                  user-service auth is in the path, not
#                                  Caddy bypass returning plaintext)
#   5. /.well-known/jwks.json → HTTP 200 + valid JWKS shape (.keys[]),
#                                  on USER_SERVICE_BASE_URL (users.* vhost — #245/#414)
#   6. /auth/oauth/google/login → HTTP 200 + JSON .authorization_url
#                                  pointing at accounts.google.com
#                                  (proves user-service OAuth wiring
#                                  alive: env client_id, PKCE generator,
#                                  Redis state-store all reachable).
#                                  Hit on USER_SERVICE_BASE_URL directly
#                                  for honest attribution — see #256.
#
# Env (post-#245-carve-out topology): frontend + isnad-graph API live on
# `isnad.noorinalabs.com`; the pure user-service API surface (health, JWKS,
# /auth/*, /api/v1/users…) lives on `users.noorinalabs.com`. The #245
# carve-out (merged e321e9a7) retired the user-service bindings on the
# isnad vhost, so the user-service checks (2 + 5, and 6) all target
# USER_SERVICE_BASE_URL directly — see deploy#414:
#   ISNAD_BASE_URL          (default: https://isnad.noorinalabs.com)
#   USER_SERVICE_BASE_URL   (default: https://users.noorinalabs.com —
#                            canonical pure user-service API surface)
#   LANDING_BASE_URL        (default: https://noorinalabs.com)
#   SMOKE_REPORT            Path to write GH-summary-formatted markdown
#                           (default: smoke-report.md)
#   TIMEOUT                 Per-check curl timeout, seconds (default: 5)

set -euo pipefail

ISNAD_BASE_URL="${ISNAD_BASE_URL:-https://isnad.noorinalabs.com}"
USER_SERVICE_BASE_URL="${USER_SERVICE_BASE_URL:-https://users.noorinalabs.com}"
LANDING_BASE_URL="${LANDING_BASE_URL:-https://noorinalabs.com}"
SMOKE_REPORT="${SMOKE_REPORT:-smoke-report.md}"
TIMEOUT="${TIMEOUT:-5}"

# Result tracking ----------------------------------------------------
PASS=0
FAIL=0
ROWS=()           # markdown table rows
START_NS="$(date +%s%N)"

record() {
  # record <name> <pass|fail> <time_ms> <detail>
  local name="$1" status="$2" time_ms="$3" detail="$4"
  if [ "$status" = "pass" ]; then
    PASS=$((PASS + 1))
    echo "  PASS [${time_ms}ms]: $name — $detail"
  else
    FAIL=$((FAIL + 1))
    echo "  FAIL [${time_ms}ms]: $name — $detail"
  fi
  ROWS+=("| $name | ${status^^} | ${time_ms}ms | $detail |")
}

SMOKE_BODY="/tmp/smoke_body.$$"
trap 'rm -f "$SMOKE_BODY"' EXIT

# http_check <url> → "<http_code> <time_total_seconds>" on stdout,
# response body written to $SMOKE_BODY.
http_check() {
  curl -sS -o "$SMOKE_BODY" \
       -w '%{http_code} %{time_total}' \
       --max-time "$TIMEOUT" \
       "$1" 2>/dev/null || echo "000 0"
}

read_body() {
  cat "$SMOKE_BODY" 2>/dev/null || echo ""
}

ms_from_secs() {
  awk -v t="$1" 'BEGIN { printf("%d", t * 1000) }'
}

# --- 1. isnad-graph /health -----------------------------------------
{
  read -r code secs <<<"$(http_check "${ISNAD_BASE_URL}/health")"
  body="$(read_body)"
  ms="$(ms_from_secs "$secs")"
  if [ "$code" = "200" ]; then
    if echo "$body" | jq -e '.status' >/dev/null 2>&1; then
      st="$(echo "$body" | jq -r '.status')"
      case "$st" in
        healthy|degraded|ok) record "isnad /health" pass "$ms" "status=$st" ;;
        *)                   record "isnad /health" fail "$ms" "unexpected status=$st" ;;
      esac
    else
      # Caddy could in principle return 200 with non-JSON; treat as fail
      # because the API is supposed to answer here.
      record "isnad /health" fail "$ms" "HTTP 200 but body is not JSON-shaped"
    fi
  else
    record "isnad /health" fail "$ms" "HTTP $code (expected 200)"
  fi
}

# --- 2. user-service /health ----------------------------------------
# Post-#245 carve-out: user-service is its own vhost (users.*); the old
# isnad-vhost rewrite (ISNAD_BASE_URL/api/v1/user-service/health → /health)
# was retired when the carve-out merged (e321e9a7), so probe the canonical
# user-service surface directly. See deploy#414.
{
  read -r code secs <<<"$(http_check "${USER_SERVICE_BASE_URL}/health")"
  ms="$(ms_from_secs "$secs")"
  if [ "$code" = "200" ]; then
    record "user-service /health" pass "$ms" "HTTP 200"
  else
    record "user-service /health" fail "$ms" "HTTP $code (expected 200)"
  fi
}

# --- 3. landing root ------------------------------------------------
{
  read -r code secs <<<"$(http_check "${LANDING_BASE_URL}/")"
  ms="$(ms_from_secs "$secs")"
  if [ "$code" = "200" ]; then
    record "landing /" pass "$ms" "HTTP 200"
  else
    record "landing /" fail "$ms" "HTTP $code (expected 200)"
  fi
}

# --- 4. narrator query (expect 401 + JSON body) ---------------------
# The JSON-shape check matters: a misconfigured Caddy returning plaintext
# "401 Unauthorized" from the proxy itself would still pass an HTTP-only
# check but means user-service is not actually responding (Bereket note).
{
  read -r code secs <<<"$(http_check "${ISNAD_BASE_URL}/api/v1/narrators?limit=1")"
  body="$(read_body)"
  ms="$(ms_from_secs "$secs")"
  if [ "$code" = "401" ] || [ "$code" = "403" ]; then
    if echo "$body" | jq -e '.detail or .error or .message' >/dev/null 2>&1; then
      record "narrator /api/v1/narrators" pass "$ms" "HTTP $code + JSON body (auth path live)"
    else
      record "narrator /api/v1/narrators" fail "$ms" "HTTP $code but body is not JSON — proxy may be answering instead of API"
    fi
  else
    record "narrator /api/v1/narrators" fail "$ms" "HTTP $code (expected 401/403 unauth)"
  fi
}

# --- 5. JWKS endpoint -----------------------------------------------
# Post-#245 carve-out: JWKS lives on the user-service vhost (users.*); the
# isnad-vhost binding was retired with the carve-out (it returned an empty
# .keys array after the move). Probe USER_SERVICE_BASE_URL directly. See
# deploy#414.
{
  read -r code secs <<<"$(http_check "${USER_SERVICE_BASE_URL}/.well-known/jwks.json")"
  body="$(read_body)"
  ms="$(ms_from_secs "$secs")"
  if [ "$code" = "200" ] && echo "$body" | jq -e '.keys | length > 0' >/dev/null 2>&1; then
    nkeys="$(echo "$body" | jq -r '.keys | length')"
    record "jwks.json" pass "$ms" "HTTP 200, $nkeys key(s)"
  elif [ "$code" = "200" ]; then
    record "jwks.json" fail "$ms" "HTTP 200 but missing/empty .keys array"
  else
    record "jwks.json" fail "$ms" "HTTP $code (expected 200)"
  fi
}

# --- 6. /auth/oauth/google/login (expect 200 + JSON authorization_url) ----
# user-service exposes GET /auth/oauth/{provider}/login (see
# user-service/src/app/routers/auth.py:269) which returns
# OAuthLoginResponse — a JSON body with `authorization_url` pointing at
# the OAuth provider. It is NOT a 302 redirect; the SPA reads the JSON
# and navigates the browser. A 200 + .authorization_url containing the
# expected provider domain proves the wiring (provider client_id env
# present, PKCE generation working, Redis state-store reachable).
#
# We hit ${USER_SERVICE_BASE_URL} directly rather than ${ISNAD_BASE_URL}
# so attribution is honest (deploy#256 caveat 4): /auth/* is dual-bound
# on isnad.* + users.* during the absolute-URL frontend cutover (#245
# phase 2), but the canonical user-service vhost is users.*.
{
  read -r code secs <<<"$(http_check "${USER_SERVICE_BASE_URL}/auth/oauth/google/login")"
  body="$(read_body)"
  ms="$(ms_from_secs "$secs")"
  if [ "$code" = "200" ]; then
    if echo "$body" | jq -e '.authorization_url' >/dev/null 2>&1; then
      auth_url="$(echo "$body" | jq -r '.authorization_url')"
      if echo "$auth_url" | grep -qiE 'accounts\.google\.com|google\.com/o/oauth'; then
        host="$(echo "$auth_url" | sed -E 's|^https?://([^/]+).*|\1|')"
        record "/auth/oauth/google/login wiring" pass "$ms" "HTTP 200 + .authorization_url → $host"
      else
        host="$(echo "$auth_url" | sed -E 's|^https?://([^/]+).*|\1|')"
        record "/auth/oauth/google/login wiring" fail "$ms" "HTTP 200 but .authorization_url host=$host (expected accounts.google.com)"
      fi
    else
      record "/auth/oauth/google/login wiring" fail "$ms" "HTTP 200 but body missing .authorization_url"
    fi
  else
    record "/auth/oauth/google/login wiring" fail "$ms" "HTTP $code (expected 200)"
  fi
}

# --- Summary --------------------------------------------------------
END_NS="$(date +%s%N)"
TOTAL_MS=$(( (END_NS - START_NS) / 1000000 ))
TOTAL=$((PASS + FAIL))

echo ""
echo "==> Prod smoke summary: $PASS/$TOTAL passed, total ${TOTAL_MS}ms"

# Markdown report for $GITHUB_STEP_SUMMARY
{
  echo "## Prod Smoke Results"
  echo ""
  echo "| Check | Result | Time | Detail |"
  echo "|-------|--------|------|--------|"
  for row in "${ROWS[@]}"; do
    echo "$row"
  done
  echo ""
  echo "**Totals:** ${PASS}/${TOTAL} passed — total runtime **${TOTAL_MS}ms** (budget: <60000ms)"
  if [ "$TOTAL_MS" -gt 60000 ]; then
    echo ""
    echo "> WARNING: total runtime exceeded the 60s budget defined in deploy#87."
  fi
} > "$SMOKE_REPORT"

if [ "$FAIL" -gt 0 ]; then
  echo "RESULT: FAILED — $FAIL/$TOTAL checks did not pass"
  exit 1
fi

echo "RESULT: ALL SMOKE CHECKS PASSED"
exit 0

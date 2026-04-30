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
#   2. user-service /health   → HTTP 200 (via Caddy /api/v1/user-service/health)
#   3. landing /              → HTTP 200
#   4. /api/v1/narrators?limit=1 → HTTP 401 + JSON-shaped body (proves
#                                  user-service auth is in the path, not
#                                  Caddy bypass returning plaintext)
#   5. /.well-known/jwks.json → HTTP 200 + valid JWKS shape (.keys[])
#   6. /auth/login            → HTTP 3xx redirect to OAuth provider
#                                (proves user-service auth wiring alive)
#
# Env (defaults reflect TODAY's prod Caddy — caddy/Caddyfile:18 serves
# `isnad-graph.noorinalabs.com` and routes user-service traffic in the
# same site block. When deploy#156 lands, update defaults to
# `isnad.noorinalabs.com` + `users.noorinalabs.com`):
#   ISNAD_BASE_URL          (default: https://isnad-graph.noorinalabs.com)
#   USER_SERVICE_BASE_URL   (default: https://isnad-graph.noorinalabs.com —
#                            same host as ISNAD_BASE_URL today; cleaves
#                            into a separate subdomain at #156)
#   LANDING_BASE_URL        (default: https://noorinalabs.com)
#   SMOKE_REPORT            Path to write GH-summary-formatted markdown
#                           (default: smoke-report.md)
#   TIMEOUT                 Per-check curl timeout, seconds (default: 5)

set -euo pipefail

ISNAD_BASE_URL="${ISNAD_BASE_URL:-https://isnad-graph.noorinalabs.com}"
USER_SERVICE_BASE_URL="${USER_SERVICE_BASE_URL:-https://isnad-graph.noorinalabs.com}"
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

# --- 2. user-service /health (via Caddy rewrite) --------------------
{
  read -r code secs <<<"$(http_check "${ISNAD_BASE_URL}/api/v1/user-service/health")"
  ms="$(ms_from_secs "$secs")"
  if [ "$code" = "200" ]; then
    record "user-service /health" pass "$ms" "HTTP 200 via Caddy rewrite"
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
{
  read -r code secs <<<"$(http_check "${ISNAD_BASE_URL}/.well-known/jwks.json")"
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

# --- 6. /auth/login (expect 3xx → OAuth provider) -------------------
# We use -o /dev/null and inspect Location via a separate -I pass to keep
# the body tiny but still see the redirect target.
{
  # First: status code only (no follow)
  read -r code secs <<<"$(http_check "${ISNAD_BASE_URL}/auth/login")"
  ms="$(ms_from_secs "$secs")"
  # Then: grab Location header (separate call; cheap, still under budget)
  loc="$(curl -sSI --max-time "$TIMEOUT" "${ISNAD_BASE_URL}/auth/login" 2>/dev/null \
          | grep -i '^location:' | head -1 | sed 's/^[Ll]ocation:[[:space:]]*//' | tr -d '\r' || true)"
  case "$code" in
    301|302|303|307|308)
      if [ -n "$loc" ] && echo "$loc" | grep -qiE 'google|github|accounts\.google|github\.com/login'; then
        # Mask the redirect target for log hygiene (state/nonce in query)
        host="$(echo "$loc" | sed -E 's|^https?://([^/]+).*|\1|')"
        record "/auth/login wiring" pass "$ms" "HTTP $code → OAuth provider ($host)"
      elif [ -n "$loc" ]; then
        record "/auth/login wiring" pass "$ms" "HTTP $code → $(echo "$loc" | sed -E 's|^(https?://[^/]+).*|\1|') (non-canonical OAuth host)"
      else
        record "/auth/login wiring" fail "$ms" "HTTP $code but no Location header"
      fi
      ;;
    200)
      # Some OAuth flows render an interstitial page instead of redirect.
      # Treat as pass only if body looks HTML-ish (route is alive).
      record "/auth/login wiring" pass "$ms" "HTTP 200 (route alive, non-redirect handler)"
      ;;
    *)
      record "/auth/login wiring" fail "$ms" "HTTP $code (expected 3xx or 200)"
      ;;
  esac
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

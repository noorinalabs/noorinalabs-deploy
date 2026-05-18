#!/usr/bin/env bash
# verify_deployment.sh — Generic deployment verification (legacy / manual).
# Checks deploy workflow, live site, API health, security headers, and SSL.
#
# Note (deploy#87): Post-deploy verification in CI is now split by env:
#   - Stg verify (full integration suite) lives in `verify-deploy.yml`'s
#     `verify-stg` job and runs `integration-tests/run-tests.sh`.
#   - Prod verify (smoke battery, <60s) lives in `verify-deploy.yml`'s
#     `verify-prod` job and runs `scripts/verify_prod_smoke.sh`.
# This script is retained for manual/operator use against arbitrary
# environments (used by `docs/runbooks/user-service-migration.md`) and is
# intentionally NOT invoked by `verify-deploy.yml` anymore.
#
# Usage:
#   ./scripts/verify_deployment.sh [--site=URL] [--landing=URL]
#                                  [--user-service=URL] [--skip-workflow]
#                                  [--skip-ssl]
#
# Environment:
#   SITE_URL              Override the default API host URL
#                         (default: https://isnad.noorinalabs.com)
#   LANDING_URL           Landing-page URL to check. Empty disables the
#                         landing reachability check.
#                         (default: https://noorinalabs.com)
#   USER_SERVICE_URL      Base URL whose `/health` is hit for the
#                         user-service auth-plane check. Empty disables.
#                         (default: $SITE_URL — auth-plane routes are
#                         dual-bound on isnad.* and users.* during the
#                         #245 absolute-URL frontend cutover; pass
#                         USER_SERVICE_URL=https://users.noorinalabs.com
#                         to hit the pure user-service surface directly.)
#   GH_REPO               Override the GitHub repo for workflow checks
#                         (default: noorinalabs/noorinalabs-deploy)
#   ROLLBACK_TAG          If set, tag the current deployment for rollback
#                         reference

set -euo pipefail

# ---------- Configuration ----------

SITE_URL="${SITE_URL:-https://isnad.noorinalabs.com}"
# Landing-page reachability check (deploy#73). Empty = skip.
LANDING_URL="${LANDING_URL:-https://noorinalabs.com}"
# user-service auth-plane health (deploy#73). Empty = skip. Defaults to
# SITE_URL because the auth-plane routes are currently dual-bound on
# isnad.* and users.* during the #245 absolute-URL frontend cutover, so
# /api/v1/user-service/health resolves via either. Operators wanting to
# hit the pure user-service surface pass
# USER_SERVICE_URL=https://users.noorinalabs.com explicitly.
USER_SERVICE_URL="${USER_SERVICE_URL:-$SITE_URL}"
GH_REPO="${GH_REPO:-noorinalabs/noorinalabs-deploy}"
SKIP_WORKFLOW="${SKIP_WORKFLOW:-false}"
SKIP_SSL="${SKIP_SSL:-false}"
ROLLBACK_TAG="${ROLLBACK_TAG:-}"
TIMEOUT=10

# Parse CLI args
for arg in "$@"; do
  case "$arg" in
    --site=*) SITE_URL="${arg#*=}" ;;
    --landing=*) LANDING_URL="${arg#*=}" ;;
    --user-service=*) USER_SERVICE_URL="${arg#*=}" ;;
    --skip-workflow) SKIP_WORKFLOW=true ;;
    --skip-ssl) SKIP_SSL=true ;;
    --help|-h)
      # Print the leading comment block (Usage + Environment sections).
      # Stops at the first non-comment, non-blank line.
      awk '/^#!/ {next} /^#/ {print substr($0,3); next} {exit}' "$0"
      exit 0
      ;;
  esac
done

# ---------- Helpers ----------

PASS=0
FAIL=0
WARN=0

pass() { PASS=$((PASS + 1)); echo "  PASS: $1"; }
fail() { FAIL=$((FAIL + 1)); echo "  FAIL: $1"; }
warn() { WARN=$((WARN + 1)); echo "  WARN: $1"; }
section() { echo ""; echo "==> $1"; }

# ---------- 1. Deploy Workflow Status ----------

if [ "$SKIP_WORKFLOW" = "false" ]; then
  section "Deploy Workflow Status"
  if ! command -v gh &>/dev/null; then
    warn "gh CLI not installed — skipping workflow check"
  else
    latest=$(gh run list --repo "$GH_REPO" --workflow=deploy.yml --limit 1 --json status,conclusion,headSha,createdAt 2>/dev/null || echo "")
    if [ -z "$latest" ] || [ "$latest" = "[]" ]; then
      warn "No deploy workflow runs found"
    else
      conclusion=$(echo "$latest" | jq -r '.[0].conclusion // "in_progress"')
      status=$(echo "$latest" | jq -r '.[0].status')
      sha=$(echo "$latest" | jq -r '.[0].headSha' | cut -c1-8)
      created=$(echo "$latest" | jq -r '.[0].createdAt')
      if [ "$conclusion" = "success" ]; then
        pass "Latest deploy succeeded (sha=$sha, at=$created)"
      elif [ "$status" = "in_progress" ]; then
        warn "Deploy still in progress (sha=$sha)"
      else
        fail "Latest deploy conclusion=$conclusion (sha=$sha, at=$created)"
      fi
    fi
  fi
fi

# ---------- 2. Live Site HTTP 200 ----------

section "Live Site Reachability"
http_code=$(curl -s -o /dev/null -w "%{http_code}" --max-time "$TIMEOUT" "$SITE_URL" 2>/dev/null || echo "000")
if [ "$http_code" = "200" ]; then
  pass "Site returns HTTP 200 at $SITE_URL"
else
  fail "Site returned HTTP $http_code at $SITE_URL"
fi

# ---------- 3. API Health Endpoint ----------

section "API Health Check"
health_resolved=false
# Try /health first (Caddy direct), then /api/v1/health as fallback
for health_path in "/health" "/api/v1/health"; do
  health_url="${SITE_URL}${health_path}"
  health_resp=$(curl -s --max-time "$TIMEOUT" "$health_url" 2>/dev/null || echo "")
  if echo "$health_resp" | jq -e '.status' &>/dev/null; then
    health_status=$(echo "$health_resp" | jq -r '.status')
    if [ "$health_status" = "healthy" ] || [ "$health_status" = "degraded" ] || [ "$health_status" = "ok" ]; then
      pass "Health endpoint (${health_path}) reports status=$health_status"
    else
      fail "Health endpoint (${health_path}) status=$health_status"
    fi
    health_resolved=true
    break
  fi
done
# If neither endpoint returned parseable JSON, fall back to HTTP status code
if [ "$health_resolved" = "false" ]; then
  health_code=$(curl -s -o /dev/null -w "%{http_code}" --max-time "$TIMEOUT" "${SITE_URL}/health" 2>/dev/null || echo "000")
  if [ "$health_code" = "200" ]; then
    pass "Health endpoint returned HTTP 200 (non-JSON response)"
  else
    fail "Health endpoint unreachable or invalid (HTTP $health_code)"
  fi
fi

# ---------- 3a. Landing-Page Reachability (deploy#73) ----------

if [ -n "$LANDING_URL" ]; then
  section "Landing-Page Reachability"
  landing_code=$(curl -s -o /dev/null -w "%{http_code}" --max-time "$TIMEOUT" "$LANDING_URL" 2>/dev/null || echo "000")
  if [ "$landing_code" = "200" ]; then
    pass "Landing returns HTTP 200 at $LANDING_URL"
  elif [ "$landing_code" = "000" ]; then
    fail "Landing unreachable (timeout/connection error) at $LANDING_URL"
  else
    fail "Landing returned HTTP $landing_code at $LANDING_URL"
  fi
fi

# ---------- 3b. User-Service Health (deploy#73) ----------

# Two routes exist depending on the deploy#156 cutover state:
#   1. /api/v1/user-service/health — Caddy rewrite at caddy/Caddyfile:88-89
#      that proxies to user-service:8000. Used today while user-service
#      traffic shares the API host with isnad-graph.
#   2. /health — direct path on the user-service subdomain after the #156
#      cutover lands `users.noorinalabs.com` as a separate site block.
#
# IMPORTANT: pre-#156, /health on the SHARED host hits **isnad-graph's**
# /health (caddy/Caddyfile:101 — `handle /health { reverse_proxy api:8000 }`),
# NOT user-service. So the second-route fallback is only correct when an
# explicit subdomain URL was passed via --user-service= or the
# USER_SERVICE_URL env var; if USER_SERVICE_URL == SITE_URL, that fallback
# would falsely report user-service healthy when only isnad-graph is up
# (Aisha review on PR #206, hot spot 4 — flagged 2026-04-30).
if [ -n "$USER_SERVICE_URL" ]; then
  section "User-Service Health Check"
  us_resolved=false
  for us_path in "/api/v1/user-service/health" "/health"; do
    # Skip the /health fallback on the shared host — it routes to
    # isnad-graph, not user-service. Only safe to probe /health when an
    # explicit user-service subdomain (post-#156) was passed.
    if [ "$us_path" = "/health" ] && [ "$USER_SERVICE_URL" = "$SITE_URL" ]; then
      continue
    fi
    us_url="${USER_SERVICE_URL}${us_path}"
    us_code=$(curl -s -o /dev/null -w "%{http_code}" --max-time "$TIMEOUT" "$us_url" 2>/dev/null || echo "000")
    if [ "$us_code" = "200" ]; then
      pass "User-service health endpoint (${us_path}) returned HTTP 200"
      us_resolved=true
      break
    fi
  done
  if [ "$us_resolved" = "false" ]; then
    if [ "$USER_SERVICE_URL" = "$SITE_URL" ]; then
      fail "User-service health unreachable at ${USER_SERVICE_URL}/api/v1/user-service/health (Caddy rewrite path; the /health fallback only applies post-#156 with a separate user-service subdomain)"
    else
      fail "User-service health unreachable at $USER_SERVICE_URL (tried /api/v1/user-service/health and /health)"
    fi
  fi
fi

# ---------- 4. API Status Endpoint (detailed) ----------

section "API Status (detailed service health)"
status_url="${SITE_URL}/status"
status_resp=$(curl -s --max-time "$TIMEOUT" "$status_url" 2>/dev/null || echo "")
if echo "$status_resp" | jq -e '.status' &>/dev/null; then
  pub_status=$(echo "$status_resp" | jq -r '.status')
  pub_message=$(echo "$status_resp" | jq -r '.message // ""')
  if [ "$pub_status" = "operational" ]; then
    pass "Status endpoint reports operational"
  else
    warn "Status endpoint reports $pub_status: $pub_message"
  fi
else
  warn "Status endpoint not available or unexpected format"
fi

# ---------- 5. Key API Endpoints (smoke test — expect 401 without auth) ----------

section "API Endpoint Smoke Tests"
endpoints=("/api/v1/narrators" "/api/v1/hadiths" "/api/v1/collections" "/api/v1/search" "/api/v1/parallels" "/api/v1/timeline")
for ep in "${endpoints[@]}"; do
  ep_code=$(curl -s -o /dev/null -w "%{http_code}" --max-time "$TIMEOUT" "${SITE_URL}${ep}" 2>/dev/null || echo "000")
  if [ "$ep_code" = "401" ] || [ "$ep_code" = "403" ] || [ "$ep_code" = "200" ]; then
    pass "Endpoint $ep reachable (HTTP $ep_code)"
  elif [ "$ep_code" = "000" ]; then
    fail "Endpoint $ep unreachable (timeout/connection error)"
  else
    warn "Endpoint $ep returned HTTP $ep_code"
  fi
done

# ---------- 6. Security Headers ----------

section "Security Headers"
# Check headers on both the main site (Caddy) and API endpoint to get the full set
headers=$(curl -s -D - -o /dev/null --max-time "$TIMEOUT" "$SITE_URL" 2>/dev/null || echo "")
api_headers=$(curl -s -D - -o /dev/null --max-time "$TIMEOUT" "${SITE_URL}/health" 2>/dev/null || echo "")
# Merge both sets of headers for checking
headers="${headers}
${api_headers}"
required_headers=(
  "X-Content-Type-Options"
  "X-Frame-Options"
  "Strict-Transport-Security"
  "X-XSS-Protection"
  "Referrer-Policy"
  "Content-Security-Policy"
)
for hdr in "${required_headers[@]}"; do
  if echo "$headers" | grep -qi "^${hdr}:"; then
    val=$(echo "$headers" | grep -i "^${hdr}:" | head -1 | cut -d: -f2- | xargs)
    # X-XSS-Protection: 0 is the OWASP-recommended value (disable browser XSS filter)
    if [ "$hdr" = "X-XSS-Protection" ] && [ "$val" = "0" ]; then
      pass "Header $hdr present ($val — OWASP recommended)"
    else
      pass "Header $hdr present ($val)"
    fi
  else
    fail "Header $hdr missing"
  fi
done

# ---------- 7. Caddy Config Reload Verification ----------

section "Caddy Config Verification"
# Verify that Caddy (the reverse proxy) is serving security headers on the root URL.
# This confirms that the running Caddy process has loaded the current Caddyfile,
# not a stale cached config from a previous deployment.
caddy_headers=$(curl -s -D - -o /dev/null --max-time "$TIMEOUT" "$SITE_URL" 2>/dev/null || echo "")
caddy_check_headers=("X-XSS-Protection" "Content-Security-Policy" "X-Content-Type-Options")
for chdr in "${caddy_check_headers[@]}"; do
  if echo "$caddy_headers" | grep -qi "^${chdr}:"; then
    pass "Caddy serves $chdr header"
  else
    fail "Caddy does NOT serve $chdr header — config may not be reloaded"
  fi
done
# Check for Caddy server identifier (confirms traffic flows through Caddy)
if echo "$caddy_headers" | grep -qi "^server:.*caddy"; then
  pass "Response served by Caddy (Server header present)"
elif echo "$caddy_headers" | grep -qi "^server:"; then
  server_val=$(echo "$caddy_headers" | grep -i "^server:" | head -1 | cut -d: -f2- | xargs)
  warn "Server header present but not Caddy ($server_val)"
else
  warn "No Server header — cannot confirm Caddy is the reverse proxy"
fi

# ---------- 8. SSL Certificate ----------

if [ "$SKIP_SSL" = "false" ]; then
  section "SSL Certificate"
  host=$(echo "$SITE_URL" | sed 's|https://||' | sed 's|/.*||')
  cert_info=$(echo | openssl s_client -servername "$host" -connect "${host}:443" 2>/dev/null | openssl x509 -noout -dates -subject 2>/dev/null || echo "")
  if [ -z "$cert_info" ]; then
    fail "Could not retrieve SSL certificate for $host"
  else
    not_after=$(echo "$cert_info" | grep "notAfter" | cut -d= -f2)
    if [ -n "$not_after" ]; then
      expiry_epoch=$(date -d "$not_after" +%s 2>/dev/null || date -j -f "%b %d %T %Y %Z" "$not_after" +%s 2>/dev/null || echo "0")
      now_epoch=$(date +%s)
      days_left=$(( (expiry_epoch - now_epoch) / 86400 ))
      if [ "$days_left" -gt 14 ]; then
        pass "SSL certificate valid for $days_left more days (expires: $not_after)"
      elif [ "$days_left" -gt 0 ]; then
        warn "SSL certificate expires in $days_left days (expires: $not_after)"
      else
        fail "SSL certificate expired or expiring today (expires: $not_after)"
      fi
    else
      fail "Could not parse SSL certificate expiry"
    fi

    subject=$(echo "$cert_info" | grep "subject" | head -1)
    if echo "$subject" | grep -qi "$host"; then
      pass "SSL certificate subject matches $host"
    else
      warn "SSL certificate subject may not match: $subject"
    fi
  fi
fi

# ---------- 9. Response Time Spot Check ----------

section "Response Time"
time_total=$(curl -s -o /dev/null -w "%{time_total}" --max-time "$TIMEOUT" "${SITE_URL}/health" 2>/dev/null || echo "0")
time_ms=$(echo "$time_total * 1000" | bc 2>/dev/null | cut -d. -f1 || echo "0")
if [ -n "$time_ms" ] && [ "$time_ms" -lt 500 ] 2>/dev/null; then
  pass "Health endpoint responded in ${time_ms}ms (threshold: 500ms)"
elif [ -n "$time_ms" ] && [ "$time_ms" -lt 2000 ] 2>/dev/null; then
  warn "Health endpoint responded in ${time_ms}ms (above 500ms threshold)"
else
  fail "Health endpoint response time ${time_ms}ms exceeds 2000ms"
fi

# ---------- 10. Rollback Tag ----------

if [ -n "$ROLLBACK_TAG" ]; then
  section "Rollback Tag"
  if command -v gh &>/dev/null; then
    current_sha=$(gh api "repos/$GH_REPO/commits/main" --jq '.sha' 2>/dev/null | cut -c1-8 || echo "unknown")
    echo "  INFO: Tagging current deployment as $ROLLBACK_TAG (sha=$current_sha)"
    echo "  INFO: To rollback, deploy this tag: git checkout $ROLLBACK_TAG"
  fi
fi

# ---------- 11. Routing Carve-outs (deploy#135) ----------

# Caddy evaluates `handle` blocks in source order; first match wins. Any
# path-prefix carve-out that should land on a different upstream than its
# catch-all parent MUST appear as an earlier `handle` block. This section
# exercises one representative path per known carve-out on $SITE_URL and
# asserts the response shape (status code + body content-type) is
# consistent with the intended upstream. It would have caught the deploy#133
# regression where `/auth/callback/*` matched `/auth/*` and hit user-service
# (JSON 404) instead of the frontend (React AuthCallbackPage HTML).
#
# Carve-out → expected-upstream map (mirrors caddy/Caddyfile `isnad.*` block):
#   /auth/callback/*      → frontend       (HTML, 200/404)
#   /auth/oauth/*/login   → user-service   (JSON, 302/401/404 — NOT HTML)
#   /api/v1/health        → isnad-graph    (JSON, 200/401/404)
#   /api/v1/narrators     → isnad-graph    (JSON, 200/401/403)
#   /api/v1/users         → user-service   (JSON, 401/405 — carve-out from /api/*)
#   /.well-known/jwks.json → user-service  (JSON, 200)

section "Routing Carve-outs"

# Fetch headers + body in one shot per probe; check status code AND
# content-type. Body sniff is a defense-in-depth check for the case where
# both upstreams return the "right" status code but for the wrong reason.
probe() {
  local path="$1"
  local expected_codes="$2"   # space-separated, e.g. "200 404"
  local expected_kind="$3"    # "html" or "json"
  local label="$4"

  local url="${SITE_URL}${path}"
  local body_file headers_file
  body_file=$(mktemp)
  headers_file=$(mktemp)
  local code
  code=$(curl -s -o "$body_file" -D "$headers_file" -w "%{http_code}" --max-time "$TIMEOUT" "$url" 2>/dev/null || echo "000")
  local ct
  ct=$(grep -i '^content-type:' "$headers_file" 2>/dev/null | head -1 | cut -d: -f2- | xargs || echo "")

  local code_ok=false
  for ec in $expected_codes; do
    if [ "$code" = "$ec" ]; then
      code_ok=true
      break
    fi
  done

  if [ "$code_ok" = "false" ]; then
    fail "$label: $path returned HTTP $code (expected one of: $expected_codes) — possible carve-out shadowing"
    rm -f "$body_file" "$headers_file"
    return
  fi

  # Body-shape assertion: HTML carve-out must NOT return JSON; JSON
  # carve-out must NOT return HTML. The error mode this defends against
  # is a misordered handle block routing to the wrong upstream that
  # happens to return the expected status code.
  case "$expected_kind" in
    html)
      if echo "$ct" | grep -qi 'application/json'; then
        fail "$label: $path returned JSON content-type ($ct) but should hit frontend (HTML) — carve-out likely shadowed by an earlier catch-all"
      elif head -c 200 "$body_file" | grep -qi '"detail"\|"message"\|"status_code"'; then
        # FastAPI/Starlette/user-service JSON error shape leaking through
        fail "$label: $path body looks like JSON error shape but expected HTML — likely hitting user-service instead of frontend"
      else
        pass "$label: $path → HTTP $code, content-type=${ct:-unset} (frontend-shaped)"
      fi
      ;;
    json)
      if echo "$ct" | grep -qi 'text/html'; then
        fail "$label: $path returned HTML content-type ($ct) but should hit an API upstream (JSON) — carve-out likely shadowed by frontend default"
      else
        pass "$label: $path → HTTP $code, content-type=${ct:-unset} (API-shaped)"
      fi
      ;;
  esac
  rm -f "$body_file" "$headers_file"
}

# Frontend carve-out from /auth/* catch-all. The probe path is intentionally
# non-existent on the React SPA so we don't depend on a real OAuth state;
# the React app serves index.html for any unmatched path, so 200 with HTML
# is the success shape (the SPA renders an error route client-side).
probe "/auth/callback/verify-shape-probe" "200 404" "html" "frontend /auth/callback/*"

# user-service /auth/* catch-all (no carve-out). Hitting the OAuth login
# initiator without a configured provider should give a 302 (redirect to
# provider), 401 (unauthenticated), 404 (provider not configured in this
# env), or 400 (validation). All four are valid "user-service answered"
# shapes; HTML response would indicate the request fell through to the
# frontend default handler.
probe "/auth/oauth/google/login" "200 302 400 401 404" "json" "user-service /auth/oauth/*/login"

# isnad-graph /api/v1/health — carve-out from /api/* (api:8000 — same
# upstream, but exercises the /api/* block, distinct from the top-level
# /health block tested in §3).
probe "/api/v1/health" "200 401 404" "json" "isnad-graph /api/v1/health"

# isnad-graph /api/v1/narrators — covered by §5 but classified here too
# under the carve-out lens.
probe "/api/v1/narrators" "200 401 403" "json" "isnad-graph /api/v1/narrators"

# user-service /api/v1/users — carve-out from the /api/* catch-all (which
# would otherwise send it to isnad-graph). 401/405 are valid auth-required
# shapes from user-service; HTML or 501 would indicate a routing
# regression (501 is isnad-graph's "delegated to user-service via Caddy"
# response per ontology/repos/isnad-graph.yaml:72).
probe "/api/v1/users" "401 403 405" "json" "user-service /api/v1/users (carve-out)"

# user-service JWKS endpoint — well-known IETF path, carve-out from any
# parent prefix. Should always be 200 with JSON keys array.
probe "/.well-known/jwks.json" "200" "json" "user-service /.well-known/jwks.json"

# ---------- Summary ----------

section "Summary"
total=$((PASS + FAIL + WARN))
echo "  Total checks: $total"
echo "  Passed: $PASS"
echo "  Failed: $FAIL"
echo "  Warnings: $WARN"
echo ""

if [ "$FAIL" -gt 0 ]; then
  echo "RESULT: FAILED — $FAIL check(s) did not pass"
  exit 1
elif [ "$WARN" -gt 0 ]; then
  echo "RESULT: PASSED WITH WARNINGS — $WARN warning(s)"
  exit 0
else
  echo "RESULT: ALL CHECKS PASSED"
  exit 0
fi

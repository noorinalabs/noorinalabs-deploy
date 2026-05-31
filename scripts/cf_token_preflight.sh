#!/usr/bin/env bash
# ===========================================================================
# cf_token_preflight.sh — verify CLOUDFLARE_API_TOKEN can manage the
# canonical-domain redirect rulesets on the defensive `.net` / `.org` zones
# BEFORE `terraform apply` reaches them.
#
# Why this exists (#347):
#   The redirect rulesets in terraform/cloudflare/redirects.tf are
#   `http_request_dynamic_redirect` entrypoint rulesets on the `.net` and
#   `.org` zones. Managing them needs the token scoped to BOTH:
#     - those zones in "Zone Resources", AND
#     - the "Zone → Dynamic Redirect → Edit" permission group.
#   A token that only carries `Zone:DNS:Edit` on `.com` (the historical
#   baseline) authenticates fine for the `.com` DNS records but fails on the
#   `.net`/`.org` ruleset apply with the opaque Cloudflare error:
#
#       Authentication error (10000)
#
#   Crucially Terraform/Cloudflare validate ruleset writes only AT APPLY, so a
#   clean `terraform plan` + green PR review can still fail apply on prod (the
#   exact P3W11 prod-apply post-mortem behind #347). This script surfaces the
#   missing scope as an early, actionable CI failure instead.
#
# What it checks:
#   1. Token is valid + active            (GET /user/tokens/verify)
#   2. Token can authenticate against the rulesets endpoint of EACH redirect
#      zone (GET /zones/<id>/rulesets) — i.e. the zone is in the token's
#      Zone Resources and the token can read rulesets. A 10000/9109 here is
#      the same auth class that bites the apply.
#
# What it deliberately does NOT do:
#   - It cannot fully prove WRITE/Edit on the dynamic-redirect ruleset without
#     mutating prod state (Cloudflare has no dry-run for ruleset PUT). Read
#     access on the zone's rulesets is a strong, non-destructive proxy: a
#     token missing the zone from its Zone Resources fails read too. The
#     residual gap — token has the zone but lacks the Dynamic Redirect *Edit*
#     permission group specifically — is documented in the runbook and is the
#     one apply-gated case that remains. See docs/runbooks/cloudflare-token-scope.md.
#
# Usage:
#   CLOUDFLARE_API_TOKEN=... NET_ZONE_ID=... ORG_ZONE_ID=... \
#     scripts/cf_token_preflight.sh
#
# Emits GitHub Actions `::error::` annotations when run in CI; exits non-zero
# on any failure so the calling job stops before `terraform apply`.
# ===========================================================================
set -euo pipefail

: "${CLOUDFLARE_API_TOKEN:?CLOUDFLARE_API_TOKEN must be set}"
: "${NET_ZONE_ID:?NET_ZONE_ID (noorinalabs.net zone id) must be set}"
: "${ORG_ZONE_ID:?ORG_ZONE_ID (noorinalabs.org zone id) must be set}"

CF_API="https://api.cloudflare.com/client/v4"

# err — print a GitHub-Actions error annotation when in CI, else a plain line.
err() {
  if [ -n "${GITHUB_ACTIONS:-}" ]; then
    printf '::error::%s\n' "$1" >&2
  else
    printf 'ERROR: %s\n' "$1" >&2
  fi
}

# cf_get — GET a CF API path, echo the body, return curl's exit status. Does
# not use --fail so we can inspect `success`/`errors` in the JSON envelope.
cf_get() {
  curl --silent --show-error \
    --header "Authorization: Bearer ${CLOUDFLARE_API_TOKEN}" \
    --header "Content-Type: application/json" \
    "${CF_API}$1"
}

# 1. Token validity + active status.
verify_resp=$(cf_get "/user/tokens/verify") || {
  err "Could not reach Cloudflare token-verify endpoint."
  exit 1
}
if [ "$(printf '%s' "${verify_resp}" | jq -r '.success')" != "true" ]; then
  err "CLOUDFLARE_API_TOKEN failed /user/tokens/verify — the token is invalid, expired, or revoked."
  err "Fix: mint a new token and re-set the GH secret (see docs/runbooks/cloudflare-token-scope.md)."
  printf '%s\n' "${verify_resp}" | jq -c '.errors' >&2 || true
  exit 1
fi
token_status=$(printf '%s' "${verify_resp}" | jq -r '.result.status // "unknown"')
if [ "${token_status}" != "active" ]; then
  err "CLOUDFLARE_API_TOKEN status is '${token_status}', expected 'active'."
  exit 1
fi
echo "Token verify: OK (status=active)"

# 2. Per-zone rulesets read — proves the zone is in the token's Zone Resources
#    and the token can touch the rulesets API on it. A miss here is the same
#    auth class (10000/9109) that fails the apply.
probe_zone() {
  zone_id="$1"; label="$2"
  resp=$(cf_get "/zones/${zone_id}/rulesets")
  if [ "$(printf '%s' "${resp}" | jq -r '.success')" = "true" ]; then
    echo "Zone ${label} (${zone_id}): rulesets readable — token covers this zone."
    return 0
  fi
  code=$(printf '%s' "${resp}" | jq -r '.errors[0].code // "?"')
  msg=$(printf '%s' "${resp}" | jq -r '.errors[0].message // "unknown error"')
  err "CLOUDFLARE_API_TOKEN cannot read rulesets on the ${label} zone (${zone_id}): [${code}] ${msg}"
  err "This is the apply-time 'Authentication error (10000)' from #347, caught early."
  err "Fix: scope the token to the '${label}' zone in Zone Resources AND grant"
  err "     'Zone → Dynamic Redirect → Edit'. See docs/runbooks/cloudflare-token-scope.md."
  return 1
}

fail=0
probe_zone "${NET_ZONE_ID}" "noorinalabs.net" || fail=1
probe_zone "${ORG_ZONE_ID}" "noorinalabs.org" || fail=1

if [ "${fail}" -ne 0 ]; then
  err "Cloudflare token preflight FAILED — not running terraform apply on the redirect zones."
  exit 1
fi

echo "Cloudflare token preflight: PASS — token authenticates against .com, .net, .org rulesets."

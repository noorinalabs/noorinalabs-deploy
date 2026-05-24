# ===========================================================================
# Terraform `import` blocks — adopt the existing dynamic-redirect entrypoint
# rulesets on the `.net` / `.org` zones instead of creating new ones (#348).
#
# Cloudflare allows exactly ONE entrypoint ruleset per phase per zone. Both
# defensive TLDs already have an `http_request_dynamic_redirect` entrypoint,
# so a plain `create` of `cloudflare_ruleset.canonical_redirect[*]` is
# rejected by the API:
#
#   Error: failed to create ruleset "http_request_dynamic_redirect"
#   A similar configuration with rules already exists and overwriting will
#   have unintended consequences.
#
# These `import {}` blocks tell Terraform to adopt the existing entrypoint
# into state and then converge it to the redirect rule defined in
# redirects.tf — import-then-update, no destroy/recreate, no second entrypoint.
#
# Import ID format for the cloudflare v4 provider's zone-scoped ruleset is
# `zone/<zone_id>/<ruleset_id>`. The ruleset IDs are not committed anywhere;
# they are discovered at CI time (the "Discover redirect ruleset IDs" step in
# .github/workflows/terraform.yml) and threaded in via
# `TF_VAR_{net,org}_redirect_ruleset_id`. Variable interpolation in import-
# block `id` is supported on Terraform >= 1.6 (versions.tf pins it), and the
# zone-id + ruleset-id vars are plan-time-known, so this resolves at plan.
#
# Removability: once both zones' entrypoints are in state and a clean apply +
# idempotent re-apply have been observed, these `import {}` blocks (and the
# CI discovery step + the two ruleset-id vars) can be dropped in a follow-up —
# Terraform only consults `import {}` for resources not yet in state. Same
# transitional pattern as moved.tf.
# ===========================================================================

import {
  to = cloudflare_ruleset.canonical_redirect["net"]
  id = "zone/${var.noorinalabs_net_zone_id}/${var.net_redirect_ruleset_id}"
}

import {
  to = cloudflare_ruleset.canonical_redirect["org"]
  id = "zone/${var.noorinalabs_org_zone_id}/${var.org_redirect_ruleset_id}"
}

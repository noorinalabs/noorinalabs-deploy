# ===========================================================================
# Canonical-domain 301 redirects — noorinalabs.net + noorinalabs.org → .com
#
# The owner holds three TLD registrations as defensive (squatter-prevention).
# `.net` and `.org` are dark zones today: no productive DNS records, no app
# traffic. This file adds Cloudflare-edge 301 redirects so any HTTP/HTTPS
# request landing on `.net` or `.org` is permanently redirected to the
# matching path on `noorinalabs.com`, preserving the URL path + query string.
#
# Rationale (per issue #166):
#   - Canonical-domain consolidation: users typing the wrong TLD land on the
#     right site instead of an empty page.
#   - SEO signal consolidation: search engines treat the .com as canonical.
#   - Brand consistency.
#
# Mechanism: Cloudflare Rulesets, phase `http_request_dynamic_redirect`.
# This phase runs at the edge before any origin lookup, so the redirect is
# served by Cloudflare itself — no origin server needed for `.net` / `.org`
# (which is why they can stay dark without DNS A/AAAA records).
#
# Why a dynamic-redirect ruleset (not Bulk Redirects, not Page Rules):
#   - Bulk Redirects (option A in #166) live only in the CF dashboard — no
#     TF tracking, no diff review, no DR recovery.
#   - Page Rules (option B) are deprecated upstream by Cloudflare.
#   - `cloudflare_ruleset` with dynamic-redirect is the supported, TF-managed
#     equivalent and aligns with the parity-and-predictability principle
#     (everything in TF, nothing hand-clicked).
#
# Why C1 (extend this module) and not C2 (new root `terraform/cloudflare-
# redirects/`): fewer moving parts. Same provider, same backend, same
# `CLOUDFLARE_API_TOKEN`. New state in a separate root would add CI matrix
# entries and a second `terraform init` for one resource per zone.
#
# Token scope: the `CLOUDFLARE_API_TOKEN` was extended to cover the `.net` /
# `.org` zones (plus Dynamic Redirect edit) in #347 — it did NOT already
# cover all three zones (the earlier comment here claimed otherwise and was
# wrong; corrected per #348).
#
# Import, not create: each `.net` / `.org` zone already has an
# `http_request_dynamic_redirect` phase entrypoint ruleset, and Cloudflare
# permits only one entrypoint per phase per zone. The `cloudflare_ruleset`
# resources below are therefore IMPORTED (adopting the existing entrypoint)
# rather than created — see imports.tf and #348.
# ===========================================================================

locals {
  # Map of zone-key → zone-id for the dark TLDs that should 301 to .com.
  # Add a new entry here to redirect a new TLD; everything else is for_each-
  # driven from this map.
  redirect_zones = {
    net = var.noorinalabs_net_zone_id
    org = var.noorinalabs_org_zone_id
  }
}

resource "cloudflare_ruleset" "canonical_redirect" {
  for_each = local.redirect_zones

  zone_id = each.value
  # MUST be "default". A phase ENTRYPOINT ruleset (kind="zone") is named
  # "default" by Cloudflare convention, and `name` is ForceNew on
  # cloudflare_ruleset. Since each zone already HAS a "default"
  # http_request_dynamic_redirect entrypoint (the one we import via
  # imports.tf), any other name forces a destroy+recreate of the prod
  # entrypoint instead of an in-place adopt-and-update — which both defeats
  # the import (#348) and breaks idempotency. With name="default" the import
  # converges in place: only the rule's description + target_url expression
  # change to our path/query-preserving redirect. Per-zone labeling lives in
  # `description` (NOT ForceNew) and on the inner rule below, not in `name`.
  name        = "default"
  description = "Canonical-domain 301 redirect entrypoint for noorinalabs.${each.key} → noorinalabs.com (path + query preserved)"
  kind        = "zone"
  phase       = "http_request_dynamic_redirect"

  rules {
    action      = "redirect"
    description = "Permanent redirect to noorinalabs.com, preserving path + query"
    # Matches every request — these zones carry no productive traffic, so
    # there is nothing on `.net` / `.org` we'd want to *not* redirect.
    expression = "true"
    enabled    = true

    action_parameters {
      from_value {
        status_code = 301
        # Dynamic target: concatenate the canonical host with the incoming
        # request's URI. Cloudflare's `http.request.uri` field is the path
        # AND query string together (e.g. `/foo?x=1`), so a single concat()
        # preserves both with no conditional — there is no need to glue path
        # and query back together by hand.
        #
        # The earlier form used `if()`/`len()` and `http.request.uri.{path,
        # query}`, but Cloudflare's redirect target_url expression language
        # does NOT support `if()`/`len()` (they are not in the redirect
        # expression function set). The CF API rejected it at apply time with
        # "Filter parsing error … unknown identifier" — the plan can't catch
        # this because CF validates the expression only at apply (#348).
        #
        # Expression-based `target_url` covers query preservation inherently,
        # so the separate `preserve_query_string` boolean is intentionally
        # omitted (it applies only to static `target_url.value` redirects).
        target_url {
          expression = "concat(\"https://noorinalabs.com\", http.request.uri)"
        }
      }
    }
  }
}

# Runbook — `CLOUDFLARE_API_TOKEN` scope for the canonical-redirect rulesets

Source: [deploy#347](https://github.com/noorinalabs/noorinalabs-deploy/issues/347).
Composes with: [#166](https://github.com/noorinalabs/noorinalabs-deploy/issues/166)
(canonical-domain redirects), [#348](https://github.com/noorinalabs/noorinalabs-deploy/issues/348)
(import-not-create + apply-time expression validation),
[`terraform/cloudflare/redirects.tf`](../../terraform/cloudflare/redirects.tf),
[`terraform/cloudflare/README.md`](../../terraform/cloudflare/README.md#prerequisites).

## Symptom

`terraform apply` on the `cloudflare` root creates/refreshes the `.com` DNS
records successfully, then fails when it reaches the `.net` / `.org`
canonical-redirect rulesets:

```
cloudflare_ruleset.canonical_redirect["org"]: Creating...
Error: error creating ruleset canonical-domain-redirect-org
  Authentication error (10000)
cloudflare_ruleset.canonical_redirect["net"]: Creating...
Error: error creating ruleset canonical-domain-redirect-net
  Authentication error (10000)
```

No outage results — the `.net`/`.org` redirects simply don't apply, and those
zones carry no productive traffic. But the apply is half-done and the redirects
stay un-provisioned until the token is fixed.

## Root cause

`CLOUDFLARE_API_TOKEN` is under-scoped for ruleset management on the defensive
TLDs. Managing `http_request_dynamic_redirect` entrypoint rulesets needs the
token to carry **both** of:

1. **Zone Resources** including `noorinalabs.net` and `noorinalabs.org` (not
   just `noorinalabs.com`). A token scoped to `.com` only authenticates for
   `.com` operations and returns auth error 10000 for anything on the other
   zones — which is why the `.com` DNS in the same run succeeds while the
   `.net`/`.org` rulesets fail.
2. The **`Zone → Dynamic Redirect → Edit`** permission group. Single
   Redirects / dynamic-redirect rulesets are managed via the rulesets API;
   `Zone → DNS → Edit` alone does not authorize ruleset writes.

## Why a clean plan does not catch it

Cloudflare validates ruleset **writes** only at APPLY, not at plan. A token
missing the scope above passes `terraform plan` and a green PR review, then
fails `terraform apply` on prod. This is the same plan-vs-apply asymmetry that
bites the redirect *expression* language (#348) — clean plan, failed apply.

## Preflight guard (automated, #347)

[`scripts/cf_token_preflight.sh`](../../scripts/cf_token_preflight.sh) runs in
both the `plan-cloudflare` and `apply-cloudflare` jobs of
[`.github/workflows/terraform.yml`](../../.github/workflows/terraform.yml),
before the `terraform` steps. It:

1. Verifies the token is valid + active (`GET /user/tokens/verify`).
2. Probes `GET /zones/<id>/rulesets` for each redirect zone — a non-destructive
   proxy for "the token covers this zone and can touch the rulesets API."

A miss fails the job early with a pointed `::error::` instead of a half-applied
prod run. Run it locally the same way:

```bash
CLOUDFLARE_API_TOKEN=… \
NET_ZONE_ID="$(…noorinalabs.net zone id)" \
ORG_ZONE_ID="$(…noorinalabs.org zone id)" \
  scripts/cf_token_preflight.sh
```

### Residual apply-gated gap

The preflight proves the token **covers each zone** (zone in Zone Resources +
rulesets readable). It cannot fully prove the **`Dynamic Redirect → Edit`**
write permission without mutating prod ruleset state (Cloudflare has no dry-run
for ruleset PUT). The one case that still surfaces only at apply: token has the
zone but lacks the Dynamic Redirect *Edit* group specifically. Set both per the
fix below and that case disappears.

## Fix (owner action — Cloudflare dashboard)

> This is an owner action: the token lives behind the org-level GitHub Actions
> secret `CLOUDFLARE_API_TOKEN` and editing it requires Cloudflare dashboard
> access. Mark the PR that lands this runbook as `Refs #347`, not `Closes` —
> close #347 only after the live re-apply below is green.

1. Cloudflare dashboard → **My Profile → API Tokens** → edit the token behind
   `CLOUDFLARE_API_TOKEN` (or mint a new one — see README § "How to rotate").
2. **Permissions** — ensure all of:
   - `Zone → DNS → Edit`
   - `Zone → Zone Settings → Edit`
   - `Zone → Zone → Read`
   - `Zone → Dynamic Redirect → Edit`  ← the missing one
3. **Zone Resources** — include `noorinalabs.com`, `noorinalabs.net`,
   `noorinalabs.org` (or "All zones in account").
4. If the token value changed, re-set the secret:
   ```bash
   gh secret set CLOUDFLARE_API_TOKEN --repo noorinalabs/noorinalabs-deploy
   ```
5. Re-run `Apply (cloudflare)` (rerun the failed jobs on the original run, or
   push a no-op to `main` touching `terraform/**`). The rulesets import
   idempotently — the `.com` DNS and `stg_www_cname` already in state are
   untouched.

## Verify (live, post-apply)

```bash
# Both should 301 to the matching path on .com, preserving path + query.
curl -sI "https://noorinalabs.net/some/path?x=1" | grep -iE '^location|^HTTP'
curl -sI "https://noorinalabs.org/some/path?x=1" | grep -iE '^location|^HTTP'
# Expect: HTTP/.. 301 ... and Location: https://noorinalabs.com/some/path?x=1
```

Once both return the 301 to `noorinalabs.com`, close #347.

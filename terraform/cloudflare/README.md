# Terraform — Cloudflare DNS

Manages DNS records and zone settings for `noorinalabs.com` via the Cloudflare provider, plus canonical-domain 301 redirects for the defensive `.net` + `.org` TLDs (per #166). This is a **single module** (not per-env like `terraform/hetzner/`) because the .com zone is shared between prod and stg, and the redirect rulesets on the dark zones are single-resource each.

## Layout

```
terraform/cloudflare/
├── README.md                   # this file
├── main.tf                     # provider, zone settings, all .com DNS records
├── redirects.tf                # canonical-domain 301 redirects (.net/.org → .com)
├── variables.tf                # API token + all three zone IDs + domain
├── outputs.tf                  # prod_hostnames, stg_hostnames, ssl_mode, redirect ruleset IDs
├── moved.tf                    # state-migration history (pre-#83 renames)
├── versions.tf
└── terraform.tfvars.example
```

## Resources managed

| Resource | Name in Terraform | Proxied | Notes |
|---|---|---|---|
| Zone settings | `cloudflare_zone_settings_override.ssl` | — | SSL=strict, TLS≥1.2, always-HTTPS, auto-HTTPS-rewrites |
| `noorinalabs.com` A | `prod_apex_a` | **yes** | Prod VPS IPv4, read from hetzner state |
| `noorinalabs.com` AAAA | `prod_apex_aaaa` | **yes** | Prod VPS IPv6 (conditional on non-empty) |
| `www` A | `www_a` | **yes** | Mirrors apex; Caddy redirects www → apex |
| `www` AAAA | `www_aaaa` | **yes** | Mirrors apex |
| `isnad` CNAME | `prod_isnad_cname` | **yes** | → apex (isnad-graph app, prod) |
| `users` CNAME | `prod_users_cname` | **yes** | → apex (user-service, prod) |
| `stg` A | `stg_apex_a` | **no** | Stg VPS IPv4, gray-cloud |
| `stg` AAAA | `stg_apex_aaaa` | **no** | Stg VPS IPv6 (conditional) |
| `isnad.stg` CNAME | `stg_isnad_cname` | **no** | → `stg.noorinalabs.com` |
| `users.stg` CNAME | `stg_users_cname` | **no** | → `stg.noorinalabs.com` |

## Canonical-domain redirects (.net + .org → .com)

Per #166. The owner holds `noorinalabs.net` and `noorinalabs.org` as defensive registrations. They are dark zones today (no app DNS records). To consolidate canonical-domain traffic, SEO signals, and brand consistency, both zones serve a Cloudflare-edge 301 redirect to the matching path on `noorinalabs.com`.

| Source | Behavior | Defined in |
|---|---|---|
| `https://noorinalabs.net/<path>?<query>` | 301 → `https://noorinalabs.com/<path>?<query>` | `redirects.tf` |
| `https://noorinalabs.org/<path>?<query>` | 301 → `https://noorinalabs.com/<path>?<query>` | `redirects.tf` |

Implementation: a `cloudflare_ruleset` resource per zone in phase `http_request_dynamic_redirect`. The rule matches every request (`expression = "true"`) and rewrites the URL via a dynamic `target_url.expression`. Path + query string are preserved by string-concatenation in the expression. Because the redirect fires at the Cloudflare edge before origin lookup, no DNS A/AAAA record on `.net` / `.org` is required — the zones stay dark.

### Why dynamic-redirect Rulesets

Issue #166 enumerates three options:

- **Bulk Redirects** (dashboard UI): no TF tracking, no diff review, no DR recovery.
- **Page Rules**: deprecated upstream by Cloudflare.
- **Rulesets** (chosen): TF-managed, diff-reviewable, identical secret + provider as the rest of this module.

### Adding a new TLD redirect

If a fourth TLD is later registered:

1. Add its zone ID to `noorinalabs_<tld>_zone_id` in `variables.tf` and to `terraform.tfvars.example`.
2. Add a `<tld> = var.noorinalabs_<tld>_zone_id` entry to `local.redirect_zones` in `redirects.tf`. The ruleset is `for_each`-driven, so no other change is needed.
3. Wire the zone ID into the CI repo variables (`vars.CLOUDFLARE_NOORINALABS_<TLD>_ZONE_ID`) and the corresponding `TF_VAR_*` env in `.github/workflows/terraform.yml` (plan-cloudflare + apply-cloudflare jobs).

## Prod vs stg proxy posture

The two environments have asymmetric Cloudflare proxy posture by necessity:

| Env | Records | Proxied | TLS terminated at | Edge benefits |
|-----|---------|---------|-------------------|---------------|
| prod | apex, www, isnad, users | **true** (orange-cloud) | CF edge | DDoS mitigation, WAF, edge cache |
| stg | stg, isnad.stg, users.stg | **false** (gray-cloud) | origin Caddy (Let's Encrypt) | none |

### Why the asymmetry is forced

Cloudflare Universal SSL covers `*.noorinalabs.com` (one wildcard level) and the apex. Third-level subdomains like `isnad.stg.noorinalabs.com` are **not covered**: TLS handshake at the CF edge fails with a certificate mismatch. Enabling `proxied = true` on stg records causes immediate TLS errors (#228 confirmed this — toggled all stg records back to `proxied = false` as the forced fix).

Origin Caddy auto-provisions valid Let's Encrypt certificates for the stg hostnames directly, so stg HTTPS works without the CF proxy.

### Trade-off

Stg without the CF proxy means:
- No Cloudflare DDoS / rate-limit / WAF rules apply at the edge for stg traffic
- No CF edge caching for stg responses
- Stg origin is directly reachable (IP exposed) — acceptable for non-prod

Prod keeps the full orange-cloud posture (DDoS mitigation, WAF, edge cache).

### Remediation option: Cloudflare Advanced Certificate Manager (ACM)

Cloudflare ACM (~$10/month) adds wildcard cert coverage at any subdomain depth, including `*.stg.noorinalabs.com`. Enabling ACM would allow stg records to be proxied like prod (one dashboard toggle in the CF zone). Owner decision — paid feature, not a defect. Tracked in deploy#229.

**If ACM is enabled:** flip all stg records to `proxied = true` in `main.tf` and re-apply. No DNS record renames needed — the `proxied` field is the only change.

**If stg stays gray-cloud:** no action needed. Document the decision in deploy#229 and close as won't-fix.

### IP source — no manual var needed

Prod and stg VPS IPs are read automatically from the hetzner per-env Terraform remote state (`hetzner/prod.tfstate`, `hetzner/stg.tfstate`) via `data "terraform_remote_state"`. You do not pass them as variables. The CI apply job runs `apply-cloudflare` after `apply` (hetzner) so the state it reads is always freshly converged.

## Prerequisites

- [Terraform >= 1.6](https://developer.hashicorp.com/terraform/install)
- `CLOUDFLARE_API_TOKEN` — Cloudflare API token. **Permissions:** `Zone:DNS:Edit`, `Zone:Zone Settings:Edit`, `Zone:Zone:Read`, **and `Zone:Dynamic Redirect:Edit`** (the last is required for the `.net`/`.org` canonical-redirect rulesets — DNS edit alone is insufficient for ruleset management). **Zone Resources:** must cover all three zones — `noorinalabs.com`, `noorinalabs.net`, `noorinalabs.org` (or "All zones in account"). A token scoped to `.com` only authenticates for `.com` DNS but fails the redirect apply with `Authentication error (10000)` — see [token-scope runbook](../../docs/runbooks/cloudflare-token-scope.md) and #347.
  - **Token kind — User vs Account-Owned (#511):** Cloudflare API tokens come in two kinds. A **User API Token** verifies at `GET /user/tokens/verify`; an **Account-Owned API Token** verifies only at `GET /accounts/{account_id}/tokens/verify` and returns `code 1000 "Invalid API Token"` at the user endpoint despite being valid. The Terraform provider (`~> 4.43`) auths the same way for both (zone-scoped `api_token` calls), so **no resource change is needed** for either kind — but the CI **token-scope preflight** must hit the matching verify endpoint. The org's `CLOUDFLARE_API_TOKEN` is now account-owned, so `CLOUDFLARE_ACCOUNT_ID` (below) must be set. See the [token-scope runbook § User vs Account-Owned API tokens](../../docs/runbooks/cloudflare-token-scope.md#user-vs-account-owned-api-tokens-511).
- `CLOUDFLARE_ACCOUNT_ID` — Cloudflare **account** ID (public metadata, **not a secret**). Required when `CLOUDFLARE_API_TOKEN` is an account-owned token so the preflight verifies at the account endpoint; omit it for a user token (the preflight falls back to `/user/tokens/verify`). In CI it is the repo variable `vars.CLOUDFLARE_ACCOUNT_ID`, wired into the `Cloudflare token-scope preflight` step of both `plan-cloudflare` and `apply-cloudflare`. Set it with:
  ```bash
  gh variable set CLOUDFLARE_ACCOUNT_ID --repo noorinalabs/noorinalabs-deploy --body <account_id>
  ```
  Find the account ID in the Cloudflare dashboard (any zone → Overview → Account ID in the sidebar).
- `cloudflare_zone_id` — Zone ID for `noorinalabs.com` (Cloudflare dashboard → Overview → Zone ID)
- Backblaze B2 credentials (to read hetzner remote state — same creds as the cloudflare module's own backend)

## Backend credentials

```bash
export AWS_ACCESS_KEY_ID="your-b2-key-id"       # TF_STATE_B2_KEY_ID in CI
export AWS_SECRET_ACCESS_KEY="your-b2-app-key"  # TF_STATE_B2_APP_KEY in CI
```

## Usage

```bash
cd terraform/cloudflare
cp terraform.tfvars.example terraform.tfvars
# fill in cloudflare_api_token and cloudflare_zone_id
terraform init
terraform plan   -var-file=terraform.tfvars
terraform apply  -var-file=terraform.tfvars
```

## How to add a new DNS record

### Subdomain (e.g., `api.noorinalabs.com`)

1. Add a `cloudflare_record` resource in `main.tf`:

```hcl
resource "cloudflare_record" "prod_api_cname" {
  zone_id = var.cloudflare_zone_id
  name    = "api"
  content = var.domain          # → noorinalabs.com (apex)
  type    = "CNAME"
  ttl     = 1                   # 1 = Automatic when proxied
  proxied = true                # true for prod; false for stg 3rd-level subdomains
}
```

2. Add the corresponding stg record if needed (gray-cloud, `proxied = false`).

3. Add the hostname to the relevant output map in `outputs.tf`.

4. Update the Caddy config on the VPS to accept the new hostname.

5. Open a PR targeting `deployments/phase-3/wave-11` (or the active wave branch). CI runs `terraform plan` automatically.

### Root apex (A/AAAA)

Root apex A/AAAA records point at `local.prod_vps_ipv4` / `local.prod_vps_ipv6` (read from hetzner remote state). If you add a second apex you will conflict with the existing `prod_apex_a` — file an issue first.

## How to rotate the `CLOUDFLARE_API_TOKEN` GH secret

1. Create a new token at `https://dash.cloudflare.com/profile/api-tokens` with permissions:
   - `Zone:DNS:Edit`
   - `Zone:Zone Settings:Edit`
   - `Zone:Zone:Read`
   - `Zone:Dynamic Redirect:Edit` — required for the `.net`/`.org` canonical-redirect rulesets (#347)
   Scope it (Zone Resources) to **all three zones** — `noorinalabs.com`, `noorinalabs.net`, `noorinalabs.org` (or "All zones in account"). A `.com`-only token fails the redirect apply with `Authentication error (10000)`; see the [token-scope runbook](../../docs/runbooks/cloudflare-token-scope.md).

2. Update the secret in GitHub:

   ```bash
   gh secret set CLOUDFLARE_API_TOKEN --repo noorinalabs/noorinalabs-deploy
   # paste the new token at the prompt
   ```

3. Verify by triggering the `terraform.yml` workflow manually (Actions → Terraform → Run workflow) or via the next push to `main` that touches `terraform/**`.

4. Revoke the old token in the Cloudflare dashboard after confirming CI is green.

> **Note:** `CLOUDFLARE_API_TOKEN` is an org-level GitHub Actions secret, so it is shared with other repos in the org. Coordinate with the team before rotating if other workflows consume it.

## Emergency manual changes

For DNS emergencies, you can edit records directly at `https://dash.cloudflare.com` → `noorinalabs.com` → DNS → Records.

**Warning:** any manual change will be overwritten on the next `terraform apply`. If you make a manual change, immediately open a PR to reflect it in `main.tf` before the next CI run. Do not rely on manual changes as a permanent fix.

## Single-state design rationale

Unlike `terraform/hetzner/` (which uses `envs/{stg,prod}/` with separate state files), the Cloudflare module uses a **single root module** with one state file (`cloudflare/terraform.tfstate`). Reason: there is one Cloudflare zone (`noorinalabs.com`) — not one per environment. Both prod and stg DNS records live in the same zone and therefore the same module. The `envs/` layout in Hetzner exists because each env is a distinct VPS with distinct Terraform state; that distinction does not apply here.

See [`docs/adr/0001-tf-hetzner-per-env-state-strategy.md`](../../docs/adr/0001-tf-hetzner-per-env-state-strategy.md) for the Hetzner per-env rationale (the contrast clarifies why the cloudflare module is structured differently).

## State migration history

[`moved.tf`](./moved.tf) records state-preserving renames from the pre-#83 module. These blocks prevent destroy+recreate of existing DNS records on first apply after a rename. They can be removed after the next clean apply.

Key renames captured:
- `cloudflare_record.root_a` → `prod_apex_a`
- `cloudflare_record.www` → `www_a`
- `cloudflare_record.prod_auth_cname` → `prod_users_cname` (per main#212 Q2 hostname ruling)
- `cloudflare_record.stg_auth_cname` → `stg_users_cname`

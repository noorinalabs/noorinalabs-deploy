# Phase C Cutover Runbook — owner-driven manual

Cutting over the production stack from a hand-made (pre-Terraform) VPS to a
Terraform-managed prod box. This runbook documents the **owner-driven manual
sequence** ratified during the 2026-05-02 cutover incident, and **replaces**
the original automated path described in `deploy#86`
(routine `trig_01Bif8T51pdaYFjkbM5bERyL`).

## When this applies

You are doing a Phase C cutover when:

- A new TF-managed prod VPS already exists or is about to be created by a
  wave-merge `terraform/**` auto-apply (under
  `terraform/hetzner/envs/prod/`)
- The currently-live prod traffic is still served by a hand-made (or any
  pre-existing, out-of-Terraform) VPS
- DNS, GHCR token, and stack-secret env-scope vars still point at the old
  box

This is **not** the same as a routine prod deploy — for those, follow
`deploy-isnad-graph.md` and the standard `promote.yml` →
`deploy-prod.yml` flow.

## Why owner-driven is canonical

The original automated routine assumed it could:

1. Verify five prereqs
2. Spawn a deploy implementer
3. Call the Hetzner API to **delete** the old prod box
4. `terraform apply` to provision the replacement
5. Deploy the stack

In practice, on 2026-05-02:

- `terraform apply` had **already** auto-created the new prod box
  (`178.156.214.225`) during a wave-merge — so step 4 was a no-op and
  the routine's "world hasn't changed under me" assumption was wrong
- Decommissioning a production VPS is destructive; an async routine
  driving that step is riskier than the owner doing it from the Hetzner
  dashboard with eyes on the dependency graph
- The recovery sequence the owner actually executed — env-var swap,
  Cloudflare DNS apply, `promote.yml`, `deploy-prod.yml` with the
  `allow_stg_tags` break-glass — worked end-to-end and is now the
  proven pattern

## Pre-cutover gates (owner verifies all)

Before starting the sequence below, confirm each:

- [ ] **TF prod state is settled.** From a clean checkout of `main`:
  ```bash
  cd terraform/hetzner/envs/prod/
  terraform init -backend=true
  terraform plan
  ```
  Expected: `No changes. Your infrastructure matches the configuration.`
  If the plan shows pending changes, let the wave-merge auto-apply run
  (or apply manually) and re-verify before proceeding.

- [ ] **DNS records are Terraform-managed.** (Required as of #192.)
  Confirm `terraform/cloudflare/main.tf` contains an apex A/AAAA record
  set resolved through `local.prod_vps_ipv4` / `local.prod_vps_ipv6`,
  which in turn read from
  `data.terraform_remote_state.hetzner_prod.outputs.server_ip` /
  `server_ipv6`. Anything still hand-edited in the Cloudflare dashboard
  MUST be migrated to TF first — Phase C cutover flips DNS via TF
  apply, not the dashboard.

- [ ] **`GH_PACKAGES_TOKEN` has `write:packages` scope.** Required for
  the `promote.yml` retag step. Verify with:
  ```bash
  gh api -H "Authorization: token <token>" /user -i \
    | grep -i x-oauth-scopes
  ```
  Scopes must include `write:packages`. (A missing scope here was the
  2026-05-02 GHCR cross-repo write block — see `allow_stg_tags`
  break-glass in `break-glass.md` for the recovery path if this gate
  fails mid-cutover.)

- [ ] **Alembic pre-deploy gate is functional.** Only relevant if the
  cutover is also a first-deploy / DR-restore (no prior
  `user-postgres` on the new box). For an in-place cutover where the
  new box has been receiving stg-style traffic, the gate runs normally
  and no break-glass is required.

## Cutover sequence (owner-driven, sequential)

Run each step from a clean checkout of `main` with `gh` authenticated as
an owner-class account.

### 1. Update env-scope `VPS_HOST` for production

`deploy-prod.yml` reads `${{ vars.VPS_HOST }}` (an env-scope **variable**,
not a secret) at all three SSH/host call sites. Use `gh variable set`
— `gh secret set` would write a never-read secret while the actual
variable kept pointing at the old box, so the next prod deploy would
silently SSH the old host.

```bash
gh variable set VPS_HOST \
  --repo noorinalabs/noorinalabs-deploy \
  --env production \
  --body "<new_prod_ipv4>"
```

Replace `<new_prod_ipv4>` with the IPv4 of the new TF-managed prod box
(e.g. `178.156.214.225` for the 2026-05-02 cutover). Confirm with:

```bash
gh variable list --repo noorinalabs/noorinalabs-deploy --env production \
  | grep VPS_HOST
```

(Note: `gh variable set` is silent on success; only the `gh variable list`
read-back-verify confirms the update landed — see the org-level
"gh CLI silent-no-op family" feedback for similar gotchas.)

### 2. Confirm Cloudflare TF sees the new prod IP (no tfvars edit)

**No tfvars edit is needed.** `terraform/cloudflare/main.tf` reads the
prod VPS IPs from the hetzner prod remote state via
`data "terraform_remote_state" "hetzner_prod"` —
`local.prod_vps_ipv4` and `local.prod_vps_ipv6` resolve to
`outputs.server_ip` and `outputs.server_ipv6` of the hetzner prod root
module. `terraform/cloudflare/variables.tf` has no
`prod_vps_ipv4_address` / `prod_vps_ipv6_address` input, and
`terraform.tfvars.example` documents this explicitly ("Per-env VPS IPs
are NOT input variables.").

Verify the hetzner prod outputs match the new box before the CF apply
in step 5:

```bash
cd terraform/hetzner/envs/prod/
terraform init -backend=true
terraform output server_ip
terraform output server_ipv6
```

Both values MUST equal the new prod box IPs. If they don't, the hetzner
state has not been refreshed against the new box — let the wave-merge
auto-apply settle (or run `terraform apply` here manually) before
proceeding. The CF apply in step 5 will pick up these outputs
automatically.

### 3. Promote images

```bash
gh workflow run promote.yml --ref main
```

No break-glass inputs are required for a routine cutover (assuming the
`GH_PACKAGES_TOKEN` gate passed in pre-cutover checks). Monitor the run:

```bash
gh run list --workflow promote.yml --limit 1
gh run watch <run-id>
```

If `promote.yml` fails at the GHCR cross-repo retag step (the
2026-05-02 failure mode), recovery options are in
`break-glass.md` under `skip_stg_verify` and `allow_stg_tags` — but
the right fix is to land the token-scope correction first and re-run
`promote.yml` cleanly.

### 4. Deploy to prod

```bash
gh workflow run deploy-prod.yml --ref main
```

This consumes the freshly-promoted `prod-*` tags and SSHes to the new
prod box (now pointed at by `VPS_HOST` from step 1). Monitor:

```bash
gh run list --workflow deploy-prod.yml --limit 1
gh run watch <run-id>
```

At this point the stack is **running on the new box but DNS still points
at the old box** — verify the new stack out-of-band before flipping DNS:

```bash
curl -I -H "Host: noorinalabs.com" https://<new_prod_ipv4>/ \
  --resolve noorinalabs.com:443:<new_prod_ipv4> -k
```

Expect `HTTP/2 200`. If you see `502`/`503`/connection refused, do
**not** proceed to step 5 — the DNS flip would take prod down.

### 5. Apply Cloudflare TF (DNS flips to new prod)

The CF root module picks up the new prod IPs automatically from the
hetzner prod remote state (verified in step 2) — no tfvars or HCL
edit is required here. The `terraform plan` should show the apex and
www A/AAAA records changing to the new prod IPs purely from the
upstream state.

```bash
cd terraform/cloudflare/
terraform init -backend=true
terraform plan   # expect: apex A/AAAA records changing to new prod IPs
terraform apply
```

If the plan does NOT show the IP change, re-check step 2 — the hetzner
prod remote state is still publishing the old IPs and the CF apply
would be a no-op (leaving DNS pointed at the old box).

Within ~Cloudflare propagation window (seconds–minutes), prod traffic
flips. Verify from a non-cached resolver:

```bash
dig +short @1.1.1.1 noorinalabs.com
curl -I https://noorinalabs.com/
```

Expect the new IP and `HTTP/2 200`.

### 6. Verify the new stack end-to-end

Trigger the prod verify workflow:

```bash
gh workflow run verify-deploy.yml --ref main -f target=prod
```

Or run the manual smoke directly:

```bash
SITE_URL=https://noorinalabs.com ./scripts/verify_deployment.sh \
  --skip-workflow
```

Confirm container health on the new box:

```bash
ssh deploy@<new_prod_ipv4> \
  "docker compose -p noorinalabs \
     -f /opt/noorinalabs-deploy/compose/docker-compose.prod.yml \
     ps --format '{{.Name}}\t{{.Status}}'"
```

Every service should be `Up` (some with `(healthy)`).

### 7. Owner decommissions the old box

**Owner action — not scriptable.** From the Hetzner Cloud dashboard,
locate the old prod server (e.g. `1box-prod` / id `124917846` /
`87.99.134.161` for the 2026-05-02 cutover), confirm it is no longer
receiving traffic (DNS has flipped, no inbound connections per
Hetzner's traffic graph), and delete it.

Do **not** automate this step. The owner's eyes on the dependency
graph (DNS, monitoring, any forgotten cron jobs reaching this box) is
the safety check.

The full teardown sequence, pre-teardown gates, operator-side stale-entry
cleanup, and the in-repo reference-removal manifest live in
[`decommission-old-prod-vps.md`](decommission-old-prod-vps.md)
(`deploy#86`). Hand off to that runbook here.

## Rollback

If any step fails and prod is degraded, roll back in reverse order:

1. **Pre-DNS-flip failure (steps 1–4):** No user-visible impact yet.
   - Revert the `VPS_HOST` env-scope variable to the old IP
     (`gh variable set VPS_HOST --env production --body "<old_ipv4>"`)
   - Old box is still running the previous stack; no action needed there
   - Investigate the failure, fix root cause, retry the sequence

2. **Post-DNS-flip failure (step 5+):** Prod is on the new box.
   - **Fastest reversal:** point the hetzner prod state back at the old
     box (e.g. `terraform state` swap, or `terraform apply -refresh-only`
     against the old box's resources) and re-run the CF
     `terraform apply` — the CF root module will re-read the upstream
     outputs and flip the apex A/AAAA records back. Old stack is still
     up (step 7 has not yet run) and will resume serving traffic within
     propagation window. (No CF tfvars edit is involved — IPs are
     sourced from upstream state, per step 2.)
   - If the old box has already been decommissioned (step 7 done before
     a regression surfaced), rollback is to **roll forward** — use
     `rollback.yml` with the last-known-good `prod-*` image tag, or
     `deploy-prod.yml` with a prior `sha-*` tag via the `image_tag`
     input. The new box stays, the stack rolls back.

3. **Promote/deploy regression:** Image-level rollback uses the same
   tooling as a routine deploy:
   ```bash
   gh workflow run rollback.yml --ref main \
     -F image_tag=<prior_sha> -F service=all
   ```
   See `deploy-isnad-graph.md` § Rollback for the full procedure and
   tag-discovery commands.

## Decommission of the async routine

**Owner action (post-merge):** decide the fate of routine
`trig_01Bif8T51pdaYFjkbM5bERyL`.

Two options:

1. **Delete the routine.** Preferred. The owner-driven sequence above is
   now the ratified Phase C pattern, and leaving a stale routine wired
   up risks someone (or an orchestrator) re-triggering it against a
   prod that's already cut over.
2. **Rewrite the routine's prereq script to no-op** when the cutover is
   already complete (e.g. detect that the TF-managed prod box exists
   AND the hand-made box does not, and short-circuit with a no-op exit).
   This preserves the routine as a tripwire but adds defensive code
   paths that don't carry their weight if the routine is never invoked.

Recommendation: delete it. The runbook is the durable record.

This action cannot be performed from a worktree — the routines system is
out of reach of the implementer agent. The owner should action it after
the runbook PR merges.

## What can NOT be tested in CI

- The full cutover requires a real new Hetzner box and a real Cloudflare
  zone; there is no dry-run mode for `terraform apply` against the
  prod state backend
- `gh variable set` is silent on success; only read-back-verify with
  `gh variable list` confirms the env-scope variable update
- DNS propagation depends on Cloudflare's edge cache and the resolver
  the client is using; the runbook treats step 5 as the
  point-of-no-return for that reason

## Related issues + refs

- `deploy#86` — original routine-spawned cutover issue (this runbook
  supersedes the automated path described there; #86 should be closed
  with a link to this runbook, or kept open as a supersedes-link
  tracker — orchestrator decision)
- `deploy#231` — issue this runbook implements
- `deploy#192` — DNS records moved into Terraform (prerequisite gate)
- `deploy#232` — break-glass workflow inputs (`skip_alembic_gate`,
  `allow_stg_tags`) used during 2026-05-02 recovery
- `deploy#251` — break-glass audit/alert layer (read this before using
  any break-glass input mid-cutover)
- `noorinalabs-main#212` — two-VPS topology context
- `A.Idrissi/skip-alembic-emergency` — 2026-05-02 emergency-restore PR
- `docs/runbooks/break-glass.md` — appropriate usage of the three
  break-glass inputs
- `docs/runbooks/deploy-isnad-graph.md` — routine-deploy procedures
  (rollback tooling, tag discovery)

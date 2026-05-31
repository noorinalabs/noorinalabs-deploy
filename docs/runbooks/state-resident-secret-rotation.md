# Runbook — state-resident secret rotation (defense-in-depth)

Source: [deploy#193](https://github.com/noorinalabs/noorinalabs-deploy/issues/193)
— Phase 3 of [deploy#172](https://github.com/noorinalabs/noorinalabs-deploy/issues/172).
Inventory cross-ref: [deploy#11](https://github.com/noorinalabs/noorinalabs-deploy/issues/11)
(the broader secrets-management posture — update its rotation timestamps after a run here).
Composes with: [`state-key-annual-rotation.md`](state-key-annual-rotation.md)
(the **state-bucket key** rotation — a *different* secret class),
[`terraform-workstation-apply.md`](terraform-workstation-apply.md)
(the apply discipline every `terraform apply` below must follow).

## Why this runbook exists

Until **2026-04-30T04:30Z** the `noorinalabs-terraform-state` bucket had **no
server-side encryption at rest** — every Terraform state object was stored
plaintext for a ~23-day window (since 2026-04-07). Phase 1 + 2 (#172) enabled
SSE-B2 and deleted all 13 plaintext versions. **This runbook is Phase 3**: a
one-time **defense-in-depth rotation** of the secrets that were sitting plaintext
*inside* that state during the exposure window.

> **This is belt-and-suspenders, not incident response.** There is **no evidence
> of leak** (see #193 § Threat model). If a leak were to come to light
> retroactively, completing this rotation closes the window. Run it once; it is
> not a recurring cadence (the recurring cadence lives in #11 / future policy).

### Which secrets, and why they are "state-resident"

Two of the four pending secrets reach the live VPS through **cloud-init
`user_data`**, which Terraform renders from `local.cloud_init_vars`
(`terraform/hetzner/modules/hetzner-vps/main.tf`) — so their plaintext value is
captured in the **hetzner tfstate** whenever Terraform writes `user_data`:

| Secret | TF variable | On the live box | Captured in |
|---|---|---|---|
| `user_service_jwt_secret` | `var.user_service_jwt_secret` | cloud-init `write_files` (user-service env) — **set at first boot only** | hetzner tfstate |
| `ghcr_auth_b64` | `var.ghcr_auth_b64` | cloud-init `write_files` → `/home/deploy/.docker/config.json` — **set at first boot only** | hetzner tfstate |

> **CRITICAL — `user_data` is `ignore_changes`-masked, so a plain `terraform
> apply` does NOT re-render these onto an existing box.** `hcloud_server.app`
> carries `lifecycle { ignore_changes = [ssh_keys, user_data] }`
> (`modules/hetzner-vps/main.tf`), and Hetzner runs cloud-init exactly **once**
> at first boot — after that, `user_data` has no effect on the live box no
> matter what Terraform sends (see
> [`cloud-init-template-changes.md`](cloud-init-template-changes.md)). The two
> secrets above therefore reach a *running* box **only** via a
> **taint/replace** of the server (destroy + recreate). Sections 2 and 3 below
> use that path. **Do not** rotate these with a bare `terraform apply` — it
> updates only the tfstate value, leaving the live box serving the OLD secret
> while you believe you rotated.
>
> *(`user_postgres_password` / `user_redis_password` are also cloud-init-seeded,
> but they were rotated via #126 through the user-service's own path — out of
> scope here; see the note below.)*

The other two are **B2 application keys** minted by `terraform/backblaze/` (whose
outputs are `sensitive` and stored in the **backblaze tfstate**), then fed to the
deploy workflow as GH secrets `PIPELINE_B2_KEY` / `PIPELINE_B2_KEY_ID`:

| Secret | Origin | Reaches the box via |
|---|---|---|
| `pipeline_rw_key` | `b2_application_key.pipeline_rw` output | GH secret `PIPELINE_B2_KEY*` → deploy `.env` |
| `pipeline_ro_key` | `b2_application_key.pipeline_ro` output | GH secret (read consumers) |

> **Already rotated (Phase 3 partial credit, no action here):**
> `user_postgres_password` and `user_redis_password` (stg + prod) were rotated
> via [deploy#126](https://github.com/noorinalabs/noorinalabs-deploy/issues/126)
> on 2026-04-30. The optional `TF_STATE_B2_APP_KEY` rotation is a *separate*
> class — use [`state-key-annual-rotation.md`](state-key-annual-rotation.md), not
> this runbook.

## Pre-flight (always)

1. **Confirm SSE-B2 is still active** on the state bucket — rotating into a bucket
   that has silently lost encryption would re-expose the new values:

   ```bash
   b2 bucket get noorinalabs-terraform-state \
     | jq -r '.defaultServerSideEncryption.mode'
   # expect: SSE-B2
   ```

   If this is **not** `SSE-B2`, STOP — re-open #172 Phase 1 before rotating.

2. **Confirm zero plaintext versions** remain (Phase 2 invariant):

   ```bash
   b2 ls --json --recursive --versions b2://noorinalabs-terraform-state \
     | jq -r '.[] | select(.serverSideEncryption.mode != "SSE-B2") | .fileName'
   # expect: no output
   ```

3. **You are an authorized operator** and will follow
   [`terraform-workstation-apply.md`](terraform-workstation-apply.md) for every
   `terraform apply` below (announce in `#deploy`, check for in-flight CI apply,
   completion note). B2 has no native state lock; that discipline is the only
   thing serializing your apply against CI.

## Rotation order — lowest blast radius first

Run these as **separate units** (separate `#deploy` announcements, separate
verification). Do **not** bundle them into one apply.

---

### 1. `pipeline_rw_key` / `pipeline_ro_key` (lowest blast radius)

Pipeline workers can be restarted post-rotation; no user-facing impact.

1. **Mint fresh scoped keys.** The `b2_application_key` resources in
   `terraform/backblaze/main.tf` are *immutable* on capability/scope change —
   the canonical way to "rotate" is to taint and re-apply so B2 issues new key
   secrets (apply is workstation-only per
   [`backblaze/README.md` § Apply (workstation only)](../../terraform/backblaze/README.md#apply-workstation-only)):

   ```bash
   cd terraform/backblaze
   terraform taint b2_application_key.pipeline_rw
   terraform taint b2_application_key.pipeline_ro
   terraform apply          # mints new secrets; old keys are destroyed
   ```

2. **Push the new values to GH secrets** (stdin keeps them out of shell history —
   see `backblaze/README.md` § Retrieving sensitive outputs):

   ```bash
   terraform output -raw pipeline_rw_key_id | gh secret set PIPELINE_B2_KEY_ID    --repo noorinalabs/noorinalabs-deploy --body -
   terraform output -raw pipeline_rw_key    | gh secret set PIPELINE_B2_KEY       --repo noorinalabs/noorinalabs-deploy --body -
   terraform output -raw pipeline_ro_key_id | gh secret set PIPELINE_B2_KEY_ID_RO --repo noorinalabs/noorinalabs-deploy --body -
   terraform output -raw pipeline_ro_key    | gh secret set PIPELINE_B2_KEY_RO    --repo noorinalabs/noorinalabs-deploy --body -
   ```

3. **Redeploy** so the VPS `.env` picks up the new keys, then restart workers:

   ```bash
   gh workflow run deploy-prod.yml --repo noorinalabs/noorinalabs-deploy
   # (and deploy-stg.yml for staging)
   ```

4. **Verify:** `b2 list-keys` shows the *new* `noorinalabs-pipeline-rw` /
   `-ro` key IDs; the old IDs are gone. Pipeline workers come up healthy
   against the new credentials.

---

### 2. `ghcr_auth_b64` (medium blast radius — server replace)

Controls image pulls on the VPS — cloud-init writes it to
`/home/deploy/.docker/config.json` (`modules/hetzner-vps/cloud-init.yaml.tpl`).
It is **not** in the deploy workflow `.env` (`deploy-prod.yml` `env_keys` has no
`ghcr_auth_b64`), so a redeploy does **not** refresh it. Because it is only
written at first boot and `user_data` is `ignore_changes`-masked, updating the
state-resident value requires a **server taint/replace** — a bare
`terraform apply` will update tfstate but leave the live box's
`/home/deploy/.docker/config.json` on the OLD value.

> **Cheaper alternative (preferred if you only need to rotate the running
> cred):** edit `/home/deploy/.docker/config.json` on the box directly
> (`docker login ghcr.io` as the `deploy` user with the new PAT, or rewrite the
> `auth` blob), then still update the TF variable + tfstate so the next legitimate
> server-replace doesn't reinstate the old value. A full server replace is only
> required when you want state and box reconciled in one operation.

1. **Generate a fresh GHCR PAT** scoped `read:packages` (+ `write:packages` only
   if the same token pushes — confirm against the publishing workflow before
   widening). Base64 the `username:token` pair:

   ```bash
   NEW_GHCR_B64=$(printf '%s:%s' "<gh-username>" "<new-ghcr-pat>" | base64 -w0)
   ```

2. **Update the TF variable** (the state-resident copy). Set `ghcr_auth_b64` in
   the **prod** and **stg** workstation tfvars (NOT committed;
   `terraform.tfvars.example` shows the shape) or export `TF_VAR_ghcr_auth_b64`.

3. **Reach the live box.** Either:
   - **Direct edit (cheaper):** on the box, `sudo -u deploy docker login ghcr.io`
     with the new PAT (rewrites `/home/deploy/.docker/config.json`), then a
     plain `terraform apply` to reconcile tfstate to the new variable value
     (the `user_data` diff is `ignore_changes`-masked, so apply only writes the
     stored value — it will NOT touch the box); **or**
   - **Server replace (state + box in one op):** follow the **taint/replace**
     procedure in
     [`cloud-init-template-changes.md`](cloud-init-template-changes.md)
     (snapshot → stg-first → drain prod traffic → DNS-TTL check):

     ```bash
     cd terraform/hetzner/envs/stg   # ALWAYS stg first
     terraform apply -replace='module.vps.hcloud_server.app' -var-file=terraform.tfvars
     # verify stg healthy, then repeat for envs/prod
     ```

4. **Verify:** an image pull on the box authenticates with the new cred
   (`sudo -u deploy docker pull <a private ghcr image>` succeeds; no
   `denied`/`unauthorized` from ghcr.io); old PAT revoked in GitHub → Settings →
   Developer settings. If you replaced the server, also run the
   `cloud-init-template-changes.md` § Post-replacement checklist.

---

### 3. `user_service_jwt_secret` (highest blast radius — server replace, schedule a window)

Rotating this **invalidates every live user-service session** — all logged-in
users are kicked. Schedule for a **low-traffic window** and expect to re-auth on
your next session.

Like `ghcr_auth_b64`, this is a **cloud-init-seeded** secret (user-service env,
written at first boot) and is **not** in the deploy workflow `.env` —
`deploy-prod.yml` `env_keys` carries `JWT_PRIVATE_KEY` / `JWT_PUBLIC_KEY` (the
isnad-graph asymmetric keypair, a *different* secret) but **not**
`user_service_jwt_secret`. So a redeploy does NOT refresh it, and a bare
`terraform apply` only rewrites tfstate (the `user_data` diff is
`ignore_changes`-masked). Reaching the running box requires the **taint/replace**
path, or a direct on-box rewrite of the user-service env value.

1. **Generate a new 32-byte URL-safe value** (per the deploy#126 pattern):

   ```bash
   NEW_JWT=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
   # or: openssl rand -base64 32 | tr '+/' '-_' | tr -d '='
   ```

2. **Update the TF variable** `user_service_jwt_secret` (prod + stg tfvars or
   `TF_VAR_user_service_jwt_secret`) so the state-resident copy is the new value.

3. **Reach the live box.** Either:
   - **Direct edit (cheaper, lower blast radius):** update the
     `user_service_jwt_secret` value in the user-service's env file on the box
     and restart the user-service container (coordinate with the user-service
     repo if its own pipeline owns the env injection / restart), then a plain
     `terraform apply` to reconcile tfstate to the new variable
     (`ignore_changes` masks the `user_data` diff — apply only stores the
     value, it will NOT replace the box); **or**
   - **Server replace (state + box in one op):** follow the **taint/replace**
     procedure in
     [`cloud-init-template-changes.md`](cloud-init-template-changes.md)
     (snapshot → stg-first → drain prod traffic → DNS-TTL check):

     ```bash
     cd terraform/hetzner/envs/stg   # ALWAYS stg first
     terraform apply -replace='module.vps.hcloud_server.app' -var-file=terraform.tfvars
     # verify stg healthy, then repeat for envs/prod
     ```

4. **Verify:** existing tokens are rejected (401 on a pre-rotation token), a
   fresh login issues a token signed by the new secret, user-service health is
   green. If you replaced the server, also run the
   `cloud-init-template-changes.md` § Post-replacement checklist.

---

### 4. (optional) `TF_STATE_B2_APP_KEY` — use the dedicated runbook

If you also rotate the state-bucket key as part of closing the window, do **not**
do it here — follow [`state-key-annual-rotation.md`](state-key-annual-rotation.md)
(it has the atomic stg+prod GH-Environment-secret swap and the "don't disable the
old key until CI is green" gate). Note it in #11.

## Acceptance (per #193)

- [ ] Each rotated secret has a **fresh value not present** in the pre-2026-04-30
      state.
- [ ] **The new value reached the LIVE box, not just tfstate** — verified on the
      box (e.g. pipeline workers authenticate; `/home/deploy/.docker/config.json`
      holds the new `auth`; a pre-rotation user-service token now 401s). This is
      the bullet the `ignore_changes`-masked `user_data` makes load-bearing: a
      green `terraform apply` alone does **not** prove propagation for the
      cloud-init-seeded secrets (§2, §3).
- [ ] A post-rotation `terraform plan` against **each** affected backend reports
      `No changes` (state consistent with the new secret values — note this is
      *necessary but not sufficient*; it confirms tfstate, not the live box):

      ```bash
      terraform -chdir=terraform/hetzner/envs/prod plan -input=false   # No changes
      terraform -chdir=terraform/hetzner/envs/stg  plan -input=false   # No changes
      terraform -chdir=terraform/backblaze         plan -input=false   # No changes
      ```

- [ ] `b2 ls --json --recursive --versions b2://noorinalabs-terraform-state`
      shows **zero plaintext versions** throughout (SSE-B2 only).
- [ ] Rotation timestamps recorded in [deploy#11](https://github.com/noorinalabs/noorinalabs-deploy/issues/11).

## Out of scope

- Post-leak forensics — no leak evidence (see #193 § Threat model).
- `user_postgres_password` / `user_redis_password` — already rotated (#126).
- The bucket lifecycle rule and `services.yaml` bucket-name correction — separate
  W11 issues.
- The ongoing rotation *policy* / inventory / audit trail — that is [#11](https://github.com/noorinalabs/noorinalabs-deploy/issues/11).

# Runbook — annual state-bucket key rotation

Source: [deploy#331](https://github.com/noorinalabs/noorinalabs-deploy/issues/331)
(ADR 0004 Part-2 implementation).
Strategy: [ADR 0004 Decision C](../adr/0004-b2-state-bucket-and-key-management.md#decision-c--rotation-cadence)
(annual cadence + on-demand triggers).
Composes with: [`terraform/backblaze-bootstrap/`](../../terraform/backblaze-bootstrap/README.md)
(IaC for the bucket + writer key), [`state-bucket-lifecycle.md`](state-bucket-lifecycle.md)
(bucket lifecycle spec).

## When this runs

- **Annual cadence** — once per year, owned by the **Platform Architect role**
  (survives personnel changes). See § Calendar-reminder placement below.
- **On-demand triggers** (ADR 0004 Decision C), any of:
  - operator offboarding,
  - suspected compromise / key exposure in a commit, log, or Slack message,
  - [deploy#158](https://github.com/noorinalabs/noorinalabs-deploy/issues/158)
    `validate-creds` drift detection firing.

This covers the **state-bucket key** (bucket-scoped read/write). The **master
key** (account-wide) rotates **on-demand only** per ADR 0004 Decision D — it has
no annual cadence; see that ADR for its threat model.

## Scope of a rotation

Two key classes share the state-bucket scope (ADR 0004 Decision B3):

1. **CI keys** — `TF_STATE_B2_KEY_ID` + `TF_STATE_B2_APP_KEY`, held as GitHub
   Actions Environment secrets in **both** `staging` and `production`.
2. **Per-operator keys** — each operator's `noorinalabs-tfstate-{handle}`
   workstation key.

Rotate the class(es) relevant to the trigger. Annual cadence rotates all; an
offboarding trigger rotates only the departing operator's key.

## Procedure

> Idempotency note: each step is safe to re-run. If you are resuming a
> half-completed rotation, re-read § Resuming a half-completed rotation first
> to find your place — the dangerous state is "new key minted + CI secret NOT
> yet updated" (CI still using the old key) or "old key revoked before the new
> key was verified" (CI/operators locked out).

1. **Generate a new B2 state-bucket key.** Either via the
   `backblaze-bootstrap` module (re-apply `b2_application_key.tf_state_writer`
   to mint a fresh secret — `terraform apply -replace=b2_application_key.tf_state_writer`)
   or via the B2 console / `b2 key create` with the Decision B capability set
   (`listBuckets,listFiles,readFiles,writeFiles,deleteFiles`) scoped to
   `noorinalabs-terraform-state`. Record the new keyID + secret in the password
   vault immediately; the secret is shown **once**.

2. **Update the CI secret(s)** — set BOTH GH Environments atomically so the
   [deploy#158](https://github.com/noorinalabs/noorinalabs-deploy/issues/158)
   drift check stays green:
   ```bash
   gh secret set TF_STATE_B2_KEY_ID --env staging    --body "<new key id>"
   gh secret set TF_STATE_B2_APP_KEY --env staging    --body "<new secret>"
   gh secret set TF_STATE_B2_KEY_ID --env production  --body "<new key id>"
   gh secret set TF_STATE_B2_APP_KEY --env production  --body "<new secret>"
   ```
   (Per-operator rotation instead: each operator updates their own shell
   exports / vault entry; there is no central secret to set.)

3. **`terraform init` with the new credentials.** From any state-using root
   (e.g. `terraform/backblaze/` or a hetzner env root), re-init so the backend
   authenticates with the new key:
   ```bash
   export AWS_ACCESS_KEY_ID="<new key id>"
   export AWS_SECRET_ACCESS_KEY="<new secret>"
   terraform -chdir=terraform/backblaze init -reconfigure
   ```

4. **Import the bucket resource if this is the first `backblaze-bootstrap`
   run.** Only relevant if you minted the new key via the module and its local
   state does not yet contain the bucket:
   ```bash
   terraform -chdir=terraform/backblaze-bootstrap import \
     b2_bucket.terraform_state noorinalabs-terraform-state
   ```
   Skip on subsequent runs (the bucket is already in local state). `import` is
   idempotent against an already-imported resource (it no-ops with a notice).

5. **Apply smoke — prove the new key works before revoking the old one.** Run a
   read-then-write against the bucket with the new credentials:
   ```bash
   terraform -chdir=terraform/backblaze plan   # reads state with the new key
   ```
   A clean `plan` (state read succeeds, no auth error) confirms the new key is
   live. For CI, trigger the `terraform` workflow (or wait for the next push)
   and confirm the `validate-creds` + plan jobs are green on both envs.

6. **Revoke the old key — only after Step 5 is green.** Delete the prior key in
   the B2 console (or `b2 key delete <old-key-id>`). Never revoke before the new
   key is verified live (Step 5) — that is the lockout failure mode.

7. **Record the rotation date.** Append a line to the rotation log at the bottom
   of this runbook (date, who, which class rotated, trigger). This is the
   audit trail and the input to the next annual due-date.

## Resuming a half-completed rotation

| Where you stopped | Safe state? | Resume action |
|---|---|---|
| New key minted, vault recorded, CI not yet updated | CI still on OLD key (valid) | Continue at Step 2. Old key still works; no lockout. |
| CI updated, old key NOT yet revoked | Both keys valid | Continue at Step 5 (smoke), then 6. Two live keys is safe — they share scope. |
| Old key revoked, but a CI run was mid-flight on it | Possible in-flight failure | Re-run the failed workflow; it picks up the new secret. If state was mid-write, check `b2 ls --versions` for a partial object and restore the prior version (see state-bucket-lifecycle § Acceptance over time). |
| Lost the new secret before recording it | New key unusable | Mint another (Step 1); delete the unrecorded one in the console. |

The invariant: **never have zero valid keys**. Mint-and-verify before revoke,
always.

## Calendar-reminder placement — OWNER DECISION (deferred)

ADR 0004 Decision C assigns the annual cadence to the Platform Architect
**role**, but where the durable reminder lives is an open owner decision
(deferred per #331, if-time-permits scope):

- a recurring calendar event on a shared/role calendar, or
- a scheduled GitHub Actions workflow that opens a "rotate state-bucket key"
  issue ~30 days before the due date (see § Optional automation), or
- a recurring entry in whatever the team adopts as its operational-cadence
  tracker.

Until the owner rules, the on-demand triggers above are the operative backstop
and this runbook is the procedure. **Record the decision here once made.**

## Optional automation — OWNER DECISION (deferred)

ADR 0004 Decision C4 + this issue's deliverable 3 leave open whether to wire a
scheduled GHA workflow. Two shapes, owner picks (deferred per #331):

- **Reminder-only** — a `schedule:`d workflow that opens a tracking issue 30
  days before the annual due date; a human runs this runbook. Low risk, low
  wiring.
- **Full automation** — a workflow/script that mints, updates CI secrets, polls
  CI green, then revokes (ADR 0004 Decision C4). Higher value, but the
  "verify-green-before-revoke" gate is non-trivial to make safe against an
  in-flight workflow holding the old key (PR #177 § Step 5), and it does not
  help per-operator keys. Not built here.

Neither is built in this PR; carried forward / routed to owner.

## Rotation log

| Date | Operator | Class rotated | Trigger |
|---|---|---|---|
| _(none yet — first entry on first rotation)_ | | | |

## Refs

- [ADR 0004](../adr/0004-b2-state-bucket-and-key-management.md) — Decisions B (key model), C (cadence), D (master key).
- [`terraform/backblaze-bootstrap/README.md`](../../terraform/backblaze-bootstrap/README.md) — the IaC module.
- PR #177 `terraform/backblaze/README.md` § "Rotating the state-bucket key" — the canonical per-key rotation steps this runbook generalizes.
- [deploy#158](https://github.com/noorinalabs/noorinalabs-deploy/issues/158) — CI `validate-creds` drift detection (an on-demand trigger).

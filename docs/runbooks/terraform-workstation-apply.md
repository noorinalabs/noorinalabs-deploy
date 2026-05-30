# Runbook — workstation `terraform apply` discipline

Source: [deploy#334](https://github.com/noorinalabs/noorinalabs-deploy/issues/334)
(ADR 0005 implementation).
Decision: [ADR 0005 — Terraform state-locking on the B2 backend](../adr/0005-terraform-state-locking-on-b2-backend.md).
Composes with: [ADR 0004 § Decision A2](../adr/0004-b2-state-bucket-and-key-management.md)
(B2 bucket versioning — the recovery backstop for the unlocked window),
[state-bucket-lifecycle runbook](state-bucket-lifecycle.md).

## Why this runbook exists

ADR 0005 chose **Option D** — a GitHub Actions `concurrency:` group on the
Terraform apply jobs — to serialize CI-initiated applies against the shared B2
state files. That control has one load-bearing gap, named explicitly in the ADR:

> **Operator-workstation apply bypasses the lock.** A `terraform apply` run from
> an operator's workstation does not participate in GitHub Actions concurrency.

B2's S3-compatible PUT is last-writer-wins; there is no native lock the
workstation respects (see ADR 0005 § Context for why DynamoDB, `use_lockfile`,
Postgres, and Consul backends were all rejected). So when a human runs
`terraform apply` locally, **this procedure is the only thing preventing a
concurrent-write state corruption.** Follow it every time.

This is procedural protection, not a technical control — the same shape as
ADR 0004 § Decision D's master-key constraint. It is acceptable because the
operator class is small, workstation-apply is the exception (CI is the norm),
and B2 bucket versioning makes the failure recoverable. If the discipline
fails in practice, that is an ADR 0005 **upgrade trigger** (see below).

## Who may run a workstation apply

The authorized-operator class is the infrastructure roles on the
`noorinalabs-deploy` team (`.claude/team/roster/`). As of this runbook:

| Operator | Role |
|---|---|
| Bereket Tadesse | Infrastructure Manager |
| Weronika Zielinska | Platform Architect |
| Aisha Idrissi | SRE Engineer |
| Lucas Ferreira | SRE Engineer |

Keep this table in sync with the deploy team roster — it is the source of
truth for who is in the announce-and-acknowledge loop in Step 2. If you are not
on this list, do not run `terraform apply` from your workstation; open a PR and
let CI apply.

## The 4-step procedure

Run these in order, every time, for any workstation `terraform apply` against
`terraform/hetzner/envs/{stg,prod}/`, `terraform/cloudflare/`, or
`terraform/backblaze/`.

1. **Announce intent in `#deploy` before you apply.** Post the root module and
   env, e.g. `manual apply incoming: terraform/cloudflare (prod) — adding www
   CNAME, ETA ~5min`. The announcement is what gives other operators the chance
   to say "wait, I'm mid-apply."

2. **Wait for acknowledgement from any other active operator.** If another
   operator is active, wait for an explicit ack before proceeding. **No
   acknowledgement after 10 minutes = proceed**, and post a follow-up in
   `#deploy` noting you proceeded on the timeout.

3. **Check the GitHub Actions runs page for the affected root.** Open the
   [Terraform workflow runs](https://github.com/noorinalabs/noorinalabs-deploy/actions/workflows/terraform.yml).
   If an `Apply (<root>)` job is mid-run for the root you are about to touch,
   **wait for it to finish** — the CI apply holds the concurrency group, but
   your workstation does not respect it, so you must respect it manually.

4. **Post a completion note in `#deploy` after the apply finishes.** e.g.
   `manual apply done: terraform/cloudflare (prod), no drift`. This closes the
   loop so the next operator's Step 1 announcement has accurate context.

## If you suspect a concurrent-write corruption

Symptoms: a `terraform plan` immediately after an apply shows unexpected drift
(resources you just created appear as "to add" again), a `taint` cascade, or a
partial apply that left resources live but absent from state. The B2 bucket
version count for the affected `*.tfstate` object showing two writes seconds
apart is the smoking gun (see [state-bucket-lifecycle](state-bucket-lifecycle.md)
for the object/version layout).

Recovery path (B2 bucket versioning is enabled per
[ADR 0004 § Decision A2](../adr/0004-b2-state-bucket-and-key-management.md)):

1. **Stop all applies.** Announce a freeze in `#deploy`; do not let CI or any
   workstation apply the affected root until recovery is complete.
2. **Identify the affected state object and its versions.** In the B2 console
   (or via the S3 API against the `noorinalabs-terraform-state` bucket), list
   the versions of the affected key — e.g. `cloudflare/terraform.tfstate`,
   `hetzner/prod.tfstate`. The two rapid writes are the corruption signature.
3. **Restore the pre-corruption version.** Promote the last-good version of the
   state object (the one written *before* the racing pair) back to current.
   Keep the corrupted version — do not delete it; it is evidence and may hold
   resource IDs the good version lost.
4. **Plan for drift against real infrastructure.** Run `terraform plan` for the
   affected root. The plan now reconciles restored-state vs. what is actually
   live. Expect to see the lost apply's resources as drift.
5. **Reconcile manually.** Depending on the drift, either `terraform import` the
   live-but-unstated resources back into state, or `terraform apply` to converge
   — done by a single operator, with the freeze still in place, following Steps
   1–4 above for that apply.
6. **Post a `#deploy` postmortem** and evaluate the ADR 0005 upgrade trigger: a
   corruption that happened *despite* this discipline means the discipline
   failed, which ADR 0005 names as the escalation condition to a technical
   control (Option B — HCP Terraform).

## ADR 0005 upgrade triggers (when procedure is no longer enough)

Per ADR 0005 § Decision, escalate from this procedural control to Option B
(HCP Terraform native locking) and supersede ADR 0005 if any of:

- The active TF-operator count grows past 3 (this roster already sits at 4 —
  flag at the next infra review whether the coordination cost warrants the
  upgrade now).
- Workstation-apply moves from "exception" to "weekly or more."
- A workstation-apply incident corrupts state even with this discipline in place.
- A new TF root with a higher state-corruption blast radius lands.

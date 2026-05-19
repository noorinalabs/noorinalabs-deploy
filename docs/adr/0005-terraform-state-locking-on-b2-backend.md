# ADR 0005 — Terraform state-locking strategy on the B2 backend

- **Status:** Accepted
- **Date:** 2026-05-18
- **Author:** Weronika Zielinska (Platform Architect)
- **Context issue:** [deploy#29](https://github.com/noorinalabs/noorinalabs-deploy/issues/29)
- **Related ADRs:** [0001 — Terraform Hetzner per-env state strategy](0001-tf-hetzner-per-env-state-strategy.md), [0004 — B2 state-bucket and key management](0004-b2-state-bucket-and-key-management.md)
- **Supersedes:** none
- **Superseded by:** none

## Context

Five Terraform root modules in this repo store state in the Backblaze B2 bucket `noorinalabs-terraform-state` via the `s3` backend (per ADR 0001 and ADR 0004):

- `terraform/hetzner/envs/stg/` → `hetzner/stg.tfstate`
- `terraform/hetzner/envs/prod/` → `hetzner/prod.tfstate`
- `terraform/cloudflare/` → `cloudflare/terraform.tfstate`
- `terraform/backblaze/` → `backblaze/terraform.tfstate`
- (planned via ADR 0004 Part-2) `terraform/backblaze-bootstrap/` → `local` or `bootstrap/` prefix

Every one of these backend configs uses the AWS S3 backend pointed at the B2 S3-compatible endpoint:

```hcl
backend "s3" {
  bucket   = "noorinalabs-terraform-state"
  key      = "<env>/terraform.tfstate"
  region   = "us-east-005"
  endpoints = { s3 = "https://s3.us-east-005.backblazeb2.com" }
  skip_credentials_validation = true
  skip_metadata_api_check     = true
  skip_region_validation      = true
  skip_requesting_account_id  = true
}
```

**No `dynamodb_table` is configured**, and no equivalent `use_lockfile = true` (Terraform 1.10+ S3-native lock file) is configured either. The result: every plan and apply currently runs against unlocked state.

### Why this matters now

Single-operator workflow has been the de facto protection. Phase-3 is moving the team toward a multi-operator + CI-driven workflow:

- **CI apply** lands in scope as soon as `.github/workflows/terraform.yml` matures past `plan` into `apply` for the `cloudflare/` and `hetzner/envs/*/` roots (currently `validate`-only per `ontology/repos/deploy.yaml` § `ci.terraform`).
- **Multi-operator** is the explicit posture in ADR 0004 Decision B3 — the per-operator state-bucket key model only exists because we expect more than one human to run `terraform apply` from their workstation.
- **Cross-root state dependencies.** `terraform/cloudflare/` consumes Hetzner state via `terraform_remote_state` (ADR 0004 § Context). Concurrent apply of `hetzner/envs/prod/` while `cloudflare/` is reading remote state is a corruption window today.

### What "state corruption" looks like in practice

Without locking, two simultaneous `apply` operations on the same state file:

1. Both fetch the same `terraform.tfstate` version from B2.
2. Both compute a plan, both apply their own deltas to local copies.
3. Both write back to B2; the second write silently overwrites the first.
4. Outcome: the first apply's resource changes are *applied to real infrastructure* but *absent from state*. Next plan shows them as drift; next apply tries to recreate them. Worst-case: a `taint` cascade across providers, or a partial-apply with no recoverable state file.

B2's S3-compatible PUT is last-writer-wins. There is no conditional-write semantics surfaced through the s3 backend's standard PutObject path that would let us detect this race without explicit locking.

### Backblaze does not offer a native LockFile equivalent

The S3 backend's two locking mechanisms are:

- **DynamoDB table** (`dynamodb_table = "..."`) — requires AWS DynamoDB; B2 does not implement DynamoDB.
- **S3-native lock file** (`use_lockfile = true`, Terraform 1.10+) — requires S3 conditional writes (`If-None-Match` + 412 Precondition Failed on existing lock). Backblaze's S3 API documentation lists conditional-PUT support as a Q2-2025 GA item; testing as of 2026-05 shows B2 returns the lock object but does not enforce `If-None-Match` reliably across edge cases (specifically: rapid retry windows < 5s pass through). Treating B2 as if it supports `use_lockfile` would be unsafe absent independent verification — and that verification work is itself out of scope for an ADR.

This ADR therefore considers locking *outside* the S3 backend's native mechanisms.

## Options considered

### Option A — Status quo (no locking)

Continue running unlocked state. Rely on social/process protection: "only one operator at a time, announced before apply."

- **Pros:**
  - Zero new infrastructure or code.
  - Matches today's actual behavior; no migration cost.
- **Cons — why rejected:**
  - Silent failure mode. The protection is operator memory + Slack discipline, both of which decay. The first time the discipline fails, state is corrupted with no rollback (B2 bucket versioning is enabled per ADR 0004 § Decision A2's bucket-config plan, which provides a *recovery* path — but the failure is still silent at the moment it happens).
  - Does not scale to CI apply. CI workflows have no native "ask the human if anyone else is running apply" gate.
  - Fails the "what happens when this fails?" question: corruption is silent, recovery requires B2 object-version restoration *after* drift is noticed.
  - Blocks the multi-operator posture that ADR 0004 already committed to.

### Option B — HCP Terraform (Terraform Cloud) free tier as state backend

Migrate the state backend from B2 `s3` to `cloud { organization = "noorinalabs" workspaces { name = "..." } }`. State lives in HCP. Locking is native and serialized at the workspace level.

- **Pros:**
  - Native locking, battle-tested. Same code path the rest of the Terraform community uses.
  - Free tier covers up to 5 users and 500 managed resources — sufficient for our current footprint.
  - State versioning, audit log, drift detection come along for free.
  - Removes the lock-mechanism question entirely (one less thing to maintain).
- **Cons — why rejected (for now, not forever):**
  - **Architectural inversion of ADR 0001 + ADR 0004.** Both ADRs treat B2 as the canonical state store. ADR 0004's Decisions A–D collectively codify "the team owns the state bucket; here's how." Moving state to HCP makes the bucket-and-key ADRs vestigial overnight — the per-operator B2 state-bucket keys (Decision B3) no longer have a purpose; the `backblaze-bootstrap` module (Decision A2) no longer manages state-bearing infrastructure. That's a large surface area to invalidate for what is, at this scale, a locking problem.
  - **External dependency.** State availability now requires HCP availability. B2 outages and HCP outages are independent; moving from B2 to HCP doesn't reduce dependency count, it just shifts which vendor we're betting on. (B2 has been our chosen storage vendor since the org's founding; HCP is a new vendor relationship.)
  - **Egress / data-locality story unclear.** State files contain everything from Cloudflare API tokens (in `terraform.tfstate` when sensitive outputs are present) to VPS IPs. ADR 0002 already classifies what flows through state. Moving that state to HCP means it traverses HashiCorp's infrastructure; we have no current contractual relationship documenting that boundary.
  - **Free tier cliff.** 5 users / 500 resources is comfortable today but is a cliff we'd hit as the team or infrastructure footprint grows. Paid tier introduces a recurring spend item the team has not budgeted.
  - **Migration cost.** Each of the five backend configs needs a `terraform state pull` → `cloud {}` reconfigure → `terraform state push` migration, plus updating CI workflows and operator runbooks. Real engineering hours.

  The cons are not "B is bad" — B is technically the strongest option. They're "B is a different conversation than locking." This ADR flags B as the **upgrade trigger** if and when the team scale or the workstation-apply story changes; see § Decision below.

### Option C — PostgreSQL backend

Switch from `s3` to `pg` backend. Terraform's native PostgreSQL backend supports advisory locking on the same connection. The team already operates Postgres (per `ontology/repos/deploy.yaml` § `compose_services.postgres`).

- **Pros:**
  - Native locking via `pg_advisory_lock`.
  - Postgres is already in the stack; no new vendor.
  - State versioning available via Postgres backup discipline (which we already need for application data).
- **Cons — why rejected:**
  - **Bootstrap recursion.** The Postgres instance lives on the Hetzner VPS that `terraform/hetzner/envs/prod/` provisions. The TF that needs the lock cannot lock against a database the same TF is creating. Mitigation: a separate "state-Postgres" instance, deployed out of band — which means *another* hand-managed piece of infrastructure (the exact failure mode ADR 0004 was written to close). Net infrastructure complexity increases, not decreases.
  - **Same ADR-0001/0004 inversion problem as Option B.** State leaves B2. The bucket-and-key ADRs become vestigial in the same way. Worse, the alternative state store (Postgres) introduces its own backup, rotation, and DR concerns that B2 currently handles for us.
  - **Connectivity coupling.** Every operator and every CI run now needs network reachability to the state-Postgres instance, plus credentials with `pg_advisory_lock` capability. B2 is reachable from anywhere with HTTPS; Postgres typically isn't (without exposing it on a public IP, which introduces its own threat model).
  - **Operational asymmetry.** Postgres outage → no Terraform applies anywhere. B2 outage today → same constraint, but B2 has a better historical uptime track record than a single-instance Postgres on our infrastructure.

### Option D — Application-layer locking via GitHub Actions concurrency group ← **chosen**

Add `concurrency: { group: "terraform-apply-${{ matrix.root }}-${{ matrix.env }}", cancel-in-progress: false }` to the `terraform.yml` apply jobs. GitHub serializes runs within a group; a second push to a Terraform-touching path waits for the first apply to complete before starting.

- **Pros:**
  - **Zero new infrastructure.** Built into GitHub Actions; no provider, no backend, no vendor relationship to introduce.
  - **Preserves ADR 0001 + 0004 thesis.** State stays in B2. All bucket-and-key ADRs remain load-bearing as written.
  - **Right-sized for the threat model.** The primary corruption risk today is two CI runs racing (e.g., two PRs merging to main in rapid succession, each touching `terraform/cloudflare/`). Concurrency group fully closes this risk for CI-initiated apply.
  - **Composes with the rest of the GHA workflow.** No special state-machine reasoning needed; this is a one-line addition per apply job.
  - **Cheap to reverse.** If a future ADR adopts Option B, removing the concurrency group is a one-line revert; no state migration.
- **Cons — accepted and mitigated below:**
  - **Operator-workstation apply bypasses the lock.** A `terraform apply` run from a Platform Architect's or SRE's workstation does not participate in GH Actions concurrency. This is the load-bearing limitation.
  - **Discovery latency.** A long-running apply (~5–10 minutes for a `hetzner/envs/prod/` change with VPS recreate) blocks subsequent runs even when they'd touch unrelated resources. Acceptable; we run apply rarely enough that occasional 10-minute waits are not material.
  - **Per-root granularity required.** The concurrency group must be keyed on `(root_module, env)` not just "terraform" globally — otherwise unrelated applies (cloudflare vs hetzner-stg) serialize unnecessarily. The matrix configuration in the implementation issue must reflect this.

  The operator-workstation bypass is real. The mitigation is a workstation-apply discipline runbook (defined in the follow-up implementation issue):

  1. Announce intent in `#deploy` Slack channel before manual apply; include the root module and env.
  2. Wait for explicit acknowledgement from any other active operator (no acknowledgement after 10 minutes = proceed, log in Slack).
  3. Check the GitHub Actions runs page for the affected root — if a workflow is mid-apply, wait.
  4. Append a "manual apply: <root>/<env>, <reason>" Slack message after completion.

  This is the same shape of social-protection ADR 0004 § Decision D used for the master-key constraint ("master key MUST NOT exist in CI" — a charter rule, not an enforcement). It is acceptable here for the same reason: the operator class is small (3 named individuals per `ontology/repos/deploy.yaml` § `team`), the action is rare (workstation-apply is the exception, not the norm), and the failure mode is recoverable (B2 bucket versioning per ADR 0004 § Decision A enables state restoration).

### Option E — Consul or etcd for native locking

Stand up a Consul (or etcd) cluster purely to back Terraform's `consul` backend, which has native locking.

- **Pros:** strongest locking semantics; battle-tested.
- **Cons — why rejected:**
  - Operationally heavy. A Consul cluster needs ≥3 nodes for HA, backup discipline, leader-election ops experience, and its own monitoring story. Adding Consul to the stack to solve a locking problem is several orders of magnitude over-provisioned for our team scale (current: ~2 active TF operators, 5 TF roots, single-region single-cloud footprint).
  - Same ADR-0001/0004 inversion as Options B and C, and worse: state lives in Consul rather than B2, with no existing institutional knowledge of Consul operations on the team.
  - Cost-to-benefit math fails at every tier the team has been at or will be at in Phase 3.

## Decision

Adopt **Option D — GitHub Actions concurrency group** for CI-initiated Terraform apply, paired with a workstation-apply discipline runbook for operator-initiated apply.

### Specifics

- **Concurrency-group key**: `terraform-apply-${{ matrix.root }}-${{ matrix.env }}` (per-root, per-env granularity).
- **cancel-in-progress**: `false`. A second apply queues behind the first; it does not cancel it. Mid-apply cancellation is dangerous on its own (partial state writes); the queue-and-wait shape matches the lock semantics we want.
- **Scope**: applies only. `terraform plan` and `terraform validate` jobs do not need locking (they don't write state); leaving them unlocked preserves PR-feedback parallelism.
- **Workstation-apply runbook**: separately delivered with the implementation; the 4-step Slack-coordination procedure described under Option D § Cons.

### Upgrade trigger (when to revisit Option B)

This decision is right-sized for today and bounded by the workstation-apply discipline. Revisit and adopt Option B if any of:

- The active TF-operator count grows past 3 (workstation-apply discipline coordination cost scales super-linearly with operator count).
- Workstation-apply moves from "exception" to "weekly or more" (the discipline runbook becomes routine friction, not a backstop).
- A workstation-apply incident corrupts state, even with the discipline in place (the discipline failed — escalate to a technical control).
- A new TF root with a higher state-corruption blast radius lands (e.g., something managing customer data, which we don't have today but might in Phase 4).

The trigger conditions are recorded here so a future ADR proposing Option B has a pre-agreed evidence threshold rather than re-litigating the architecture.

## Consequences

### Positive

- **CI-initiated state corruption is fully closed.** The primary threat vector — two PRs merging in rapid succession both touching the same TF root — is impossible with the concurrency group in place.
- **ADR 0001 + ADR 0004 remain load-bearing.** State stays in B2; the bucket-and-key work this team has done is preserved and extends naturally.
- **Zero new infrastructure.** No Consul, no Postgres-for-state, no HCP relationship. The locking gain costs one line of YAML per apply job.
- **Cheaply reversible.** If the upgrade trigger fires later, removing the concurrency group is trivial and does not constrain the Option B migration path.
- **Composes with existing ontology.** `ontology/repos/deploy.yaml` § `ci.terraform` will need a one-field addition documenting the locking mechanism; no other ontology surface is affected.

### Negative / ongoing costs

- **Operator-workstation apply is unlocked.** The mitigation is procedural (Slack announce + runbook), not technical. This is the load-bearing limitation; the upgrade trigger explicitly names it as the escalation path if discipline fails.
- **Per-root concurrency-group keys add implementation complexity.** The matrix expansion must encode `(root, env)`; getting the key wrong (too coarse → unnecessary serialization; too narrow → no protection) is a foot-gun. The implementation issue's CI changes must include a smoke test that two concurrent runs on the same `(root, env)` do serialize.
- **No protection against malicious/buggy concurrent applies that bypass the group.** A workflow that omits the `concurrency:` stanza in the future would silently regain the corruption window. Mitigation: a lint rule or CODEOWNERS requirement on `.github/workflows/terraform.yml` changes; defer to the implementation issue.
- **Discipline runbook is the weakest possible enforcement.** Same caveat as ADR 0004 § Decision C's annual-rotation reminder: humans can ignore Slack announcements. The upgrade trigger acknowledges this and pre-commits the team to escalate if the discipline fails.

### Failure modes explicitly considered

| Question | Answer |
|---|---|
| What happens if two PRs touching `terraform/cloudflare/` are merged 10 seconds apart? | Both workflows enter the `terraform-apply-cloudflare-prod` concurrency group. The second waits for the first to complete; runs sequentially. Net effect: serialized applies, no state race. |
| What happens if a CI apply is mid-run and an operator runs `terraform apply` from their workstation against the same root? | The CI apply holds no advisory lock the workstation respects; the workstation apply proceeds. Last-writer-wins on B2. The discipline runbook is the only protection. Mitigation if this fires: B2 bucket versioning (ADR 0004 § Decision A2 bucket config) enables state-file restoration from the prior version; the affected resources require a follow-up `terraform plan` to verify drift and a manual reconciliation. Add a `#deploy` Slack postmortem entry and consider whether the upgrade trigger has fired. |
| What happens if two operators run `terraform apply` against the same root from their own workstations at the same time? | Same outcome as the previous row. The discipline runbook is the protection (announce + acknowledge + wait). Detection: B2 bucket version count for the affected `terraform.tfstate` object will show two writes in rapid succession; if this is detected post-hoc (e.g., during DR rehearsal), it is evidence the discipline failed and the upgrade trigger should be evaluated. |
| What happens if the GH Actions concurrency group is misconfigured (e.g., key collision between two unrelated roots)? | Unrelated applies serialize unnecessarily; performance regression, not a correctness regression. Detection: workflow run-time anomaly; correctable in a follow-up PR. The smoke test from the implementation issue's CI changes will catch the wrong-key shape before merge. |
| What happens if GitHub Actions concurrency itself has an outage or bug that allows two queued runs to start simultaneously? | The protection degrades to the same posture as Option A. This is a vendor-availability risk we accept; GH Actions concurrency has been stable since launch (2021) and a documented failure mode would be a wide-blast-radius event for many GitHub-using teams (not just us). |
| What happens if a future PR adds a new TF root without the `concurrency:` stanza? | Silent regression to Option A for that root. Mitigation: the implementation issue includes adding a CODEOWNERS rule or a lint check on `.github/workflows/terraform.yml`; the exact mechanism is the implementer's choice (CODEOWNERS is cheaper; an actual lint catches drift in untouched files; the implementer picks). |
| What happens if B2 bucket versioning is disabled (today, or later, by drift)? | The "B2 versioning enables state restoration" mitigation collapses. Workstation-apply discipline becomes the only protection. ADR 0004 § Decision A2 places bucket versioning under IaC management; once that lands, versioning-disabled would show as a `terraform plan` diff. Until then (Part-2 of ADR 0004), versioning is a console-set value vulnerable to silent drift. |
| What happens if the upgrade trigger fires (e.g., 4th active operator joins)? | File a new ADR superseding this one's Decision section, proposing Option B (HCP Terraform). The architectural inversion cost named under Option B's Cons becomes the migration plan: each of the five backend configs needs a `terraform state pull` → reconfigure → `state push` migration, with a dedicated freeze window. Pre-committing to the trigger here means the future ADR is a migration plan, not a debate. |

## Implementation deferred

This ADR defines the locking strategy. Implementation is **deferred to a follow-up issue** (filed alongside the PR that lands this ADR), following the same pattern as ADR 0004 → deploy#331.

Scope of the follow-up:

1. Add `concurrency:` stanza to apply jobs in `.github/workflows/terraform.yml` (or whichever workflow will host the apply matrix when CI apply lands).
2. Encode the per-root, per-env concurrency-group key.
3. Add the workstation-apply discipline runbook at `docs/runbooks/terraform-workstation-apply.md`.
4. Add the CODEOWNERS or lint protection against new roots regressing to no-locking.
5. Add a CI smoke test (two same-key runs serialize, two different-key runs parallelize).
6. Update `ontology/repos/deploy.yaml` § `ci.terraform` with the locking-mechanism field.

The implementation is straightforward but multi-step; it is sized for its own PR rather than being bundled into this ADR.

## Refs

- [deploy#29](https://github.com/noorinalabs/noorinalabs-deploy/issues/29) — this ADR's context issue.
- [ADR 0001](0001-tf-hetzner-per-env-state-strategy.md) — per-env state strategy in B2; this ADR preserves that thesis.
- [ADR 0004](0004-b2-state-bucket-and-key-management.md) — B2 state-bucket IaC management + key model; this ADR composes with Decision A2's bucket-versioning plan (the recovery backstop for the workstation-apply unlocked window).
- [Backblaze B2 S3-compatible API docs — conditional writes](https://www.backblaze.com/docs/cloud-storage-s3-compatible-api) — the basis for the § Context note that B2's `If-None-Match` enforcement is not yet reliable enough to lean `use_lockfile` on.
- `noorinalabs-main:ontology/repos/deploy.yaml` § `ci.terraform` — current CI surface; implementation issue will extend with a locking-mechanism field.
- **Implementation issue (to be filed at PR-creation time):** `tech-debt(infra): implement ADR 0005 state-locking decision (#29 follow-up)` — workflow concurrency, workstation runbook, CODEOWNERS/lint, smoke test, ontology update.

# ADR 0004 — B2 state-bucket IaC-management + state-key rotation strategy

- **Status:** Accepted
- **Date:** 2026-05-18
- **Author:** Weronika Zielinska (Platform Architect)
- **Context issue:** [deploy#180](https://github.com/noorinalabs/noorinalabs-deploy/issues/180)
- **Lineage:** follow-up to [deploy#93](https://github.com/noorinalabs/noorinalabs-deploy/issues/93) / [PR #177](https://github.com/noorinalabs/noorinalabs-deploy/pull/177) (operator runbook for `terraform/backblaze/`) and [deploy#172](https://github.com/noorinalabs/noorinalabs-deploy/issues/172) (SSE-B2 verification)
- **Related ADRs:** [0001 — Terraform Hetzner per-env state strategy](0001-tf-hetzner-per-env-state-strategy.md), [0002 — Hetzner module outputs classification gate](0002-hetzner-outputs-classification.md)
- **Supersedes:** none
- **Superseded by:** none

## Context

`noorinalabs-terraform-state` is the Backblaze B2 bucket that holds every Terraform state file in this repo:

- `hetzner/stg.tfstate` and `hetzner/prod.tfstate` (per-env Hetzner VPS state, per ADR 0001)
- `cloudflare/terraform.tfstate` (Cloudflare DNS + zone settings; consumes Hetzner state via `terraform_remote_state`)
- `backblaze/terraform.tfstate` (the pipeline-bucket + scoped-key module — itself state-stored in the same bucket whose creation it *cannot* manage)

Five backend configs across `terraform/hetzner/envs/{stg,prod}/backend.tf`, `terraform/cloudflare/{main,versions}.tf`, and `terraform/backblaze/versions.tf` all reference the same bucket name. The bucket is the load-bearing root of every TF apply in this project.

Two architectural gaps were codified as the status quo by PR #177 (the operator runbook for `terraform/backblaze/`) without an explicit decision record:

1. **The bucket itself is not IaC-managed.** It was created out-of-band in the B2 console. There is no source of truth for its name, region, lifecycle policy, versioning state, default-SSE setting, or object-lock configuration anywhere in the repo. The chicken-and-egg is real: the bucket holds state for the modules that would otherwise manage it. If we lose the bucket (or need to rebuild in DR), the recovery path today is "remember what knobs the operator set in the console four waves ago." [deploy#172](https://github.com/noorinalabs/noorinalabs-deploy/issues/172) verified SSE-B2 is enabled — that fact lives in the issue's verification comment, not in any reconcilable artifact.

2. **The state-bucket key has no rotation cadence, no ADR, and an undocumented choice of model.** PR #177's `terraform/backblaze/README.md` introduces a per-operator naming convention (`noorinalabs-tfstate-{operator-handle}`) and a per-operator workstation provisioning model. It works. But it is a per-operator long-lived secret with no TTL, no documented rotation interval, and no recorded rationale vs the alternatives (shared single key in a password manager, short-lived TTL keys via `b2 key create --duration`, CI-only rotated keys with operators on a separate read-only debugging key).

ADR 0001 says under "What happens if backend credentials rotate?": *"Same credentials are used for both envs … Rotation is a single-step operation documented in the README."* The runbook documents *creation*; rotation appeared in PR #177 (`terraform/backblaze/README.md` § "Rotating the state-bucket key") but the *strategy* (cadence, ownership, model selection) was deliberately deferred to this ADR per the PR-177 review thread.

Adjacent: [deploy#158](https://github.com/noorinalabs/noorinalabs-deploy/issues/158) addresses *detection* of rotation drift between envs (CI `validate-creds` job). This ADR addresses *strategy* — what the rotation cadence and key-management model **should be**.

The decision needed to be recorded now, while only ~2 operators have provisioned their per-operator state-bucket keys per the new runbook. Every additional operator who follows the runbook commits more muscle memory to the per-operator model; undoing it later is structurally cheaper today than after a six-operator team has built up around it.

## Decisions

This ADR records four decisions (A–D, per the issue body). Each follows the ADR 0001 pattern: enumerate alternatives, pick one, and justify on the "what happens when this fails?" axis.

### Decision A — `noorinalabs-terraform-state` bucket IaC-management

**Adopt a `terraform/backblaze-bootstrap/` root module with a `local` backend, executed once-per-DR-event, that manages the state bucket itself.**

#### Options considered

##### Option A1 — leave the bucket console-managed (status quo)

Document the console settings as prose in `terraform/backblaze/README.md` and accept "operator memory" as the source of truth.

- **Pros:** zero new code. No chicken-and-egg.
- **Cons — why rejected:**
  - DR recovery is "remember what knobs were set" — exactly the failure mode that pushed the team toward TF for everything else.
  - SSE-B2 enabling, lifecycle rules, versioning — all invisible to PR review. Drift between what the bucket *actually does* and what the team *thinks it does* is the high-likelihood failure.
  - Future bucket-configuration changes (e.g., enabling object-lock for a compliance posture) have no review surface — a single operator with console access can change the production state-bucket's lifecycle policy with no PR, no diff, no notification.
  - Fails the "what happens when this fails?" question: bucket deletion or accidental config change is unrecoverable without operator memory; no plan can be generated against the production state-bucket.

##### Option A2 — separate `terraform/backblaze-bootstrap/` root with `local` backend ← **chosen**

A new root module at `terraform/backblaze-bootstrap/` declares `b2_bucket.terraform_state` (and any future state-bucket peers — e.g., a separate bucket for non-TF artifacts the team chooses to centralize later). Its `terraform.tfstate` is stored in a local file, committed to the repo at `terraform/backblaze-bootstrap/terraform.tfstate.encrypted` after `git-crypt`/`sops` encryption, OR uploaded to a separate "bootstrap state" object inside `noorinalabs-terraform-state` itself under a `bootstrap/` prefix (chicken-and-egg accepted: the bootstrap module's state lives inside the very bucket it manages, but it is only read during DR or bucket-config changes — a once-per-quarter event, not a per-PR event).

- **Pros:**
  - Bucket configuration becomes a reviewable diff. Adding object-lock, changing lifecycle, enabling versioning — all show up in `terraform plan` like any other change.
  - DR recovery becomes "clone the repo, decrypt the bootstrap state, `terraform apply`" — single deterministic procedure rather than tribal knowledge.
  - The chicken-and-egg is bounded: the bootstrap module only needs to run for bucket-config changes or DR, not for every TF apply. The day-to-day apply path (every other TF root in the repo) is unaffected.
  - Composes with Decision B's per-operator-state model: bootstrap is an operator-driven once-per-cycle event with a master key, same shape as `terraform/backblaze/` apply.
- **Cons:**
  - Bootstrapping a fresh bucket from a true-zero state requires the operator to either (a) bootstrap with `local` backend, then `terraform import` the live bucket and migrate state to a `bootstrap/` prefix in the bucket itself, or (b) keep the state encrypted-in-repo permanently. Both are defensible; the choice is deferred to the Part-2 implementation issue.
  - One more root module to maintain. Mitigated: it is small (~30 lines, one `b2_bucket` resource and its config), and changes rarely.

##### Option A3 — `terraform import` exercise documented in a runbook (no IaC)

Document a "to reconstruct the bucket, run these `b2 bucket create` + `b2 update-bucket` commands, then `terraform import` everything" procedure. No persistent IaC.

- **Pros:** less code than A2.
- **Cons — why rejected:**
  - Runbook drift is the failure mode the team has tried to design out everywhere else (see ADR 0003's note on `taint`/`replace` runbooks being acceptable only because they have a known cost; bucket-config drift has no such guardrail).
  - The runbook still doesn't make config changes reviewable — it's import-once-and-pray, with no ongoing reconciliation.
  - Fails the "what happens when this fails?" question identically to A1: the runbook can lie or rot, and there's no plan to compare against.

#### Decision

Adopt **Option A2**: a `terraform/backblaze-bootstrap/` root with a `local` backend (or `bootstrap/` prefix in the state bucket itself; choice of state-storage mechanism deferred to Part-2 implementation issue). Bucket configuration becomes a reviewable artifact; the chicken-and-egg is bounded to a once-per-cycle event.

### Decision B — state-bucket key model

**Adopt the existing hybrid model — per-operator long-lived workstation keys + CI-environment-scoped long-lived keys — and codify it as the explicit decision (not the accidental status quo).**

#### Current shape (de facto)

Per PR #177's runbook and `.github/workflows/terraform.yml`:

- **CI** uses two GitHub Actions Environment secrets per env: `TF_STATE_B2_KEY_ID` and `TF_STATE_B2_APP_KEY`, scoped to GH Environments `staging` and `production`. Both envs hold the *same* key value today (per ADR 0001's "same credentials are used for both envs" line). Rotation requires updating both atomically — that risk is what [deploy#158](https://github.com/noorinalabs/noorinalabs-deploy/issues/158) tracks.
- **Operators** each provision their own `noorinalabs-tfstate-{operator-handle}` key from the B2 console, scoped to the state bucket, and export it as `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` in their workstation shell.

CI and operators currently use *different keys*, both with the same scope (state-bucket-only) and same capabilities (`listBuckets,listFiles,readFiles,writeFiles,deleteFiles`).

#### Options considered

##### Option B1 — single shared state-bucket key in 1Password

One key. Stored in a shared password vault. Both CI secrets and operator shells use the same `keyID` + `applicationKey`.

- **Pros:**
  - One key to rotate; rotation drift between envs is impossible by construction.
  - Simplest mental model.
- **Cons — why rejected:**
  - **Worst-case blast radius.** A single compromise (one operator's shell history, one accidental commit, one logged-in laptop) exposes the credential for the entire team and CI. Revocation requires immediate rotation across every operator and both CI envs simultaneously.
  - Audit attribution is impossible. A "this key did X" log entry in B2 maps to "someone on the team or CI did X" — no per-actor traceability when investigating an incident.
  - Onboarding/offboarding pressure: when an operator leaves the team, the *only* response is full key rotation. With per-operator keys, the response is "delete that operator's key" — no team-wide disruption.
  - Fails the "what happens when this fails?" question on detection: a stolen shared key looks identical to legitimate use; per-operator keys at least correlate to a specific actor.

##### Option B2 — short-lived TTL keys via `b2 key create --duration`

Every operator session (and CI run) mints a fresh key with a duration of a few hours, uses it, and lets it expire.

- **Pros:**
  - Compromise window is bounded by the TTL.
  - No persistent secret on disk.
- **Cons — why rejected:**
  - **Bootstrap recursion.** Minting a state-bucket key requires the master b2 key (account-wide `writeKeys` capability). That makes the master key the new "long-lived secret to protect" — Decision D's threat model now governs the actual blast radius, and we haven't reduced anything; we've just pushed the trust boundary.
  - Operational cost: every `terraform init` becomes a key-minting CLI round-trip plus an export. CI minutes go up. Operator friction goes up. The team is small (~2 active TF operators); per-apply minting is not justified by the threat model.
  - Does not compose with the GH Actions secret model (CI secrets are static; rotating-on-every-run requires a `pre-apply` step that calls `b2 key create` with the master key already present in CI — which means the master key now lives in CI, *worse* than today).
  - Fails the "what happens when this fails?" question on operational continuity: a minted short-lived key that expires mid-apply leaves a half-applied state file with no rollback path.

##### Option B3 — per-operator long-lived workstation keys + CI-environment long-lived keys (status quo, hybrid) ← **chosen**

What we have today. Each operator owns one named state-bucket key; CI owns one per GH Environment; rotation is on-demand or on-cadence (Decision C).

- **Pros:**
  - **Bounded blast radius per compromise.** An operator's laptop compromise exposes one named key with bucket-only scope; revocation is "delete that one key in the console" with no team-wide disruption.
  - **Per-actor audit attribution.** B2's per-key access logs map to operator handle or CI environment — useful in any post-incident investigation.
  - **Onboarding/offboarding is mechanical.** New operator → mint their key, follow runbook. Departing operator → delete their key. No cross-team coordination.
  - **No master-key escalation in CI.** CI's state-bucket key cannot mint other keys; the threat model is bounded by the bucket scope.
  - Composes with the existing PR-177 runbook with zero additional operator burden — what the team is already doing becomes the explicitly chosen design.
- **Cons:**
  - More keys to track. Mitigated by the `noorinalabs-tfstate-{operator-handle}` naming convention — every key is self-identifying in the B2 console.
  - CI keys are long-lived even though CI is a closed system; theoretical compromise (e.g., a malicious workflow PR that exfiltrates the secret) is mitigated only by repo write-access protection, not by key lifetime.
  - Rotation cadence is not automatic; requires Decision C to be load-bearing.

##### Option B4 — read-only debugging key for operators + write-capable CI-only key

Operators get a `listFiles,readFiles`-only key (suitable for `terraform plan` and state inspection but not `apply`). CI is the only path with `writeFiles,deleteFiles` capability.

- **Pros:**
  - Operator workstation compromise cannot corrupt state.
  - Clear separation: humans plan, machines apply.
- **Cons — why rejected:**
  - Breaks the existing "operator can run `terraform apply` locally for emergency response" pattern. The team is small and on-call response sometimes requires a workstation apply (e.g., reverting a CI-applied change when CI itself is the source of the problem).
  - Today's `terraform/backblaze/` runbook *requires* an operator workstation apply — it's `NOT applied in CI` per the README. A read-only operator key cannot run that module at all.
  - Adds two key-classes per operator to provision and track for marginal threat-model improvement over B3.

#### Decision

Adopt **Option B3**: per-operator long-lived workstation keys + CI-environment long-lived keys (hybrid). Codify the choice; today's runbook becomes the canonical implementation of an explicit decision rather than an accidental convergence.

### Decision C — rotation cadence

**Annual rotation, scheduled via a calendar-reminder runbook owned by the Platform Architect role; on-demand rotation triggered by any of: operator offboarding, suspected compromise, key exposure in a commit/log, or [deploy#158](https://github.com/noorinalabs/noorinalabs-deploy/issues/158) drift detection.**

#### Options considered

##### Option C1 — on-demand only (no cadence)

Rotate when there's a reason (compromise suspicion, operator departure, etc.); otherwise leave alone.

- **Pros:** zero recurring operational cost.
- **Cons — why rejected:**
  - Compliance posture: "we rotate when we feel like it" fails the most basic audit question.
  - Stale-key risk grows with age — a 4-year-old key has been in 4 years' worth of shell histories, laptop backups, screen-recordings, etc.
  - No forcing function to exercise the rotation runbook. The first real rotation under pressure (post-incident) is the rotation that discovers the runbook is broken.

##### Option C2 — 90-day rotation ← rejected

Quarterly rotation, calendar-driven.

- **Pros:** strong compliance posture; rotation runbook stays exercised.
- **Cons — why rejected:**
  - Cost-to-benefit math doesn't carry. The team is ~2 active TF operators + 2 CI envs = 4 keys to rotate per cycle. Quarterly = 16 key rotations/year. Each rotation is a 5-step procedure (PR #177 § "Rotating the state-bucket key") that takes 10–15 minutes if uneventful, plus the CI re-trigger window.
  - The threat model doesn't justify quarterly for our scale. We're not handling regulated data; B2 keys are scoped to a single bucket containing public infrastructure metadata (the SSE-B2 verification in [deploy#172](https://github.com/noorinalabs/noorinalabs-deploy/issues/172) covers the at-rest exposure already).

##### Option C3 — annual rotation + on-demand triggers ← **chosen**

Calendar reminder on the Platform Architect's calendar, fires once per year. On-demand rotation triggered by: operator offboarding, suspected compromise, accidental exposure, or [deploy#158](https://github.com/noorinalabs/noorinalabs-deploy/issues/158) drift detection. Each trigger executes the PR #177 `terraform/backblaze/README.md` § "Rotating the state-bucket key" procedure.

- **Pros:**
  - Rotation runbook gets exercised at least once a year per key — sufficient to catch runbook rot before a real incident.
  - Calendar reminder is durable; assigning it to a role (Platform Architect) rather than a person makes it survive personnel changes.
  - On-demand triggers cover the real threat-model events; the cadence is a backstop for the slow-decay risk.
  - Matches the threat model: state-bucket-scoped keys protecting non-regulated public-infrastructure metadata don't need quarterly cycling.
- **Cons:**
  - A calendar-reminder runbook is the weakest possible enforcement (humans can ignore reminders). The Part-2 implementation issue should consider whether a GitHub Actions scheduled workflow that opens a "rotation due" issue is worth the wiring — left to implementer judgment.
  - Annual cadence still leaves an 11-month window where a leaked key remains valid if the leak goes undetected. Accepted as proportional to the threat model.

##### Option C4 — automated rotation tooling

A script (`infra/scripts/rotate-b2-state-key.sh`) that mints a new key, updates CI secrets via `gh secret set`, verifies CI green, then deletes the old key — all in one command.

- **Pros:** removes the manual-procedure friction.
- **Cons — deferred, not rejected:**
  - The script doesn't help operator-workstation keys (each operator must still update their own shell exports / password vault).
  - The "verify CI green before deleting" step requires polling and `gh run watch`, which is non-trivial to get right (the procedure has to be safe against an in-flight workflow holding the old key — see PR #177 § Step 5).
  - **Disposition:** in scope for the Part-2 implementation issue as an optional enhancement. The annual cadence decision stands regardless of whether the rotation is scripted or manual.

#### Decision

Adopt **Option C3**: annual cadence + on-demand triggers. Calendar reminder owned by the Platform Architect role. Procedure is the PR #177 § "Rotating the state-bucket key" runbook. Automation is a Part-2 implementation question.

### Decision D — master b2 key (`TF_VAR_b2_*`)

**Per-operator long-lived master keys (same shape as Decision B3) + on-demand rotation only (no cadence). Master key MUST NOT exist in CI.**

#### Context

The master b2 key has account-wide `writeBuckets` + `writeKeys` capabilities (per `terraform/backblaze/README.md` § "Provisioning the master key"). It is used only by the b2 provider in `terraform/backblaze/main.tf` (and in the new `terraform/backblaze-bootstrap/` from Decision A), and only when applying those modules. Per the runbook: *"NOTE: This module is not applied in CI — run it once, manually, from an operator workstation."*

#### Threat model — how it differs from the state-bucket key

| Axis | State-bucket key (Decision B) | Master key (Decision D) |
|---|---|---|
| Capability | bucket-scoped read/write on one bucket | account-wide: create/delete buckets, mint/revoke keys |
| Lifetime in shell | exported for every TF session | exported only for `terraform/backblaze*` apply (rare) |
| Exists in CI? | yes (GH Actions Environment secrets) | **NO** — operator workstation only |
| Compromise blast radius | exfiltrate/corrupt one bucket's state | full B2 account compromise: create/delete any bucket, mint keys with any scope, exfiltrate all bucket contents |
| Mitigation today | bucket scope; SSE-B2 at-rest | "only in operator shells during a bootstrap apply" + master-key-displayed-once B2 console policy |

The master key is structurally *more dangerous per credential* (full account access) but structurally *less exposed* (operator-shell-only, rare-use). The two factors roughly cancel for everyday risk but diverge sharply for the cadence question.

#### Options considered

##### Option D1 — apply Decision B + C uniformly to the master key

Per-operator + annual cadence, same as the state-bucket key.

- **Pros:** symmetry; one rotation procedure to remember.
- **Cons — why rejected:**
  - Annual cadence makes little sense for a credential that is exported for ~5 minutes once or twice per year (Decision A's `backblaze-bootstrap` apply + the existing `backblaze/` pipeline-key apply). The credential spends 99.99% of its existence in the password manager, not in a shell. The bulk of the threat model is "password manager compromise" or "B2 console compromise," neither of which annual rotation meaningfully addresses.
  - 16 master-key rotations/year (4 operators × Decision A + Decision B's bootstrap module ÷ wishful thinking) becomes operational noise that competes for attention with the genuinely-cadenced state-bucket rotation.

##### Option D2 — per-operator long-lived + on-demand rotation only ← **chosen**

Each operator mints a master key once, stores it in their password manager, rotates on-demand only. Triggers: operator offboarding, suspected compromise, B2 console compromise notification.

- **Pros:**
  - Aligns rotation cost with actual exposure (rare-use credential → rare-rotation).
  - Per-operator preserves audit attribution and bounded compromise blast radius — same wins as Decision B3.
  - **Hard constraint preserved: master key MUST NOT exist in CI.** This is the load-bearing protection; cadence is secondary.
- **Cons:**
  - The master key's age can grow large without rotation. Accepted: the rare-use posture means the credential's exposure surface is dominated by storage (password manager) rather than runtime, and the storage threat is independent of credential age.

##### Option D3 — shared single master key in 1Password (team-wide)

One master key, shared via password vault.

- **Pros:** simplest mental model.
- **Cons — why rejected:**
  - Same blast-radius problem as Decision B1, amplified by the master key's account-wide capability. A single compromise enables full B2 account takeover.
  - Compounds with the rare-use posture: a shared master key compromised today may not be detected for months because nobody is logging into B2 with it regularly.

##### Option D4 — short-lived master keys

Mint a master key, use it for one apply, delete it.

- **Pros:** smallest possible compromise window.
- **Cons — deferred, not rejected:**
  - Adds operational friction to the already-rare bootstrap-apply path.
  - Worth considering as an enhancement in the Part-2 implementation issue if `b2 key create --duration` covers the master-key capability set (TBD: it may not — short-lived keys historically have capability restrictions on B2).
  - **Disposition:** revisit in Part-2 if the operational story is cleaner than the current "manually delete in console after apply" pattern.

#### Decision

Adopt **Option D2**: per-operator long-lived master keys, on-demand rotation only, **master key MUST NOT exist in CI**. The hard constraint is the load-bearing protection; the rotation cadence is intentionally less aggressive than Decision C because the threat model differs.

## Consequences

### Positive

- **DR posture upgraded.** Decision A means the state bucket's configuration is reviewable and reconstructible from source. The current "operator memory" failure mode is closed.
- **Key model is explicit, not accidental.** Decision B codifies what the team is already doing as a deliberate choice, with the rationale recorded for future operators who would otherwise have to infer it from the runbook.
- **Rotation has a forcing function.** Decision C's annual calendar + on-demand triggers ensure the rotation runbook is exercised at least once per year per key, catching runbook rot before a real incident.
- **Master-key threat model is recorded.** Decision D's "master key MUST NOT exist in CI" constraint is now a quoted ADR line rather than a runbook implication, which makes future PRs that propose CI master-key usage (e.g., "let's automate `terraform/backblaze/` in CI") have an explicit gate to clear.
- **Composes with [deploy#158](https://github.com/noorinalabs/noorinalabs-deploy/issues/158).** The CI `validate-creds` job becomes the detection layer for rotation-drift; this ADR is the strategy layer. Together they close the rotation gap end-to-end.
- **Composes with [deploy#172](https://github.com/noorinalabs/noorinalabs-deploy/issues/172).** SSE-B2 verification is the at-rest control; this ADR records the access-control + rotation strategy. Bucket-side and key-side defenses are now both load-bearing.

### Negative / ongoing costs

- **Decision A adds one more root module** (`terraform/backblaze-bootstrap/`). Maintenance is low (~30 lines, changes rarely), but it is one more `terraform init` target operators must know about. Mitigated by clear documentation in the Part-2 implementation issue.
- **Decision A leaves state-storage choice for `backblaze-bootstrap` deferred** to Part-2. Either `local` backend with encrypted-in-repo state, or a `bootstrap/` prefix in the bucket itself, are defensible; the implementer chooses with the tradeoff written up.
- **Decision B3's per-operator CI keys** are long-lived. Theoretical compromise via a malicious workflow PR is mitigated only by repo write-access protection, not by key lifetime. Accepted — the alternative (short-lived CI keys minted by a master key in CI) is worse, because it puts the master key in CI, violating Decision D's hard constraint.
- **Decision C's calendar reminder is the weakest possible enforcement.** Humans can ignore reminders. The Part-2 implementation issue should consider whether a scheduled GHA workflow opening a "rotation due" issue is worth the wiring; left to implementer judgment.
- **Decision D's "no rotation cadence" for the master key** means a stale master key can live in a password vault for years. Accepted because the master key is rare-use; the storage-threat dominates the runtime-threat, and storage-threat is age-independent.

### Failure modes explicitly considered

| Question | Answer |
|---|---|
| What happens if an operator's state-bucket key is leaked (committed to a repo, posted in Slack)? | Delete that named key in the B2 console; mint a replacement per the PR #177 runbook. Per-operator naming limits blast radius to that operator's apply path; CI is unaffected. Triggers Decision C's "on-demand" rotation path. |
| What happens if the shared CI state-bucket key is leaked? | Treated as a "suspected compromise" event under Decision C. Rotate both `staging` and `production` GH Environment secrets atomically per the runbook Step 2; the [deploy#158](https://github.com/noorinalabs/noorinalabs-deploy/issues/158) `validate-creds` job catches any half-applied rotation on the next CI run. |
| What happens if the master key is leaked? | Full B2 account compromise scenario. Procedure: (1) immediately delete the leaked master key in B2 console, (2) mint replacement, (3) audit `b2 list-keys` and `b2 ls --recursive` for anomalies, (4) consider whether any scoped keys minted by the master key need rotation as well (worst case: all of them, including pipeline_rw / pipeline_ro). The Decision D "master key NOT in CI" constraint means the compromise surface is bounded to an operator workstation, not a CI environment. |
| What happens if the bucket is accidentally deleted? | Decision A's `backblaze-bootstrap` module enables `terraform apply` reconstruction. State recovery still requires a separate path (bucket-versioning / snapshot restoration if SSE-B2 retained the objects; otherwise re-`terraform import` of every infrastructure resource). The bucket-recovery half is now load-bearing; the state-file-recovery half is a separate concern flagged for the Part-2 issue. |
| What happens if Decision A's `backblaze-bootstrap` state file is itself lost? | Re-`terraform import` the bucket's current config into a fresh state. The runbook for this is part of the Part-2 implementation deliverable. One-shot recovery, not a recurring concern. |
| What happens if an operator leaves the team? | Delete their named state-bucket key and master key (if minted) from the B2 console. No other rotation required — bounded blast radius per Decision B3 + D2. Onboarding the replacement is the PR #177 runbook applied to a new operator handle. |
| What happens if [deploy#158](https://github.com/noorinalabs/noorinalabs-deploy/issues/158) fires the drift alarm? | Triggers Decision C's on-demand rotation path. Both env secrets get updated atomically per the runbook; the drift alarm's purpose is to catch the case where they did *not* get updated atomically. |
| What happens if the annual calendar reminder is missed? | Backstop is the on-demand triggers (Decision C). The threat model accepts an extended window because the per-operator scope of Decision B3 means a missed-rotation impact is bounded to that operator's exposure window, not the team's. |
| What happens if a future ADR wants to put the master key in CI (e.g., automate `terraform/backblaze/` apply)? | This ADR's Decision D is the gate. A future ADR proposing master-key-in-CI must explicitly supersede Decision D and justify the blast-radius change. The constraint is recorded here so it cannot be silently traded away. |

## Refs

- [deploy#180](https://github.com/noorinalabs/noorinalabs-deploy/issues/180) — this ADR's context issue.
- [deploy#93](https://github.com/noorinalabs/noorinalabs-deploy/issues/93) — operator runbook gaps (closed by PR #177).
- [deploy#177](https://github.com/noorinalabs/noorinalabs-deploy/pull/177) — `terraform/backblaze/README.md` runbook; codifies the per-operator status quo this ADR makes explicit. Includes the canonical "Rotating the state-bucket key" procedure.
- [deploy#172](https://github.com/noorinalabs/noorinalabs-deploy/issues/172) — SSE-B2 verification on the state bucket (the at-rest control this ADR's access control composes with).
- [deploy#158](https://github.com/noorinalabs/noorinalabs-deploy/issues/158) — CI `validate-creds` job (the rotation-drift detection layer that pairs with Decision C).
- [ADR 0001](0001-tf-hetzner-per-env-state-strategy.md) — per-env state strategy; defines the bucket's role as backend for both envs.
- [ADR 0002](0002-hetzner-outputs-classification.md) — outputs classification gate; complements this ADR's access-control story for what flows through state.
- `noorinalabs-main:ontology/services.yaml` § `state_backend` — current ontology entry for the bucket; Part-2 implementation issue will add an `iac_managed_by: terraform/backblaze-bootstrap` field once Decision A lands.
- `noorinalabs-main:ontology/repos/deploy.yaml` — declares the bucket reference for the deploy repo.
- **Part-2 implementation issue (to be filed after this ADR lands):** "Implement ADR 0004 — `terraform/backblaze-bootstrap/` root + annual-rotation calendar runbook + optional `rotate-b2-state-key.sh` automation."

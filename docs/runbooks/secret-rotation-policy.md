# Secret rotation policy

Source: [deploy#11](https://github.com/noorinalabs/noorinalabs-deploy/issues/11)
(ops: Secrets management and rotation).
Companion: [`../secrets-inventory.md`](../secrets-inventory.md) (the per-secret
inventory this policy governs).

This is the **policy layer** — *who* owns rotation, *when* each class rotates,
and *how* the cadence is enforced. The mechanical procedures live in the
per-secret runbooks the inventory points at; this file does not duplicate them.

## Principles

1. **Per-environment separation is mandatory.** `staging` and `production` hold
   distinct values for every per-env secret (GitHub Environments). A secret
   shared across envs must be explicitly justified in the inventory's
   "Env-separated" column (only account-wide provider creds qualify today).
2. **Least standing privilege in CI.** Account-grade credentials do not live in
   CI. The B2 master key is the canonical example — ADR 0004 Decision D bars it
   from CI; #361 removes the residual wiring. CI gets read-only / bucket-scoped
   keys; account-grade operations are workstation-only.
3. **Rotation cost is matched to exposure.** Rare-use, storage-dominated
   credentials (e.g. the B2 master key) rotate **on-demand**; runtime-exposed
   shared credentials (e.g. the state-bucket key) rotate on an **annual**
   cadence + on-demand triggers. See the cadence table below.
4. **Every rotatable secret has a runbook or a provider path.** No secret should
   be rotatable only by tribal knowledge. The inventory's "Runbook" column is
   the audit: a blank there for a non-provider secret is a gap to close.

## Cadence table (derived from the inventory)

| Class | Cadence | Trigger | Procedure |
|---|---|---|---|
| B2 state-bucket key | **annual** | calendar (Platform Architect) + on-demand | [`state-key-annual-rotation.md`](state-key-annual-rotation.md) |
| B2 master key | on-demand | offboarding / suspected compromise | ADR 0004 Decision D (workstation-only) |
| SSH deploy/root keys | on-demand | offboarding / suspected compromise | [`ssh-key-rotation.md`](ssh-key-rotation.md) |
| State-resident app secrets (jwt/ghcr/pipeline) | on-demand | the #172 SSE-window defense-in-depth pass | state-resident-secret-rotation runbook ([#193](https://github.com/noorinalabs/noorinalabs-deploy/issues/193)) |
| DB / cache passwords | **automated** (quarterly + on-demand) | scheduled cadence; compromise; offboarding | [`db-password-rotation.md`](db-password-rotation.md) (`rotate-db-passwords.yml`) |
| OAuth / PAT / webhook | provider | provider rotation / expiry | provider console |

## On-demand triggers (apply to every class)

Rotate immediately, regardless of cadence, on any of:

- Operator offboarding (rotate everything that operator held).
- A secret value appearing in a commit, log, Slack message, or screen share.
- A provider compromise notification (B2, GitHub, Google, Cloudflare, Hetzner).
- A drift-detection alarm firing (e.g. deploy#158 `validate-creds`).

## Audit trail

Until a central manager provides one natively (see § Central management):

- **Rotation events** are recorded as a dated line in the relevant runbook's
  history and in the originating issue (e.g. #126 recorded the 2026-04-30
  Postgres/Redis rotation; #193 records the Phase-3 pass; #11 holds the
  running inventory of rotation timestamps).
- **GitHub Actions secret changes** carry an implicit audit trail in the repo's
  audit log (org → Settings → Audit log, `action:secret`).
- **B2 / provider key creation/deletion** is visible in each provider's console
  activity log.

This is a **distributed** audit trail, not a single pane — its consolidation is
part of the central-management decision below.

## Automated rotation — built (deploy#387)

> **Superseded by [ADR 0007](../adr/0007-central-secrets-manager.md) (Accepted
> 2026-06-12) + deploy#387.** The "target, not yet built" framing below is
> historical; automated DB-password rotation now exists.

#11's acceptance asks that **at least database passwords have automated
rotation**. ADR 0007 chose to build this as a **bespoke scheduled GH Actions
workflow on the GitHub-Environment-secrets baseline** (Option A — no new secrets
manager), rotating **per-secret** (S1) on a **P2 cadence (quarterly + on-demand)
with the P3 automated mechanism**:

- **Workflow:** [`.github/workflows/rotate-db-passwords.yml`](../../.github/workflows/rotate-db-passwords.yml)
  mints a URL-safe value, applies it to the running Postgres/Redis via
  [`scripts/rotate_db_password.sh`](../../scripts/rotate_db_password.sh)
  (health-gated, with automatic rollback), and — only on success — writes it to
  the GH Environment secret. Scheduled runs target **staging**; **production is
  owner-gated** (manual `workflow_dispatch` behind the `production` Environment
  approval rule).
- **Operator runbook:** [`db-password-rotation.md`](db-password-rotation.md) —
  on-demand trigger, the `SECRETS_ADMIN_TOKEN` prerequisite, recovery, and what
  is only verifiable at runtime.
- The on-demand `.env`-swap path (the #126 pattern) remains the manual fallback.

## Central management — ~~open decision (owner / ADR)~~ DECIDED

> **Superseded by [ADR 0007](../adr/0007-central-secrets-manager.md) (Accepted
> 2026-06-12).** The owner chose the **A + B hybrid**: GitHub Environment
> secrets (per-env) as the baseline store **plus SOPS + age for git-resident
> config files**. No new managed service; Vault (D) was judged over-scaled and
> SaaS (C) retained only as the documented fallback. The "deliberately does not
> pick one / the choice is the owner's" framing below is **historical** — the
> decision is recorded in ADR 0007; treat the table + recommendation as the
> options analysis that fed it.

#11 lists four options for *where* secrets live. This is a **security
architecture decision with real tradeoffs**, and per team convention an
options-with-tradeoffs choice of this weight is an **owner / ADR call**, not an
implementer default. This PR deliberately did **not** pick one (ADR 0007 since
did). The framing, for the decision:

| Option | Pros | Cons / cost |
|---|---|---|
| **Stay on GH Actions secrets** (status quo + this policy) | zero new infra; native to CI; per-env Environments already give separation | no dynamic secrets; manual rotation; distributed audit trail |
| **SOPS + age** (encrypt in git, decrypt at deploy) | git-versioned, reviewable, auditable diffs; no SaaS | key-management bootstrap; decrypt-at-deploy wiring; still static secrets |
| **Doppler / 1Password Secrets Automation** (SaaS) | easy setup; rotation + audit built-in; sync to GH | external dependency + cost; another trust boundary |
| **HashiCorp Vault** | dynamic secrets, leases, full audit | heavy for current scale; operational burden |

**Recommendation for the decision (not a decision):** at current scale the
status quo + this policy + SOPS-for-git-resident-config is the lowest-friction
defensible path; Vault is over-scaled. But the choice is the owner's. When made,
record it as an ADR and supersede the relevant rows of this policy.

## Deterministic rotation engine (deploy#513)

The cadence table above is now backed by a **machine-readable inventory** and a
**deterministic decision engine**, so rotation-due status is computed rather than
tracked by hand (the gap behind the CF / B2 fire-drills of #510 / #511):

- **Inventory (ground truth):** [`scripts/secret_rotation_inventory.yaml`](../../scripts/secret_rotation_inventory.yaml)
  is the structured counterpart of [`../secrets-inventory.md`](../secrets-inventory.md) —
  per secret: scope, owner role, refresh method, cadence, apply process, owner-gate
  flag, and last-rotated date. Names and metadata only, never a value. The test
  suite reconciles its name set against the markdown inventory so the two cannot
  drift.
- **Engine:** [`scripts/secret_rotation.py`](../../scripts/secret_rotation.py) is a
  pure, injected-clock decision engine. It derives a TTL per cadence
  (annual/semiannual/quarterly; on-demand / on-deploy / provider have no calendar
  TTL), computes `next_due = last_rotated + TTL`, schedules a task at
  `next_due − lead_time` (the "TTL − ~1 day" of the design), self-reschedules a
  completed rotation to its next fire time, and classifies each due secret as
  fully programmatic vs owner-notify (a human inflection point — a provider
  console re-roll, a prod-gate approval, a promote sign-off).
- **Per-class refresh:** each secret declares a refresh method — `generate_and_replace`
  (we mint a URL-safe value via CSPRNG), `provider_reroll` (re-roll upstream, capture
  the new value), or `provider_managed` (lifecycle owned by the provider). The plan
  generalizes the mint → verify → store → apply pattern of
  [`../../scripts/rotate_db_password.sh`](../../scripts/rotate_db_password.sh)
  (health-gated, with rollback) and the fix_b2_plan_key routine from #510. Plans are
  value-free by construction — the engine never prints or logs a secret value.
- **Usage:** `python3 scripts/secret_rotation.py {validate,status,due,plan,next-due}`.
  `--now YYYY-MM-DD` fixes the clock so output is deterministic (and testable).

This engine is the **unit-mechanic core**. It does not itself run a live rotation.

## What this PR delivers vs. defers

- **Delivered (PR-time, static):** the secrets inventory; this rotation policy;
  per-env-separation principle; cadence table; audit-trail-today description;
  the central-management decision framing; and (deploy#513) the machine-readable
  inventory + deterministic TTL-due / refresh-plan / self-reschedule engine above.
- **Deferred (runtime / decision-gated):** the live GitHub Actions cron workflow
  that *runs* the self-rescheduling tasks; owner-notification delivery
  (Slack/email/issue); the session-start hook wiring that renders the rotation-due
  surface; provider-specific re-roll execution (CF/B2 console automation); and
  auto-generating the inventory skeleton from `gh secret list`. Tracked as a
  deploy#513 follow-up.

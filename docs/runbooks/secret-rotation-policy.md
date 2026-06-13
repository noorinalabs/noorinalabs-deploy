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

## Central management — open decision (owner / ADR)

#11 lists four options for *where* secrets live. This is a **security
architecture decision with real tradeoffs**, and per team convention an
options-with-tradeoffs choice of this weight is an **owner / ADR call**, not an
implementer default. This PR deliberately does **not** pick one. The framing,
for the decision:

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

## What this PR delivers vs. defers

- **Delivered (PR-time, static):** the secrets inventory; this rotation policy;
  per-env-separation principle; cadence table; audit-trail-today description;
  the central-management decision framing.
- **Deferred (runtime / decision-gated):** automated DB-password rotation (its
  own follow-up issue); the central-secrets-manager tool choice (owner/ADR);
  a consolidated audit pane (falls out of the tool choice).

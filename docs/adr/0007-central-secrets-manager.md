# ADR 0007 — Central secrets-manager tool + rotation policy

- **Status:** Accepted — owner decision 2026-06-12 (Steven French)
- **Date:** 2026-06-12
- **Author:** Weronika Zielinska (Platform Architect)
- **Context issue:** [deploy#388](https://github.com/noorinalabs/noorinalabs-deploy/issues/388)
- **Lineage:** deferred from [deploy#11](https://github.com/noorinalabs/noorinalabs-deploy/issues/11) (secrets management & rotation) / [PR #381](https://github.com/noorinalabs/noorinalabs-deploy/pull/381), which framed the options but deliberately did **not** pick a tool. `docs/runbooks/secret-rotation-policy.md` § Central management holds the open framing this ADR resolves.
- **Related ADRs:** [0004 — B2 state-bucket + key-management](0004-b2-state-bucket-and-key-management.md) (state-bucket key model + on-demand rotation), [0006 — per-env, per-role SSH keys](0006-per-env-per-role-ssh-keys.md) (blast-radius split)
- **Supersedes:** none (this ADR, once **Accepted**, supersedes the "Central management — open decision" and "Automated rotation" rows of `docs/runbooks/secret-rotation-policy.md`)
- **Superseded by:** none
- **Unblocks:** [deploy#387](https://github.com/noorinalabs/noorinalabs-deploy/issues/387) (automated DB-password rotation — parked on this choice; the rotation mechanism differs per tool)

## Context

This is an **owner / ADR decision, not implementer-default work.** Picking a secrets-manager tool determines the project's secret blast-radius, trust boundaries, recurring cost, and operational burden — exactly the class of options-with-tradeoffs choice the team routes to the owner via an ADR (cf. the check-accepted-ADR / owner-policy-call discipline). This document **frames** the decision; it does not make it. The architect records a non-binding recommendation in § Recommendation, but the Decision rows below are left open for the owner.

### Where secrets live today

The status quo is **GitHub Actions / GitHub Environment secrets, per-environment separated**, governed by `docs/runbooks/secret-rotation-policy.md`:

- Per-env separation is mandatory — `staging` and `production` are distinct GH Environments holding distinct values for every per-env secret. Only account-wide provider creds are shared, and only with justification in `secrets-inventory.md`.
- Least standing privilege in CI — account-grade credentials (e.g. the B2 master key) are barred from CI per ADR 0004 Decision D; CI gets read-only / bucket-scoped keys.
- The audit trail today is **distributed**, not a single pane: GH Actions secret changes land in the org audit log (`action:secret`); B2/provider key events live in each provider's console; rotation events are dated lines in the relevant runbook and originating issue.

### What is unresolved

Two coupled things:

1. **Tool / posture** — where secrets should live going forward (this ADR's four options A–D below).
2. **Rotation policy** — the cadence and scope at which each secret class rotates. The existing cadence table in `secret-rotation-policy.md` is mostly **on-demand** today; #11's acceptance asks that *at least database passwords* gain automated rotation. The rotation *mechanism* differs per tool (a SaaS or Vault rotates very differently from a GH-Actions-secret swap), so the owner needs to decide tool **and** policy together. § Rotation-policy options below lays out the cadence/scope choices so both can be decided in one pass.

### Current scale (the axis every tradeoff is judged on)

- One repo's worth of deploy secrets; ~2 operators provisioned; two live environments (stg, prod) on single-VPS-per-env Hetzner boxes.
- The heaviest secret classes are already addressed by their own ADRs: state-bucket key (0004), SSH keys (0006). What remains centrally unmanaged is the app-runtime set: DB/cache passwords, JWT signing key, GHCR pull token, pipeline creds, OAuth/PAT/webhook secrets.
- No compliance regime today mandates dynamic secrets, short leases, or a consolidated audit pane. That can change; the ADR notes where each option's headroom sits.

## Decision — Accepted 2026-06-12 (owner)

> **Decision (owner, 2026-06-12):** **A + B hybrid** — **Option A** (GitHub Environment secrets, per-environment separated) as the baseline secret store, **plus Option B** (SOPS + age) adopted specifically for **git-resident config files**.
> **Rotation policy:** **P2 (periodic + on-demand)** for all secret classes now; **#387 — automated DB-password rotation (P3)** is the next implementation step, built as a bespoke scheduled workflow on top of the A baseline (no tool change). **Scope:** per-secret rotation events (not bulk). On-demand triggers from `secret-rotation-policy.md` remain a floor on top of the P2 cadence.
> Rationale: lowest-friction defensible posture at current ~2-operator / 2-environment scale (matches the architect's § Recommendation); Vault (D) over-scaled, SaaS (C) retained as the documented fallback if A+B manual-rotation burden proves too high.

### Option framing — the four postures

Each option is judged on the same axes the team uses elsewhere: **trust boundary** (who/what must be trusted with plaintext), **blast radius** (what one compromise grants), **operational burden**, **recurring cost**, and **"what happens when this fails?"**

#### Option A — stay on GH Actions / GH Environment secrets (status quo + policy)

Keep secrets in GH Environment secrets; formalize the existing rotation policy as the governing artifact; close gaps with runbooks rather than tooling.

- **Trust boundary:** GitHub (already trusted — it hosts the code, CI, and deploy identity). No *new* third party enters the trust boundary.
- **Blast radius:** an org/repo-admin compromise or a malicious workflow on a protected branch can read env secrets at job runtime. Per-env Environments already wall stg from prod. Secrets are static and long-lived, so a leaked value is valid until manually rotated.
- **Operational burden:** lowest — zero new infra, zero new bootstrap. Rotation is manual (update the Environment secret, redeploy).
- **Recurring cost:** $0.
- **Failure mode:** a leaked static secret stays valid until someone notices and rotates by hand; the audit trail to reconstruct "who changed what when" is distributed across the GH audit log + provider consoles. No dynamic-secret / short-lease safety net.
- **Headroom:** none toward dynamic secrets or a single audit pane — those require B/C/D. Fine while scale and compliance pressure stay where they are.

#### Option B — SOPS + age (encrypt-in-git, decrypt at deploy)

Secrets live as `sops`-encrypted files committed to the repo, encrypted to `age` recipients (operator keys + a CI key); decrypted at deploy time on the runner/box.

- **Trust boundary:** the `age` private keys. CI holds one age key (still a GH secret — so GitHub remains in the boundary), operators hold their own. No SaaS.
- **Blast radius:** scoped to whoever holds an age private key. Compromise of the CI age key ≈ compromise of the GH secret today, but the *encrypted* values are public-in-repo, so the age key is the whole game — its leak exposes every secret encrypted to it until re-keyed. Re-keying = re-encrypt all files to a new recipient set.
- **Operational burden:** moderate — one-time key-management bootstrap (recipient list, key distribution, CI decrypt wiring), then secrets become reviewable, diffable, git-versioned artifacts. Strong fit for **git-resident config** (compose `.env`-shaped files, app config with embedded secrets).
- **Recurring cost:** $0 (open-source).
- **Failure mode:** still **static** secrets — no leases, no auto-rotation. An age key leak is high-impact because ciphertext is in git history forever (rotation must assume historical ciphertext is compromised). Bootstrapping key distribution wrong (e.g. an operator's age key on a shared box) silently widens the boundary.
- **Headroom:** good for auditability (every change is a reviewable diff); none for dynamic secrets.

#### Option C — Doppler / 1Password Secrets Automation (SaaS)

A managed secrets platform holds the values; secrets sync into GH Actions / are fetched at deploy via a service token. Rotation, versioning, and an audit pane are built in.

- **Trust boundary:** **a new third party** (Doppler / 1Password) plus the service token that grants access to it. The SaaS now holds plaintext for every synced secret.
- **Blast radius:** a leaked service token (or a SaaS-side compromise) exposes whatever that token's scope covers. Mitigated by per-env tokens + scoped projects; widened by the fact that the values now sit outside infrastructure you control.
- **Operational burden:** lowest *after* setup — rotation, audit, and sync are turnkey. Setup is account provisioning + token wiring + sync config.
- **Recurring cost:** **non-zero, recurring** — per-seat / per-secret SaaS pricing. Smallest dollar figure of the "real tooling" options, but it is an ongoing line item and a vendor dependency.
- **Failure mode:** SaaS outage at deploy time blocks deploys unless values are cached/mirrored; vendor lock-in on the sync model; an added external trust boundary that must itself be audited (SOC2 etc.). Offboarding the vendor means re-homing every secret.
- **Headroom:** built-in rotation + consolidated audit pane out of the box; some dynamic-secret support (1Password less so than Vault).

#### Option D — HashiCorp Vault (dynamic secrets)

Self-host (or HCP-host) Vault; issue **dynamic, leased** secrets (e.g. short-TTL DB creds minted per-deploy) with a full audit device.

- **Trust boundary:** the Vault cluster + its unseal keys / root token + the auth method CI uses (AppRole/OIDC). Vault becomes the single most security-critical service in the estate.
- **Blast radius:** smallest *per-secret* (leases expire; dynamic DB creds are short-lived and per-consumer), but **largest concentrated** — a Vault compromise or unseal-key leak is catastrophic. The audit device gives the best single-pane forensic trail of any option.
- **Operational burden:** **highest** — cluster ops, unseal/seal management, backup/DR of Vault's own state, upgrade treadmill, auth-method plumbing. This is a service you now run.
- **Recurring cost:** infra (self-host) or HCP subscription, plus the real cost: **operator time** to run it safely.
- **Failure mode:** Vault down = no secrets = no deploys (and potentially no DB access if creds are dynamic). Mis-managed unseal keys can hard-brick access to everything. Over-scaled for a ~2-operator, 2-env, single-VPS-per-env project.
- **Headroom:** maximal — dynamic secrets, fine-grained leasing, full audit. The right answer at a much larger scale or under a compliance regime that mandates short-lived creds; over-built for today.

### Option comparison at current scale

| Axis | A — GH secrets | B — SOPS+age | C — SaaS | D — Vault |
|---|---|---|---|---|
| New trust boundary | none | none (age keys) | **SaaS vendor** | Vault cluster |
| Secret lifetime | static | static | static (+some dynamic) | **dynamic/leased** |
| Blast radius on key leak | env secret scope | every value under that age key | token scope (off-infra) | concentrated-catastrophic |
| Audit trail | distributed | reviewable git diffs | **single pane** | **single pane + leases** |
| Rotation | manual | manual | built-in | dynamic/automatic |
| Recurring $ | $0 | $0 | per-seat/secret | infra + **operator time** |
| Op burden | lowest | moderate (bootstrap) | low (post-setup) | **highest** |
| Fits git-resident config | n/a | **best** | n/a | n/a |
| Right-sized for current scale | yes | yes | borderline | **no (over-scaled)** |

## Rotation-policy options (decide alongside the tool)

The cadence table in `secret-rotation-policy.md` is mostly **on-demand** today. The owner needs to set the rotation **policy** — cadence + scope — and because the *mechanism* differs per tool, these are presented as choices to decide together with A–D. Two independent knobs:

### Knob 1 — cadence per secret class

| Secret class | Status quo | Option P1 — on-demand only | Option P2 — periodic + on-demand | Option P3 — automated/dynamic |
|---|---|---|---|---|
| DB / cache passwords | on-demand `.env` swap | keep manual; rotate on trigger | quarterly + on-demand | **per-deploy or scheduled mint** (the #387 target) |
| JWT signing key | on-demand | manual on trigger | semi-annual + on-demand | scheduled re-issue w/ overlap window |
| GHCR / pipeline tokens | on-demand | manual on trigger | annual + on-demand | provider/tool-rotated |
| State-bucket key | annual (ADR 0004) | — already set by ADR 0004 — | — | — |
| OAuth / PAT / webhook | provider expiry | provider | provider | provider |

> On-demand triggers from `secret-rotation-policy.md` (offboarding, value-in-logs, provider-compromise, drift alarm) **always apply on top of** whichever cadence is chosen — they are a floor, not an alternative.

### Knob 2 — scope of a rotation event

| Scope option | Meaning | Tradeoff |
|---|---|---|
| **S1 — per-secret** | rotate only the affected/expiring value | least disruption; most events to track |
| **S2 — per-class** | rotate a whole class together (e.g. all DB creds) | fewer events; larger per-event blast on mistake |
| **S3 — per-operator** | on offboarding, rotate everything that operator held | mandatory floor regardless of S1/S2 (already in policy) |

### How tool choice constrains rotation policy

- **A (GH secrets):** realistically caps at **P2** for DB passwords (periodic + on-demand). P3-automated needs a scheduled workflow that mints → updates the Environment secret → applies to the running Postgres/Redis → redeploys on a green health check, with rollback — buildable but bespoke (this is exactly #387's scope).
- **B (SOPS+age):** same cadence ceiling as A (static secrets), but rotation = re-encrypt + commit + deploy; auditable via git history.
- **C (SaaS):** **P3** for managed classes is turnkey; cadence becomes a console setting.
- **D (Vault):** **P3 by construction** for dynamic DB creds (mint short-TTL per consumer); cadence becomes lease TTL.

## Recommendation (non-binding — the decision is the owner's)

Per the note already in `secret-rotation-policy.md` § Central management, and judged on current scale:

> **Stay on Option A (GH Environment secrets) + formalize this rotation policy, and adopt Option B (SOPS + age) specifically for git-resident config files** — the lowest-friction defensible posture at current scale. Pair it with **rotation policy P2 (periodic + on-demand) now**, with **P3-automated DB-password rotation (#387) as the next implementation step** layered on top without changing the tool.

Rationale:

- A introduces **no new trust boundary** and keeps secrets inside infrastructure already trusted with the code and deploy identity. The per-env Environment separation already gives the stg/prod isolation ADRs 0001/0004/0006 were built to protect.
- B closes A's weakest gap — git-resident config secrets that want to be reviewable diffs — without a SaaS bill or a Vault to operate.
- **Vault (D) is over-scaled.** Its concentrated blast radius and operational burden (unseal management, DR of Vault's own state, the "Vault down = no deploys" failure) are not justified by a ~2-operator, 2-env project with no dynamic-secret compliance mandate. Revisit D if scale or a compliance regime demanding short-lived creds arrives.
- **SaaS (C)** is the strongest *if* the owner values a turnkey single audit pane + built-in rotation enough to accept a recurring bill and an added external trust boundary. It is the recommended fallback if the manual-rotation burden of A+B proves too high in practice.

Whatever is chosen, #387 (automated DB-password rotation) is unblocked the moment this Decision row is filled: its mechanism follows directly from the tool (bespoke scheduled workflow under A/B; console toggle under C; lease TTL under D).

## Consequences

- **If A+B (recommended):** no new infra/cost; rotation stays partly manual; #387 becomes a bespoke scheduled-workflow build; audit trail stays distributed (documented as accepted). `secret-rotation-policy.md` § Central-management + Automated-rotation rows are superseded by this ADR's Decision.
- **If C:** a vendor + service-token enter the trust boundary; recurring cost; #387 and the consolidated audit pane fall out of the platform; offboarding the vendor is a future migration cost.
- **If D:** a Vault cluster becomes the most security-critical service; highest operational burden; #387 becomes dynamic-lease config; strongest forensic posture.
- **In all cases:** ADRs 0004 (state-bucket key) and 0006 (SSH keys) remain authoritative for their classes; this ADR governs the *app-runtime* secret set and the central-management posture only.

## What this ADR delivers vs defers

- **Delivered:** the four-option framing with per-option trust-boundary/blast-radius/cost/failure analysis at current scale; the rotation-policy cadence + scope option matrix; the tool→policy constraint mapping; a non-binding architect recommendation.
- **Deferred to the owner:** the Decision row (tool/posture) and the rotation-policy selection (cadence P-knob + scope S-knob).
- **Deferred to implementation (post-decision):** wiring the chosen tool; #387 automated DB-password rotation in the chosen mechanism; superseding the affected `secret-rotation-policy.md` rows once this ADR is Accepted.

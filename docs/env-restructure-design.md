# Design — per-env env-var restructure for the NoorinALabs platform

- **Status:** Accepted — owner sign-off 2026-05-28
- **Date:** 2026-05-28
- **Author:** Weronika Zielinska (Platform Architect)
- **Context issue:** [deploy#332](https://github.com/noorinalabs/noorinalabs-deploy/issues/332) — per-env env-var restructure DESIGN (part-2 of [deploy#116](https://github.com/noorinalabs/noorinalabs-deploy/issues/116))
- **Grounded in:** [deploy#116](https://github.com/noorinalabs/noorinalabs-deploy/issues/116) inventory — `scripts/env-inventory.py`, `docs/env-inventory.csv`, `docs/env-inventory.md`
- **Implementation:** out of scope. A CI-validation implementation issue is filed *after* this design is accepted.

> This is a **design proposal**, not an ADR and not an implementation. It recommends a target shape and surfaces three owner-decision points. Nothing here changes a single env file until the decisions are signed off and the follow-up implementation issue lands.

## Context

The deploy#116 inventory measured the env-var surface deterministically across all 7 child repos (`scripts/env-inventory.py`, re-runnable to byte-identical output). The numbers:

- **182 distinct env vars**, **681 total references**.
- **97 vars referenced from more than one source file** — i.e. >half the surface has multiple sources of truth.
- Reference types: 191 docker-compose, 176 GitHub-Actions `secrets.*`, 121 `.env.example`, 53 CLAUDE.md prose, **51 `os.environ`/`os.getenv` call sites**, 42 Pydantic `BaseSettings` fields, 22 `process.env.*`, 21 GHA `vars.*`, 3 `import.meta.env.*`, 1 `env_file`.
- Concentration: `noorinalabs-deploy` carries 369 of the 681 references — it is the integration point where every service's env converges.

Today there is **no per-env file structure at all**. The shape is:

- A single flat **`compose/.env.example`** (75 lines) is the only checked-in env artifact in the deploy repo. There is no `env/` directory.
- The live per-env `.env` is **assembled at deploy time** by the `.github/actions/write-deploy-env` composite, which SSHes the file onto the VPS from GitHub Actions **Environment-scoped secrets** (`environment: staging` / `production`), plus a `base_domain` input and an `extra_env` block of selector vars (`PROM_CONFIG_FILE`, `ALERTMANAGER_CONFIG_FILE`, `SLACK_WEBHOOK_FILE`).
- Per-env *divergence* is already handled, but ad-hoc: `BASE_DOMAIN` (`stg.noorinalabs.com` vs `noorinalabs.com`), `*_CONFIG_FILE` selector vars that pick `prometheus.{prod,stg}.yml` etc., and `${VAR:?must be set}` guards in `compose/docker-compose.prod.yml` that fail-fast on a missing required var.
- The same `compose/docker-compose.prod.yml` serves **both** stg and prod — there is no `docker-compose.stg.yml`. Env, not compose topology, is the per-env axis.

### Problems this design addresses

1. **No source-of-truth boundary.** 97 multi-source vars mean a value can be declared in `.env.example`, re-declared in a compose `environment:` block, *and* listed in a workflow `env:`/`env_keys` line — with nothing reconciling them. The `write-deploy-env` step's `env:` block and `env_keys` CSV (27 keys) must be kept in lockstep by hand; a drift between them is a silent missing-var at deploy.
2. **Onboarding a new env is undocumented and manual.** There is no written "add `dev` / add a customer env" procedure. The de-facto steps live across a workflow file, a composite action, and operator memory.
3. **Secret vs non-secret is not separated structurally.** `compose/.env.example` mixes `changeme` secret placeholders (`NEO4J_PASSWORD`, `JWT_SECRET`) with genuinely-non-secret config (`KAFKA_UI_READONLY=true`, `PIPELINE_B2_REGION`, `PROM_CONFIG_FILE`). The reader cannot tell from the file which lines are secrets.
4. **No CI gate that a service's `Settings` can actually load** from the env shape. `compose-validate.yml` validates that `docker compose config` resolves, but nothing asserts that each service's Pydantic `Settings()` constructs successfully from the designed file layout.

### What this design does *not* try to fix

- It does **not** move secrets out of GitHub Actions secrets as a precondition (that is owner-decision #2).
- It does **not** rewrite the 51 `os.environ` call sites as a precondition (owner-decision #3).
- It does **not** add a `docker-compose.stg.yml`. Per-env divergence stays an env-file axis, not a compose-topology axis — the current single-compose model is sound.

## The recommended shape

A **base → env → service hybrid layout** of checked-in **non-secret** env files, with **secrets kept out of the repo** and injected at deploy time, validated by a CI gate that loads every service's `Settings`.

### File layout (non-secrets, checked into the deploy repo)

```
env/
  base.env                # org-wide non-secret defaults (every env inherits)
  stg.env                 # stg-only non-secret overrides
  prod.env                # prod-only non-secret overrides
  services/
    isnad-graph.env       # service-specific non-secret defaults (env-agnostic)
    user-service.env
    ingest-platform.env
secrets/
  .gitignore              # ignores *.env; only *.env.example is tracked
  stg.env.example         # secret KEYS for stg (values are placeholders)
  prod.env.example        # secret KEYS for prod
```

Resolution order (last wins), assembled by `write-deploy-env`:
`base.env` → `services/<svc>.env` → `<env>.env` → injected secrets.

`compose/.env.example` is retained as the **human-readable union catalogue** (every var the stack consumes, secret + non-secret) — generated/checked by the same CI gate so it cannot drift.

**Why hybrid, not flat-per-env or Pydantic-only:**

- **vs flat single `.env` per env (status quo):** a flat file forces every shared default to be copy-pasted into both `stg` and `prod`, which is exactly how 97 vars ended up multi-sourced. Base inheritance kills the copy-paste.
- **vs Pydantic-only (no `.env` files):** non-Python consumers exist — 22 `process.env.*` (landing/frontend), 191 compose interpolations, 176 GHA secrets. A `.env` file is the only lingua franca all of them read. Pydantic-only would strand the non-Python half of the surface.
- **vs file-per-service-per-env (`env/stg/isnad-graph.env`):** that is owner-decision #1. The hybrid above is the *non-secret-default* skeleton; whether the per-env *leaf* is one file or one-file-per-service is the decision.

### Inheritance model

**Base → service → env → secrets**, last-writer-wins, with one hard rule: **a var is declared in exactly one tier as its source of truth.** A non-secret env-invariant default lives in `base.env`. A non-secret per-env value (`BASE_DOMAIN`, `PROM_CONFIG_FILE`) lives in `<env>.env`. A non-secret service-shaped default lives in `services/<svc>.env`. A secret lives only in the secret store (decision #2), never in any checked-in tier. The CI gate (below) flags any var that appears as a real value in more than one tier — collapsing the 97 multi-source vars to single-source over time.

### Secret vs non-secret separation

The structural rule: **`env/**` is non-secret and reviewable in PRs; secrets never appear there.** Secret *keys* are catalogued in `secrets/<env>.env.example` (placeholder values only) so the required set is reviewable and the CI gate can assert completeness; secret *values* come from the store chosen in decision #2 and are assembled into the live `.env` at deploy time exactly as `write-deploy-env` does today. This makes "is this line a secret?" answerable by directory, not by reading the value.

The load-bearing enforcement of that boundary is a **per-directory `secrets/.gitignore` containing `*.env` plus `!*.env.example`** — it ignores every real secret file in `secrets/` while keeping the `.env.example` key-catalogues tracked. This is not optional and is not covered by the repo-root `.gitignore`: a top-level bare `.env` entry matches only a literal `./.env`, *not* `secrets/stg.env`. Without the per-dir `secrets/.gitignore`, a `secrets/prod.env` written by an operator for a local stack would be silently committable — exactly the leak this layout exists to prevent. The CI gate should additionally fail if any tracked file under `secrets/` does not end in `.env.example`.

### "Add a new env" workflow (end-to-end)

With the recommended shape, onboarding `dev` or a customer env `acme` becomes:

1. `cp env/stg.env env/<new>.env`; edit the per-env non-secret values (`BASE_DOMAIN`, `*_CONFIG_FILE`).
2. `cp secrets/stg.env.example secrets/<new>.env.example`; this is the **checklist of secrets to provision** for the new env.
3. Provision the secret values into the chosen store (decision #2) — e.g. create a GH Actions Environment `<new>` and set each key.
4. Add a `deploy-<new>.yml` (or extend the matrix) that calls `write-deploy-env` with `environment: <new>` and `base_domain` for the new env.
5. CI gate runs: every service's `Settings()` is constructed against `base.env + services/*.env + <new>.env + secret placeholders` and must succeed before merge.

The procedure becomes a documented, copy-driven checklist instead of operator memory. (Today, step 5 doesn't exist and steps 1–4 are implicit in the workflow files.)

### CI validation gate

A new CI job (implementation deferred to the post-acceptance issue) that, for each service:

1. Assembles the **non-secret** env layers (`base.env` → `services/<svc>.env` → `<env>.env`) plus **placeholder** values for every key listed in `secrets/<env>.env.example`.
2. Imports and constructs that service's Pydantic `Settings()` against the assembled environment, asserting it loads without `ValidationError`.
3. Asserts every key in `secrets/<env>.env.example` and every `${VAR:?...}` guard in `compose/docker-compose.prod.yml` is satisfied by some tier — catching the `env:`/`env_keys` drift described in Problem #1.
4. Re-runs `scripts/env-inventory.py` and fails if `docs/env-inventory.{csv,md}` is stale — keeping the inventory honest as the surface evolves.

This is the one piece worth building first after acceptance: it is the forcing function that prevents the 97-multi-source problem from re-growing.

## Owner-decision points

Each of the following was a deliberate **owner (Steven) call**. The owner signed off on all three on 2026-05-28; the chosen option, options table, and rationale are retained below for the record.

### ✅ Decided (owner sign-off 2026-05-28) #1 — single-file-per-env vs file-per-service-per-env

> **Decision: Option A — single file per env** (`env/<env>.env`) on the base + `env/services/*.env` skeleton.

`env/<env>.env` (e.g. `env/stg.env`) vs `env/<env>/<service>.env` (e.g. `env/stg/isnad-graph.env`).

| Option | Pros | Cons / failure mode |
|---|---|---|
| **A — single file per env** (`env/stg.env`) | Fewest files; matches today's single-`.env`-per-env mental model; trivial diff to read "what is stg?"; the deploy already assembles one flat `.env`, so this is the smallest change. | A 182-var file is a big blast radius — one malformed line breaks every service's startup. Service ownership is implicit (no per-service file boundary). |
| **B — file per service per env** (`env/stg/isnad-graph.env`) | Clear service ownership; a service-team edits only its file; smaller blast radius per change; composes with CODEOWNERS per service. | More files (envs × services); cross-service shared vars (`BASE_DOMAIN`, DB creds shared by api + exporter) must live in a base/shared file anyway, so you get *both* layouts; risk of the same var drifting across two service files. |

**My recommendation: Option A (single file per env) for the per-env leaf, plus `env/services/<svc>.env` for service-shaped non-secret defaults** (the hybrid above). Rationale: at our scale (2 envs, ~6 services) the file-count cost of B outweighs its ownership benefit, and B re-creates the multi-source risk it tries to solve (shared vars live in a base file *and* the cross-service vars still need a home). Single-file-per-env keeps the diff legible and the assembly identical to today's `write-deploy-env`; service-shaped *defaults* get their own files, but the per-env *leaf* stays one file. **What happens when this fails?** A bad `env/prod.env` line under A breaks all services at once — mitigated by the CI `Settings()`-load gate catching it pre-merge, and by `${VAR:?}` compose guards failing fast and visibly rather than silently. Revisit B if/when a service team wants independent ownership of its env surface (the natural upgrade trigger).

### ✅ Decided (owner sign-off 2026-05-28) #2 — stg/prod credentials home

> **Decision: stay on GitHub Actions env-scoped secrets** (Option A), and add the CI completeness gate (`secrets/<env>.env.example` ⊆ provisioned keys) to kill the 27-key hand-sync drift. The vault (Option B) upgrade-trigger is recorded as written below.

Where the **secret values** live.

| Option | Pros | Cons / failure mode |
|---|---|---|
| **A — GH Actions env-scoped secrets** (current) | Already in use (176 `secrets.*` refs, `environment: staging/production` scoping); zero new vendor; `write-deploy-env` already assembles from it; per-env isolation via GH Environments; free. | No secret versioning/audit beyond GH's; rotation is manual per-secret via `gh secret set`; secrets are only reachable from CI (an operator can't pull a value for a local stack without re-entering it); 27-key `env:`+`env_keys` hand-sync is the drift surface. |
| **B — external vault** (HashiCorp Vault / Doppler / Infisical) | Versioning, audit log, rotation, dynamic secrets; one source for CI *and* operator workstations; fine-grained access. | New vendor relationship + availability dependency (a vault outage blocks deploys); cost; migration of 176 secret refs; over-provisioned for ~30 secrets × 2 envs at current scale. Same "external dependency" objection ADR 0005 raised against HCP. |
| **C — B2-encrypted secrets file** (`sops`/`git-crypt`, decrypt at deploy) | Secrets versioned in git (encrypted); no new runtime vendor (B2 already in stack per ADR 0004); reviewable that a key *exists* (not its value); operator can decrypt locally. | Key-management recursion (the decrypt key becomes the new long-lived secret to protect — same trap ADR 0004 Decision D names); a mis-scoped decrypt key exposes *all* secrets at once (worst blast radius); `sops` tooling burden. |

**My recommendation: stay on Option A (GH Actions env-scoped secrets) and fix its one real weakness with the CI gate** — the gate asserting `secrets/<env>.env.example` ⊆ provisioned keys removes the 27-key hand-sync drift, which is the actual pain, not the storage backend. This mirrors ADR 0005's reasoning: the strongest option (a vault) is "a different conversation" than the problem in front of us (drift + no separation), and introducing a vendor-availability dependency on the deploy path is a real cost at our scale. **What happens when this fails?** A GH secret leak is contained to one Environment (stg or prod, not both) by the existing `environment:` scoping — smaller blast radius than Option C's single decrypt key. **Upgrade trigger** (record it now so the next conversation is a migration plan, not a debate): adopt Option B when (a) operator-local secret access becomes routine, (b) a compliance/audit requirement lands, or (c) secret rotation cadence becomes load-bearing enough that manual `gh secret set` is the bottleneck.

### ✅ Decided (owner sign-off 2026-05-28) #3 — allow `os.environ.get(...)` outside Pydantic `Settings`?

> **Decision: Pydantic `Settings` mandatory in service `src/`; raw `os.environ` allowed in tests, integration-tests, and scripts** (Option B), enforced by a `**/src/**`-scoped lint.

The inventory found **51 `os.environ`/`os.getenv` call sites**. Disallow-and-refactor (all config flows through a `Settings` class) vs allow-where-pragmatic.

Critical grounding fact: **the 51 sites are overwhelmingly test/glue code, not production app config.** By location: 18 in `noorinalabs-isnad-ingest-platform/` (mostly tests), **17 in `noorinalabs-deploy/integration-tests/`**, 6 in ingest-platform `src/`, 5 in data-acquisition `src/`, 2 in isnad-graph `src/`, 2 isnad-graph other, 1 deploy `.github`. Representative names confirm it: `E2E_BASE_URL`, `FAKE_OAUTH_AUDIENCE`, `ISNAD_BASE_URL`, `ISOLATION_CHECK_RESULT`, `RUN_MODE` — test harness wiring, not service settings. Only a handful (~13, in `src/`) are production-path reads.

| Option | Pros | Cons / failure mode |
|---|---|---|
| **A — disallow everywhere; all config via `Settings`** | One config front door per service; every var is typed, validated, documented in one class; the CI `Settings()`-load gate covers 100% of config. | Refactoring 51 sites — most of them test glue — for marginal benefit; tests legitimately reach for env directly (`monkeypatch.setenv`, ad-hoc `E2E_BASE_URL`); a blanket ban fights the grain of test code. |
| **B — allow-where-pragmatic: `Settings` mandatory in `src/`, `os.environ` allowed in tests/scripts** ← recommended | Targets the ~13 production-path reads (the ones that matter) without churning ~38 test/glue sites; aligns the rule with where config-correctness actually has blast radius; cheap to enforce (lint scoped to `src/`). | Two rules to remember (src vs test); a lint must encode the path scope; risk of a prod read sneaking into a `scripts/` file that the lint doesn't cover. |

**My recommendation: Option B — `Settings` mandatory in service `src/`, `os.environ` permitted in tests, integration-tests, and one-off scripts.** Rationale: the inventory proves the 51 sites are ~75% test/glue; a disallow-everywhere rule would refactor mostly-test code for a benefit that only exists on the ~13 `src/` reads. Scope the discipline to where mis-read config breaks a running service. **What happens when this fails?** A production `src/` read that bypasses `Settings` escapes the CI `Settings()`-load gate (the gate only sees declared fields) — so the enforcement must be a lint (`ruff`/`flake8` custom rule or grep gate) scoped to `**/src/**` that flags `os.environ`/`os.getenv`, with an allowlist for tests/scripts. That lint is part of the post-acceptance implementation issue.

## Consequences

### Positive

- **Source-of-truth boundary becomes structural.** Directory (`env/` non-secret vs secret store) answers "is this a secret?"; the one-tier-per-var rule + CI gate collapse the 97 multi-source vars over time.
- **"Add a new env" becomes a documented copy-checklist** instead of operator memory spread across a workflow, a composite, and a `.env.example`.
- **The 27-key `env:`/`env_keys` hand-sync drift is eliminated** by the CI completeness check, independent of which secrets-home option is chosen.
- **The inventory stays honest** — the gate fails on a stale `env-inventory.{csv,md}`, so the deploy#116 deliverable keeps paying off.
- **Minimal disruption.** The recommended path keeps the single-compose model, keeps GH-secrets storage, and keeps `write-deploy-env` as the assembler — it adds structure and a gate, it does not rewrite the deploy path.

### Negative / ongoing costs

- **One more layout to learn** (`env/base + services + <env>` + `secrets/*.example`). Mitigated by the copy-driven onboarding checklist and the CI gate that explains failures.
- **The CI gate is net-new work** — it must import each service's `Settings`, which means the deploy repo's gate needs the service packages importable (or a thin per-service shim). Sized in the implementation issue.
- **Recommendations #1–#3 are conservative by design** (stay-and-fix over migrate). If the owner wants a more aggressive end-state (vault, file-per-service, disallow-everywhere), the upgrade triggers above name when to revisit — but each adds cost the current scale doesn't yet justify.

## Success criteria / next steps

1. **Owner sign-off on the 3 decision points received 2026-05-28** (#1 single-file-per-env + service skeleton, #2 stay-on-GH-secrets + completeness gate, #3 `Settings`-mandatory-in-`src/` + scoped lint). With sign-off in hand, the **next action is filing the CI-validation implementation issue** (item 2 below); deploy#332 closes when this design PR merges.
2. **File the CI-validation implementation issue** after acceptance: build the per-service `Settings()`-load gate + the secret-key-completeness check + the `os.environ` lint (scope per decision #3) + the `env-inventory` staleness check.
3. **Migrate incrementally**, not big-bang: introduce `env/base.env` + `env/<env>.env` behind the existing `write-deploy-env` assembly, collapse multi-source vars tier-by-tier (97 → 0) with the gate enforcing single-source as each lands.

## Refs

- [deploy#332](https://github.com/noorinalabs/noorinalabs-deploy/issues/332) — this design's context issue.
- [deploy#116](https://github.com/noorinalabs/noorinalabs-deploy/issues/116) — env-var inventory (part-1); the evidence base for every number cited here.
- `scripts/env-inventory.py`, `docs/env-inventory.csv`, `docs/env-inventory.md` — the deterministic inventory this design is grounded in.
- `compose/.env.example`, `compose/docker-compose.prod.yml`, `.github/actions/write-deploy-env`, `.github/workflows/deploy-{stg,prod}.yml` — the current env-assembly shape this design evolves.
- [ADR 0004](adr/0004-b2-state-bucket-and-key-management.md), [ADR 0005](adr/0005-terraform-state-locking-on-b2-backend.md) — the "stay-and-fix at current scale + record the upgrade trigger" pattern this design follows for decisions #1 and #2.

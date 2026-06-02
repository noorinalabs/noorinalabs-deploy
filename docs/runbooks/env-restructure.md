# Runbook: env-restructure layout & validation gate

Operational reference for the per-env env-var layout introduced by deploy#363
(implementing the accepted design `docs/env-restructure-design.md`, deploy#332).

**Audience:** SRE engineer or service owner editing env config or onboarding a
new environment.

## The layout

Non-secret env config is checked into `env/` as inheritance tiers; secret KEYS
are catalogued in `secrets/` (placeholder values only); secret VALUES live in
GitHub Actions env-scoped secrets (design decision #2) and are injected into the
live `.env` at deploy time by `.github/actions/write-deploy-env`.

```
env/
  base.env                # org-wide non-secret defaults (every env inherits)
  stg.env                 # stg-only non-secret overrides
  prod.env                # prod-only non-secret overrides
  services/
    isnad-graph.env       # service-shaped non-secret defaults (env-agnostic)
    user-service.env
    ingest-platform.env
secrets/
  .gitignore              # ignores *.env; only *.env.example tracked
  stg.env.example         # secret KEYS for stg (placeholder values)
  prod.env.example        # secret KEYS for prod
```

**Resolution order (last wins):**
`base.env` → `services/<svc>.env` → `<env>.env` → injected secrets.

**The one hard rule:** a var is declared in exactly one tier as its source of
truth. The validation gate flags drift; never declare the same var's real value
in two tiers.

### Secret vs non-secret — answerable by directory

- `env/**` is non-secret and reviewable in PRs. Secret values NEVER go here.
- `secrets/<env>.env.example` lists secret KEYS with placeholder values so the
  required set is reviewable and the gate can assert completeness.
- The per-directory `secrets/.gitignore` (`*.env` + `!*.env.example`) is
  **load-bearing**: the repo-root bare `.env` entry does NOT cover
  `secrets/prod.env` (a bare `.env` pattern matches files literally named
  `.env`, not `secrets/<env>.env`). Without it, an operator's local
  `secrets/prod.env` would be silently committable. The gate additionally fails
  if any tracked file under `secrets/` is not a `*.env.example` catalogue.

## The validation gate (`scripts/env_validate.py`)

Runs in CI via `.github/workflows/env-validate.yml`. Four checks, in the
design's priority order:

| Check | What it asserts | Where it runs |
|---|---|---|
| `settings-load` | Each service's Settings loads from the assembled non-secret tiers + secret placeholders, with no ValidationError. The forcing function. | deploy repo alone (hermetic) |
| `secret-keys` | `secrets/<env>.env.example` ⊆ `deploy-<env>.yml` `env_keys` passlist; every `${VAR:?}` compose guard satisfied by some tier; no real secret committed under `secrets/`. | deploy repo alone |
| `os-environ` | No raw os env-config under `**/src/**` — flags both `os.environ`/`os.getenv` (dotted) and `from os import environ/getenv` (direct import); module aliasing (`import os as o`) is a documented limitation (Settings mandatory in service src/; tests/scripts allowed — decision #3). | over any `src/` checked out |
| `inventory` | `docs/env-inventory.{csv,md}` not stale vs a fresh `env-inventory.py` scan. **Warn-only** — staleness emits a `::warning::` and stays GREEN (org-wide drift is non-blocking by design; refresh tracked in deploy#398). A genuine generator failure is still a hard error. | org-tree job (siblings checked out) |

Run locally:

```bash
python3 scripts/env_validate.py all          # every check
python3 scripts/env_validate.py settings-load # one check
```

`os-environ` and `inventory` no-op when the relevant trees aren't present (the
deploy repo has no `src/`; the inventory check needs the full 7-repo org tree),
so `all` is safe to run from a deploy-only checkout.

### Per-service Settings shims

`settings-load` uses thin per-service shims in `scripts/env_validate_schemas.py`
rather than importing each service's real Settings class — the deploy repo's CI
checks out only the deploy repo, so service `src/` packages aren't importable
(the design blesses this). The shims mirror the security-relevant validators of
the real classes (user-service `ENVIRONMENT` allowlist + OAuth-override
prod-guard + RS256 keypair presence; isnad-graph DB-credential presence). **Keep
them in sync** when a service adds a validated, deploy-relevant field:

- user-service: `noorinalabs-user-service/src/app/config.py`
- isnad-graph: `noorinalabs-isnad-graph/src/config.py`

## Add a new environment (`dev`, customer env `acme`, …)

1. `cp env/stg.env env/<new>.env`; edit per-env non-secret values
   (`BASE_DOMAIN`, `*_CONFIG_FILE`).
2. `cp secrets/stg.env.example secrets/<new>.env.example`; this is the checklist
   of secrets to provision.
3. Provision the secret values: create a GH Actions Environment `<new>` and
   `gh secret set` each key from the catalogue.
4. Add `deploy-<new>.yml` (or extend the matrix) calling `write-deploy-env`
   with `environment: <new>` and `base_domain`. Keep its `env_keys` passlist in
   sync with `secrets/<new>.env.example` — the gate enforces this.
5. Add the new env to `ENVS` in `scripts/env_validate.py` so the gate validates
   it. CI then load-tests every service against the new env on every PR.

## Migration status (incremental, not big-bang)

deploy#363 establishes the `env/` + `secrets/` skeleton and the validation gate
(the forcing function). The next incremental steps, gated by the validator as
each lands:

- Wire `write-deploy-env` to assemble the live `.env` from the `env/` tiers (it
  currently writes per-env constants via the caller's `extra_env`/`env_keys`).
  Touches the live deploy path — sequence carefully against in-flight compose
  work.
- Collapse the remaining multi-source vars to single-source tier-by-tier
  (97 → 0), with `secret-keys`/`settings-load` enforcing single-source as each
  moves.

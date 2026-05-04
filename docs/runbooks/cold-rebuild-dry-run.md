# Cold-rebuild dry-run

## Purpose

The cold-rebuild dry-run is the **acceptance gate for the first-deploy /
cold-start workflow-bug class** that surfaced under emergency pressure
during P3W2 (2026-05-01 → 2026-05-02).

PR-time review cannot detect this class — every one of these bugs only
manifests when the workflow encounters fresh / empty / cold state for the
first time. The gate codifies each known bug shape as a static or dynamic
invariant and runs it on every PR that touches a promotion-pathway,
migration, or TF-apply workflow.

This runbook is acceptance criterion 1 for [`#249`](https://github.com/noorinalabs/noorinalabs-deploy/issues/249).

## What the gate covers

The gate is implemented as `.github/workflows/cold-rebuild-dryrun.yml` and
the helper script `.github/workflows/scripts/cold_rebuild_static_checks.py`.

| Bug | Issue | Fix PR | Detection |
|-----|-------|--------|-----------|
| terraform.yml ephemeral keypair | [#216](https://github.com/noorinalabs/noorinalabs-deploy/issues/216) | [#217](https://github.com/noorinalabs/noorinalabs-deploy/pull/217) | static — `cold_rebuild_static_checks.py` asserts (a) terraform.yml contains no `ssh-keygen`, (b) `modules/hetzner-vps/deploy.pub` exists, (c) env-root `ssh_public_key_path` defaults point at the canonical pubkey. |
| promote.yml retag-token mismatch | (no issue — fixed inline via PAT regen + `GH_PACKAGES_TOKEN` swap) | (inline) | static — asserts the retag job's docker/login-action authenticates with `secrets.GH_PACKAGES_TOKEN`, never `secrets.GITHUB_TOKEN`. |
| promote.yml stg-latest TOCTOU | [#234](https://github.com/noorinalabs/noorinalabs-deploy/issues/234) | [#236](https://github.com/noorinalabs/noorinalabs-deploy/pull/236) | static — asserts `Resolve source tag` step assigns `source_tag="sha-${SHORT}"` and never to a `*-latest` floating tag. |
| promote.yml multi-arch parity | [#239](https://github.com/noorinalabs/noorinalabs-deploy/issues/239) | [#240](https://github.com/noorinalabs/noorinalabs-deploy/pull/240) | dynamic — `buildx-shape-dryrun` job builds a multi-arch + single-arch fixture against a local registry, runs `imagetools create`, and asserts the shape-aware parity logic from promote.yml passes for both shapes. Includes a negative control that asserts the pre-#239 naive comparison still fails on single-arch. |
| db-migrate.yml psycopg-vs-asyncpg | [#235](https://github.com/noorinalabs/noorinalabs-deploy/issues/235) | [#236](https://github.com/noorinalabs/noorinalabs-deploy/pull/236) | static — extracts the `postgresql+<driver>://` scheme from db-migrate.yml's `DATABASE_URL=` line and asserts the driver package is declared in user-service/pyproject.toml (read from the wave-aware sibling checkout, matching `integration-tests.yml`'s pattern). |

## When the gate runs

- **Every PR** that touches any of: `promote.yml`, `terraform.yml`,
  `db-migrate.yml`, `deploy-{stg,prod,all,isnad-graph,landing-page}.yml`,
  `cold-rebuild-dryrun.yml`, `cold_rebuild_static_checks.py`,
  `terraform/hetzner/**`, this runbook.
- **Push to `main`** or any `deployments/phase-*/wave-*` branch with the
  same path filter — catches direct merges and wave-branch progress.
- **Weekly schedule** (Mon 06:00 UTC) — catches drift introduced by PRs
  that did not match the path filter (e.g., a refactor in
  `integration-tests/scripts/check_tag_invariants.py` that silently
  changed the contract).
- **`workflow_dispatch`** — operator-initiated re-run.

## How to read a failure

The summary job emits a per-bug-guard table. Click into the failed
upstream job for the diagnostic line. Each `cold_rebuild_static_checks.py`
failure prints in the form:

```
[FAIL]  Promote: no floating-tag source (bug #3, #234)
  - [promote-no-floating-source] (#234) promote.yml `Resolve source tag` ...
```

The bracketed check ID is searchable in the script. The parenthesized
issue is the canonical bug ticket.

### Static check failures

Open `cold_rebuild_static_checks.py` and grep for the check ID. The
docstring of the function that emitted the failure describes the bug
shape and the fix shape. Fix the workflow to restore the fix shape
(commit-by-commit revert if a recent change re-introduced the bug
shape).

### `buildx-shape-dryrun` failures

A failure here means either:

1. The shape-aware parity logic was regressed — check whether
   `promote.yml`'s `Verify digest parity` step changed. The fixture-pair
   builds in this job mirror the production shape distribution
   (api/frontend/user-service multi-arch; landing-page single-arch).
2. `docker buildx imagetools create` behavior changed in a runner image
   bump. Compare the runner's docker version to the version in use when
   #239 was filed (Docker 24.0.x era). If buildx semantics changed,
   review whether #240's shape-aware logic is still load-bearing — the
   negative-control step in this job will surface that as a `::warning::`.

## Limitations

This is a **regression gate for known bug shapes**, not a discovery tool
for new first-deploy bugs.

- Genuinely-novel cold-state defects (e.g., a future cloud-init template
  change that breaks `cloud-init status` on fresh VPSes) still have to be
  found at first-deploy time.
- The gate does **not** apply Terraform against a real Hetzner project,
  does **not** push to the real GHCR, and does **not** run alembic
  against a real Postgres. Real cold-rebuild ship-tests still happen at
  the next first-deploy event (new VPS bring-up, new env, DR restore).
- The `buildx-shape-dryrun` job is hermetic: it uses a `registry:2`
  service container on the runner and `localhost:5000` references. It
  does NOT exercise GHCR's auth path or rate limits.
- The driver/pyproject parity check assumes the user-service image is
  built from `pyproject.toml` and that any DB-driver package is declared
  there as a dep with a quoted-string-plus-version-constraint shape
  (matches today's `"asyncpg>=0.30.0",`). A future move to a different
  packaging tool may need the regex updated.

## Manual cold-rebuild ship-test

The dryrun gate does NOT replace a real cold-rebuild rehearsal. When the
team needs to validate end-to-end cold provisioning (new env, DR drill,
pre-merge of a major TF change), follow these steps against a scratch
Hetzner project:

1. **Hetzner**: create a fresh project. Do NOT reuse the existing
   `noorinalabs` project — TF state collisions guaranteed.
2. **TF state**: temporarily point `TF_STATE_B2_KEY_ID` /
   `TF_STATE_B2_APP_KEY` at a scratch B2 bucket, OR override
   `state_key` in the env root to `hetzner/cold-test.tfstate`.
3. **GHCR**: do NOT exercise promote.yml against the real registry. Use
   the dryrun job's local-registry fixture pattern if you need to test
   retag changes; for the real first-deploy class, observe the next
   genuine first-deploy event.
4. **Alembic**: spin up a scratch user-postgres + user-service image
   locally (`docker-compose.dev.yml` is sufficient) and run `alembic
   upgrade head` against the URL the workflow assembles.
5. **VPS bring-up**: `terraform apply` from the scratch state, then SSH
   in with the `DEPLOY_SSH_PRIVATE_KEY` half. Verify the canonical
   pubkey from `modules/hetzner-vps/deploy.pub` is in
   `/home/deploy/.ssh/authorized_keys`. Tear the VPS down when done.

This rehearsal is at-most quarterly — the dryrun gate is what catches
day-to-day regressions.

## Adding a new guard

When a new first-deploy / cold-start bug surfaces:

1. **Triage the bug** as usual. Land the fix PR.
2. **Add a guard** to `cold_rebuild_static_checks.py` (for static-shape
   bugs) or to `cold-rebuild-dryrun.yml` (for dynamic-shape bugs that
   need a fixture). The guard should fail loudly if the bug shape
   reappears AND prove the fix shape is still in place.
3. **Update this runbook**: add a row to the "What the gate covers"
   table with the issue number, fix PR, and detection mechanism.
4. **Update `cold-rebuild-dryrun.yml`'s top-of-file comment** with the
   new bug count and a one-line summary.

The path filter on the workflow's `pull_request` / `push` triggers
already covers any future workflow file added under `.github/workflows/`
that matches the existing patterns; if the new workflow falls outside
those patterns, add its path to the filter.

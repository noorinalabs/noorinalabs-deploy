# noorinalabs-deploy

Deployment orchestration — Terraform, Docker Compose, GitHub Actions workflows.

This repo owns everything that runs on the production server: VPS provisioning
(`terraform/`), the production stack (`compose/`), the reverse proxy (`caddy/`),
the observability stack (`infra/`), backup units (`systemd/`), operational
scripts (`scripts/`), and the deploy/verify workflows (`.github/workflows/`).
Service repos own what they build; this repo owns what runs on the server. See
[`CLAUDE.md`](CLAUDE.md) for the full architecture and deployment flow.

## Git hooks (required)

This repo mirrors its CI checks locally via [pre-commit](https://pre-commit.com/).
After cloning, install BOTH hook stages once:

```bash
pre-commit install                       # commit-stage checks
pre-commit install --hook-type pre-push  # push-stage checks
```

- **Commit stage** runs: `terraform-fmt`, `terraform-validate`, `gitleaks`,
  `actionlint`, `ruff-format`, `ruff-lint`, and `env-example-check`.
- **Pre-push stage** runs: `mypy` (over `scripts/`, `.github/workflows/scripts/`,
  and `.claude/hooks/` when present), `pytest` for the sync-gate unit tests
  (`.claude/lib/tests/`), and `pytest` for the scripts tests (`scripts/tests/`).

These mirror the repo's CI workflows (`terraform.yml`, `lint-workflows.yml` /
`docs.yml`, `hooks-lint.yml`, `compose-validate.yml`, `precommit-ci-sync.yml`) so
failures surface locally before a PR — running BOTH installs is mandatory under
the org-wide local⇄CI parity rule (noorinalabs-main#684). The
`Pre-commit ⇄ CI sync-drift gate` (`.claude/lib/pre_commit_ci_sync.py`) fails the
build if a check CI enforces is dropped from this mirror.

Never bypass a hook with `--no-verify`, and never push, PR, or merge with a
known-failing check without explicit owner permission. If `pre-commit install`
"cowardly refuses" because `core.hooksPath` is set (some clones inherited a stale
path from a repo rename), unset it first so hooks resolve to this repo's own
`.git/hooks`:

```bash
git config --unset core.hooksPath
```

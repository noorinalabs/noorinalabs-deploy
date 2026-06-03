# Branch Protection — noorinalabs-deploy (P3 end-state #4, main#322)

Phase-3 end-state criterion #4 (`noorinalabs-main#322`): **CI failures block all
merges** on every repo's default branch, org-wide — enforced server-side by
GitHub, not only by the Hook 4 comment-gate. This directory carries the
canonical ruleset for this repo's `main`:

| File | Purpose |
|------|---------|
| `ruleset-main.json` | The repository ruleset payload (GitHub REST `/rulesets`). |
| `apply-ruleset.sh`  | Owner/admin-gated apply + read-back-verify. Idempotent (create-or-update). |
| `SPEC.md`           | This document — the shape and the why. |

This is noorinalabs-deploy's adoption of the parent-canonical spec
(`noorinalabs-main` charter `pull-requests.md` § *Org-Wide Branch Protection +
Admin-Merge Exceptions*), modeled on the W13 live pilot
(`noorinalabs-data-acquisition`, ruleset id `17091263`) and the W14 sibling
pilots (`noorinalabs-user-service`, `noorinalabs-landing-page`).

## Application status

The **spec + apply script** land in this PR (W14, `Refs noorinalabs-main#322`).
The actual **apply is owner/admin-gated** and is a **post-merge step**:

1. Creating a repository ruleset requires repo-admin permission, which the agent
   `gh` principal (`parametrization`) does not hold for this purpose.
2. Applying default-branch protection while a wave-branch PR is in flight can
   block our own merges, so the apply runs from a window with **no in-flight
   default-branch merge** — post-wave-wrapup is the safe window.

So #322 is **met for this repo only when the owner has run `apply-ruleset.sh`
and read-back-verified the ruleset on `main`.** `#322` stays OPEN as the
org-wide rollout tracker until all 8 default branches carry the protection.

This repo currently has **no ruleset and no classic branch protection** on
`main` (`gh api repos/noorinalabs/noorinalabs-deploy/branches/main/protection`
returns 404 at the time of writing) — this ruleset is added fresh, there is no
existing default-branch protection to reconcile or duplicate. (The deploy
charter's existing "require 1 review" ruleset targets the `deployments/**` wave
branches — a *different* target ref — and is unaffected by this `~DEFAULT_BRANCH`
ruleset.)

## The ruleset shape (and why)

A **repository ruleset** targeting `~DEFAULT_BRANCH`, `enforcement: active`:

- **`pull_request` with `required_approving_review_count: 0`** — the load-bearing
  decision. GitHub's "require approvals" counts **formal** GitHub PR reviews,
  which our team structurally cannot produce: the `gh` auth principal IS the PR
  author (`parametrization`), so a formal self-approval **422s**, and our review
  discipline runs on **issue-comment verdicts** validated by Hook 4
  (`validate_pr_review`), not formal reviews. A naive "require 1 approval" rule
  would **deadlock every merge**. Reviewer-count enforcement stays with Hook 4.

- **`required_status_checks` rule — OMITTED for this repo** — the
  deploy-specific divergence from the user-service pilot. **Every one of
  noorinalabs-deploy's CI workflows is `paths:`-filtered** (terraform.yml,
  compose-validate.yml, hooks-lint.yml, lint-workflows.yml, integration-tests.yml,
  db-migrate*.yml, the new docs.yml, …). A `strict_required_status_checks_policy`
  ruleset hard-requires that *every listed context reports* before merge — but a
  path-filtered check **does not run** on a PR that does not touch its paths, so
  its context **never reports** and the merge **deadlocks**. user-service can
  hard-require `check`/`openapi-snapshot-drift` because its `ci.yml` is
  *unconditional*; deploy has no such always-on gate, so requiring any specific
  context here would block unrelated PRs.

  The intent is therefore **no required status check at the ruleset layer**. Note
  the GitHub REST API **rejects** a `required_status_checks` rule carrying an
  empty `required_status_checks` array (HTTP 422: "Invalid parameter
  required_status_checks: Expected at least 1 elements, got 0"), so the rule is
  **omitted entirely** rather than included-with-`[]`. (An earlier revision
  shipped the rule present-with-`[]`, which never applied — deploy#395; mirrors
  the parent-repo correction noorinalabs-main#322 / commit 29cfc88.) The
  PR-required + no-force-push + no-delete protections — the bulk of #322's
  intent — are fully active. **The server-side CI-failure-blocks-merge guarantee
  on deploy is instead carried by the operator-side Hook 4 +
  `validate_pr_ci_status` ADMIN_MERGE_EXCEPTION gate** until/unless an
  unconditional CI gate is added.

  **If deploy later gains an unconditional (un-path-filtered) PR CI gate**, add
  its job-name context here and to `apply-ruleset.sh`'s read-back, e.g.:

  ```json
  { "type": "required_status_checks",
    "parameters": { "strict_required_status_checks_policy": true,
      "required_status_checks": [ { "context": "<unconditional-job-name>" } ] } }
  ```

  Re-confirm any context against live check-runs at apply time — job names can
  change: `gh api repos/<repo>/commits/<default-sha>/check-runs --jq '.check_runs[].name'`.

- **`deletion` + `non_fast_forward`** — no force-push / branch-delete on `main`.

- **`bypass_actors`: Repository-admin (`actor_id: 5`, `bypass_mode: always`)** —
  keeps the orchestrator's `--admin` wave→main wrapup merges and the charter
  single-reviewer / doc-sweep / emergency exceptions working. The GitHub-side
  bypass is mirrored on the operator side by the hook-validated
  `ADMIN_MERGE_EXCEPTION` gate (`validate_pr_ci_status`), which **audits** every
  `--admin` merge to the Annunaki trail — defense in depth: the ruleset covers
  UI/external/batch-loop merges, the hook covers `gh pr merge` and names the
  exceptions.

## How to apply (owner)

```bash
# From a window with NO in-flight default-branch merge (post-wave-wrapup):
.github/branch-protection/apply-ruleset.sh            # create or update
DRY_RUN=1 .github/branch-protection/apply-ruleset.sh  # preview only

# Then read-back-verify the detail (rules + bypass actor):
gh api repos/noorinalabs/noorinalabs-deploy/rulesets \
  --jq '.[] | select(.name|startswith("Protect main")) | .id'
gh api repos/noorinalabs/noorinalabs-deploy/rulesets/<id>
```

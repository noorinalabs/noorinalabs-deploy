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

**Live** — ruleset id `17139848` is applied and enforced on `noorinalabs-deploy`'s
`main`, as of `2026-06-01T19:54:09-04:00` (renamed in place on `2026-09-10`; see
"The ruleset shape" below for the rename rationale). Verify at any time:

```bash
gh api repos/noorinalabs/noorinalabs-deploy/rulesets/17139848
```

Live rules, confirmed by read-back on 2026-09-10:

| Rule | Value |
|------|-------|
| `deletion` | present — no branch delete on `main` |
| `non_fast_forward` | present — no force-push on `main` |
| `pull_request` | `required_approving_review_count: 0`, `allowed_merge_methods: [merge, squash, rebase]` |
| `required_status_checks` | **omitted by design** — see below |
| `bypass_actors` | `actor_id: 5` (Repository admin), `bypass_mode: always` |
| `enforcement` | `active` |

`noorinalabs-main#322` (Phase-3 end-state criterion #4) **closed on
2026-06-02** once this and the other 7 default branches carried the
protection. Any remaining branch-protection follow-up items (such as this
rename) are tracked on the org-wide tracker `noorinalabs-main#1464`, which
stays open; #322 itself does not reopen for them.

The omission of `required_status_checks` here is **deliberate**, not an
oversight or a not-yet-applied state — see "The ruleset shape" below for why,
and note the ruleset's name now says so explicitly (`CI green enforced by
validate_pr_ci_status hook`) rather than implying a server-side status-check
gate exists. CI-green-blocks-merge for this repo is instead enforced at merge
time by the `validate_pr_ci_status` hook, which this repo's
`.claude/settings.json` registers (from the parent's absolute hook path) on
every `PreToolUse` Bash call. (The deploy charter describes a separate
"require 1 review" rule for `deployments/**` wave branches; no such ruleset
is applied — per-ref rules for those branches return empty, and review-count
enforcement there is hook-side (`validate_pr_review`) — see deploy#712.)

`apply-ruleset.sh` remains the idempotent create-or-update tool for future
changes to this ruleset (e.g. adding `required_status_checks` if deploy ever
gains an unconditional CI gate — see below). Re-applying it is still an
owner/admin-gated step, run from a window with no in-flight default-branch
merge, because creating/updating a repository ruleset requires repo-admin
permission that the agent `gh` principal (`parametrization`) does not hold.

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

  The live ruleset's `name` was renamed on 2026-09-10 (owner ruling, "Rename to
  match the shape") from `Protect main — require PR + green CI (...)` to
  `Protect main — require PR; CI green enforced by validate_pr_ci_status hook
  (...)`, so the name stops promising a server-side "green CI" gate this
  ruleset deliberately does not provide — the enforcement point is the
  `validate_pr_ci_status` hook, registered from the parent's absolute path at
  `.claude/settings.json:59` in this repo. `ruleset-main.json`'s `name` field
  was updated to match in the same change (deploy-side half of
  `noorinalabs-main#1464`; see `noorinalabs-main#1544` for the parent-repo
  ruleset's matching rename).

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

`apply-ruleset.sh` matches the live ruleset by **name** (`select(.name == ...)`),
so if you are ever renaming a live ruleset, do the live rename first and only
then update `ruleset-main.json`'s `name` to match — updating the payload's name
before the live rename would make the script's name-match miss the existing
ruleset and attempt a `CREATE` (a duplicate) instead of an `UPDATE`.

```bash
# From a window with NO in-flight default-branch merge (post-wave-wrapup):
.github/branch-protection/apply-ruleset.sh            # create or update
DRY_RUN=1 .github/branch-protection/apply-ruleset.sh  # preview only

# Then read-back-verify the detail (rules + bypass actor):
gh api repos/noorinalabs/noorinalabs-deploy/rulesets \
  --jq '.[] | select(.name|startswith("Protect main")) | .id'
gh api repos/noorinalabs/noorinalabs-deploy/rulesets/<id>
```

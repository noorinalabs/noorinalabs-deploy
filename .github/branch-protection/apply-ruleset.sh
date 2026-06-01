#!/usr/bin/env bash
# Apply the noorinalabs-deploy default-branch protection ruleset.
#
# Phase-3 end-state criterion #4 (noorinalabs-main#322): CI failures block all
# merges on every repo's default branch, enforced server-side by GitHub (not
# only by the Hook 4 comment-gate). This script is the OWNER/ADMIN-gated apply
# step: it POSTs (or, if a same-named ruleset already exists, PUT-updates) the
# canonical ruleset committed alongside it (ruleset-main.json).
#
# Why this is a post-merge, owner-run step — not done in the PR:
#   Creating/updating a repository ruleset requires repo-admin permission, which
#   the agent gh principal (parametrization) does not hold for this purpose, and
#   applying default-branch protection while a wave-branch PR is in flight can
#   block our own merges. So the durable PR artifact is the SPEC + this script;
#   the owner runs it from a window with no in-flight default-branch merge
#   (post-wave-wrapup is the safe window), then read-back-verifies.
#
# Design notes (see SPEC.md in this directory + charter pull-requests.md
# § Org-Wide Branch Protection):
#   * required_approving_review_count: 0 — GitHub's "require approvals" counts
#     FORMAL PR reviews, which our team structurally cannot produce (the gh auth
#     principal IS the PR author, so a formal self-approval 422s). Reviewer-count
#     enforcement stays with Hook 4 (validate_pr_review). A "require 1 approval"
#     rule would deadlock every merge.
#   * Required status checks: EMPTY for deploy. Every deploy CI workflow is
#     paths-filtered, so a `strict` ruleset naming any context would deadlock
#     PRs that don't touch that path (the check never runs → its context never
#     reports). The PR-required + no-force-push + no-delete protections are still
#     fully active; the CI-blocks-merge guarantee is carried operator-side by
#     Hook 4 + the ADMIN_MERGE_EXCEPTION gate until deploy gains an unconditional
#     PR CI gate. When it does, add `{ "context": "<job-name>" }` to
#     ruleset-main.json's required set (confirm against live check-runs first).
#   * Repository-admin always-bypass (actor_id 5) keeps the orchestrator's
#     --admin wave→main wrapup merges + the charter single-reviewer/doc-sweep/
#     emergency exceptions working. The hook-side ADMIN_MERGE_EXCEPTION gate
#     (validate_pr_ci_status) audits every such bypass.
#
# Usage:
#   ./apply-ruleset.sh                 # apply to noorinalabs/noorinalabs-deploy
#   REPO=owner/name ./apply-ruleset.sh # override target repo (for re-use)
#   DRY_RUN=1 ./apply-ruleset.sh       # print the payload + planned action, no write

set -euo pipefail

REPO="${REPO:-noorinalabs/noorinalabs-deploy}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PAYLOAD="$SCRIPT_DIR/ruleset-main.json"

if [[ ! -f "$PAYLOAD" ]]; then
  echo "ERROR: ruleset payload not found at $PAYLOAD" >&2
  exit 1
fi

RULESET_NAME="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['name'])" "$PAYLOAD")"

echo "Repo:    $REPO"
echo "Ruleset: $RULESET_NAME"
echo "Payload: $PAYLOAD"
echo

# Is there already a ruleset with this name? (idempotent re-apply / update.)
EXISTING_ID="$(gh api "repos/$REPO/rulesets" \
  --jq ".[] | select(.name == \"$RULESET_NAME\") | .id" 2>/dev/null || true)"

if [[ "${DRY_RUN:-0}" == "1" ]]; then
  echo "DRY_RUN — would $( [[ -n "$EXISTING_ID" ]] && echo "UPDATE ruleset $EXISTING_ID" || echo "CREATE a new ruleset" ):"
  cat "$PAYLOAD"
  exit 0
fi

if [[ -n "$EXISTING_ID" ]]; then
  echo "Updating existing ruleset id $EXISTING_ID ..."
  gh api -X PUT "repos/$REPO/rulesets/$EXISTING_ID" --input "$PAYLOAD"
else
  echo "Creating new ruleset ..."
  gh api -X POST "repos/$REPO/rulesets" --input "$PAYLOAD"
fi

echo
echo "=== Read-back verification ==="
gh api "repos/$REPO/rulesets" \
  --jq ".[] | select(.name == \"$RULESET_NAME\") | {id, name, enforcement, target}"
echo
echo "Confirm the rules (pull_request req_approvals=0, deletion, non_fast_forward)"
echo "and the bypass actor in the ruleset detail (gh api repos/$REPO/rulesets/<id>)"
echo "before declaring #322 met for this repo. required_status_checks is empty by"
echo "design (deploy CI is path-filtered) — see SPEC.md."

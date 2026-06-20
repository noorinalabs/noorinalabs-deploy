---
name: feedback_ruleset_empty_checks_422
description: "GitHub /rulesets API 422s on an empty required_status_checks array; path-filtered-CI repos must OMIT the rule, not pass []"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 7e042acd-06d6-4813-a40c-4eac8f291ea2
---

When applying a GitHub repository ruleset via `gh api repos/<repo>/rulesets -X POST --input <json>`, a `required_status_checks` rule whose `required_status_checks` array is **empty** is **rejected**: `HTTP 422 Validation Failed — Invalid parameter required_status_checks: Expected at least 1 elements, got 0`.

For **path-filtered-CI repos** (CI `ci.yml` has a `paths:` filter, so gate jobs don't run on out-of-scope PRs — e.g. noorinalabs-main, noorinalabs-deploy), you cannot require any unconditional status check (it would deadlock a docs/status-only PR waiting on a check that never reports). The fix is to **OMIT the entire `required_status_checks` rule object** from the `rules` array — NOT include it with `[]`. The ruleset then enforces PR-only + no-force-push/branch-delete; per-PR merge-on-red stays with Hook 14 (`validate_pr_ci_status`).

**Why:** The canonical branch-protection `ruleset-main.json` for path-filtered repos was authored with an empty-array `required_status_checks` — a delivered-but-never-applicable artifact (deploy carried 0 live rulesets, confirming it never applied). Exactly the case the W14-retro delivered-vs-applied charter rule exists to catch.

**How to apply:** When applying or authoring a ruleset for a path-filtered-CI repo, drop the `required_status_checks` rule entirely. For repos with unconditional CI, keep it with live-verified job-name contexts. ALWAYS re-confirm each context against `gh api repos/<repo>/commits/<default-sha>/check-runs --jq '.check_runs[].name'` at apply time — matrix jobs expand (`ci` → `ci (20.x)`), so an exact-name context can silently never match. Read-back-verify every applied ruleset at origin (`gh api .../rulesets/<id>`). Admin always-bypass (actor_id 5) preserves the orchestrator's direct contents-API status/ontology pushes to main — prove it with a probe PUT after applying. P3W15 #322 8/8 apply, 2026-06-01.

---
name: project_promote_gate_stg_verify_refresh
description: "How to satisfy deploy promote.yml's v2 stg-verify gate before a prod promotion (workflow_run-only; refresh via deploy-stg)"
metadata: 
  node_type: memory
  type: project
  originSessionId: 090bf6d5-0d19-47c9-9b85-67bfff1c5396
---

deploy `promote.yml`'s Schema-v2 "Gate — stg verify-deploy success" (deploy#199) honors ONLY `workflow_run`-triggered verify-deploy runs (the ones auto-fired after a "Deploy to staging" run completes). A manual `gh workflow run verify-deploy.yml -f target=stg` (event=`workflow_dispatch`) is **deliberately ignored** by the gate — it walks runs and `continue`s on any `event != workflow_run`.

So when promote is blocked because the latest qualifying stg-verify artifact is stale or its digests diverge from current `stg-latest`, the refresh is:
1. `gh workflow run deploy-stg.yml --ref main -f image_tag=stg-<short>` (redeploy current stg-latest to stg; idempotent).
2. That completion auto-fires a `workflow_run` verify-deploy that snapshots the **current** stg-latest per-service digests into a fresh `stg-verify-result` artifact.
3. Re-run `promote.yml`. The gate now compares plan-resolved stg-latest digests vs that fresh artifact.

Other promote mechanics confirmed 2026-06-12 (P4W3, deploy#420 prod-login fix):
- `promote.yml` empty `source_sha` = promote whatever `stg-latest` points to (resolves via `:stg-latest`); non-empty = explicit `sha-<short>` path (resolves via `:sha-<short>`). Both resolve via `docker buildx imagetools inspect --format '{{.Manifest.Digest}}'`.
- Scope with `-f images="api,frontend"` to promote only the isnad-graph build and skip the user-service alembic gate (per per-service tag routing [[feedback_stg_deploy_per_service_tag_routing]]).
- The `production` GH-environment has `deployment_branch_policy: null` (no branch restriction), so promote can be dispatched from a wave branch (`--ref deployments/phase-4/wave-3`) and still get the production environment + owner-approval gate — no main hotfix required for a gate fix that lands on the wave branch.
- The v2 per-service gate had a real defect that blocked ALL v2 promotions (deploy#423): its `verify_digest` extraction passed the service key as a trailing python ARG (`python3 -c "…os.environ['K']…" K="$key"`) instead of an env PREFIX (`K="$key" python3 -c …`), so `os.environ['K']` raised KeyError → swallowed by `2>/dev/null||echo ""` → empty verify_digest → false "N of N divergence" every run. Real fix = env-prefix (PR #425). NB #424 (whitespace-strip) was a WRONG-RCA first attempt (hypothesised `\r`); harmless but didn't fix it — my local repro masked the bug by accidentally using the prefix form. Lesson: reproduce the EXACT command form (argv vs env-prefix matters). See [[feedback_prod_frontend_runtime_config_lag]].

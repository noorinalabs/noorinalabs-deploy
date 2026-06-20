---
name: feedback_db_migrate_gate_expected_head_drift
description: "deploy db-migrate-ci-gate fails on ANY compose-touching PR when its EXPECTED_MERGE_HEAD pin lags the user-service stg-latest image's alembic head."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 7e042acd-06d6-4813-a40c-4eac8f291ea2
---

noorinalabs-deploy's `db-migrate-ci-gate.yml` triggers on `compose/docker-compose.prod.yml` changes and pulls `ghcr.io/noorinalabs/noorinalabs-user-service:stg-latest`, then asserts the image's alembic head == `EXPECTED_MERGE_HEAD` pinned in `db-migrate.yml` (L70). When a user-service migration lands and advances `stg-latest` (e.g. 0040→0041) WITHOUT a matching deploy-repo PR bumping that pin, the gate fails `alembic_version mismatch — got '0041', expected '0040'` on EVERY subsequent compose-touching PR — even ones with zero DB relevance (e.g. caddy/alloy healthcheck edits, P4W1 deploy#402 / PR #409 2026-06-02).

**Why:** the pin is a cross-repo coupling that only the deploy repo can bump, but it's invalidated by a user-service merge — classic drift where "local green" (the user-service PR) leaves a sibling red.

**Tracking issue:** deploy#407 (diagnosed by Idris, fixed by Aisha in PR #411 — bump 0040→0041, +8/-5 to db-migrate.yml — folded into P4W1; merged with all 18 checks green incl. the alembic dry-run gate). I (Lucas) was second reviewer on #411 and independently confirmed 0041 is the sole user-service alembic head before approving.

**How to apply:** (1) When triaging a compose PR's red `db-migrate-ci-gate`, check whether the diff actually touches migrations/db-migrate.yml; if not, it's this drift, NOT your bug — attribute it as pre-existing and surface separately. (2) The real fix is a deploy PR bumping `EXPECTED_MERGE_HEAD` in `db-migrate.yml` in lockstep with the user-service migration (the workflow comment at L67-70/L192 says exactly this). (3) Recurrence-prevention follow-up (Idris, NOT yet implemented as of PR #411): either derive the expected head from checked-out user-service source rather than a literal, OR a user-service-side CI gate that fails a migration-adding PR unless deploy's `EXPECTED_MERGE_HEAD` is bumped in lockstep (org pattern: machine-enforce cross-artifact sync, cf. pre-commit⇄CI sync-drift gate). Companion to [[feedback_cross_repo_wave_ref_resolution]] and the general "local clean must not diverge from CI/sibling" theme.

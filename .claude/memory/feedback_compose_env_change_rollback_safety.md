---
name: feedback_compose_env_change_rollback_safety
description: "Compose env-var changes must stay rollback-compatible — rollback.yml rewrites only the image tag against CURRENT compose, so removing an old image's env vars bricks rollback."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 7e042acd-06d6-4813-a40c-4eac8f291ea2
---

When a compose env change switches a service from one config form to another (e.g. interpolated `DATABASE_URL`/`REDIS_URL` → discrete `DATABASE_*`/`REDIS_*` component vars, deploy#403/PR#415), do NOT remove the old form — KEEP both.

**Why:** `rollback.yml` does `actions/checkout` of the CURRENT ref + `sed`-rewrites only the image-tag env var in `.env` + `docker compose up --force-recreate` against the CURRENT `compose/docker-compose.prod.yml`. It does NOT check out a historical compose. So rolling a service back to an OLD image that reads only the removed env form runs against a compose that no longer provides it → the app falls back to in-image localhost defaults → bricks the container exactly when rollback is needed. Forward deploy looks fine, masking the regression.

**How to apply:** For a config-form migration in compose, ship BOTH forms keyed to the same secrets. The new image ignores the old form when the new trigger is set (e.g. us#151 ignores `DATABASE_URL` when `DATABASE_HOST` is set), an old rolled-back image uses the retained form. Strictly safe both directions; passlist-drift gate tolerates extra keys. Verify the rollback workflow's actual mechanism (checkout ref + what it rewrites) before claiming "no breakage window" — state it for forward AND rollback. Aisha-caught, P4W1. Related: [[feedback_runtime_gate_scoping]], [[feedback_verify_diagnosis_before_delegating]].

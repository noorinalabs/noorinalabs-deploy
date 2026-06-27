# W21 isnad-graph → prod promotion readiness (#723)

> DIAGNOSTIC / READINESS runbook — NON-MUTATING. Documents how the owner promotes
> the W21 isnad-graph build to prod ahead of the main#723 data re-validation. No
> step here mutates prod; the owner runs the promotion + approval deliberately.

Author: Weronika Zielinska (Platform Architect, noorinalabs-deploy)
Date: 2026-06-26/27 UTC · main#723 prod re-validation prep

## TL;DR

- Staging IS the W21 build (ig `e4c0c542`, PR #1138; `_active_window` enricher +
  `RETURN count(n) AS matched` fix verified in tree).
- The `image-tag-invariants.yml` failure is a **tooling/pagination bug**, NOT a
  real registry tag-state problem. The registry is healthy.
- BUT the same pagination bug also breaks `promote.yml`'s **default (stg-latest)
  plan path** for the ig images → so it IS a soft blocker for the *default*
  invocation. Work around it by promoting with explicit `source_sha=e4c0c54`.
- stg-verify artifact is **schema v2** → gate is per-service digest equality,
  age-independent. Digests match. No `skip_stg_verify` needed.
- Prod approval gate fires at the `retag` job (`environment: production`,
  reviewer = `parametrization`).

## Turnkey promotion command (owner runs after approving)

```bash
gh workflow run promote.yml \
  --repo noorinalabs/noorinalabs-deploy \
  --ref main \
  -f source_sha=e4c0c54 \
  -f images=api,frontend
# leave skip_stg_verify / skip_alembic_gate / break_glass_reason EMPTY
```

`source_sha=e4c0c54` is the load-bearing input: it makes `plan` resolve digests
by direct `sha-e4c0c54` inspect instead of the 100-tag stg-latest walk that the
pagination bug breaks. It pins the exact W21 digest either way.

Approval point: the run pauses at **Retag (stg → prod)** for the `production`
environment reviewer (owner). Approve in the run's web UI. After retag, the
unguarded `trigger-prod-deploy` job dispatches `deploy-prod.yml` to the VPS.

## Rollback (if prod smoke fails)

```bash
gh workflow run rollback.yml --repo noorinalabs/noorinalabs-deploy --ref main \
  -f service=all -f image_tag=prod-e850e36
```

Previous prod = `prod-e850e36` (api sha256:49e662…, frontend sha256:3166a9…).
`rollback.yml` is also `environment: production` (owner approval again).

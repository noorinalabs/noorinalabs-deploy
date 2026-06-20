---
name: feedback_stg_deploy_per_service_tag_routing
description: "deploy-stg must route the dispatching service's sha to ONLY that service's image tag; a single shared IMAGE_TAG breaks cross-service (user-service sha pulled the isnad frontend → not found)."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 080813cd-f3b8-434d-974c-badf58620c96
---

`noorinalabs-deploy/.github/workflows/deploy-stg.yml` originally resolved a single `IMAGE_TAG=stg-<dispatch-sha>` from *any* `repository_dispatch`, but compose binds `IMAGE_TAG` to **both api and frontend** (isnad-graph images) while user-service/landing have their own tag vars. So a **user-service** merge (dispatch `deploy-noorinalabs-user-service`, sha e.g. `bfe16c9`) made api/frontend try to pull `stg-bfe16c9` — a tag that only exists in the *user-service* GHCR package → `frontend: not found`. Every user-service-only stg deploy failed; it only self-healed when an isnad-graph push happened to follow. Fixed in deploy#418 / PR #419 (2026-06-10).

**Why:** each service has an independent commit stream and its own GHCR package. One dispatch sha is meaningful for exactly one service's images. `deploy-prod.yml` already did this right (separate `API_FRONTEND_TAG`/`USER_SERVICE_TAG`/`LANDING_TAG`, each → `*-latest` fallback); stg was the outlier.

**How to apply:** when a fan-in deploy is triggered by a service dispatch, key on `github.event.action` and route the sha to ONLY that service's image tag; every other service falls back to `stg-latest` (its last-good pointer), passed via the composite's `extra_image_tags`. Add a pre-flight GHCR manifest-existence check (`docker buildx imagetools inspect` on the runner, after `docker login`) over **all** services `PULL_SERVICES` lists, before touching the VPS — a missing image then fails fast with a named `MISS` line instead of a raw daemon error mid-deploy. Monitor the dispatched run with `/watch-deploy stg <sha>` (main#623). Verify each service publishes `stg-latest` before relying on the fallback.

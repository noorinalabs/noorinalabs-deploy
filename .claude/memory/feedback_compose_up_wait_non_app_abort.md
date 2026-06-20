---
name: feedback_compose_up_wait_non_app_abort
description: "Prod `docker compose up --wait` health-gates the ENTIRE stack — an unhealthy NON-app service (e.g. the kafka pipeline broker) aborts the bring-up and can leave caddy(reverse-proxy)+frontend stuck 'Created' → TOTAL edge outage (521), even when the app's own deps are healthy. Scope the prod rollout `up` to the app+edge tier; never gate the app on pipeline-tier health."
metadata:
  node_type: memory
  type: feedback
  originSessionId: 090bf6d5-0d19-47c9-9b85-67bfff1c5396
---

P4W3, first real **v2** prod ship (deploy#420 login fix, 2026-06-12). Approving the promote → retag → deploy-prod chain caused a **full prod outage** — not from the app change, but from deploy-path fragility. Three distinct gaps surfaced, all filed (deploy#427/#428/#429):

1. **#429 — `compose up --wait` aborts the whole bring-up on ANY unhealthy service.** deploy-prod runs `docker compose up --wait` over the FULL stack. Kafka (the ingestion broker) crash-looped, so `--wait` aborted the dependency-ordered bring-up *before* it reached caddy + frontend (later in the graph) → both stuck in **`Created`**. api came up healthy on the new tag, but **caddy (reverse proxy) never started → all public endpoints 521 (Cloudflare origin-unreachable) → total outage.** The app's own deps (neo4j/postgres/redis) were all healthy and the new image was correct — the app was hostage to an unrelated pipeline broker's health. **Fix: scope the prod `up` to the app+edge tier (api/frontend/caddy + their real deps); bring up the pipeline tier non-gating (exclude from the `--wait` set).** Recovery was a targeted `docker compose ... up -d frontend caddy` (neither depends on kafka) → ~30s, 521s cleared, fix live.

2. **#428 — prod-only dirty kafka volume + preflight blind spot.** Kafka crash-looped because prod's `noorinalabs_kafka_data` volume carried a leftover bitnami-era `config/` dir at the KRaft log-dir root; apache/kafka:3.9.2 LogManager fatally rejects non-topic dirs. STG was clean (migrated onto a fresh volume in the bitnami→apache cutover #385/#100); prod kept the old layout. The #393 re-bootstrap runbook (`docs/runbooks/kafka-cluster-id-rebootstrap.md`) covers this CLASS but its `write-deploy-env` preflight only compares cluster-id, so the stray-dir variant slipped through to `compose up`. Remediation = wipe+reformat the volume (pipeline replays from B2 by design — accepted in #385/#100). **Volume wipe is pipeline-owner-gated (Bereket/Nurul) — do NOT wipe without sign-off.**

3. **#427 — auto-rollout silently skipped.** `trigger-prod-deploy` in promote.yml has no explicit `if:`, so its implicit `if: success()` evaluates the **transitive** needs closure. A skipped `migrate-prod` (user-service not in an api/frontend-only promotion) made it return false → the VPS rollout job skipped silently, even though its direct needs [plan, retag] both succeeded. So the auto-rollout had effectively NEVER fired for a no-user-service promote; Aisha had to dispatch deploy-prod.yml manually. Fix: `if: ${{ always() && needs.plan.result=='success' && needs.retag.result=='success' }}` (same guard `retag` already uses).

**General lessons (reusable):**
- A monolithic `compose up --wait` couples app availability to EVERY service's health. Tier the rollout: app+edge must come up independently of pipeline/analytics/monitoring services. A broker hiccup must never down the reverse proxy.
- GitHub Actions implicit `if: success()` is evaluated over the **transitive** needs graph, not just direct `needs`. A skipped ancestor (even one a mid-job survived via `if: always()`) silently skips no-`if:` downstream jobs. Give rollout/finalizer jobs an explicit `always() && <direct-needs>=='success'` guard.
- Staging green ≠ prod green when the divergence is **volume/state**, not code/image: stg migrated onto a clean volume; prod inherited dirty state. State-migration parity is its own check. Companion to [[feedback_passing_repro_masks_bug]].
- Incident discipline that worked: SSH ground-truth (`docker compose ps`) corrected an optimistic "app probably rolled fine" read (caddy=`Created` was the real outage); targeted non-destructive restore (start only the stuck app+edge containers) before any destructive infra fix; destructive kafka volume wipe held for explicit pipeline-owner authorization.

---
name: project_streaming_e2e_prereqs
description: "Kafka streaming-pipeline E2E on staging (B2→Kafka→workers→Neo4j) prerequisites + gotchas — deferred past P4W6; #601 met via batch load instead"
metadata: 
  node_type: memory
  type: project
  originSessionId: 44d2d904-df74-40d3-b13d-f18441e349aa
---

P4W6 satisfied main#601 ("data pipeline runs E2E to Neo4j on staging") via the
**batch** path (da#141 `scripts/narrator_graph/run.py --skip-resolve` loads
pre-produced parquets directly into staging Neo4j). The **Kafka streaming** path
(ip#83 workers: dedup/enrich/normalize/graph-load) was deliberately left
**deployable-but-down** — bringing the workers up now would idle them (no
producer feeds `pipeline.raw.landed` yet). Bjørn's verified prerequisites for the
future streaming E2E (don't re-discover these):

1. **`ghcr-publish.yml` must be on `main` before `workflow_dispatch` works** —
   GitHub only registers a dispatch workflow once it's on the default branch.
   `gh workflow run … --ref <wave>` 404s until then. Merging ip→main publishes
   `latest`+`stg-<sha>`+`stg-latest` and does NOT deploy (no deploy fan-in by design).
2. **First GHCR publish = PRIVATE package → the box (anonymous pull) 404s.**
   Sibling app packages are public. After first publish, flip
   `noorinalabs-isnad-ingest-platform` visibility → Public via GitHub UI
   (Packages → Settings → Danger Zone; no reliable REST flip). Verify:
   `gh api /orgs/noorinalabs/packages/container/noorinalabs-isnad-ingest-platform --jq .visibility`.
3. **Stale Kafka topics on the box** — created by the OLD `init-topics.sh`
   (`pipeline.raw.new`/`pipeline.norm.done`); workers consume canonical
   `pipeline.raw.landed`…`pipeline.normalize.done` with auto-create OFF.
   `kafka-init` is a completed one-shot — compose won't re-run it on file change.
   Force: `docker compose … up -d --force-recreate --no-deps kafka-init` then verify topics.
4. **A producer must feed `pipeline.raw.landed`** (acquire→B2→Kafka) — NOT built
   this wave. Without it the "offsets advance" assertion can't pass even with
   healthy workers. This is the real remaining gap for streaming E2E.

Compose delivery: box `/opt/noorinalabs-deploy` tracks `main`; wave-6 deploy =
main + deploy#440 only (4 workers + kafka-init; zero app-service changes; `.env`
gitignored so secrets preserved). Bring-up: name the 4 services explicitly, no
`--remove-orphans`; box Compose v2.40.3 ≥2.20 so later app `up -d --remove-orphans`
won't reap profiled workers. One-click bring-up tracked as deploy#443.
Trivy in the publish workflow runs AFTER push (`exit-code:1`) — a red publish run
still means the image IS on GHCR; check the digest. Full runbook: ip#83 PR #86 RUNBOOK.md.
See [[project_staging_pipeline_not_wired]], [[feedback_appears_in_merge_null]].

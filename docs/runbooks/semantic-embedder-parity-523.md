# Runbook: semantic-search embedder parity rollout (deploy#523)

> ROLLOUT runbook for the `compose/docker-compose.prod.yml` `api`-service change that
> moves the API onto the model-capable `-embed` image. NON-MUTATING to write; the
> owner/orchestrator runs the stg → prod sequence deliberately. This repo's PR only
> ships the IaC; it does not deploy.

Author: Weronika Zielinska (Platform Architect, noorinalabs-deploy)
Context: [deploy#523](https://github.com/noorinalabs/noorinalabs-deploy/issues/523)
· discovered during isnad-graph ig#1148 / PR #1164.

## Why

The `api` service ran the **torch-free** `noorinalabs-isnad-graph` runtime image with
no `EMBEDDING_MODEL`, so `src/config.py` fell back to the lexical `HashingEmbedder`
for **query** embedding — while the corpus was embedded with
`paraphrase-multilingual-MiniLM-L12-v2` (`MiniLM`). Both produce `vector(384)`, so the
cosine comparison raises **no** error: `GET /api/v1/search/semantic` returns **HTTP
200 with a meaningless ranking** (silent garbage), on **both** stg and prod (one
compose file drives both boxes). A plain "200 + non-empty" probe passes on the bug.

## The change (this PR — IaC only)

`compose/docker-compose.prod.yml`, `api` service only:

- **Image** → `${API_IMAGE:-ghcr.io/noorinalabs/noorinalabs-isnad-graph-embed}:${IMAGE_TAG:-latest}`.
  The `-embed` image already carries fastapi + `uvicorn[standard]` + the `src` wheel +
  torch + sentence-transformers + baked `MiniLM` weights, and is published with the
  same `<env>-<sha>` tag scheme (built on the same isnad-graph main push) — so it
  serves `uvicorn src.api.app:create_app` with **no isnad-graph change**.
- **`EMBEDDING_MODEL=paraphrase-multilingual-MiniLM-L12-v2`** added to the `api`
  environment (explicit even though baked into the image).
- **`--workers 4` → `--workers 2`** — each worker loads MiniLM independently on first
  search (no cross-process share), so worker count multiplies the resident model
  footprint.
- **`mem_limit` 2G → 4G** (hard cap), reservations 512M → 1G (soft/advisory outside
  Swarm) — the envelope `--workers 2` × ~1.5–3G ≈ 3–6G spans against the 4G cap:
  fine at the realistic inference low end, container-level OOM risk (api only) at the
  high end → **validate stg api RSS before promoting prod** (stg capacity check below).
- **Healthcheck `start_period` 20s → 60s** — conservative slack for the heavier
  image. NOTE this does **not** cover a startup model load: `get_embedder()` is
  `@lru_cache` and loads MiniLM lazily from the semantic-search **request** handler,
  never `lifespan`, so `/health` does not gate on it. The cold-load cost is
  first-semantic-request latency **per worker** (eager warmup tracked in ig#1175).
- **stg pre-flight** (`deploy-stg.yml`) now inspects the `-embed` manifest for `api`
  (it shares `AF_TAG`), not the torch-free image — otherwise the deploy#418 tag
  pre-flight would guard a manifest nothing pulls.
- **Smoke gate** (`verify_stg_smoke.sh` / `verify_prod_smoke.sh`) gains a semantic
  topical-relevance check (mirrors isnad-graph `scripts/semantic_smoke.py`): a
  hashing↔`MiniLM` mismatch now fails the post-deploy smoke instead of passing on
  garbage.

## Rollout sequence

Merge the compose PR first (staging deploys automatically on merge to main).

1. **stg** — the api + smoke change deploy together. `embed:stg-<sha>` already exists
   for every push, so the stg rollout pulls it with no extra step. Verify: the new
   `semantic /search (patience)` + `semantic /search (prayer)` smoke rows PASS.
2. **stg capacity check (gate before prod).** The 4G limit is a HARD cap and each
   worker's model RSS loads lazily on first search, so real footprint only appears
   under traffic. After warming both workers (hit `/api/v1/search/semantic` a few
   times — see the first-hit latency note below), check api RSS on the stg box:

   ```bash
   ssh <stg> 'docker stats --no-stream noorinalabs-api-1'   # MEM USAGE / LIMIT
   ```

   If RSS sits comfortably below 4G (expected for single-query inference), promote
   prod as-is. If it approaches 4G, **do not promote** — drop to `--workers 1` (or
   raise `limits.memory` within prod's ~8.2G headroom) in a follow-up PR first. A
   container-level OOM would restart-loop the api (api only, not host-wide).

   > First-hit latency: because the model loads lazily per worker, the first
   > semantic query each worker serves is several seconds slower (cold load). The
   > smoke's 15s `SEMANTIC_TIMEOUT` tolerates one cold load; a deterministic per-worker
   > warmup is the ig#1175 follow-up.
3. **prod** — promote, then approve the prod deploy. See the caveat below **before**
   promoting.

## CRITICAL prod caveat — the prod promotion MUST include `embed`

The `-embed` image has **no `prod-<sha>` tags** today: embed promotion is **opt-in**
(deploy#470) — a normal `api,frontend,user-service,landing` promotion does **not**
retag embed to prod. Because the `api` now *pulls* the embed image, the prod
promotion **must explicitly include `embed`** so `embed:prod-<sha>` exists before the
prod rollout pulls it — with the **same `source_sha`** as the stack, so the api's
`IMAGE_TAG` (`prod-<sha>`) resolves to a real embed manifest:

```bash
gh workflow run promote.yml --repo noorinalabs/noorinalabs-deploy --ref main \
  -f source_sha=<sha> \
  -f images=api,frontend,user-service,landing,embed
# approve the `production` Environment gate on the retag job
```

Omitting `embed` → prod `api` fails at `docker compose pull` (missing manifest). This
is registry-side retag (IaC), not a VPS one-off. `stg` needs no such step.

> Note: this is a **new consumer** of the embed image's prod tag. The
> [corpus-reembedding runbook](corpus-reembedding.md) § "Prerequisite for `prod`"
> documents the *re-embed job's* dependency on `embed:prod-latest`; here the **live
> api** depends on `embed:prod-<sha>`, so — unlike the re-embed prerequisite — you
> must promote embed **alongside** the stack (same sha), not alone.

## Rollback

Rollback is **IaC, never a hand-edit on the box** (owner directive). Two paths:

1. **Tag rollback** (`rollback.yml -f service=all -f image_tag=prod-<older-sha>`) —
   rolls image tags back to an earlier promoted sha. Works only if
   `embed:prod-<older-sha>` exists, i.e. that older sha was promoted **with embed**.
   Make "include embed" the standing prod-promotion habit so every promoted sha is
   rollback-eligible.
2. **Image revert to the torch-free API** (reverts the parity change itself, back to
   consistent-lexical `hashing` — a degraded-but-not-broken floor): **`git revert`**
   the compose change (or a one-line PR flipping the `API_IMAGE` default back to
   `ghcr.io/noorinalabs/noorinalabs-isnad-graph` **and** dropping the api's
   `EMBEDDING_MODEL`), then redeploy. Durable, reviewed, and survives the next deploy.

> Do **NOT** rely on hand-setting `API_IMAGE` in the VPS `.env`: `write-deploy-env`
> **truncates** `.env` on every deploy and does not re-emit `API_IMAGE`, so a box-edit
> silently reverts on the next deploy. If an operator must intervene between deploys as
> a stop-gap, treat any SSH `.env` override as **ephemeral** (reapply, or land the IaC
> revert above). A durable fast-override would require threading `API_IMAGE` as a
> `deploy-{stg,prod}.yml` input into the persisted env write — a possible follow-up if
> PR-speed rollback proves too slow (it should not: lexical-vs-garbage is degradation,
> not an outage).

## Follow-up (not a blocker)

With the api sourced from the embed image, the embed **publish** is now
release-critical (previously it mattered only for re-embed jobs). It already builds on
every isnad-graph main push; hardening it into the required publish set — and making
`embed` part of the default prod-promotion set now that the stack depends on it — is
worth a follow-up.

- **ig#1175** — eager MiniLM warmup in isnad-graph `lifespan` (+ deterministic
  per-worker post-deploy warmup) so the first-semantic-request cold load doesn't land
  on a real user. App-side fix; do not implement from this repo.
- Review comments already filed as out-of-scope follow-ups: **ig#1174** (embed-image
  Trivy scan gap) and **deploy#526** (promote digest-gate).

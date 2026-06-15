# Runbook: corpus re-embedding (isnad-graph semantic search)

**Scope:** the repeatable, env-gated mechanism that re-embeds the isnad-graph hadith corpus with a real 384-dim multilingual model, rebuilds the pgvector index, and verifies recall. Implemented in `.github/workflows/reembed-corpus.yml` + the profile-gated `isnad-graph-embed` compose service. Design of record: [ADR 0008](../adr/0008-corpus-reembedding-mechanism.md). Context issue: [deploy#461](https://github.com/noorinalabs/noorinalabs-deploy/issues/461); capstone run tracked in [isnad-graph#1071](https://github.com/noorinalabs/noorinalabs-isnad-graph/issues/1071).

**Why this exists:** semantic search returned lexically-arbitrary results because the 34,028-hadith corpus was embedded with the dependency-free `HashingEmbedder` (token-overlap, not meaning). The application already supports a real model via `EMBEDDING_MODEL`; this mechanism is the operational trigger to (re-)embed on the cluster. The owner directed (2026-06-15) that this be a GitHub Action / IaC artifact — NOT a one-off live SSH run — and declined an interim minimum-similarity floor.

**NOT in scope:**

- The embedding/reindex/recall CLI itself — that ships in isnad-graph (`isnad embed-hadiths` / `reindex-embeddings` / `verify-recall`, ig#1057 / ig#1088).
- Loading hadith nodes into Neo4j — that is the ingest pipeline (`docs/runbooks/` ingest material).

## The constraint that bites first: 384 dimensions

The pgvector embedding column is `vector(384)` with a write-time dimension guard. The model **MUST** emit 384-dim vectors, and (the corpus is bilingual Arabic + English) be multilingual.

- **Use `paraphrase-multilingual-MiniLM-L12-v2`** — 384-dim, multilingual, ~470 MB, CPU-viable. This is the workflow default.
- **Do NOT use `distiluse-base-multilingual-cased-v2`** — it is **512-dim** and will fail the dimension guard at write time. Any model swap must preserve 384-dim or ship a coupled migration of the pgvector column + guard.

## Architecture summary

```
reembed-corpus.yml (workflow_dispatch — env-gated, dry_run-default-true)
    │  SSH as deploy → docker compose --profile embed pull
    │
    ├── docker compose run --rm --no-deps isnad-graph-embed isnad embed-hadiths --batch-size N
    ├── docker compose run --rm --no-deps isnad-graph-embed isnad reindex-embeddings
    └── docker compose run --rm --no-deps isnad-graph-embed isnad verify-recall   ← exit code = GATE
           │
           └── emits /var/lib/node_exporter/textfile_collector/corpus_reembed.prom (if: always)
```

Each step is a one-shot `--rm` container from the profile-gated `isnad-graph-embed` service, attached to the live project's `backend` (neo4j/postgres/redis) **and** `egress` (HuggingFace weight download) networks, with the `st_model_cache` volume mounted at `SENTENCE_TRANSFORMERS_HOME` so the model downloads once and persists. DB credentials come from the VPS `.env` via compose interpolation — never on argv.

## How to trigger

1. Go to **Actions → "Re-embed corpus (isnad-graph semantic search)" → Run workflow**.
2. Inputs:
   | Input | Default | Notes |
   |---|---|---|
   | `env` | — (required) | `stg` or `prod`. `prod` requires the production Environment's manual approval. |
   | `model` | `paraphrase-multilingual-MiniLM-L12-v2` | MUST be 384-dim. |
   | `batch_size` | `256` | Embedding batch size. |
   | `dry_run` | `true` | Leave `true` to plan only. Set **`false`** for a real re-embed. |
   | `image` | _(empty)_ | Empty → `ghcr.io/noorinalabs/noorinalabs-isnad-graph-embed:<env>-latest`. Pass an explicit ref to pin. |
3. **First do a dry run** (`dry_run=true`): it logs the resolved plan and validates that the `isnad-graph-embed` service resolves under the live `.env`, writing nothing.
4. Then re-run with **`dry_run=false`** to execute. On `prod`, approve the queued Environment gate.

### Staging-first

Re-embed `stg` first, eyeball search quality, then `prod`. There is no automatic stg→prod chaining — each env is a separate dispatch (and prod is approval-gated).

## Expected duration

- **Cold model cache (first run on an env):** add the one-time ~470 MB weight download (a few minutes on the VPS link) on top of the embed time.
- **Embedding ~34k hadiths** on the single CPU VPS: order of minutes to low tens of minutes depending on `batch_size`. The `concurrency` group serializes runs per env, so a second dispatch queues rather than overlapping.
- **Index rebuild + recall verify:** seconds to a couple of minutes.

The `corpus_reembed_last_run_duration_seconds` metric records the actual wall-clock per run.

## Recall verification (the gate)

`isnad verify-recall` is the **gate**: its exit code fails the workflow. A green run means the re-embed produced an index that returns the expected documents for the verification queries. A red `verify-recall` means the corpus is NOT trustworthy — do not declare the re-embed done; investigate below.

Pass `--queries "patience,prayer"` style overrides only if you are debugging specific queries; the CLI default query set is what the gate uses.

## Observability

A `.prom` textfile-collector file is emitted on the VPS on every real run (skipped on `dry_run`), atomically (temp + rename), at `/var/lib/node_exporter/textfile_collector/corpus_reembed.prom`:

```
corpus_reembed_last_run_success{env="...",model="..."}            <0|1>
corpus_reembed_last_run_timestamp_seconds{env="...",model="..."}  <unix-ts>
corpus_reembed_last_run_duration_seconds{env="...",model="..."}   <seconds>
corpus_reembed_rows_embedded{env="...",model="..."}               <n | -1 if unparsed>
```

`node-exporter` mounts the textfile directory read-only and scrapes it. If the directory is missing/unwritable, metric emission is skipped with a `WARNING` (the run itself is unaffected) — recover the directory per `docs/runbooks/user-service-alembic.md#observability`.

## Failure modes and recovery

### 1. `verify-recall` fails after the embed

**Cause:** wrong model (dimension mismatch silently truncated upstream, or a non-semantic model), a partial embed write, or the index was not rebuilt.

**Recovery:**

1. Confirm the model was 384-dim and multilingual (the default is). A 512-dim model fails the column guard at write time — the embed step itself would have failed first.
2. Re-dispatch with `dry_run=false`. The embed + reindex are idempotent (they re-write the corpus embeddings and rebuild the index); a clean re-run overwrites a partial one.
3. If recall is still poor with a correct model, the problem is upstream in isnad-graph (query path, index params ig#1057, or corpus text) — hand to the isnad-graph team with the `verify-recall` output. Do NOT mask it with a similarity floor (owner declined that, ADR 0008).

### 2. Embed step fails with a dimension-guard error

```
... vector dimension 512 does not match column vector(384) ...
```

**Cause:** a 512-dim model (e.g. `distiluse-base-multilingual-cased-v2`) was passed as `model`.

**Recovery:** re-dispatch with a 384-dim model (the default). No DB cleanup needed — the guard rejected the write, so nothing was persisted.

### 3. GHCR pull fails — embed image manifest not found

```
... manifest for ghcr.io/noorinalabs/noorinalabs-isnad-graph-embed:<tag> not found ...
```

**Cause:** the embed-capable image has not been published yet (isnad-graph#1088), or the resolved `<env>-latest` tag does not exist.

**Recovery:** confirm ig#1088 has published the embed image and the env tag exists; or pass an explicit known-good ref via the `image` input.

### 4. `.env` missing on the VPS

```
ERROR: /opt/noorinalabs-deploy/.env is missing on <env> VPS.
```

**Cause:** fresh VPS, or the env-file was removed. compose needs it to interpolate the DB credentials.

**Recovery:** run the main deploy workflow (`deploy-<env>` / `deploy-isnad-graph.yml`) once to write `.env`, then re-dispatch. On prod, treat a mid-life disappearance as a sev-2 and escalate to Bereket.

### 5. Cold cache is re-downloading every run

**Cause:** the `st_model_cache` volume was cleared, or a different `model` id was used (sentence-transformers keys the cache by model id, so each distinct model downloads once).

**Recovery:** none needed — it is correct behavior. The next run with the same model reuses the cached weights. If you must reclaim space, see § Clearing the model cache.

## Abort / rollback

- **Abort a queued run:** cancel the workflow run from the Actions UI. Because `concurrency.cancel-in-progress` is `false`, a queued (not-yet-started) run is safe to cancel. Avoid cancelling a run that is mid-embed — a partial write is recoverable only by a clean re-run (next item).
- **"Rollback" a bad re-embed:** there is no snapshot-restore of embeddings; the forward fix is a clean re-run with a correct model (the embed overwrites the corpus vectors and rebuilds the index). The previous (e.g. `HashingEmbedder`) state is not preserved and is not worth preserving — it was the defect being fixed.
- **Clearing the model cache** (only if reclaiming disk or forcing a fresh download), on the VPS:
  ```bash
  docker volume rm noorinalabs_st_model_cache   # only when no embed container is running
  ```
  The next run re-downloads the weights.

## Break-glass (CI cannot reach the box)

If GitHub Actions cannot dispatch (outage), run the same one-shot directly on the VPS as `deploy`. This is the compose-native path the workflow parallels:

```bash
cd /opt/noorinalabs-deploy
export EMBED_IMAGE="ghcr.io/noorinalabs/noorinalabs-isnad-graph-embed:<env>-latest"
echo "$GHCR_TOKEN" | docker login ghcr.io -u "$GHCR_USER" --password-stdin
docker compose -p noorinalabs -f compose/docker-compose.prod.yml --env-file .env --profile embed pull isnad-graph-embed

C="docker compose -p noorinalabs -f compose/docker-compose.prod.yml --env-file .env --profile embed run --rm --no-deps isnad-graph-embed"
$C isnad embed-hadiths --batch-size 256
$C isnad reindex-embeddings
$C isnad verify-recall          # non-zero exit ⇒ recall NOT verified
docker logout ghcr.io
```

DB credentials are read from `/opt/noorinalabs-deploy/.env` by compose — do not put them on the command line. The `.prom` metrics are emitted only by the workflow path; a break-glass run will not move the gauges (note it in `#deploy`).

## Escalation

| Failure | Primary | Secondary |
|---|---|---|
| `verify-recall` red with a correct model | Mateo (isnad-graph, embed CLI) | Lucas Ferreira (SRE) |
| Embed/pull fails on stg | Lucas Ferreira (SRE) | Aisha Idrissi (SRE) |
| Embed/pull fails on prod | Bereket Tadesse (IM) | Weronika Zielinska (Platform Architect) |
| `.env` / VPS state problem | Lucas Ferreira (SRE) | Weronika Zielinska (Platform Architect) |
| Embed image not published / tag wrong | Mateo (isnad-graph) | Bereket Tadesse (IM) |

## Related

- [ADR 0008](../adr/0008-corpus-reembedding-mechanism.md) — design of record.
- `.github/workflows/reembed-corpus.yml` — the dispatch workflow.
- `compose/docker-compose.prod.yml` § `isnad-graph-embed` — the profile-gated one-shot service + `st_model_cache` volume.
- `docs/runbooks/user-service-alembic.md` — the SSH one-shot + textfile-collector precedent this mechanism mirrors.
- [isnad-graph#1071](https://github.com/noorinalabs/noorinalabs-isnad-graph/issues/1071) — capstone re-embed run; [#1057](https://github.com/noorinalabs/noorinalabs-isnad-graph/issues/1057) — reindex; [#1088](https://github.com/noorinalabs/noorinalabs-isnad-graph/issues/1088) — embed image + CLI.

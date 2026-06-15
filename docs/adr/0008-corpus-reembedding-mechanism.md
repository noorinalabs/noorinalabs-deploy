# ADR 0008 — Repeatable corpus re-embedding mechanism

- **Status:** Accepted — owner directive 2026-06-15 (Steven French)
- **Date:** 2026-06-15
- **Author:** Weronika Zielinska (Platform Architect)
- **Context issue:** [deploy#461](https://github.com/noorinalabs/noorinalabs-deploy/issues/461)
- **Related ADRs:** [0007 — central secrets-manager + rotation policy](0007-central-secrets-manager.md) (the `-e`-not-argv credential discipline this ADR reuses), [0005 — Terraform state-locking on B2](0005-terraform-state-locking-on-b2-backend.md) (GH-Actions `concurrency` as the right-sized serialization control)
- **Supersedes:** none
- **Superseded by:** none
- **Unblocks:** [isnad-graph#1071](https://github.com/noorinalabs/noorinalabs-isnad-graph/issues/1071) (capstone — run the real re-embed via this mechanism and verify recall, stg→prod). Depends on the isnad-graph embed-capable image + `reindex-embeddings` (ig#1057) + `verify-recall` CLI surface (sibling work, ig#1088).

## Context

Semantic search over the 34,028-hadith corpus returns lexically-arbitrary results. The root cause is not the query path — the query embedder was already corrected (ig#1049) — but the **corpus**: it is embedded with the dependency-free `HashingEmbedder`, which produces token-overlap vectors, not meaning vectors. A real sentence-transformer model never ran against the corpus on the cluster.

The application code already supports a real model: the embedder is selected at runtime from the `EMBEDDING_MODEL` environment variable, and the pgvector embedding column is declared `vector(384)` with a dimension guard. What is missing is **the operational mechanism** — a repeatable, observable, env-gated way to (re-)embed the whole corpus on the cluster and rebuild the index, then prove the result with a recall check.

Two owner directives (2026-06-15) frame the decision:

1. **Build this as a GitHub Action / IaC artifact — NOT a one-off live SSH run.** A human SSHing to the box and running an embed command by hand is not repeatable, not reviewable, leaves no audit trail, has no environment-protection gate, and cannot be re-run deterministically when the model or corpus changes. The mechanism must be a versioned, dispatchable workflow.
2. **No interim minimum-similarity floor.** A prior proposal was to mask the garbage results by thresholding cosine similarity so low-quality matches are hidden. The owner declined this band-aid: it hides the symptom instead of fixing the corpus. This ADR delivers the root-cause fix.

### The 384-dimension constraint

The pgvector column is `vector(384)` and the application asserts the embedder's output dimension matches at write time. The model therefore **MUST** emit 384-dim vectors. The hadith corpus is bilingual (Arabic source text + English translation), so the model must be multilingual.

- **Chosen model: `paraphrase-multilingual-MiniLM-L12-v2`** — 384-dim, multilingual (50+ languages incl. Arabic), ~470 MB weights, CPU-inference-viable on the single VPS.
- **Rejected: `distiluse-base-multilingual-cased-v2`** — also multilingual but **512-dim**, which would violate the column's dimension guard and fail at write time. Any future model swap must preserve the 384-dim contract or ship a coupled migration of the pgvector column + dim-guard.

### Where this runs

The cluster is a single Hetzner VPS running `docker-compose` (not Kubernetes). The canonical precedent for a one-shot cluster data/DB operation is the alembic pre-deploy gate (`.github/workflows/db-migrate.yml` + `docs/runbooks/user-service-alembic.md`): a `workflow_dispatch`/`workflow_call` workflow that SSHes to the box as the `deploy` user (`appleboy/ssh-action`, `secrets.DEPLOY_SSH_PRIVATE_KEY`, `vars.VPS_HOST`), runs `docker run --rm` inside the compose network with credentials passed via `-e` (never argv — CWE-214), and emits `.prom` textfile-collector metrics on every run. This ADR's mechanism is the embedding analogue of that gate. The compose-up analogue of the one-shot is the `user-service-migrate` service (`restart: "no"` + `depends_on … service_healthy`).

## Decision

Adopt a three-part mechanism, all landed in `noorinalabs-deploy`:

### 1. `reembed-corpus.yml` — a `workflow_dispatch` re-embedding workflow

Mirrors `db-migrate.yml`'s SSH → `docker run --rm` shape.

- **Inputs:** `env` (choice `stg`/`prod`), `model` (string, default `paraphrase-multilingual-MiniLM-L12-v2`), `batch_size` (string, default `256`), `dry_run` (boolean, default `true`), `image` (string, default empty → resolves to the env-scoped embed image).
- **Environment protection:** `environment: ${{ inputs.env == 'prod' && 'production' || 'staging' }}` — prod requires manual approval, per-env secrets/host resolve from the matching GH Environment. Same mapping as `db-migrate.yml` / `terraform.yml`.
- **Concurrency:** `group: reembed-corpus-${{ inputs.env }}`, `cancel-in-progress: false`. A re-embed is a long, write-heavy job against the live pgvector store; two must never overlap on one env (ADR 0005's serialization rationale).
- **Execution:** SSH as `deploy` → GHCR login → `docker compose --profile embed pull` → run, in order, `isnad embed-hadiths --batch-size <n>` → `isnad reindex-embeddings` (ig#1057) → `isnad verify-recall`, each as a `docker compose --profile embed run --rm --no-deps -e EMBEDDING_MODEL=… isnad-graph-embed <cmd>` against the live project. **The exit code of `verify-recall` is the gate** — a recall-verification failure fails the workflow. DB credentials come from the VPS `.env` via compose interpolation (never on argv).
  - **Why `docker compose run`, not raw `docker run` (the db-migrate shape):** the embed container needs TWO networks — the internal `backend` (neo4j/postgres/redis) AND `egress`. The embed image bakes the default model (see § 3), so the happy path needs no download; `egress` is required only for a model **swap** (a non-baked `EMBEDDING_MODEL`) and HuggingFace metadata. A raw `docker run` attaches a single `--network`; `docker compose run` attaches both from the `isnad-graph-embed` service definition and reuses its env/volume, keeping creds in the VPS `.env`. db-migrate needs only one internal network (`user-backend`), so its raw `docker run` is correct there; this op's two-network need is the deliberate divergence.
- **`dry_run=true` (the default):** plans and logs the resolved invocation (env, host, image, model, batch size, target network) and exits **without** running any of the three mutating/reading commands — it writes no embeddings and rebuilds no index. A real run requires explicitly setting `dry_run=false`.
- **Observability:** emits a `.prom` textfile-collector file on the VPS on every real run (`if: always()` discipline) with success/exit-status, run timestamp, duration, model label, and a best-effort rows-embedded count — exactly the node-exporter textfile pattern `db-migrate.yml` established (atomic temp+rename, writability pre-check).

### 2. Compose one-shot `isnad-graph-embed` service

Added to `compose/docker-compose.prod.yml` on the `user-service-migrate` pattern: `restart: "no"`, `depends_on` neo4j + postgres `service_healthy`, `EMBEDDING_MODEL` env, the same DB env the `api` service reads. It is the `compose`-native path (fresh-stack / break-glass) that the SSH workflow's `docker run` parallels — the same dual structure as `db-migrate.yml` (SSH path) ↔ `user-service-migrate` (compose path).

- **Profile-gated** (`profiles: ["embed"]`). A re-embed of 34k hadiths is expensive and must **never** run on an ordinary `docker compose up` (which app deploys execute). The profile keeps the definition validated by `docker compose config` while excluding it from the default up — the same mechanism the `pipeline` workers use. It is brought up deliberately with `docker compose --profile embed run --rm isnad-graph-embed …`.

### 3. Model weights: baked into the decoupled embed image AND volume-cached

The embed-capable image is a **separate, decoupled** GHCR image (`…-isnad-graph-embed`, published by an independent `build-and-push-embed` job in isnad-graph's `ghcr-publish.yml` — ig#1089), NOT the main `…-isnad-graph` image that every app deploy pulls. It **bakes** the default 384-dim model at `HF_HOME=/opt/hf-cache`. On top of that, per the owner's volume-cache decision, a **named volume** `st_model_cache` is mounted at that **same** `/opt/hf-cache` path (with `SENTENCE_TRANSFORMERS_HOME` and `HOME` also pointed there). The combination is deliberate:

- **Baked** → the default model is present the instant the container starts; the happy-path re-embed has no runtime dependency on HuggingFace being reachable.
- **Volume mounted at the baked path** → Docker populates the empty volume from the baked weights on first run, so the cache **persists across image re-pulls** (a new image tag does not re-download), and a model **swap** (a non-baked `EMBEDDING_MODEL`) downloads the new model into the same volume (the only path that needs `egress`).

This sidesteps the original objection to baking (§ Alternatives B): the weights are NOT in the main image, so no app deploy pays for them.

### 4. Runbook

`docs/runbooks/corpus-reembedding.md` — trigger steps, expected duration, the recall-verify gate, abort/rollback, and break-glass (the compose `--profile embed` path) for when CI cannot reach the box.

## Alternatives rejected

### A. Live one-off SSH run (owner-declined)

An operator SSHes to the VPS and runs the embed command by hand once. **Rejected by owner directive 2026-06-15.** It is not repeatable (the next model/corpus change repeats the manual toil), not reviewable (no diff, no PR), unaudited (no environment-protection approval, no `.prom` signal), and not deterministic (no captured inputs). The whole point of this ADR is to replace this with a versioned, dispatchable artifact.

### B. Bake model weights into the **main** isnad-graph image (rejected)

Ship the ~470 MB weights inside the **main `…-isnad-graph`** image (the one every app deploy pulls) at build time. **Rejected:** it bloats every isnad-graph image build by ~0.5 GB, couples the model choice to the app's image rebuild cadence, and slows *every* isnad-graph deploy's pull even when no re-embed is happening.

What shipped instead (§ 3) keeps the app image lean: the weights are baked into a **separate, decoupled** `…-isnad-graph-embed` image (ig#1089) that only the re-embed op pulls, and a named volume mounted at the baked `HF_HOME` gives persistence across re-pulls and headroom for model swaps. So baking is avoided *where it would hurt* (the app image) and used *where it helps* (a dedicated embed image that is never on the deploy hot path), satisfying the owner's volume-cache intent.

### C. Interim minimum-similarity floor (owner-declined)

Threshold cosine similarity so low-quality matches are hidden from the UI, leaving the `HashingEmbedder` corpus in place. **Rejected by owner directive 2026-06-15** — it masks the symptom (arbitrary results) without fixing the cause (meaningless vectors). This ADR fixes the corpus instead.

### D. Run the embedding inside the always-on `api` container

`docker exec` the embed command into the live `noorinalabs-api-1` container. **Rejected:** a multi-minute CPU-bound embedding job inside the request-serving container contends with live traffic and the container's `read_only` filesystem has no writable model-cache path. A one-shot container with its own resource envelope and the cache volume is cleaner and isolates the heavy job from serving.

## Consequences

### Positive

- **Repeatable + reviewable + audited.** Re-embedding is a dispatchable, version-controlled workflow with environment-protection approval on prod and a `.prom` metric per run — not tribal SSH knowledge.
- **Root-cause fix.** Replaces token-overlap vectors with real multilingual sentence embeddings; no similarity-floor band-aid.
- **Recall-gated.** `verify-recall`'s exit code fails the workflow, so a bad re-embed (wrong model, partial write, index not rebuilt) is caught before it is declared done.
- **Cheap model swaps.** Model is a workflow input + a volume cache, not an image rebuild.
- **Precedent-faithful.** Reuses the `db-migrate.yml` SSH/`-e`/textfile-collector pattern and the `user-service-migrate` compose one-shot pattern, so operators already understand the shape.

### Negative / ongoing costs

- **Two definitions of one operation.** The SSH `docker run` path (CI) and the compose `--profile embed` path (break-glass) must stay in sync on env/network/volume — the same maintenance the db-migrate ↔ user-service-migrate pair already carries.
- **Volume lifecycle.** `st_model_cache` is a persistent named volume; a stale cached model after a model swap is possible if a custom local model name collides. Sentence-transformers keys cache by model id, so the standard swap (one HF model → another) is safe; the runbook notes how to clear the volume if needed.
- **CPU-bound duration.** Embedding ~34k hadiths on the single VPS is multi-minute; the workflow's `concurrency` group prevents overlap and the runbook records the expected duration.
- **Image dependency.** The mechanism is inert until the isnad-graph embed-capable image (ig#1088) and the `reindex-embeddings` (ig#1057) / `verify-recall` CLI surface ship. The `image` input default is parameterized and pinned once ig#1088 publishes.

### Failure modes explicitly considered

| Question | Answer |
|---|---|
| What if `verify-recall` fails after a re-embed? | The workflow fails (its exit code is the gate). The corpus retains whatever was last written; investigate per the runbook (wrong model dim, partial write, index not rebuilt) and re-dispatch. The `.prom` `_success=0` gauge surfaces it. |
| What if two re-embeds are dispatched on the same env? | The second queues behind the first (`concurrency` group, `cancel-in-progress: false`). They never race the pgvector store. |
| What if someone runs an ordinary `docker compose up`? | The `isnad-graph-embed` service is profile-gated (`profiles: ["embed"]`) and does **not** start. No accidental 34k-row re-embed on a routine deploy. |
| What if a 512-dim model is passed as `model`? | The application's dimension guard rejects the write (the `vector(384)` column). The embed step fails, `verify-recall` never passes, the workflow fails. The runbook calls out the 384-dim constraint. |
| What if `dry_run` is left at its default? | Nothing is written — the default is `true` precisely so a fat-fingered dispatch is a no-op plan, not a destructive run. A real run is an explicit `dry_run=false`. |
| What if the model weights are not yet cached? | The default model is **baked** into the embed image at `HF_HOME=/opt/hf-cache`, and the `st_model_cache` volume is populated from it on first run — so the default model needs no download at all. Only a model **swap** (a non-baked `EMBEDDING_MODEL`) downloads (~470 MB) into the volume on first use, then persists. |

## Refs

- [deploy#461](https://github.com/noorinalabs/noorinalabs-deploy/issues/461) — this ADR's context issue (mechanism implementation).
- [isnad-graph#1071](https://github.com/noorinalabs/noorinalabs-isnad-graph/issues/1071) — capstone: run the real re-embed via this mechanism, verify recall (stg→prod).
- [isnad-graph#1057](https://github.com/noorinalabs/noorinalabs-isnad-graph/issues/1057) — `reindex-embeddings` (index rebuild) command.
- [isnad-graph#1088](https://github.com/noorinalabs/noorinalabs-isnad-graph/issues/1088) — embed-capable image + `ml` extra + `verify-recall` CLI surface (the interface contract this workflow invokes).
- [isnad-graph#1049](https://github.com/noorinalabs/noorinalabs-isnad-graph/issues/1049) — query-path embedder fix (already shipped; established the corpus, not the query, is the remaining problem).
- `.github/workflows/db-migrate.yml` + `docs/runbooks/user-service-alembic.md` — the SSH one-shot + textfile-collector precedent this mechanism mirrors.
- `docs/runbooks/corpus-reembedding.md` — the operational runbook landed with this ADR.

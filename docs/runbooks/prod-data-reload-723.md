# Prod Data-Reload Runbook — main#723 Re-validation

> OWNER-EXECUTED runbook. This document is prep only; it mutates nothing on its
> own. Every destructive / prod-touching step below is marked
> **[OWNER-RUN / DESTRUCTIVE]** — do not let any automation run them.

**Author:** Alejandra Reyes-Fuentes (Staff Data Engineer, noorinalabs-data-acquisition)
**Goal:** make main#723 acceptance criteria #1–#4 pass by purging the polluted graph and reloading
the W20 (Path-B segmentation/dedup) + W21 (narrator-date) corrected output.

---

## 0. Ordering dependency (READ FIRST)

This data reload MUST happen **after** the isnad-graph service images carrying the W20/W21 loader
code are promoted to prod (see the companion runbook `prod-promote-723.md`). The loaders
(`src/graph/load_nodes.py`, `load_edges.py`) and the parse/resolve stages that emit segmented
narrators + reconciled dates ship inside the `noorinalabs-data-acquisition` loader image (built from
the repo at the W20/W21 SHAs), and the read/verify surface (`/api/v1/search`, `/parallels`,
`/admin/data/*`) ships inside the isnad-graph API image. If you reload data with an old API image,
the verify checks below can false-red even on correct data.

**Sequence:**

1. Promote isnad-graph API + frontend to prod (`noorinalabs-deploy` → `Actions > Promote to Prod`,
   `promote.yml`; approve the `production` GH Environment gate). See `prod-promote-723.md` and the
   deploy `RUNBOOK.md` "Prod deploy" section.
2. Confirm prod is healthy: `gh workflow run verify-deploy.yml -f target=prod` → green.
3. Produce the reload Parquet from the **same W20/W21 SHAs** (§3), and load it via the published
   loader image at those SHAs — the `<env>-latest` loader image `deploy-data-load.yml` resolves
   must be built from the promoted SHA (or pass an explicit `image` ref).
4. THEN run this runbook.

---

## 1. Pre-flight — backup + BEFORE inventory  [OWNER-RUN]

### 1a. Take a full Neo4j + Postgres backup FIRST (non-negotiable)

The deploy repo already has an offline-dump + B2-upload backup script. On the **prod VPS**:

```bash
ssh deploy@<PROD_VPS_HOST>
cd /opt/noorinalabs-deploy
# Secrets read from the deploy env (names only): B2_KEY_ID, B2_APP_KEY, B2_BUCKET
sudo --preserve-env=B2_KEY_ID,B2_APP_KEY,B2_BUCKET ./scripts/backup.sh
```

- Dumps Postgres (`pg_dump --format=custom`) + Neo4j (offline `neo4j-admin database dump`,
  stop→dump→restart), checksums, uploads to Backblaze B2 under `daily/<date>/` (or `weekly/` on Sun).
- **Verify the backup landed before proceeding:** `./scripts/restore.sh --list` and confirm today's
  dated dir is present with both `isnad-pg-*.dump` and `isnad-neo4j-*.dump.zst`.
- This dated path is your rollback handle in section 6. Record it.

> Note: `backup.sh` briefly stops the Neo4j container for the offline dump — expect a short read
> outage during pre-flight. Run during a low-traffic window.

### 1b. Capture the BEFORE inventory (baseline to compare against)

As an admin user (the `/admin/data/*` router is `require_admin`-gated), capture and SAVE:

```bash
# --- Mint an admin JWT (user-service https://users.noorinalabs.com issues it) ---
# Email/password path — providers `email/password` is enabled. The password is
# prompted (never echoed / never in shell history); no secret is hard-coded here.
read -rs -p "Admin password: " PW; echo
TOKEN=$(curl -s -X POST https://users.noorinalabs.com/auth/login \
  -H 'Content-Type: application/json' \
  -d "$(jq -nc --arg e 'YOUR_ADMIN_EMAIL' --arg p "$PW" '{email:$e, password:$p}')" \
  | jq -r '.access_token')
unset PW
# Sanity-check without dumping the JWT:  echo "${TOKEN:0:18}…"
# The /auth/login response is {access_token, refresh_token, token_type:"bearer"}.
#
# If /auth/login returns 401 (admin account is OAuth-only, no password set): log in
# via the browser, then take the access token from the post-login redirect URL
# *fragment* (…#token=<access>), or from devtools → Network → the `Authorization:
# Bearer` header on any API XHR. The admin endpoints are role-gated, so the token
# must belong to your admin account.
BASE=https://isnad.noorinalabs.com
curl -sS -H "Authorization: Bearer $TOKEN" "$BASE/api/v1/admin/data/overview" | tee before_overview.json
curl -sS -H "Authorization: Bearer $TOKEN" "$BASE/api/v1/admin/data/sources"  | tee before_sources.json
```

Record the main#723 baseline numbers from these payloads so the AFTER comparison is concrete:

- `total_nodes` / `total_relationships` (overview) — expect ~768,920 Hadith nodes, ~650k orphan
  `sanadset`-derived narrator nodes, near-zero isnad chains.
- The **collection-linked %** baseline = `(Hadith with APPEARS_IN) / total Hadith` ≈ **8.98%**
  (criterion #1 baseline). Confirm directly via the Cypher in section 5.1.
- `/sources` gives the per-corpus `hadith_count` / `collection_count` — note every `source_corpus`
  value present; that list drives the purge in section 2.

---

## 2. Purge the polluted graph  [OWNER-RUN / DESTRUCTIVE]

**Owner decision (#723): full purge-and-reload of ALL source corpora is the chosen path** — not a
`sanadset`-only purge. The rationale and the narrower fallback are documented below.

**Which corpus is the headline pollution:** the orphan narrators are the `sanadset` corpus
(`SourceCorpus.SANADSET = "sanadset"` in `src/models/enums.py`) — raw `<NAR>` isnad-string fragments
minted as `Narrator` nodes pre-W20. The W20 Path-B fix (`src/parse/sanadset.py`, da#219/da#221)
re-segments those tags and emits proper Collections.

**Why a purge is required (not just a reload):** the loaders MERGE on stable, corpus-namespaced ids
(`load_staging.sh` header: "idempotent … MERGE on stable, corpus-namespaced ids"). The corrected
W20 output mints *different* narrator ids than the old raw-isnad firehose, so re-loading **adds**
the corrected nodes but leaves the ~650k stale orphan nodes behind. They must be explicitly deleted.

**Why FULL purge-and-reload (the chosen path):** main#723 reports graph-wide breakage (empty chains,
broken search, empty parallels), and W20/W21 dedup is **cross-corpus** — canonical `Narrator` nodes
carry a `source_corpora` array and `STUDIED_UNDER` edges span corpora. Purging only `sanadset` risks
leaving cross-corpus duplicates and stale edges, and the per-source purge keys on the **singular**
`n.source_corpus`, so it can clip canonical narrators shared with other corpora. Purging **every**
`source_corpus` present and reloading the complete corrected corpus set is the globally-consistent
path and is the procedure below.

> **Narrower fallback (not chosen):** if the owner later wants the minimal targeted fix, purge
> `sanadset` alone — but accept the cross-corpus dedup caveat above (possible residual duplicates /
> stale cross-corpus edges).

The per-source purge endpoint (`src/api/routes/admin/data.py`, ig#989) is **one corpus per call**,
two-phase (dry-run preview → typed-confirmation real run). Because it is one-corpus-per-call, drive
it with an **array of every `source_corpus` present** and loop the call over the array — first a
full dry-run pass, then (after you've reviewed the previews) a guarded real-run pass.

```bash
BASE=https://isnad.noorinalabs.com   # TOKEN already exported from §1b

# --- Build the corpus array -------------------------------------------------
# PREFERRED — derive it straight from the §1b baseline so it is EXACTLY the set
# present in prod (no guesswork). Confirm the jq path matches before_sources.json's
# actual shape; the `// .[]` fallback handles a bare-array root.
CORPORA=( $(jq -r '(.sources // .)[].source_corpus' before_sources.json | sort -u) )

# FALLBACK — if you don't have before_sources.json handy, paste the explicit list
# you noted from /sources. This is the full SourceCorpus enum as a TEMPLATE; replace
# it with the ACTUAL present values — do NOT ship this dump blindly.
# CORPORA=( sanadset sunnah lk thaqalayn fawaz open_hadith muhaddithat itqan halimbahae mis bihar tusi )

# Sanity-check the array BEFORE touching anything destructive.
printf 'Will purge %d corpora: %s\n' "${#CORPORA[@]}" "${CORPORA[*]}"
test "${#CORPORA[@]}" -gt 0 || { echo "EMPTY corpus array — STOP, fix the source list"; }
```

```bash
# 2a. DRY RUN over EVERY corpus first — previews counts, touches nothing
#     (dry_run defaults true). Review all previews before running 2b.
for C in "${CORPORA[@]}"; do
  echo "=== DRY RUN: $C ==="
  curl -sS -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    "$BASE/api/v1/admin/data/purge" \
    -d "{\"source_corpus\":\"$C\",\"dry_run\":true}" | jq .
done
```

```bash
# 2b. REAL RUN over EVERY corpus — DESTRUCTIVE. Run ONLY after reviewing 2a.
#     confirmation must echo source_corpus EXACTLY, so we pass "$C".
#     The HTTP-code guard makes a failed call STOP the loop (see 503 note below)
#     instead of continuing on through the remaining corpora.
for C in "${CORPORA[@]}"; do
  echo "=== PURGE (real): $C ==="
  resp=$(curl -sS -w '\n%{http_code}' -X POST -H "Authorization: Bearer $TOKEN" \
    -H 'Content-Type: application/json' "$BASE/api/v1/admin/data/purge" \
    -d "{\"source_corpus\":\"$C\",\"dry_run\":false,\"confirmation\":\"$C\"}")
  code=$(printf '%s\n' "$resp" | tail -1)
  printf '%s\n' "$resp" | sed '$d' | jq .
  if [ "$code" != "200" ]; then
    echo "ABORT: purge of '$C' returned HTTP $code (expected 200). This call did NOT modify the graph — STOP, investigate, then resume from the remaining corpora." >&2
    break
  fi
done
```

- Real run does `MATCH (n) WHERE n.source_corpus = $corpus DETACH DELETE n` and writes an audit entry.
- A failed destructive write surfaces as **503** (it does NOT degrade to a silent success). The loop's
  `code != 200` guard catches the 503 and any other non-success and **breaks** — when it fires,
  the graph was not modified by that call; investigate Neo4j health, then re-run 2b (already-purged
  corpora dry-run/real-run cleanly to ~0, so a resume over the full array is safe and idempotent).
- After the loop completes for all corpora, confirm the graph is empty of content nodes (section 5.1
  returns ~0 Hadith).

> Scope note: this purge is **graph-level** (Neo4j nodes/edges). It is distinct from the
> ingest-platform pipeline reset (ig#970, `/admin/reset/*`) which wipes the staging store — not needed
> here. Postgres relational / `pgvector` rows are reloaded by the loader's own MERGE/upsert in
> section 4; the section-1a backup is the safety net for both stores.

---

## 3. Produce W20/W21 corrected Parquet  [non-destructive — runs on the build host]

Run the full pipeline from the `noorinalabs-data-acquisition` repo **checked out at the promoted
W20/W21 SHA** so the staging output carries Path-B segmentation/dedup + the narrator-date stages.

```bash
cd <REPO_ROOT>/noorinalabs-data-acquisition
git checkout <W20_W21_SHA>          # same SHA whose loader image you promoted in section 0
make setup                           # uv sync --group ml  (CAMeLBERT/FAISS deps for dedup)

# Stages (or `make pipeline` to run all five end-to-end):
make acquire   # Phase 1: download sources → data/raw/  (needs source-API secrets, see below)
make parse     # Phase 1: parse → data/staging/ Parquet — includes Path-B sanadset re-segmentation
               #          (src/parse/sanadset.py _segment_nar_content / _is_narrator_like) +
               #          per-source narrator-date parse (src/parse/narrator_dates.py, da#164)
make resolve   # Phase 2: NER + disambiguation + dedup (FAISS/CAMeLBERT) AND narrator-date
               #          reconciliation: src/resolve/date_reconcile.py (da#165) +
               #          src/resolve/tabaqa_dates.py tabaqa fallback (da#166)
make validate  # gate: strict data-quality validation → data/reports/validation_report.json
```

- **Secrets (names only, set in the build host env / `.env` — never inline):** `SUNNAH_API_KEY`,
  `KAGGLE_USERNAME`, `KAGGLE_KEY` for `acquire`. DB creds are NOT needed for acquire/parse/resolve.
- **Do NOT run `make load` / `make enrich` here** — the local `make load` targets the dev/local
  Neo4j from `.env`. Prod load goes through section 4 (the remote loader image). Keeping load out of
  this step is what prevents an accidental local-vs-prod mixup.
- Sanity-check staging before loading: `make profile-data` and confirm
  `data/staging/narrator_mentions_sanadset.parquet` row count dropped materially vs the pre-W20
  firehose (the re-segmentation baseline recalibration), and that `narrators_bio_*` rows carry
  `death_year_ah` populated for dated narrators.

---

## 4. Load corrected data to prod  [OWNER-RUN]

> **SUPERSEDES `scripts/load_staging.sh`.** The batch load is now behind IaC —
> the `deploy-data-load.yml` workflow (deploy#546) — instead of the build-host
> one-off. `load_staging.sh` (rsync Parquet from a workstation + `docker build`
> the loader on the box) is retired for routine loads: it is an un-repeatable,
> box-local step and the owner directed repeatable data loads behind GH Actions +
> IaC. The workflow sources the Parquet from B2 on the VPS (never through the
> runner), reuses the published loader image via a profile-gated compose service
> (no on-VPS build), sources the NEO4J password from the VPS `.env` via compose
> (never on argv/logs), and gates prod behind the `production` Environment.

The load is now a two-step flow: **publish the resolved Parquet to B2** (§4a),
then **dispatch `deploy-data-load.yml`** (§4b).

### 4a. Publish the §3 Parquet set to the pipeline B2 bucket

Upload the curated + staging Parquet produced in §3 to `noorinalabs-pipeline`
under a **versioned prefix** you will pass as `parquet_ref`. The workflow's
contract (see its header block) expects this exact layout:

```text
noorinalabs-pipeline/<parquet_ref>/curated/narrators_canonical.parquet
noorinalabs-pipeline/<parquet_ref>/curated/narrator_mentions_resolved.parquet
noorinalabs-pipeline/<parquet_ref>/curated/narrator_mentions_resolved_muhaddithat.parquet
noorinalabs-pipeline/<parquet_ref>/staging/hadiths_*.parquet
noorinalabs-pipeline/<parquet_ref>/staging/collections_*.parquet
noorinalabs-pipeline/<parquet_ref>/staging/narrator_mentions_*.parquet   # full load only
noorinalabs-pipeline/<parquet_ref>/staging/network_edges_*.parquet       # full load only
noorinalabs-pipeline/<parquet_ref>/staging/parallel_links.parquet        # full load only
```

- Choose a reproducible `parquet_ref`, e.g. `staged/narrator-resolve/$(date -u +%Y-%m-%d)-<SHA>`
  (the W20/W21 SHA from §0), so the load is reproducible and a prior good set is
  always re-loadable. It is a **bucket-relative prefix** — no bucket name, no
  leading slash.
- Publishing is a data-acquisition helper (owner/da item, not this workflow).
  The credential-safe pattern is rclone native env against the pipeline bucket
  (mirror `scripts/backup.sh`: `RCLONE_CONFIG_*` env, no CLI-flag secrets) using
  the pipeline B2 key (`PIPELINE_B2_KEY_ID` / `PIPELINE_B2_KEY`).

### 4b. Dispatch `deploy-data-load.yml`

```bash
# Dry-run FIRST (default) — verifies the B2 objects exist + prints the load plan,
# writes NOTHING to the graph:
gh workflow run deploy-data-load.yml \
  -f env=prod \
  -f parquet_ref='staged/narrator-resolve/<YYYY-MM-DD>-<SHA>' \
  -f load_args='load' \
  -f dry_run=true

# Review the dry-run summary, then the REAL load (approve the `production`
# Environment gate when prompted):
gh workflow run deploy-data-load.yml \
  -f env=prod \
  -f parquet_ref='staged/narrator-resolve/<YYYY-MM-DD>-<SHA>' \
  -f load_args='load' \
  -f dry_run=false
```

- `env=prod` maps to the `production` GH Environment → **manual approval gate**;
  `env=stg` (the validated pre-prod gate) does not. Per stg-gate policy, prod is
  a promotion of a verified-good stg load — run the stg load + §5 verify first.
- `load_args='load'` does the **full** load (nodes + edges incl. `APPEARS_IN`,
  `STUDIED_UNDER`, `PARALLEL_OF`, `GRADED_BY`) — `load --nodes-only` would NOT
  populate the chains/edges that criteria #2 and #4 depend on, so `load` is
  required here.
- The loader MERGEs narrator date props (`birth_year_ah`, `death_year_ah`,
  `generation`) and the segmented narrators/edges. It is idempotent — an SSH-drop
  mid-load is recoverable by re-dispatching (MERGE converges).
- The workflow's post-load step reads back the `source_corpus` distribution
  (Hadith per corpus) into the job log — sanity-check it matches the expected
  corpus set before moving to verify.
- **Postgres / pgvector:** the loader upserts relational + embedding rows as part
  of the load; the semantic-search index is rebuilt from this. (If semantic
  search still 500s in section 5.3 because the pgvector backend was not
  provisioned for an env, that is an environment-provisioning item, not a data
  defect — note it but don't block on it.)

---

## 5. Verify — map to main#723 criteria #1–#4

Run AFTER the load completes. Cypher can go via the prod neo4j container
(`docker exec <neo4j-container> cypher-shell -u neo4j -p "$PW" '<query>'`, password from
`NEO4J_AUTH` as in `load_staging.sh`); HTTP checks hit the prod API with an admin token.

### 5.1 Criterion #1 — collection-linked % materially up from 8.98%

```cypher
MATCH (h:Hadith)
WITH count(h) AS total, count { (h)-[:APPEARS_IN]->(:Collection) } AS linked
RETURN total, linked, 100.0 * linked / total AS pct_linked;
```

PASS = `pct_linked` materially above 8.98% (Path-B B1 emits one Collection per book so every sanadset
hadith links). Cross-check against `/api/v1/admin/data/overview` (`APPEARS_IN` relationship count)
and `/admin/data/sources` (`collection_count` per corpus, no longer ~0 for sanadset).

### 5.2 Criterion #2 — STUDIED_UNDER populated (not 186)

```cypher
MATCH ()-[r:STUDIED_UNDER]->() RETURN count(r) AS studied_under_edges;
```

PASS = `studied_under_edges` materially above the broken-state 186 (the W20 re-segmentation feeds
real narrators into the chain, so STUDIED_UNDER is no longer starved). Also confirm isnad chains
exist — a chain is represented as `TRANSMITTED_TO` relationships carrying a `chain_id` property
(there is no `HAS_CHAIN` / `HAS_ISNAD` edge), so count the distinct chains by `chain_id`:

```cypher
MATCH ()-[r:TRANSMITTED_TO]->() WHERE r.chain_id IS NOT NULL
RETURN count(DISTINCT r.chain_id) AS chains;  // expect >> 0
```

(Confirm the exact edge/property and labels against the loaded schema at run time — check
`/admin/data/overview` `relationship_counts` for the live edge names.)

### 5.3 Criterion #3 — search returns Hadith; semantic search 200 not 500

```bash
BASE=https://isnad.noorinalabs.com
# Full-text (Neo4j fulltext index hadith_search/narrator_search):
curl -sS -H "Authorization: Bearer $TOKEN" "$BASE/api/v1/search?q=<arabic-term>&limit=5" | jq '.total, (.results|length)'
# Semantic (pgvector):
curl -sS -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $TOKEN" \
  "$BASE/api/v1/search/semantic?q=<arabic-term>&limit=5"
```

PASS = full-text `total > 0` with Hadith results present, and semantic returns **200** (not 500).
A clean fulltext index is rebuilt as part of the load; if `search/semantic` still 500s, confirm
pgvector was provisioned for prod (env item — see section 4 note).

### 5.4 Criterion #4 — compare / parallel pairs non-empty

```bash
curl -sS -H "Authorization: Bearer $TOKEN" "$BASE/api/v1/parallels?limit=5" | jq '.total, (.pairs|length)'
```

PASS = `total > 0` (PARALLEL_OF edges loaded by the full load). Spot-check one pair detail:
`/api/v1/parallels/<hadith_id>` returns a non-empty `parallels` array.

### 5.5 Whole-system smoke

```bash
gh workflow run verify-deploy.yml -f target=prod    # deploy repo: <60s prod smoke battery
```

Confirm green, then re-pull `/admin/data/overview` + `/sources` and diff against the section-1b
BEFORE snapshots to document the delta on the main#723 issue.

---

## 6. Rollback — if the reload goes wrong  [OWNER-RUN / DESTRUCTIVE]

The section-1a backup is the restore point. On the prod VPS:

```bash
ssh deploy@<PROD_VPS_HOST>
cd /opt/noorinalabs-deploy
# Restore the dated path you recorded in section 1a (or 'latest'):
sudo --preserve-env=B2_KEY_ID,B2_APP_KEY,B2_BUCKET \
  ./scripts/restore.sh daily/<YYYY-MM-DD>
```

- `restore.sh` downloads from B2, verifies checksums, then restores Postgres
  (`pg_restore --clean --if-exists`) and Neo4j (offline `neo4j-admin database load
  --overwrite-destination`, stop→load→restart). It prompts `Type YES to confirm` unless `--force`.
- This OVERWRITES the current databases back to the pre-reload state — the polluted-but-known graph,
  which is strictly safer than a half-applied reload.
- After restore, re-run section 5.5 prod smoke to confirm the service is healthy on the restored
  data, then triage what failed before re-attempting the reload.

> **Caveat (deploy#498):** `restore.sh` defaults `COMPOSE_FILE` to `docker-compose.prod.yml`,
> whereas `backup.sh` uses `compose/docker-compose.prod.yml` — the two scripts disagree on the
> compose-file default. Until deploy#498 aligns them, set `COMPOSE_FILE=compose/docker-compose.prod.yml`
> explicitly when invoking `restore.sh`, or the restore may resolve the wrong / non-existent compose
> file mid-rollback. With the explicit override:
>
> ```bash
> COMPOSE_FILE=compose/docker-compose.prod.yml \
>   sudo --preserve-env=B2_KEY_ID,B2_APP_KEY,B2_BUCKET,COMPOSE_FILE \
>   ./scripts/restore.sh daily/<YYYY-MM-DD>
> ```

---

## Quick reference — exact command surface

| Action | Command | Where |
|---|---|---|
| Backup | `./scripts/backup.sh` | deploy repo, prod VPS |
| Restore | `./scripts/restore.sh <daily/DATE\|latest>` | deploy repo, prod VPS |
| BEFORE/AFTER inventory | `GET /api/v1/admin/data/overview`, `/admin/data/sources` | isnad-graph API |
| Purge (dry-run) | `POST /api/v1/admin/data/purge {"source_corpus":C,"dry_run":true}` | isnad-graph API |
| Purge (real) | `POST .../purge {"source_corpus":C,"dry_run":false,"confirmation":C}` | isnad-graph API |
| Produce Parquet | `make acquire parse resolve validate` | data-acquisition repo |
| Publish Parquet to B2 | rclone upload §3 output to `noorinalabs-pipeline/<parquet_ref>/{curated,staging}/` | data-acquisition repo / VM |
| Load to prod | `gh workflow run deploy-data-load.yml -f env=prod -f parquet_ref=<ref> -f load_args=load -f dry_run=false` | deploy repo (approve `production` gate) |
| Verify search | `GET /api/v1/search?q=…`, `/search/semantic?q=…`, `/parallels` | isnad-graph API |
| Prod smoke | `gh workflow run verify-deploy.yml -f target=prod` | deploy repo |

**Secret NAMES referenced (never inline a value):** `B2_KEY_ID`, `B2_APP_KEY`, `B2_BUCKET`,
`PIPELINE_B2_KEY_ID`, `PIPELINE_B2_KEY`, `NEO4J_AUTH` / `NEO4J_PASSWORD`, `SUNNAH_API_KEY`,
`KAGGLE_USERNAME`, `KAGGLE_KEY`.

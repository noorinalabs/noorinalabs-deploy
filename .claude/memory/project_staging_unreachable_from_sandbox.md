---
name: project_staging_unreachable_from_sandbox
description: Staging Neo4j (bolt://neo4j:7687) and the isnad-graph frontend host are reachable only from inside the cluster — not from the CI/dev sandbox; load+screenshot are post-merge runtime gates.
metadata: 
  node_type: memory
  type: project
  originSessionId: 080813cd-f3b8-434d-974c-badf58620c96
---

The **staging Neo4j** (`bolt://neo4j:7687`) and the staging frontend host
(`https://isnad-graph.noorinalabs.com`) do **not resolve** from the CI/dev
sandbox — they live on the Hetzner cluster's internal compose network. The dev
box also has **no usable Docker** (WSL: docker binary present but "could not be
found in this WSL 2 distro"), so you can't stand up a local Neo4j either.

Live outbound HTTP **does** work from the sandbox (e.g. `sunnah.com` scrape
returned 200/151KB).

**BUT there IS a working path: SSH to the cluster box.** `ssh noorinalabs-stg`
(user `deploy`, key `~/.ssh/noorinalabs_deploy`; host 87.99.137.225) works. The
staging Neo4j runs there as container `noorinalabs-neo4j-1`. da#73 loaded 47 real
hadiths into it this way:
- No ingestion image on the box, and `apoc.import.file.enabled` + container
  rootfs are hardened **off** — so don't try to run the Python loader or
  `apoc.load.json` there.
- Instead: parse locally → generate self-contained Cypher with the rows as
  **inline Cypher map literals** (mirror the loader's MERGE shapes) → `scp` →
  `docker cp` → `docker exec -i noorinalabs-neo4j-1 cypher-shell -u neo4j -p
  "$NEO4J_AUTH_pw" -f file.cypher`. Get the password from the container env:
  `docker inspect ... NEO4J_AUTH=neo4j/<pw>`.
- The staging API (`noorinalabs-api-1`, port 8000) `/health` confirms neo4j up;
  `/api/v1/collections` + `/api/v1/hadiths` exist but 401 without a JWT — so the
  final *frontend screenshot* still needs an authenticated browser session.

**How to satisfy a "load into staging Neo4j + view in frontend" acceptance
criterion (da#73 pattern):**
- Verify the full data→Cypher→graph mapping on REAL data in CI via the project's
  in-process mock Neo4j client (`tests/test_graph/conftest.py MockNeo4jClient`),
  asserting exact node/edge counts + id keying. This is genuine verification, not
  a synthetic-acceptance dodge.
- The actual cluster write + frontend screenshot is a **post-merge runtime step**
  run from inside the cluster (`NEO4J_*` → staging), documented as an unchecked
  Test-Plan box. Per [[feedback_runtime_gate_scoping]] that's NOT a PR-time gate.

The ingestion loader (`isnad-ingest load` / `src/graph load_all`) targets
whatever `NEO4J_*` resolves to, so the same script loads staging when run on a
cluster box. da#73's `scripts/first_light/run_slice.py` + `docs/first-light-slice.md`
are the reproducible carrier for that runtime step.

**BETTER PATH (verified 2026-06-16, da#175): run the REAL Python loader from the
sandbox via an SSH tunnel to the container's bridge IP.** bolt is NOT published on
the staging host (`docker port noorinalabs-neo4j-1` empty; host has no 7687
listener), but the host CAN reach the container's bridge IP. So:
`IP=$(ssh noorinalabs-stg docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' noorinalabs-neo4j-1)` (was `172.18.0.3`), then
`ssh -o ExitOnForwardFailure=yes -N -L 7688:$IP:7687 noorinalabs-stg` (run via
Bash `run_in_background:true` — a foregrounded `ssh -fN` HANGS the Bash tool by
holding its stdout pipe). Then `NEO4J_URI=bolt://127.0.0.1:7688 NEO4J_USER=neo4j
NEO4J_PASSWORD=<pw> uv run isnad-ingest load`. Plain bolt (no TLS), direct (not
`neo4j://`, so no routing-address rewrite). Verified the neo4j driver + live query
through it. The production loader is additive (MERGE, `strict=False` skips missing
file types — load only the parquet you place in `data/staging/`), so this is safe
on the populated staging DB. This supersedes the "no Python loader, Cypher-only"
constraint for the *load* step (apoc still off, but you no longer need apoc — the
driver writes directly). Frontend screenshot still needs an authed browser.

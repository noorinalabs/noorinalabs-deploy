#!/usr/bin/env python3
"""Generate / staleness-check noorinalabs-deploy's structural ontology index (#493).

This is noorinalabs-deploy's consumer side of the C×T2 distributed structural
ontology (noorinalabs-main#820, generator noorinalabs-main#855; wiring deploy#493).

**Hybrid index -- stub mode for HCL (deploy-specific).**
HCL/Terraform is NOT cleanly AST-derivable by the current ``ontology_gen`` generator
(the spike measured Python/ast only -- noorinalabs-main#820 Task 1). So this repo ships
a HYBRID committed index:

* **Auto-generated portion** -- Python helper scripts (``scripts/``,
  ``integration-tests/``, ``.claude/lib/``, ``.github/workflows/scripts/``) indexed
  by the owned ``ontology_gen`` generator (noorinalabs-main#855), the same generator
  every other consumer repo uses.
* **Hand-curated HCL stub** -- Terraform modules (Hetzner, Cloudflare, Backblaze) and
  Docker Compose declarations represented as ``file`` nodes with ``lang: hcl`` /
  ``lang: yaml``, embedded in ``_HCL_STUB_NODES`` / ``_HCL_STUB_EDGES`` below.
  These are documented as a STUB: they capture structure, not symbol-level detail.

``emit`` merges both halves and writes the committed artifacts deterministically.
``check`` regenerates the same merge in a temp dir and fails if it differs from what is
committed. Because the HCL stub is a constant in this script, the merge is deterministic
-- a Python source change OR an update to the stub constants produces detectable drift.

When an HCL backend is added to ``ontology_gen`` (noorinalabs-main#820 Task 1
follow-on), replace ``_HCL_STUB_NODES`` / ``_HCL_STUB_EDGES`` / ``_HCL_STUB_LLMS``
with a standard ``generate()`` call and remove the merge step. The rest of this script
(subcommands, locate_generator, CI wiring) stays identical to the isnad-graph pattern.

Why a sibling generator instead of a vendored copy
==================================================
The generator is deliberately NOT copied here. A vendored copy would fork: a fix to
the extractor in noorinalabs-main would silently not reach the six consuming repos,
re-introducing the drift the owned-generator design exists to remove (eval
noorinalabs-main#854). CI checks out noorinalabs-main as a sibling (resolving the ref
to the matching wave branch with a ``main`` fallback -- deploy#159 cross-repo pattern)
and passes ``--gen-lib`` so this script can import ``ontology_gen``. Local dev relies
on the standard org layout (this repo cloned beneath ``noorinalabs-main/``).

Subcommands
===========
* ``emit``  -- (re)generate + merge, write committed index in place.
* ``check`` -- regenerate + merge into temp dir; fail (exit 1) if it differs.
* ``register-merge-driver`` -- register the union merge-driver in git config.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REPO_NAME = "noorinalabs-deploy"
OUT_REL = Path("ontology/structural")
ARTIFACTS = ("code-graph.json", "llms.txt")

ENV_GEN_LIB = "ONTOLOGY_GEN_LIB"

# ---------------------------------------------------------------------------
# Sort helpers -- replicate CodeGraph.to_dict() ordering so the merged graph
# is byte-identical to what the generator would produce if HCL were supported.
# Node sort key: (path, line, kind_rank, id)
# Edge sort key: (type, src, dst)
# ---------------------------------------------------------------------------
_KIND_RANK: dict[str, int] = {
    k: i for i, k in enumerate(("file", "module", "class", "func", "method"))
}


def _node_sort_key(n: dict) -> tuple[str, int, int, str]:
    return (n["path"], n["line"], _KIND_RANK.get(n["kind"], 99), n["id"])


def _edge_sort_key(e: dict) -> tuple[str, str, str]:
    return (e["type"], e["src"], e["dst"])


# ---------------------------------------------------------------------------
# Hand-curated HCL/YAML stub (deploy#493, pending HCL derivability).
# Update these constants when the Terraform topology changes.
# REMOVE them when an HCL extractor lands in ontology_gen and replace with a
# straight generate() call.
# ---------------------------------------------------------------------------

_HCL_STUB_NODES: list[dict] = [
    # --- Terraform / Hetzner VPS module (ADR 0001, per-env layout) ---
    {
        "id": "terraform/hetzner/modules/hetzner-vps/main.tf",
        "kind": "file",
        "path": "terraform/hetzner/modules/hetzner-vps/main.tf",
        "line": 1,
        "lang": "hcl",
    },
    {
        "id": "terraform/hetzner/modules/hetzner-vps/variables.tf",
        "kind": "file",
        "path": "terraform/hetzner/modules/hetzner-vps/variables.tf",
        "line": 1,
        "lang": "hcl",
    },
    {
        "id": "terraform/hetzner/modules/hetzner-vps/outputs.tf",
        "kind": "file",
        "path": "terraform/hetzner/modules/hetzner-vps/outputs.tf",
        "line": 1,
        "lang": "hcl",
    },
    {
        "id": "terraform/hetzner/modules/hetzner-vps/versions.tf",
        "kind": "file",
        "path": "terraform/hetzner/modules/hetzner-vps/versions.tf",
        "line": 1,
        "lang": "hcl",
    },
    # --- Terraform / Hetzner stg env root ---
    {
        "id": "terraform/hetzner/envs/stg/backend.tf",
        "kind": "file",
        "path": "terraform/hetzner/envs/stg/backend.tf",
        "line": 1,
        "lang": "hcl",
    },
    {
        "id": "terraform/hetzner/envs/stg/main.tf",
        "kind": "file",
        "path": "terraform/hetzner/envs/stg/main.tf",
        "line": 1,
        "lang": "hcl",
    },
    {
        "id": "terraform/hetzner/envs/stg/outputs.tf",
        "kind": "file",
        "path": "terraform/hetzner/envs/stg/outputs.tf",
        "line": 1,
        "lang": "hcl",
    },
    {
        "id": "terraform/hetzner/envs/stg/variables.tf",
        "kind": "file",
        "path": "terraform/hetzner/envs/stg/variables.tf",
        "line": 1,
        "lang": "hcl",
    },
    {
        "id": "terraform/hetzner/envs/stg/versions.tf",
        "kind": "file",
        "path": "terraform/hetzner/envs/stg/versions.tf",
        "line": 1,
        "lang": "hcl",
    },
    # --- Terraform / Hetzner prod env root ---
    {
        "id": "terraform/hetzner/envs/prod/backend.tf",
        "kind": "file",
        "path": "terraform/hetzner/envs/prod/backend.tf",
        "line": 1,
        "lang": "hcl",
    },
    {
        "id": "terraform/hetzner/envs/prod/main.tf",
        "kind": "file",
        "path": "terraform/hetzner/envs/prod/main.tf",
        "line": 1,
        "lang": "hcl",
    },
    {
        "id": "terraform/hetzner/envs/prod/outputs.tf",
        "kind": "file",
        "path": "terraform/hetzner/envs/prod/outputs.tf",
        "line": 1,
        "lang": "hcl",
    },
    {
        "id": "terraform/hetzner/envs/prod/variables.tf",
        "kind": "file",
        "path": "terraform/hetzner/envs/prod/variables.tf",
        "line": 1,
        "lang": "hcl",
    },
    {
        "id": "terraform/hetzner/envs/prod/versions.tf",
        "kind": "file",
        "path": "terraform/hetzner/envs/prod/versions.tf",
        "line": 1,
        "lang": "hcl",
    },
    # --- Terraform / Cloudflare ---
    {
        "id": "terraform/cloudflare/imports.tf",
        "kind": "file",
        "path": "terraform/cloudflare/imports.tf",
        "line": 1,
        "lang": "hcl",
    },
    {
        "id": "terraform/cloudflare/main.tf",
        "kind": "file",
        "path": "terraform/cloudflare/main.tf",
        "line": 1,
        "lang": "hcl",
    },
    {
        "id": "terraform/cloudflare/moved.tf",
        "kind": "file",
        "path": "terraform/cloudflare/moved.tf",
        "line": 1,
        "lang": "hcl",
    },
    {
        "id": "terraform/cloudflare/outputs.tf",
        "kind": "file",
        "path": "terraform/cloudflare/outputs.tf",
        "line": 1,
        "lang": "hcl",
    },
    {
        "id": "terraform/cloudflare/redirects.tf",
        "kind": "file",
        "path": "terraform/cloudflare/redirects.tf",
        "line": 1,
        "lang": "hcl",
    },
    {
        "id": "terraform/cloudflare/variables.tf",
        "kind": "file",
        "path": "terraform/cloudflare/variables.tf",
        "line": 1,
        "lang": "hcl",
    },
    {
        "id": "terraform/cloudflare/versions.tf",
        "kind": "file",
        "path": "terraform/cloudflare/versions.tf",
        "line": 1,
        "lang": "hcl",
    },
    # --- Terraform / Backblaze B2 (state backend bucket) ---
    {
        "id": "terraform/backblaze/main.tf",
        "kind": "file",
        "path": "terraform/backblaze/main.tf",
        "line": 1,
        "lang": "hcl",
    },
    {
        "id": "terraform/backblaze/outputs.tf",
        "kind": "file",
        "path": "terraform/backblaze/outputs.tf",
        "line": 1,
        "lang": "hcl",
    },
    {
        "id": "terraform/backblaze/variables.tf",
        "kind": "file",
        "path": "terraform/backblaze/variables.tf",
        "line": 1,
        "lang": "hcl",
    },
    {
        "id": "terraform/backblaze/versions.tf",
        "kind": "file",
        "path": "terraform/backblaze/versions.tf",
        "line": 1,
        "lang": "hcl",
    },
    # --- Terraform / Backblaze bootstrap (bucket + app-key creation) ---
    {
        "id": "terraform/backblaze-bootstrap/main.tf",
        "kind": "file",
        "path": "terraform/backblaze-bootstrap/main.tf",
        "line": 1,
        "lang": "hcl",
    },
    {
        "id": "terraform/backblaze-bootstrap/outputs.tf",
        "kind": "file",
        "path": "terraform/backblaze-bootstrap/outputs.tf",
        "line": 1,
        "lang": "hcl",
    },
    {
        "id": "terraform/backblaze-bootstrap/variables.tf",
        "kind": "file",
        "path": "terraform/backblaze-bootstrap/variables.tf",
        "line": 1,
        "lang": "hcl",
    },
    {
        "id": "terraform/backblaze-bootstrap/versions.tf",
        "kind": "file",
        "path": "terraform/backblaze-bootstrap/versions.tf",
        "line": 1,
        "lang": "hcl",
    },
    # --- Terraform / apply-locking smoke (ADR 0005 / deploy#334) ---
    {
        "id": "terraform/_lock-smoke/main.tf",
        "kind": "file",
        "path": "terraform/_lock-smoke/main.tf",
        "line": 1,
        "lang": "hcl",
    },
    # --- Docker Compose ---
    {
        "id": "compose/docker-compose.minio.yml",
        "kind": "file",
        "path": "compose/docker-compose.minio.yml",
        "line": 1,
        "lang": "yaml",
    },
    {
        "id": "compose/docker-compose.prod.yml",
        "kind": "file",
        "path": "compose/docker-compose.prod.yml",
        "line": 1,
        "lang": "yaml",
    },
]

_HCL_STUB_EDGES: list[dict] = [
    # Hetzner env roots call the shared hetzner-vps module
    {
        "src": "terraform/hetzner/envs/stg/main.tf",
        "dst": "terraform/hetzner/modules/hetzner-vps/main.tf",
        "type": "references",
    },
    {
        "src": "terraform/hetzner/envs/prod/main.tf",
        "dst": "terraform/hetzner/modules/hetzner-vps/main.tf",
        "type": "references",
    },
]

_HCL_STUB_LLMS = """
# ---------------------------------------------------------------------------
# HCL/YAML STUB (deploy#493, pending HCL derivability in ontology_gen)
# The sections below are HAND-CURATED. Terraform/HCL is not yet cleanly
# AST-derivable by ontology_gen. When an HCL extractor lands (noorinalabs-
# main#820 Task 1 follow-on), remove this section and extend the generator.
# ---------------------------------------------------------------------------

## terraform/hetzner/modules/hetzner-vps/ [hcl] STUB
Shared Hetzner VPS module -- provisions server, firewall, SSH key, labels.
Callers: terraform/hetzner/envs/stg/main.tf, terraform/hetzner/envs/prod/main.tf
provider: hetznercloud/hcloud ~>1.49  resource_naming: noorinalabs-${env}[-suffix]
files: main.tf, variables.tf, outputs.tf, versions.tf

## terraform/hetzner/envs/stg/ [hcl] STUB
Staging Terraform root -- calls modules/hetzner-vps; backend S3/B2 hetzner/stg.tfstate.
server: noorinalabs-stg (CPX21, Ashburn)  GH Environment: staging
files: main.tf, backend.tf, variables.tf, outputs.tf, versions.tf

## terraform/hetzner/envs/prod/ [hcl] STUB
Production Terraform root -- calls modules/hetzner-vps; backend S3/B2 hetzner/prod.tfstate.
server: noorinalabs-prod (CPX41, Ashburn)  GH Environment: production
files: main.tf, backend.tf, variables.tf, outputs.tf, versions.tf

## terraform/cloudflare/ [hcl] STUB
Cloudflare DNS + zone-settings module. provider: cloudflare/cloudflare ~>4.43.
Manages A/CNAME records, subdomains map (name->proxied_bool), zone SSL/TLS settings.
files: main.tf, variables.tf, outputs.tf, versions.tf, imports.tf, redirects.tf, moved.tf

## terraform/backblaze/ [hcl] STUB
Backblaze B2 bucket Terraform module (Terraform state backend bucket).
files: main.tf, variables.tf, outputs.tf, versions.tf

## terraform/backblaze-bootstrap/ [hcl] STUB
Backblaze bootstrap module -- creates the B2 bucket + application key for Terraform state.
files: main.tf, variables.tf, outputs.tf, versions.tf

## terraform/_lock-smoke/ [hcl] STUB
Apply-locking smoke test (ADR 0005 / deploy#334 -- null_resource serialization proof).
files: main.tf

## compose/docker-compose.prod.yml [yaml] STUB
Production Docker Compose stack -- application + databases + observability + messaging.
services: api, frontend, landing, user-service, caddy, neo4j, postgres, redis,
  user-postgres, user-redis, prometheus, grafana, loki, promtail, alertmanager,
  node-exporter, postgres-exporter, user-postgres-exporter, kafka, kafka-init, kafka-ui
networks: backend (internal), frontend (public via Caddy), user-backend (internal)

## compose/docker-compose.minio.yml [yaml] STUB
MinIO S3-compatible object storage overlay (local dev / DR testing).
"""


# ---------------------------------------------------------------------------
# Generator location (same pattern as isnad-graph #1128)
# ---------------------------------------------------------------------------


def locate_generator(repo_root: Path, explicit: str | None) -> Path | None:
    """Return the dir to put on sys.path so ``import ontology_gen`` resolves."""
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    env = os.environ.get(ENV_GEN_LIB)
    if env:
        candidates.append(Path(env))
    for ancestor in [repo_root, *repo_root.parents]:
        candidates.append(ancestor / ".claude" / "lib")
        candidates.append(ancestor / "noorinalabs-main" / ".claude" / "lib")

    for cand in candidates:
        if (cand / "ontology_gen" / "__main__.py").is_file():
            return cand.resolve()
    return None


def _not_found_message() -> str:
    return (
        "could not locate the ontology_gen generator package.\n"
        "  The generator lives in noorinalabs-main at .claude/lib/ontology_gen/\n"
        "  (it is intentionally NOT vendored into this repo -- single source of truth).\n"
        f"  Set {ENV_GEN_LIB}=<path-to>/noorinalabs-main/.claude/lib or pass\n"
        "  --gen-lib <path>. CI passes the sibling-checkout path automatically.\n"
    )


def _load_generate(gen_lib: Path):  # type: ignore[return]
    if str(gen_lib) not in sys.path:
        sys.path.insert(0, str(gen_lib))
    from ontology_gen.generate import generate  # noqa: PLC0415

    return generate


# ---------------------------------------------------------------------------
# Merge helpers -- combine the auto-generated Python graph with the HCL stub.
# Replicates CodeGraph.to_dict() ordering/dedup rules exactly so the merged
# graph is byte-identical to what a hypothetical unified generator would produce.
# ---------------------------------------------------------------------------


def _merge_graph(generated_path: Path) -> dict:
    """Load the generated code-graph.json and merge in _HCL_STUB_NODES/EDGES."""
    base: dict = json.loads(generated_path.read_text(encoding="utf-8"))

    all_nodes = base["nodes"] + _HCL_STUB_NODES
    all_edges = base["edges"] + _HCL_STUB_EDGES

    # Dedup nodes by id (first occurrence wins -- auto-generated first).
    seen_ids: set[str] = set()
    unique_nodes: list[dict] = []
    for n in all_nodes:
        if n["id"] in seen_ids:
            continue
        seen_ids.add(n["id"])
        unique_nodes.append(n)
    unique_nodes.sort(key=_node_sort_key)

    # Dedup edges and filter to resolvable endpoints.
    valid_ids = {n["id"] for n in unique_nodes}
    seen_edges: set[tuple[str, str, str]] = set()
    unique_edges: list[dict] = []
    for e in all_edges:
        if e["src"] not in valid_ids or e["dst"] not in valid_ids:
            continue
        key = (e["src"], e["dst"], e["type"])
        if key in seen_edges:
            continue
        seen_edges.add(key)
        unique_edges.append(e)
    unique_edges.sort(key=_edge_sort_key)

    return {"nodes": unique_nodes, "edges": unique_edges}


def _serialize_graph(graph_dict: dict) -> str:
    """Canonical one-record-per-line JSON matching ontology_gen.model.serialize_graph()."""

    def _records(items: list[dict]) -> str:
        rendered = [
            json.dumps(item, ensure_ascii=False, separators=(",", ":"), sort_keys=False)
            for item in items
        ]
        return ",\n".join(rendered)

    parts = ["{", '"nodes": [']
    if graph_dict["nodes"]:
        parts.append(_records(graph_dict["nodes"]))
    parts.append("],")
    parts.append('"edges": [')
    if graph_dict["edges"]:
        parts.append(_records(graph_dict["edges"]))
    parts.append("]")
    parts.append("}")
    return "\n".join(parts) + "\n"


def _build_into(gen_lib: Path, repo_root: Path, out_dir: Path) -> dict:
    """Generate Python portion + merge HCL stub -- write both artifacts to out_dir."""
    generate = _load_generate(gen_lib)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        py_counts = generate(repo_root, tmp_dir, REPO_NAME)

        merged = _merge_graph(tmp_dir / "code-graph.json")
        graph_text = _serialize_graph(merged)

        base_llms = (tmp_dir / "llms.txt").read_text(encoding="utf-8")
        llms_text = base_llms + _HCL_STUB_LLMS

        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "code-graph.json").write_text(graph_text, encoding="utf-8")
        (out_dir / "llms.txt").write_text(llms_text, encoding="utf-8")

    return {"py_counts": py_counts, "merged": merged}


# ---------------------------------------------------------------------------
# Subcommand implementations
# ---------------------------------------------------------------------------


def cmd_emit(gen_lib: Path, repo_root: Path) -> int:
    result = _build_into(gen_lib, repo_root, repo_root / OUT_REL)
    py = result["py_counts"]
    merged = result["merged"]
    print(
        f"ontology_gen: {REPO_NAME} -> {OUT_REL} "
        f"(py_files={py['files']} py_nodes={py['nodes']} py_edges={py['edges']} "
        f"hcl_stub_nodes={len(_HCL_STUB_NODES)} hcl_stub_edges={len(_HCL_STUB_EDGES)} "
        f"total_nodes={len(merged['nodes'])} total_edges={len(merged['edges'])})"
    )
    return 0


def cmd_check(gen_lib: Path | None, repo_root: Path, require_generator: bool) -> int:
    committed_dir = repo_root / OUT_REL
    missing = [a for a in ARTIFACTS if not (committed_dir / a).is_file()]
    if missing:
        sys.stderr.write(
            f"error: committed structural index missing: {missing}\n"
            "  Generate it with: python3 scripts/structural_ontology.py emit\n"
        )
        return 1

    if gen_lib is None:
        if require_generator:
            sys.stderr.write("error: " + _not_found_message())
            return 2
        sys.stderr.write(
            "warning: " + _not_found_message() + "  Skipping local staleness check "
            "(CI enforces it authoritatively).\n"
        )
        return 0

    drifted = False
    with tempfile.TemporaryDirectory() as tmp:
        fresh_dir = Path(tmp)
        _build_into(gen_lib, repo_root, fresh_dir)
        for artifact in ARTIFACTS:
            committed = (committed_dir / artifact).read_text(encoding="utf-8")
            fresh = (fresh_dir / artifact).read_text(encoding="utf-8")
            if committed == fresh:
                continue
            drifted = True
            sys.stderr.write(f"\nDRIFT: ontology/structural/{artifact} is stale vs source.\n")
            diff = difflib.unified_diff(
                committed.splitlines(keepends=True),
                fresh.splitlines(keepends=True),
                fromfile=f"committed/{artifact}",
                tofile=f"regenerated/{artifact}",
                n=1,
            )
            shown = 0
            for line in diff:
                sys.stderr.write(line if line.endswith("\n") else line + "\n")
                shown += 1
                if shown >= 60:
                    sys.stderr.write("  ... (diff truncated)\n")
                    break

    if drifted:
        sys.stderr.write(
            "\nThe committed structural ontology index is out of date.\n"
            "Regenerate and commit it:\n"
            "  python3 scripts/structural_ontology.py emit\n"
            "  git add ontology/structural/\n"
        )
        return 1
    print("OK: structural ontology index is current with source.")
    return 0


def cmd_register_merge_driver(gen_lib: Path, repo_root: Path) -> int:
    import subprocess  # noqa: PLC0415

    # Module form is required: merge_driver.py uses a relative import, so a bare
    # ``python3 .../merge_driver.py`` raises ImportError.
    driver = f"PYTHONPATH={gen_lib} python3 -m ontology_gen.merge_driver %O %A %B %P"
    subprocess.run(
        ["git", "config", "merge.ontology-codegraph.driver", driver],
        cwd=repo_root,
        check=True,
    )
    subprocess.run(
        ["git", "config", "merge.ontology-codegraph.name", "union merge for code-graph.json"],
        cwd=repo_root,
        check=True,
    )
    print(f"registered merge driver 'ontology-codegraph': {driver}")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("emit", "check", "register-merge-driver"),
        help="emit: write the index; check: fail if stale; register-merge-driver: git config.",
    )
    parser.add_argument(
        "--gen-lib",
        default=None,
        help=(
            "Directory containing the ontology_gen package (parent repo's "
            f".claude/lib). Defaults to ${ENV_GEN_LIB} or auto-discovery."
        ),
    )
    parser.add_argument(
        "--repo-root",
        default=str(REPO_ROOT),
        help="Repo root to index (default: this repo).",
    )
    parser.add_argument(
        "--require-generator",
        action="store_true",
        help=(
            "Treat a missing generator as a hard error (exit 2) instead of a "
            "graceful local skip. CI passes this so a check never false-passes."
        ),
    )
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    gen_lib = locate_generator(repo_root, args.gen_lib)

    if args.command == "check":
        return cmd_check(gen_lib, repo_root, args.require_generator)

    if gen_lib is None:
        sys.stderr.write("error: " + _not_found_message())
        return 2
    if args.command == "emit":
        return cmd_emit(gen_lib, repo_root)
    return cmd_register_merge_driver(gen_lib, repo_root)


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Drift guard for the prod tiered-rollout service lists (deploy#434).

The prod deploy brings the stack up in tiers (deploy#429): ``write-deploy-env``
takes two space-separated inputs — ``gated_services`` (tier 1, health-gated
``up --wait``) and ``deferred_services`` (tier 3, the Kafka pipeline, brought up
NON-gating). ``scripts/compose_tiered_up.sh`` partitions the stack against the
runtime ``docker compose config --services`` output using exactly these names.

Nothing today checks that every name in those two lists is a REAL compose
service. A service renamed or removed in ``compose/docker-compose.prod.yml``
(or a plain typo in the workflow input) would silently drop out of its tier:
a stale ``deferred_services`` entry stops excluding anything from tier 2, and a
stale ``gated_services`` entry makes ``compose up --wait`` fail mid-rollout in
prod — exactly the edge-outage failure mode deploy#429 set out to prevent.

This gate mirrors compose-validate.yml's passlist-drift check: it reads the
authoritative service set from ``docker compose config --services`` (fed on
stdin so the check stays hermetic + unit-testable) and asserts

    gated_services ∪ deferred_services  ⊆  compose config --services

for every deploy workflow that sets the inputs. The service list is taken with
NO ``--profile`` — the same view ``compose_tiered_up.sh`` and the deploy itself
see, so the four ``profiles: ["pipeline"]`` workers are legitimately absent and
must never appear in a tier list.

Exit 0 if every tiered name resolves to a real service; exit 1 with a per-name
diagnostic otherwise.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import IO

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

# Deploy workflows that may set the tiered-rollout inputs. stg leaves them empty
# (legacy single-`up`); prod sets both. Listed explicitly — same shape as
# compose-validate.yml's passlist-drift, which names deploy-{stg,prod}.yml.
DEPLOY_WORKFLOWS = (
    ".github/workflows/deploy-stg.yml",
    ".github/workflows/deploy-prod.yml",
)

# The composite whose `with:` carries the tier inputs.
WRITE_DEPLOY_ENV = "write-deploy-env"
TIER_INPUTS = ("gated_services", "deferred_services")


def read_services(stream: IO[str]) -> set[str]:
    """Parse newline-separated `docker compose config --services` output."""
    return {line.strip() for line in stream if line.strip()}


def tier_lists(workflow_path: Path) -> dict[str, set[str]]:
    """Map each tier input → its service-name set for one deploy workflow.

    Walks every step that `uses:` the write-deploy-env composite and unions the
    space-separated `with.{gated,deferred}_services` values. A workflow that
    never sets an input (stg) yields empty sets for it.
    """
    doc = yaml.safe_load(workflow_path.read_text())
    tiers: dict[str, set[str]] = {name: set() for name in TIER_INPUTS}
    for job in (doc.get("jobs") or {}).values():
        for step in job.get("steps", []) or []:
            if WRITE_DEPLOY_ENV not in str(step.get("uses", "")):
                continue
            with_block = step.get("with") or {}
            for name in TIER_INPUTS:
                raw = with_block.get(name)
                if raw:
                    tiers[name].update(str(raw).split())
    return tiers


def find_drift(services: set[str], tiers: dict[str, set[str]]) -> dict[str, set[str]]:
    """Return {input_name: names not in `services`} for any non-empty drift."""
    return {name: members - services for name, members in tiers.items() if members - services}


def main() -> int:
    services = read_services(sys.stdin)
    if not services:
        print(
            "::error::no services on stdin — pipe "
            "`docker compose config --services` into this gate."
        )
        return 1

    failed = False
    for rel in DEPLOY_WORKFLOWS:
        path = REPO_ROOT / rel
        if not path.is_file():
            print(f"::error file={rel}::deploy workflow not found.")
            failed = True
            continue
        tiers = tier_lists(path)
        drift = find_drift(services, tiers)
        if drift:
            failed = True
            for name, unknown in sorted(drift.items()):
                print(
                    f"::error file={rel}::{name} names services not in "
                    f"`docker compose config --services`: {sorted(unknown)}"
                )
            print(
                "  A tiered-rollout name must be a real compose service. A "
                "renamed/removed/typo'd entry silently drops out of its tier "
                "(deploy#429): a stale deferred name stops excluding anything "
                "from the non-gating tier; a stale gated name fails "
                "`compose up --wait` mid-rollout in prod."
            )
        else:
            tallied = sum(len(m) for m in tiers.values())
            print(f"OK {rel}: {tallied} tiered service name(s) all resolve.")

    if failed:
        return 1
    print(f"Service-tier drift check: OK ({len(services)} compose services).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

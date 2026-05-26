#!/usr/bin/env python3
"""Regression guard for ADR 0005 (#334) Terraform state-locking.

Every Terraform *apply* job in .github/workflows/terraform.yml MUST carry a
`concurrency:` block so concurrent applies to the same B2 state file serialize
(ADR 0005 § Decision, Option D). A new TF root added without the stanza
silently regresses to unlocked state — the failure ADR 0005's failure-mode
table calls out ("a future PR adds a new TF root without the concurrency
stanza"). This check makes that regression a CI failure instead of a silent gap.

Rules enforced for each job whose id starts with `apply`:
  1. Has a `concurrency` mapping (not the string short-form).
  2. `concurrency.group` contains the `terraform-apply-` prefix.
  3. `concurrency.cancel-in-progress` is explicitly false (queue, never cancel
     mid-apply — partial state writes are dangerous).

Exit 0 if all apply jobs comply; exit 1 with a per-job diagnostic otherwise.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parent.parent / ".github/workflows/terraform.yml"
GROUP_PREFIX = "terraform-apply-"


def main() -> int:
    if not WORKFLOW.is_file():
        print(f"::error::workflow not found: {WORKFLOW}")
        return 1

    doc = yaml.safe_load(WORKFLOW.read_text())
    jobs = doc.get("jobs", {})
    apply_jobs = {jid: spec for jid, spec in jobs.items() if jid.startswith("apply")}

    if not apply_jobs:
        # No apply jobs today (validate-only posture). Nothing to guard, but
        # say so loudly so a reviewer notices if this is unexpected.
        print("note: no `apply*` jobs found in terraform.yml — nothing to guard.")
        return 0

    failures: list[str] = []
    for jid, spec in sorted(apply_jobs.items()):
        conc = spec.get("concurrency")
        if conc is None:
            failures.append(f"{jid}: missing `concurrency:` block (ADR 0005 #334).")
            continue
        if not isinstance(conc, dict):
            failures.append(
                f"{jid}: `concurrency` must be a mapping with group + "
                f"cancel-in-progress, got short-form string {conc!r}."
            )
            continue
        group = str(conc.get("group", ""))
        if GROUP_PREFIX not in group:
            failures.append(
                f"{jid}: concurrency.group {group!r} must contain "
                f"{GROUP_PREFIX!r} (per-root, per-env key)."
            )
        if conc.get("cancel-in-progress") is not False:
            failures.append(
                f"{jid}: concurrency.cancel-in-progress must be explicitly "
                f"false (queue, never cancel mid-apply); got "
                f"{conc.get('cancel-in-progress')!r}."
            )

    if failures:
        print("::error::Terraform apply locking guard FAILED (ADR 0005 / #334):")
        for f in failures:
            print(f"::error::  {f}")
        print(
            '::error::Add `concurrency: {group: "terraform-apply-<root>-<env>", '
            "cancel-in-progress: false}` to each apply job. See "
            "docs/adr/0005-terraform-state-locking-on-b2-backend.md."
        )
        return 1

    print(
        f"OK: all {len(apply_jobs)} apply job(s) carry a "
        f"`terraform-apply-*` concurrency group with cancel-in-progress:false."
    )
    for jid, spec in sorted(apply_jobs.items()):
        print(f"  - {jid}: group={spec['concurrency']['group']!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

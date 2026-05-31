# Decommission Runbook — old hand-made prod VPS

Tearing down the original **hand-made `isnad-graph-prod` VPS** (system name
`noorinalabs-1box-prod`, `87.99.134.161` / Hetzner id `124917846`) once the
Phase C cutover to the Terraform-managed prod box has completed and been
verified.

> **The title of `deploy#86` is historically misleading.** The "hand-made
> isnad-graph-prod VPS" is not an isnad-graph-only box — it runs the **whole
> 20-container prod stack** (caddy + landing + isnad-graph api/frontend +
> user-service + neo4j + 2× postgres + 2× redis + kafka + observability).
> Decommissioning it tears down all of prod on the old host, which is only
> safe once traffic has fully flipped to the new box.

## Relationship to the cutover runbook

This runbook is the **handoff target of step 7** in
[`phase-c-cutover.md`](phase-c-cutover.md). The cutover runbook drives the
*migration* (env-var swap → image promote → deploy to new box → DNS flip →
verify); its step 7 ("Owner decommissions the old box") hands off to the
checklist below for the *teardown + reference hygiene*.

Do **not** run anything in this runbook until the cutover runbook has been
executed end-to-end and the new prod box is confirmed serving live traffic.

## Hard dependency — this is GATED, not done

**This runbook is operator-gated. Nothing here destroys the box automatically,
and as of this PR the old box has NOT been decommissioned.**

The teardown is blocked on:

- **`deploy#156`** — `infra(cutover): migrate isnad-graph.noorinalabs.com →
  isnad.noorinalabs.com on Caddy + compose + OAuth-redirect (post #83)`.
  This is the Wave-13 cutover issue and is **OPEN** at the time of writing.
  Until it lands and DNS has flipped to the TF-managed prod box, the old box
  is still serving production and must NOT be touched.
- **`deploy#83`** — Cloudflare per-service subdomains (CLOSED; prerequisite).

If you are reading this and `deploy#156` is still open, **stop** — the only
actionable work is staging this runbook + the reference-removal manifest
(which this PR does). The physical teardown waits for the operator,
post-cutover-verification.

## Pre-teardown gates (operator verifies all)

Confirm every box before deleting anything:

- [ ] **Cutover is complete.** `phase-c-cutover.md` steps 1–6 have all run
      and verified. `dig +short @1.1.1.1 noorinalabs.com` returns the
      **new** TF-managed prod IP, and `curl -I https://noorinalabs.com/`
      returns `HTTP/2 200` served by the new box.
- [ ] **No inbound traffic on the old box.** From the Hetzner dashboard
      traffic graph for `1box-prod` (id `124917846`), inbound has dropped to
      noise after the DNS TTL expired. Cross-check Caddy access logs on the
      old box show no recent real requests:
      ```bash
      ssh deploy@87.99.134.161 \
        "docker logs --since 15m noorinalabs-caddy-1 2>&1 | tail -50"
      ```
- [ ] **A successful end-to-end backup exists from the NEW box.** Per
      `main#212` option C, the new prod box must have completed one full
      backup (dump → compress → B2 upload → retention prune) before the old
      box is destroyed — this is the only recovery path once the old box is
      gone. Confirm the latest backup object in B2 is timestamped after
      cutover and sized plausibly.
- [ ] **Observability is green on the new box.** Grafana prod dashboards and
      Alertmanager show the new box healthy; no firing alerts that imply the
      stack is degraded.

If any gate fails, do NOT proceed — see the cutover runbook's Rollback
section. The old box is your rollback target until it is destroyed.

## Teardown sequence (operator-driven — NOT scriptable)

> **Owner action.** Decommissioning a production VPS is destructive and
> irreversible (no rollback — alpha pre-release). An async routine or agent
> MUST NOT drive these steps; the operator's eyes on the dependency graph
> are the safety check.

### 1. Snapshot before delete (optional safety net)

If the Hetzner plan allows, take a final snapshot of `1box-prod` before
deletion as a short-lived insurance policy. Delete the snapshot once the new
box has run clean for a few days. (Snapshots incur cost — do not leave them
indefinitely.)

### 2. Delete the old server from the Hetzner Cloud dashboard

From the Hetzner Cloud dashboard, locate `1box-prod`
(id `124917846` / `87.99.134.161` / `2a01:4ff:f0:be57::1`), confirm once
more it is no longer receiving traffic, and **delete the server**.

Because the old box is **outside Terraform**, there is no `terraform destroy`
for it — deletion is a manual dashboard action. (The TF-managed stg and prod
boxes live in `terraform/hetzner/envs/{stg,prod}/` and are NOT touched here.)

### 3. Remove operator-side stale entries

On any workstation that has SSH'd to the old box:

```bash
ssh-keygen -R 87.99.134.161
ssh-keygen -R '2a01:4ff:f0:be57::1'
```

Remove any `Host` block in `~/.ssh/config` that pinned the old prod IP.

### 4. Land the reference-removal manifest (see below)

Open the follow-up PR that strips the in-repo references to the old box.
That work is **staged behind this gate** — see the manifest — because some of
those references (notably the legacy rollback workflow) must stay live until
the cutover is confirmed irreversible.

## Reference-removal manifest (staged behind cutover)

These are the in-repo references to the old hand-made box, enumerated from a
`grep` at this PR's HEAD. They are **intentionally NOT removed in this PR** —
removing them now would either be premature (the legacy workflow is still the
rollback path) or would falsify still-accurate "currently the hand-made box
serves prod" statements. Each is listed with the disposition the post-cutover
cleanup PR should apply.

| File | Reference | Disposition post-cutover |
|------|-----------|--------------------------|
| `.github/workflows/deploy-isnad-graph.yml` | "once `deploy#86` decommissions the hand-made box this file will be removed entirely" + targets legacy `vars.VPS_HOST` | **Delete the entire workflow.** It is the manual single-VPS rollback path; safe to remove only once the new box is the irreversible prod. |
| `terraform/cloudflare/main.tf` (≈L73–80) | comment: "Currently the hand-made 1box-prod (`87.99.134.161` / …); cuts over to the new TF-managed noorinalabs-prod via deploy#86" | Rewrite comment to past tense: prod now points at the TF-managed box; drop the old IP/cutover framing. (The `cloudflare_record` resources themselves already read IPs from upstream TF state — no resource change.) |
| `RUNBOOK.md` (≈L82) | `bootstrap-vps.sh` "Pre-cloud-init VPSes (the hand-made `isnad-graph-prod` box …)" rationale | Drop the "pre-cloud-init / hand-made box" bullet as a live reason to run bootstrap; the residual-bootstrap rationale survives via the `#163` gap-coverage bullet. |
| `terraform/hetzner/README.md` (≈L130, L133) | "live production runs on a hand-made VPS outside Terraform (`isnad-graph-prod`) … decommission in `deploy#86`" | Update to past tense — prod is now fully TF-managed; convert to historical note. |
| `docs/adr/0001-tf-hetzner-per-env-state-strategy.md` (≈L91, L129) | hand-made `isnad-graph-prod` "is the current live box and will be decommissioned separately" | ADRs are immutable records — **do not rewrite history.** Add a dated "Update" note that #86 decommission has completed; leave the original text intact. |
| `docs/architecture.md` (≈L155) | server name `noorinalabs-isnad-graph-prod` | Verify the table reflects the TF-managed `noorinalabs-prod` naming; correct if it still shows the old hand-made server name. |
| `ontology/repos/deploy.yaml`, `ontology/services.yaml` | multiple "decom tracked in deploy#86" / "hand-made VPS remains live until deploy#86" notes | Resolved via `/ontology-rebuild` after the cutover lands and this issue closes — not a manual edit. |

> **Why this is a manifest and not the edits themselves:** the cutover
> (`deploy#156`) is OPEN. Three of these references (the legacy workflow, the
> CF comment, the README) are **still factually correct today** — the
> hand-made box *is* still live. Editing them now would make the repo lie
> ahead of reality. The cleanup PR runs *after* the operator completes the
> teardown above, at which point each row is true to apply.

## What can NOT be tested in CI

- The teardown requires a real Hetzner dashboard action against a real
  server — there is no dry-run, and the old box is outside Terraform so there
  is no `terraform plan`/`destroy` to validate.
- The pre-teardown gates (live traffic on the new box, no traffic on the old
  box, a fresh backup in B2) all depend on real prod state and cannot be
  exercised from CI or a worktree.
- This PR delivers the **runbook + reference manifest only**; the destructive
  teardown and the staged reference removal are deliberately out of scope and
  operator-gated.

## Related issues + refs

- `deploy#86` — this issue (decommission the hand-made prod VPS). This PR
  delivers the runbook + manifest; the issue stays **open** as the tracker
  until the operator completes the teardown post-cutover.
- `deploy#156` — Wave-13 cutover (DNS/Caddy/compose/OAuth migration). **Hard
  blocker** — must land and be verified before any teardown step runs.
- `deploy#83` — Cloudflare per-service subdomains (prerequisite, closed).
- [`phase-c-cutover.md`](phase-c-cutover.md) — the cutover runbook; its step 7
  hands off to this runbook.
- `noorinalabs-main#212` — two-VPS topology + option-C cutover/backup gate.
- `docs/adr/0001-tf-hetzner-per-env-state-strategy.md` — per-env TF state;
  records that the hand-made box is destroyed/recreated-separately.

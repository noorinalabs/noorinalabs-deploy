# Break-glass workflow inputs — audit + usage

This runbook covers the three break-glass `workflow_dispatch` inputs in this
repo, when each is appropriate, and how the audit trail is captured.

Filed by deploy#251 (P3W3 Tier 1) following the P3W2 emergency-thread retro
(`charter/emergency-mode.md`). PR #232 added the bypass mechanisms; this
runbook + the audit composite action `.github/actions/break-glass-audit` are
the audibility layer that ensures every bypass is recorded, alerted, and
attributable.

## TL;DR

Every break-glass invocation MUST supply a `break_glass_reason`. On
invocation, four audit signals fire:

1. `## ⚠ BREAK-GLASS USED` block in the workflow's `$GITHUB_STEP_SUMMARY`
2. `::warning::` annotation in the run log
3. Comment appended to the pinned `audit: break-glass usage log` issue
   (label `break-glass-audit-log`)
4. Prometheus textfile-collector metric `break_glass_invocation_*.prom` on
   the prod VPS, scraped by node-exporter, surfaced via the
   `BreakGlassUsed` Alertmanager rule

Empty reason → `validate-break-glass-reason` job fails the run before any
deploy/promotion machinery starts.

## The three inputs

### `skip_stg_verify` (promote.yml)

Bypasses the gate that requires a fresh successful `verify-deploy.yml` stg
run before promotion to prod. Default freshness window is 24h (configurable
via `stg_verify_max_age_hours`).

**Appropriate when:**
- The verify-deploy workflow itself is broken (e.g. broken assertion logic,
  GHCR authz issue with the verify path) AND prod must be promoted
- Stg has been verified out-of-band and the artifact is missing/expired

**NOT appropriate when:**
- Stg verify is failing because stg is genuinely broken — fix stg first
- You're in a hurry and don't want to wait for the gate — that's what the
  gate is FOR

### `skip_alembic_gate` (promote.yml)

Bypasses the alembic pre-retag migration gate (`db-migrate.yml` against
prod user-postgres). The gate runs `alembic upgrade head` to confirm the
incoming user-service image migrates cleanly before retagging.

**Appropriate when:**
- First-deploy / DR-restore where the prod user-service stack does not
  yet exist (the gate cannot run by definition — chicken-and-egg)
- Recovering prod from a state where user-postgres is unreachable

**NOT appropriate when:**
- The migration itself is failing — fix the migration; this gate exists
  precisely to catch migration breakage before retag
- Skipping is being used to "make the gate stop being noisy"

### `allow_stg_tags` (deploy-prod.yml)

Bypasses the `prod-*` tag-shape check that normally rejects `stg-*` tags
in deploy-prod. Surfaced 2026-05-02 emergency-restore where the
`promote.yml` retag path was blocked at the GHCR cross-repo write-403 level.

**Appropriate when:**
- The promote.yml retag path is broken AND prod must be deployed from
  stg-* digests directly
- DR restore where retag is impossible

**NOT appropriate when:**
- You skipped the promotion approval gate and want to deploy stg → prod
  directly. That bypass crosses an authorization boundary, not just a
  shape check

## Acceptance criteria for using a break-glass input

Before invoking any of the three inputs:

1. Have you announced the bypass per `charter/emergency-mode.md` (the
   `[OWNER-ACTION]`-style state delta)?
2. Is the underlying gate genuinely unable to run, OR is it correctly
   reporting a real problem?
3. Will the audit-log issue comment make sense to a reader six months
   from now without you in the room?

If any answer is "no" or "unclear", investigate the gate failure first.

## How the audit trail is captured

### Composite action

`.github/actions/break-glass-audit` is a single composite action invoked by:

- `promote.yml` → `gate-stg-verify` job (when `skip_stg_verify=true`)
- `promote.yml` → `audit-skip-alembic-gate` job (when `skip_alembic_gate=true`)
- `deploy-prod.yml` → `audit-allow-stg-tags` job (when `allow_stg_tags=true`)

The composite emits all four audit signals atomically per invocation.

### Job-summary block

Visible in the GH Actions run page under each job's summary tab. Includes
the input name, actor, reason, what was bypassed, and the run URL.

### Audit-log issue (pinned)

Label: `break-glass-audit-log`. The composite action find-or-creates by
label — operators can rename the issue title without breaking the audit.

To find the audit log:

```bash
gh issue list \
  --repo noorinalabs/noorinalabs-deploy \
  --state open \
  --label break-glass-audit-log \
  --json number,title,url \
  --jq '.[]'
```

There MUST be at most one open issue with this label. If two exist, the
composite picks the lowest-numbered (oldest) and warns; close the duplicate
manually.

### Prometheus textfile metric

Filename: `/var/lib/node_exporter/textfile_collector/break_glass_invocation_<input>.prom`
on the prod VPS. One file per input name (so concurrent break-glass uses
on different inputs don't clobber each other).

Schema (one gauge per file):

```
break_glass_invocation_timestamp_seconds{input="...",actor="...",workflow="...",run_id="..."} <unix-ts>
```

The free-text reason is NOT included as a label — operator-supplied free
text is high-cardinality + injection-prone for label values; it lives in
the audit-log issue comment instead.

Permission discipline: same `[ -w "${TEXTFILE_DIR}" ]` pre-check as the
alembic gate emit (db-migrate.yml). Fail loud rather than silent metric
loss if cloud-init didn't fire on the VPS.

## Alert investigation

### Alert: `BreakGlassUsed`

Fires within ~scrape-interval of break-glass invocation; resolves after
10 minutes of no fresh invocation.

Investigation steps:

1. Note the labels: `input`, `actor`, `workflow`, `run_id`.
2. Open the workflow run:
   `https://github.com/noorinalabs/noorinalabs-deploy/actions/runs/<run_id>`.
3. Read the `## ⚠ BREAK-GLASS USED` block in the run's job summary.
4. Read the latest comment on the audit-log issue
   (`gh issue list --label break-glass-audit-log --state open`).
5. Confirm the bypass was authorized — cross-reference the
   `[OWNER-ACTION]` post in the active wave thread per
   `charter/emergency-mode.md`.
6. If the bypass appears unauthorized: page the program director and
   investigate. The audit-log comment is the durable record.

### Alert: `BreakGlassMetricStale` (`#metric-stale-cleanup`)

Fires when a `break_glass_invocation_*.prom` file is more than 30 days old.
Cleanup is informational — the audit-log issue carries the historical
record.

```bash
ssh deploy@noorinalabs-prod \
  "rm /var/lib/node_exporter/textfile_collector/break_glass_invocation_<input>.prom"
```

Replace `<input>` with the value from the alert's `input` label.

## Receiver routing caveat

As of 2026-05-03, Alertmanager's `critical` receiver webhook is a
localhost placeholder (`http://localhost:9095/webhook`) pending real
Slack/email routing — tracked in deploy#262 (post-#251 sequel; the
parse-error fix for `${VAR}` interpolation that landed the placeholder
itself was deploy#127, closed 2026-04-19). The `BreakGlassUsed` rule
fires correctly inside Prometheus + Alertmanager, but the human-facing
notification path requires #262 to land first.

Until #262 is resolved, on-call should grep the audit-log issue + check
the Alertmanager UI directly:
`https://noorinalabs.com/alertmanager/` (behind admin auth).

## What can NOT be tested in CI

- The full-flow break-glass invocation requires a real prod VPS, real
  GitHub Issue creation, and a real Prometheus scrape. CI exercises only
  the validate-reason short-circuit (empty reason → fail).
- The audit-log issue find-or-create idempotency is exercised once per
  workflow run in production; CI does not validate it.
- Alertmanager rule firing requires a live Prometheus scrape against the
  prod VPS textfile.

## Related issues

- deploy#251 — this audit/alert layer (this PR)
- deploy#232 — original break-glass inputs landing
- deploy#262 — wire Alertmanager receivers to a real human surface (post-#251 sequel)
- deploy#127 — alertmanager.yml `${VAR}` parse-error fix that landed the localhost placeholder (closed 2026-04-19; historical reference)
- deploy#161 — alembic-gate textfile pattern (template for our metric emit)
- charter/emergency-mode.md — `[OWNER-ACTION]` discipline this audit complements

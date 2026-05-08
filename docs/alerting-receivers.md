# Alertmanager Receiver Topology

Closes deploy#274. See also deploy#273 (RUNBOOK.md) and deploy#127 (config-load fix).

## Receiver categories

| Receiver | Severity | Notifier | Channel / Target |
|---|---|---|---|
| `pagerduty-critical` | `critical` | PagerDuty Events API v2 | On-call rotation |
| `slack-critical` | `critical` | Slack incoming webhook | `#alerts-critical` |
| `slack-warning` | `warning` | Slack incoming webhook | `#alerts-warning` |

Critical alerts trigger both PagerDuty and Slack (`continue: true` in the route tree). Warning alerts go to Slack only.

## Credential topology

Alertmanager does not expand environment variables in its config file at startup (see deploy#127). Credentials are injected via `scripts/render-alertmanager-config.sh`, which runs `envsubst` on `infra/alertmanager/alertmanager.yml.tmpl` before `docker compose up`.

### GitHub Actions secrets required

| Secret name | Purpose | Who provisions |
|---|---|---|
| `ALERTMANAGER_SLACK_WEBHOOK_URL` | Slack incoming webhook URL (both channels share one workspace app) | Infra manager |
| `ALERTMANAGER_PAGERDUTY_INTEGRATION_KEY` | PagerDuty Events API v2 integration key (32-char hex) | Infra manager |

These secrets must be set in both the **staging** and **production** GitHub Actions environments.

The `infra/alertmanager/alertmanager.yml` committed to the repo contains CI-safe placeholder literals. The render script overwrites this file on the VPS at deploy time — the checked-in version is only used for `amtool check-config` in CI.

## What CI validates (PR-acceptance)

- `amtool check-config infra/alertmanager/alertmanager.yml` — YAML syntax + receiver-name resolution
- `amtool config routes test severity=warning` — routes warning labels to `slack-warning`
- `amtool config routes test severity=critical` — routes critical labels to `pagerduty-critical` (first match) and `slack-critical` (continue)

## What CANNOT be tested in CI (runtime-acceptance, post-merge)

These gates require real credentials, running compose stack, or external network reach:

1. **PagerDuty round-trip** — `amtool alert add alertname=TestCritical severity=critical` on prod VPS, then verify the incident appears in PagerDuty within 2 minutes and auto-resolves on `amtool alert expire`.
2. **Slack delivery** — same test alert should appear in `#alerts-critical` within 30 seconds.
3. **Warning Slack delivery** — `amtool alert add alertname=TestWarning severity=warning` → verify `#alerts-warning`.
4. **Config render on next deploy** — after adding secrets, run `deploy-stg.yml` and confirm `amtool check-config` on the VPS-side rendered file passes with real URLs.

Document completion of each runtime gate as a comment on deploy#274 before closing.

## Runbook cross-reference

The RUNBOOK.md § Tier 0 section has been updated (deploy#274) to remove the "paging not yet wired" disclosure and document the real topology. If paging ever breaks, the triage path is:

1. SSH to VPS: `cat /opt/noorinalabs-deploy/infra/alertmanager/alertmanager.yml` — confirm rendered URLs are not placeholders.
2. `docker logs noorinalabs-alertmanager-1 --tail=50` — look for receiver-delivery errors.
3. `amtool alert` (on VPS with `--alertmanager.url=http://localhost:9093`) — confirm alert is in firing state.
4. Check PagerDuty / Slack webhook health in their respective UIs.

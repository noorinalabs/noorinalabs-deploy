# Runbook — Alertmanager Slack routing

Per-env Alertmanager (deploy#262 + #263) routes alerts to Slack via the
`api_url_file:` directive. This runbook covers the secret pipeline,
activation, troubleshooting, and rotation.

## Configuration shape

| Env  | Config file                            | Slack channel    | Repeat interval (default / critical) |
|------|----------------------------------------|------------------|--------------------------------------|
| prod | `infra/alertmanager/alertmanager.prod.yml` | `#prod-alerts`   | 4h / 1h |
| stg  | `infra/alertmanager/alertmanager.stg.yml`  | `#stg-alerts`    | 8h / 1h |

Both receivers (`default` and `critical`) point at the same Slack channel
in each env — divergence is only in `repeat_interval` (critical pages
more often). Webhook URL is the same workspace-level Slack incoming
webhook; the `channel:` field overrides the default destination.

## Secret pipeline

```
GitHub Environment secret (SLACK_WEBHOOK_URL, per-env: staging / production)
        ↓ (deploy-{stg,prod}.yml `env:` block forwards via SSH)
SSH session env var on VPS
        ↓ (deploy step writes file)
/opt/noorinalabs-deploy/infra/alertmanager/slack_webhook_url.secret  (mode 0600)
        ↓ (compose bind-mount)
/etc/alertmanager/slack_webhook_url  (inside alertmanager container)
        ↓ (api_url_file: directive at startup)
Alertmanager → Slack webhook POST
```

The host file is gitignored (`.gitignore` § `infra/alertmanager/slack_webhook_url.secret`)
so the secret never enters the repo. The example placeholder
(`infra/alertmanager/slack_webhook_url.secret.example`) IS in the repo —
contents are the literal string `<unset>`, used by CI/local for
`docker compose config` resolution.

## Activation

1. Owner creates a Slack incoming webhook in the noorinalabs workspace
   targeting the desired channel (`#prod-alerts` or `#stg-alerts`).
2. Owner adds `SLACK_WEBHOOK_URL` as an Environment secret on the
   relevant GitHub Environment (`production` or `staging`).
3. Next deploy to that env writes the real value to `slack_webhook_url.secret`
   on the VPS, with `chmod 600`.
4. Alertmanager reads the file at startup. To force re-read without a
   full deploy, restart the container:
   ```
   ssh deploy@noorinalabs-1box-prod \
     'cd /opt/noorinalabs-deploy && docker compose -p noorinalabs -f compose/docker-compose.prod.yml restart alertmanager'
   ```

## Verification

After activation, send a synthetic alert:

```
ssh deploy@noorinalabs-1box-prod \
  'docker exec noorinalabs-alertmanager-1 amtool alert add \
     alertname=SmokeTest severity=warning \
     summary="Slack routing smoke test"'
```

A message should appear in `#prod-alerts` within ~30s. If it does not,
see the Troubleshooting section.

## Troubleshooting

### No messages arriving

1. Confirm the secret file content on the VPS:
   ```
   ssh deploy@noorinalabs-1box-prod 'cat /opt/noorinalabs-deploy/infra/alertmanager/slack_webhook_url.secret'
   ```
   If output is `<unset>`, the deploy was run before `SLACK_WEBHOOK_URL`
   was set in the GitHub Environment. Re-run the deploy.

2. Check alertmanager logs for webhook POST errors:
   ```
   ssh deploy@noorinalabs-1box-prod \
     'docker compose -p noorinalabs -f /opt/noorinalabs-deploy/compose/docker-compose.prod.yml logs --tail=100 alertmanager | grep -i slack\\|webhook\\|error'
   ```
   A `404 Not Found` from Slack means the webhook URL is wrong or revoked.
   A `429 Too Many Requests` means rate-limited (rare — investigate alert
   storm).

3. Check alertmanager itself is up:
   ```
   curl -sf https://noorinalabs.com/alertmanager/-/healthy
   ```

### Wrong channel

`channel:` in the per-env yaml overrides the webhook's default channel.
If alerts are arriving in the wrong channel, the override is set
correctly but the webhook's bound channel is being interpreted by Slack
as the destination — this happens when the webhook was created against
a specific channel rather than the workspace.

Fix: rotate the webhook to be a workspace-level webhook (creator UI in
Slack), then the `channel:` override takes effect. Update the
GitHub Environment secret and redeploy.

## Rotation

Slack webhook URLs are bearer tokens — if leaked, rotate immediately:

1. Slack admin UI → revoke the existing webhook.
2. Create a new webhook (workspace-level, default channel of any kind —
   the `channel:` field in alertmanager config overrides).
3. Update `SLACK_WEBHOOK_URL` in both `production` and `staging`
   GitHub Environments.
4. Re-run the deploy workflow for each env to push the new value to the
   VPS. (Or use a no-op redeploy: `gh workflow run deploy-prod.yml -f sha=$(git rev-parse origin/main)`.)

## Related

- deploy#262 — original receiver-routing issue
- deploy#127 — historical placeholder fix (closed 2026-04-19; resolved
  the `${VAR}` interpolation parse-error that landed the
  `http://localhost:9095/webhook` placeholder)
- deploy#263 — per-env config split (this routing inherits the per-env shape)
- `docs/runbooks/break-glass.md` — break-glass alert delivery (this routing
  is what makes `BreakGlassUsed` reach a human surface)

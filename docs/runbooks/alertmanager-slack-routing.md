# Runbook — Alertmanager routing & receivers

Per-env Alertmanager (deploy#262 + #263 + #452 + #453) routes alerts to a
**primary** channel (Slack), an **independent backup** channel (Email), and a
**dead-man's switch** (Healthchecks.io). All channels use Alertmanager's native
`*_configs` receiver blocks with `*_file` secret references — there is no
bespoke notifier service. This runbook covers the channel model, the swap
procedure, the secret pipeline, activation, troubleshooting, and rotation.

## Channel model (swappable by design)

Each receiver in `alertmanager.{stg,prod}.yml` is composed of native, per-channel
`*_configs` blocks. The block IS the generic channel interface:

| Channel | Receiver block | Role | Secret (`*_file`) |
|---------|----------------|------|-------------------|
| Slack | `slack_configs` | **primary** | `slack_webhook_url` ← `SLACK_WEBHOOK_URL` |
| Email | `email_configs` | **independent backup** | `smtp_password` ← `SMTP_PASSWORD` |
| Healthchecks.io | `webhook_configs` (receiver `deadmansswitch`) | **dead-man's switch** | `healthchecks_url` ← `HEALTHCHECKS_PING_URL` |

Slack + Email live in BOTH the `default` and `critical` receivers and are
**independent notifiers** — Alertmanager attempts each separately, so a Slack
outage does not block Email (different failure domain). The dead-man's switch
is a separate receiver reached only by the always-firing `Watchdog` alert
(see § "Dead-man's switch" below).

## Configuration shape

| Env  | Config file                            | Slack channel    | Repeat interval (default / critical / watchdog) |
|------|----------------------------------------|------------------|-------------------------------------------------|
| prod | `infra/alertmanager/alertmanager.prod.yml` | `#prod-alerts`   | 4h / 1h / 5m |
| stg  | `infra/alertmanager/alertmanager.stg.yml`  | `#stg-alerts`    | 8h / 1h / 5m |

Both `default` and `critical` point at the same Slack channel in each env —
divergence is only in `repeat_interval` (critical pages more often). Webhook URL
is the same workspace-level Slack incoming webhook; the `channel:` field
overrides the default destination.

## Swapping / adding a channel

Swapping or adding a delivery channel is a **config-only change** — edit the
per-env YAML and redeploy; **no image rebuild**. The receiver structure is
designed for this: each channel is one `*_configs` block.

- **Swap the primary** (e.g. Slack → Telegram): in each receiver, replace the
  `slack_configs:` block with the equivalent native block for the new channel
  (`telegram_configs:`, `discord_configs:`, …), pointing its secret at a new
  `*_file` (e.g. `bot_token_file`). Add the new secret to the deploy pipeline
  (a new input on `.github/actions/write-deploy-env/action.yml`, a new mount in
  `compose/docker-compose.prod.yml`, a `*_FILE` line in `extra_env`, and a
  `<channel>_FILE` entry in `compose/.env.example` + `.gitignore`) following the
  exact pattern the `smtp_password` / `healthchecks_url` secrets use.
- **Add a channel** (keep Slack, add another): append the new `*_configs` block
  to the relevant receiver(s). All blocks in a receiver fire independently.
- **Remove a channel**: delete its `*_configs` block.

Validate any edit with `amtool check-config` (CI runs this — see the
`amtool check-config` job in `.github/workflows/compose-validate.yml`):

```
docker run --rm --entrypoint amtool \
  -v "$PWD/infra/alertmanager:/etc/alertmanager:ro" \
  prom/alertmanager:v0.28.1 \
  check-config /etc/alertmanager/alertmanager.prod.yml
```

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

## Email backup (independent channel)

Email is a second, independent delivery path so a Slack/webhook outage does not
blind the operator. It lives in the `email_configs` block of both the `default`
and `critical` receivers, with SMTP settings in the `global:` section of each
per-env config.

**Owner activation:**

1. Choose an SMTP relay (e.g. a Gmail account with an app password, or a
   transactional free tier such as Brevo/Mailgun/SES).
2. Edit the **non-secret** literals in `infra/alertmanager/alertmanager.{stg,prod}.yml`:
   `smtp_smarthost`, `smtp_from`, `smtp_auth_username` (currently `.invalid`
   placeholders), and the `to:` address in each `email_configs` block (currently
   `owner@example.invalid`). This is a config-only edit.
3. Add the **secret** `SMTP_PASSWORD` to the relevant GitHub Environment
   (`staging` / `production`). The deploy workflow writes it to
   `infra/alertmanager/smtp_password.secret` (mode 0600); alertmanager reads it
   via `smtp_auth_password_file`.
4. Redeploy. Until both the literals and the secret are set, the Email notifier
   simply fails-and-logs — Slack delivery is unaffected (independent notifiers).

## Dead-mans switch

(Healthchecks.io) — Alertmanager can page on anything except its own death (or Prometheus's, or the
whole VPS's). The dead-man's switch closes that blind spot: the always-firing
`Watchdog` alert (`vector(1)`, `severity: deadmansswitch`, in
`infra/prometheus/alerts.yml`) is routed to the `deadmansswitch` webhook
receiver, which pings a Healthchecks.io URL every `repeat_interval` (5m). If
Prometheus, Alertmanager, or the box dies, the heartbeat stops and
Healthchecks.io pages the owner after its grace window. **Silence is the failure
signal** — the inverse of every other alert.

**Owner activation:**

1. Create a Healthchecks.io check (free tier / nonprofit discount). Set its
   **period to 5m** (matching the Watchdog `repeat_interval`) and a **grace
   window** of ~10m so a single missed ping during a deploy restart doesn't
   false-page.
2. Configure the check's own notification integration (Slack / Email) so a
   lapsed heartbeat reaches the owner — this is Healthchecks.io-side, consistent
   with the primary-alerting channel.
3. Add the **secret** `HEALTHCHECKS_PING_URL` (the check's ping URL,
   `https://hc-ping.com/<uuid>`) to the relevant GitHub Environment. The deploy
   workflow writes it to `infra/alertmanager/healthchecks_url.secret` (mode
   0600); the webhook receiver reads it via `url_file`.
4. Redeploy. **Verify on stg** by deliberately stopping the heartbeat:
   ```
   ssh deploy@noorinalabs-1box-stg \
     'cd /opt/noorinalabs-deploy && docker compose -p noorinalabs -f compose/docker-compose.prod.yml stop alertmanager'
   ```
   Within (period + grace) ≈ 15m, Healthchecks.io should flip the check to
   "down" and notify. Restart alertmanager to resume the heartbeat:
   ```
   ssh deploy@noorinalabs-1box-stg \
     'cd /opt/noorinalabs-deploy && docker compose -p noorinalabs -f compose/docker-compose.prod.yml start alertmanager'
   ```

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

All three secrets are bearer credentials — if leaked, rotate immediately:

**Slack webhook (`SLACK_WEBHOOK_URL`):**
1. Slack admin UI → revoke the existing webhook.
2. Create a new webhook (workspace-level, default channel of any kind —
   the `channel:` field in alertmanager config overrides).
3. Update `SLACK_WEBHOOK_URL` in both `production` and `staging`
   GitHub Environments.
4. Re-run the deploy workflow for each env to push the new value to the
   VPS. (Or use a no-op redeploy: `gh workflow run deploy-prod.yml -f sha=$(git rev-parse origin/main)`.)

**SMTP password (`SMTP_PASSWORD`):** revoke/regenerate the app password (or SMTP
credential) at the relay, update `SMTP_PASSWORD` in both Environments, redeploy.

**Healthchecks.io ping URL (`HEALTHCHECKS_PING_URL`):** the ping URL embeds the
check UUID. In the Healthchecks.io UI, regenerate the check's ping URL (or
recreate the check), update `HEALTHCHECKS_PING_URL` in both Environments,
redeploy.

## Related

- deploy#262 — original receiver-routing issue
- deploy#127 — historical placeholder fix (closed 2026-04-19; resolved
  the `${VAR}` interpolation parse-error that landed the
  `http://localhost:9095/webhook` placeholder)
- deploy#263 — per-env config split (this routing inherits the per-env shape)
- deploy#452 — Email backup receiver + swappable-channel structure
- deploy#453 — Healthchecks.io dead-man's switch (`Watchdog` alert)
- `docs/runbooks/break-glass.md` — break-glass alert delivery (this routing
  is what makes `BreakGlassUsed` reach a human surface)

# Runbook — Log ingestion (Promtail → Loki)

Promtail (container `noorinalabs-promtail-1`, image `grafana/promtail:3.4.1`)
ships container stdout/stderr from the Docker socket to Loki
(`noorinalabs-loki-1`, image `grafana/loki:2.9.10`). Loki retains 7 days
of logs and is the only ingestion path — there is no Logstash, no Fluent
Bit, no alternative shipper.

The promtail container exposes self-metrics on port 9080 (server.http_listen_port
in `infra/promtail/promtail-config.yml`); Prometheus scrapes this via the
`promtail` job in `infra/prometheus/prometheus.{stg,prod}.yml`.

## Alerts

Two alerts in `infra/prometheus/alerts.yml` § `log_ingestion`:

### PromtailLogIngestionStopped

Fires when `rate(promtail_sent_entries_total[5m]) == 0` for 10 minutes.
The series exists (promtail is scrapable) but no entries are flowing.

**Causes:**
1. Docker SD discovery is broken — promtail can't enumerate containers
   on the daemon socket (typically API version mismatch; deploy#130
   bumped to promtail 3.4.1 specifically to fix this).
2. All containers are silent (rare; usually means the stack itself is
   down).
3. Loki distributor is rejecting writes (check Loki logs for
   `429 Too Many Requests` or distributor error spam).

**Investigation:**
```
ssh deploy@noorinalabs-1box-prod 'docker compose -p noorinalabs \
  -f /opt/noorinalabs-deploy/compose/docker-compose.prod.yml \
  logs --tail=100 promtail'
```

Look for:
- `error scraping containers` → Docker SD issue
- `Error sending batch ... 429` → Loki rate-limit
- `connection refused` → Loki container down (orthogonal alert
  `PromtailDown` would also fire; this one wouldn't)

**Resolution:**
- Docker SD broken → restart promtail (`docker compose restart promtail`);
  if that doesn't clear it, check the docker-ce major version on the
  VPS and bump promtail accordingly.
- Loki rate-limit → check `loki_distributor_ingester_appends_total` and
  `loki_distributor_lines_received_total`; consider bumping
  `limits_config.ingestion_rate_mb` in `infra/loki/loki-config.yml`.

### PromtailDown

Fires when `up{job="promtail"} == 0` for 5 minutes — Prometheus can't
even reach the promtail metrics endpoint. The container is either down,
in a restart loop, or unreachable on the backend network.

**Investigation:**
```
ssh deploy@noorinalabs-1box-prod 'docker compose -p noorinalabs ps promtail'
```

If the container is restarting, check `docker compose logs promtail`
for the panic / config-parse error.

**Resolution:** typically a config error introduced by a recent edit to
`infra/promtail/promtail-config.yml`. The CI gate added in deploy#212
runs `promtool` (not `promtail check`) — there is no equivalent CI
validator for promtail config today. Pre-merge: copy the file into a
local promtail container (`docker run --rm -v "$PWD/infra/promtail:/etc/promtail:ro" grafana/promtail:3.4.1 -config.file=/etc/promtail/promtail-config.yml -dry-run -log.level=warn`)
and verify clean startup.

## Why this group exists

deploy#130 removed promtail's healthcheck because the distroless image
ships without `wget`/`curl`/`nc` — any HTTP-probe healthcheck failed
permanently (~888-deep failing-streak observed in steady-state before
removal). The stated backstop in #130 was "restart-policy + Prometheus
alerting on log-rate drop." The restart-policy was already present;
deploy#131 lands the alert half so a silent promtail failure no longer
goes undetected until an operator notices Loki is empty.

## Related

- deploy#128 — original root bug (Docker SD broke on daemon bump)
- deploy#130 — healthcheck removal + promtail 3.4.1 bump
- deploy#131 — this alert group + scrape config
- `infra/promtail/promtail-config.yml` — pipeline definition
- `infra/loki/loki-config.yml` — retention + limits

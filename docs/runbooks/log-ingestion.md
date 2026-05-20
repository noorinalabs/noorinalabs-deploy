# Runbook — Log ingestion (Alloy → Loki)

Alloy (container `noorinalabs-alloy-1`, image `grafana/alloy:v1.16.1`) ships
container stdout/stderr from the Docker socket to Loki (`noorinalabs-loki-1`,
image `grafana/loki:2.9.10`). Loki retains 7 days of logs and is the only
ingestion path — there is no Logstash, no Fluent Bit, no alternative shipper.
Migrated from promtail in deploy#132; pipeline semantics (Docker SD + JSON
extraction + level/logger labels + drop-on-empty-level) are unchanged.

The alloy container exposes self-metrics and an HTTP UI on port 12345
(`--server.http.listen-addr` in `compose/docker-compose.prod.yml`); Prometheus
scrapes `/metrics` via the `alloy` job in `infra/prometheus/prometheus.{stg,prod}.yml`.
The pipeline itself is declared in `infra/alloy/config.alloy` (River syntax).

Alloy's base image ships `/bin/sh` and `wget`, so a container HTTP healthcheck
against `/-/ready` is enabled in compose. The metric-rate alerts below remain
as defense-in-depth: a healthy `/-/ready` does not prove that logs are actually
flowing (Docker SD could be broken, `loki.write` could be dropping, the pipeline
could match nothing).

## Alerts

Two alerts in `infra/prometheus/alerts.yml` § `log_ingestion`:

### AlloyLogIngestionStopped

Fires when `rate(loki_write_sent_entries_total[5m]) == 0` for 10 minutes.
The series exists (alloy is scrapable) but no entries are flowing to Loki.

**Causes:**
1. Docker SD discovery is broken — alloy can't enumerate containers on the
   daemon socket (typically API version mismatch; same failure mode that
   motivated the promtail 3.4.1 bump in deploy#130, now mitigated by tracking
   Alloy releases which keep the Docker client current).
2. All containers are silent (rare; usually means the stack itself is down).
3. Loki distributor is rejecting writes (check Loki logs for `429 Too Many
   Requests` or distributor error spam). `loki_write_dropped_entries_total`
   on alloy will also be incrementing if this is the cause.

**Investigation:**
```
ssh deploy@noorinalabs-1box-prod 'docker compose -p noorinalabs \
  -f /opt/noorinalabs-deploy/compose/docker-compose.prod.yml \
  logs --tail=100 alloy'
```

Look for:
- `discovery.docker` errors → Docker SD issue
- `loki.write ... 429` → Loki rate-limit (corroborated by non-zero
  `rate(loki_write_dropped_entries_total[5m])`)
- `connection refused` to `loki:3100` → Loki container down (orthogonal alert
  `AlloyDown` would NOT fire — alloy itself is up — but Loki's own
  healthcheck/alerting will)

**Resolution:**
- Docker SD broken → restart alloy (`docker compose restart alloy`); if that
  doesn't clear it, check the docker-ce major version on the VPS and bump
  alloy accordingly (see the digest-pin in compose).
- Loki rate-limit → check `loki_distributor_ingester_appends_total` and
  `loki_distributor_lines_received_total`; consider bumping
  `limits_config.ingestion_rate_mb` in `infra/loki/loki-config.yml`.

### AlloyDown

Fires when `up{job="alloy"} == 0` for 5 minutes — Prometheus can't even reach
the alloy metrics endpoint. The container is either down, in a restart loop,
or unreachable on the `backend` network.

**Investigation:**
```
ssh deploy@noorinalabs-1box-prod 'docker compose -p noorinalabs ps alloy'
```

If the container is restarting, check `docker compose logs alloy` for the
panic / River-parse error.

**Resolution:** typically a config error introduced by a recent edit to
`infra/alloy/config.alloy`. There is no CI-side River validator today (the
deploy#212 gate runs `promtool` only). Pre-merge: copy the file into a local
alloy container and verify clean startup:

```
docker run --rm \
  -v "$PWD/infra/alloy:/etc/alloy:ro" \
  grafana/alloy:v1.16.1 \
  fmt /etc/alloy/config.alloy
```

`alloy fmt` parses the River file and exits non-zero on syntax errors.
`alloy run --server.http.listen-addr=0.0.0.0:12345 /etc/alloy/config.alloy`
performs a full component-graph load and is the closest local equivalent to
"would this actually start in prod" — run it briefly and check stderr.

## Why this group exists

deploy#128/#130 removed promtail's healthcheck because the distroless promtail
image shipped without `wget`/`curl`/`nc` — any HTTP-probe healthcheck failed
permanently (~888-deep failing-streak observed in steady-state before removal).
The stated backstop in #130 was "restart-policy + Prometheus alerting on
log-rate drop"; the restart-policy was already present, deploy#131 landed the
alert half.

deploy#132 (the alloy migration) re-enables the container HTTP healthcheck
(Alloy's base image ships wget, so `wget -qO- http://localhost:12345/-/ready`
works) but keeps both alerts intact. The healthcheck and the metric-rate alerts
catch different failure modes — `/-/ready` returns 200 once initial config is
loaded and stays 200 as long as the server is responsive, even if `loki.write`
is dropping every entry. The rate alert is the only signal that ingestion is
actually flowing end-to-end.

## Related

- deploy#128 — original root bug (Docker SD broke on daemon bump, promtail era)
- deploy#130 — healthcheck removal + promtail 3.4.1 bump
- deploy#131 — this alert group + scrape config (promtail era)
- deploy#132 — promtail → alloy migration; healthcheck re-enabled, alerts ported
- `infra/alloy/config.alloy` — pipeline definition (River syntax)
- `infra/loki/loki-config.yml` — retention + limits

# Runbook: New-service deployment checklist

Use this checklist when adding a brand-new application service (image) to the
Noorina Labs VPS stack. It exists because deploying the landing page in Wave B
took 4 fix-and-retry cycles (cluster: `VPS_HOST` pointed at the Cloudflare proxy
IP, no GHCR image existed, VPS was not authenticated to GHCR, Caddyfile was
edited but Caddy wasn't restarted). The intent is that the next new service is
deployable on the first attempt.

**Audience:** SRE engineer or service owner adding a new application image to
the stack.
**Out of scope:** changes to an *existing* service (use
[`deploy-isnad-graph.md`](deploy-isnad-graph.md) /
[`deploy-landing-page.md`](deploy-landing-page.md) instead); infra-only changes
(VPS, observability — see [`../../RUNBOOK.md`](../../RUNBOOK.md) § Build).

## How to read this checklist

Each item is marked with one of:

- **Manual** — operator must do this; no safety net exists yet.
- **Automated in: `<ref>`** — the step is now performed by a workflow, the
  bootstrap script, or Terraform. Listed so the operator knows the step exists
  and where the safety net lives if it fails.
- **Verify** — the step is automated but worth confirming because past
  incidents have shown the automation can silently no-op (e.g. dispatch
  contract drift, [#162]).

## 1. Image build & registry (service repo)

- [ ] **GHCR image built and pushed** — service repo's `ghcr-publish.yml`
      has run at least once on a default-branch commit, producing the four
      publish-side tags required by Contract v6 (`sha-<short>`, `latest`,
      `stg-<short>`, `stg-latest`). Verify in
      `https://github.com/orgs/noorinalabs/packages/container/<image>/versions`.
      *Automated in: service repo `.github/workflows/ghcr-publish.yml`.*
- [ ] **Image name follows convention** — `ghcr.io/noorinalabs/<repo-name>`.
      Any other prefix will break the
      [image-tag-invariants](../../.github/workflows/image-tag-invariants.yml)
      smoke check.

## 2. Cross-repo dispatch contract

The deploy fan-in (`deploy-stg.yml`) listens for `repository_dispatch` events
of type `deploy-noorinalabs-<service>`. The literal event-type string IS the
contract — sender/listener mismatch returns 204 silently ([#162]).

- [ ] **`notify-deploy` job exists in the service repo's
      `ghcr-publish.yml`** with `event_type: deploy-noorinalabs-<service>`.
      *Verify against:* `ontology/repos/deploy.yaml § dispatch_contracts_received`.
- [ ] **Listener added** to `deploy-stg.yml`'s `repository_dispatch.types:`
      list **and** the per-service discrimination block (`if: github.event.action == 'deploy-noorinalabs-<service>'`).
      *Manual.* See [`deploy-stg.yml`](../../.github/workflows/deploy-stg.yml).
- [ ] **Listener added** to `services.yaml § cross_repo_dispatch_contracts`
      and `ontology/repos/deploy.yaml § dispatch_contracts_received` so a
      future repo-rename has a single source-of-truth to grep for ([#162]
      origin).

## 3. Secrets & variables (service repo + Environment scopes)

The deploy SSHes from a GitHub-hosted runner to the VPS using the `deploy`
user. The reusable workflows (`deploy-stg.yml`, `db-migrate.yml`,
`promote.yml`) read `vars.VPS_HOST` scoped to the appropriate GH Environment
(`staging` or `production`) — *not* a repo-level var.

- [ ] **`vars.VPS_HOST` set to the VPS IPv4 address** in **both** the
      `staging` and `production` GH Environments. **Not the domain**, and
      **not the Cloudflare-proxied IP** — `appleboy/ssh-action` will time out
      on either. (Wave-B failure #1.)
- [ ] **`secrets.DEPLOY_SSH_PRIVATE_KEY` set** at the org level (or per-env
      override). Public half is in the VPS's `/home/deploy/.ssh/authorized_keys`.
      *Automated in:* [`cloud-init.yaml.tpl`](../../terraform/hetzner/modules/hetzner-vps/cloud-init.yaml.tpl)
      seeds both root and deploy authorized_keys with the per-env
      `ssh_public_key` on first boot (Terraform-provisioned VPSes only).
      Cloud-init gap surfaced 2026-04-24 (`cloud_init_ssh_key_gap` in
      `ontology/repos/deploy.yaml`) — for TF-provisioned boxes confirm the key
      actually landed before relying on it. For pre-cloud-init VPSes or
      operator-added admin keys, [`scripts/bootstrap-vps.sh`](../../scripts/bootstrap-vps.sh)
      provides an idempotent append-with-dedup merge from root → deploy (#163).
- [ ] **Runtime GHCR auth** — the deploy script logs in with the
      auto-provisioned `GITHUB_TOKEN` and `trap`s a logout. No long-lived
      GHCR PAT lives on the VPS for app-service pulls.
      *Automated in:* [`deploy-isnad-graph.yml`](../../.github/workflows/deploy-isnad-graph.yml#L125-L128).
      *Wave-B failure #3 ("VPS not authenticated to GHCR — needed docker
      login") is fully obsoleted by this — DO NOT add a manual `docker login`
      to bootstrap.*
- [ ] **If the service uses `docker/login-action` for retag/promote**
      (e.g. `promote.yml`, `cold-rebuild-dryrun.yml`), use
      `secrets.GH_PACKAGES_TOKEN` (org-level PAT with `write:packages`),
      **not** `GITHUB_TOKEN`. *Automated check in:*
      [`cold_rebuild_static_checks.py`](../../.github/workflows/scripts/cold_rebuild_static_checks.py).

## 4. Stack wiring (this repo)

- [ ] **Service block added** to
      [`compose/docker-compose.prod.yml`](../../compose/docker-compose.prod.yml).
      Follow existing conventions: `read_only: true` + tmpfs, `restart:
      unless-stopped`, healthcheck, memory/cpu limits, image-only (no `build:`
      stanza), `networks:` reflecting public-vs-internal scope.
- [ ] **Caddy route added** to [`caddy/Caddyfile`](../../caddy/Caddyfile).
      *Wave-B failure #4 ("Caddy not restarted") is obsoleted — Caddyfile is
      mounted read-only and the next deploy's `docker compose up -d
      --force-recreate` rebuilds the Caddy container, picking up the new
      file. No manual restart is required.*
- [ ] **Caddyfile order check — longer-prefix-first.** Caddy evaluates
      `handle` blocks in source order; carve-outs MUST appear before their
      catch-all parents. See
      [`docs/architecture.md § Routing invariant`](../architecture.md#routing-invariant-longer-prefix-first-ordering)
      and the failure-mode lesson from [#133]/[#134]. Order-lint follow-up
      tracked in [#135] part 3.
- [ ] **Resource budget reviewed** — the prod VPS is a CPX41 (8 vCPU, 16GB).
      Sum the existing `mem_limit` values + your new service; if it exceeds
      ~14G commit, escalate to Bereket before merging.

## 5. DNS & TLS

DNS is **Terraform-managed** (`terraform/cloudflare/`). Manual record
creation in the Cloudflare UI is no longer the path — it will be reconciled
away on the next `terraform apply`.

- [ ] **Add an entry** to `terraform/cloudflare/main.tf` (or the appropriate
      module input) for the new service's hostname. For stg use a 3rd-level
      CNAME pointing at `stg.${var.domain}`; for prod use a 2nd-level CNAME
      pointing at the apex.
- [ ] **`proxied = false` for stg 3rd-level subdomains** — Cloudflare
      Universal SSL covers only one wildcard level (`*.noorinalabs.com`), so
      `<svc>.stg.noorinalabs.com` would fail TLS handshake at the CF edge if
      proxied. See [`terraform/cloudflare/README.md`](../../terraform/cloudflare/README.md)
      and [#228].
- [ ] **TF plan reviewed on the PR** — `terraform.yml` posts the plan as a
      PR comment; eyeball it before merge. *Automated in:*
      [`.github/workflows/terraform.yml`](../../.github/workflows/terraform.yml).
- [ ] **Cloudflare SSL mode = Strict** (already set zone-wide via
      `terraform/cloudflare/main.tf § zone_settings`). Caddy auto-provisions
      Let's Encrypt at origin, so Strict mode is the correct end-to-end
      shape. No per-service action needed.

## 6. Pre-deploy verification

- [ ] **`compose-validate` CI gate green** on the PR that adds the service.
      *Automated in:* [`.github/workflows/compose-validate.yml`](../../.github/workflows/compose-validate.yml).
- [ ] **`hooks-lint` and `lint-workflows` green** if you also touched any
      `.claude/hooks/**` or `.github/workflows/**`.
- [ ] **Cold-rebuild dry-run mentally reviewed** — would `cold-rebuild-dryrun.yml`
      survive your new service? See
      [`docs/runbooks/cold-rebuild-dry-run.md`](cold-rebuild-dry-run.md).

## 7. First deploy

- [ ] **Deploy to stg first** by pushing the service repo to its default
      branch (triggers `ghcr-publish.yml` → `notify-deploy` →
      `deploy-stg.yml` repository_dispatch).
- [ ] **Watch `deploy-stg.yml`** in the Actions tab — the dispatch is
      silent if mis-wired (#162 lesson), so a missing run is the diagnostic.
- [ ] **`verify-deploy.yml` stg job green** — runs the full cross-repo
      integration suite automatically after a successful stg deploy. See
      [`docs/runbooks/deploy-isnad-graph.md § Post-Deployment Verification`](deploy-isnad-graph.md#post-deployment-verification).
- [ ] **Promote to prod** via the `promote.yml` workflow (manual-approval
      gate in the `production` GH Environment).
- [ ] **Smoke test the new endpoint** —
      `curl -fsSI https://<new-host>.noorinalabs.com/` returns 200 with a
      Let's Encrypt cert from Caddy. The broader operator script:
      `SITE_URL=https://<new-host>.noorinalabs.com ./scripts/verify_deployment.sh --skip-workflow`.

## 8. Post-deploy hygiene

- [ ] **Add a rollback target** — extend `rollback.yml`'s `service` input
      enum so operators can roll just your new service. *Manual.*
- [ ] **Add observability** — Prometheus scrape config in
      `infra/prometheus/`, Grafana dashboard in `infra/grafana/dashboards/`,
      Loki labels via Alloy's existing Docker SD scrape (automatic).
      Add an Alertmanager rule for the new service's SLO if applicable.
- [ ] **Document the per-service runbook** at
      `docs/runbooks/deploy-<service>.md` following the
      [`deploy-isnad-graph.md`](deploy-isnad-graph.md) shape, and add it to
      the [Quick links table in `RUNBOOK.md`](../../RUNBOOK.md#quick-links).
- [ ] **Update `ontology/repos/deploy.yaml § docker_services.application`**
      with the new image and resource budget, then `/ontology-rebuild`.

## Origin-of-each-item (Wave B retro vs. drift since)

| Wave-B item (raw) | Current status |
|---|---|
| GHCR image built and pushed | Still required — § 1 |
| VPS_HOST set to **VPS IP** not domain | Still required and Environment-scoped now — § 3 |
| DEPLOY_SSH_PRIVATE_KEY secret set | Still required, plus cloud-init injection gap to verify — § 3 |
| GH_PACKAGES_TOKEN for GitHub Packages installs | Scope narrowed — only for retag/promote workflows now — § 3 |
| VPS can pull from GHCR (manual `docker login`) | **Obsoleted** — runtime auth via `GITHUB_TOKEN` + trap — § 3 |
| Caddyfile updated with new server blocks | Still required, plus longer-prefix-first invariant — § 4 |
| docker-compose.prod.yml updated | Still required — § 4 |
| Deploy script restarts Caddy after Caddyfile change | **Obsoleted** — `docker compose up -d --force-recreate` recreates Caddy automatically — § 4 |
| DNS record created in Cloudflare | **Migrated** to Terraform (`terraform/cloudflare/`) — § 5 |
| Cloudflare SSL mode compatible with Caddy auto-TLS | Zone-wide Strict already set in TF; per-service: stg 3rd-level must be `proxied = false` — § 5 |

[#133]: https://github.com/noorinalabs/noorinalabs-deploy/issues/133
[#134]: https://github.com/noorinalabs/noorinalabs-deploy/issues/134
[#135]: https://github.com/noorinalabs/noorinalabs-deploy/issues/135
[#162]: https://github.com/noorinalabs/noorinalabs-deploy/issues/162
[#228]: https://github.com/noorinalabs/noorinalabs-deploy/issues/228

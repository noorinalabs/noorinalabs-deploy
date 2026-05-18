# Runbook: Cloud-Init Template Changes on Existing Servers

## Background

`hcloud_server.app` in `terraform/hetzner/modules/hetzner-vps/main.tf` carries:

```hcl
lifecycle {
  ignore_changes = [ssh_keys, user_data]
}
```

Hetzner runs cloud-init exactly once at first boot. After that, `user_data` has no effect on the live box regardless of what Terraform sends. The `ignore_changes` entry prevents Terraform from triggering a destructive server replace every time `cloud-init.yaml.tpl` is edited (added in PR #217 to protect the recovered stg + prod VPSes).

**Consequence:** changes to `cloud-init.yaml.tpl` are silently ignored by `terraform plan` / `terraform apply` on existing servers. New provisions receive the updated template; existing boxes do not.

## When this matters vs. when it does not

| Change type | Needs propagation to existing boxes? | Action |
|---|---|---|
| New authorized SSH key added to template | Yes — operators locked out otherwise | Follow taint procedure below |
| New auditd rule, firewall tweak, sysctl hardening | Yes — security baseline gap | Follow taint procedure below |
| New env file or secret injected via cloud-init | Yes, if service depends on it at start | Follow taint procedure below |
| Indentation or comment-only edit | No | Merge; no further action |
| New Docker package pre-pulled at boot | No — Compose handles pulls at deploy | Merge; no further action |
| Default hostname or locale cosmetics | Usually no | Use judgement; document in commit |

If unsure: if a live box missing the change would be meaningfully less secure or functional than a freshly provisioned box, treat it as needing propagation.

## Detecting drift

Terraform will not surface the drift because `ignore_changes` masks the diff. Use this workflow to check manually:

```bash
# 1. Render the current template for an env (stg example):
cd terraform/hetzner/envs/stg
terraform show -json | jq -r '.values.root_module.child_modules[].resources[]
  | select(.address == "module.vps.hcloud_server.app")
  | .values.user_data' > /tmp/live-user-data.txt

# 2. Render what the current template *would* produce:
terraform plan -var-file=terraform.tfvars -out=/tmp/plan.tfplan
terraform show -json /tmp/plan.tfplan | jq -r '
  .resource_changes[]
  | select(.address == "module.vps.hcloud_server.app")
  | .change.after.user_data' > /tmp/planned-user-data.txt

# 3. Compare:
diff /tmp/live-user-data.txt /tmp/planned-user-data.txt
```

An empty diff means no drift. A non-empty diff means the live box would get a different cloud-init payload on fresh provision. Review the diff against the "When this matters" table above.

Note: `terraform show -json` for the live state requires a current state pull (`terraform refresh` or a recent `terraform apply`). If state is stale, run `terraform refresh -var-file=terraform.tfvars` first.

## Propagating a legitimate template change to an existing server

This procedure recreates the server. **The box will be offline for several minutes.** Schedule a maintenance window for prod.

### Pre-replacement checklist

- [ ] Verify the change is in the "Yes — needs propagation" category above
- [ ] Take a Hetzner snapshot via the console (Hetzner console → server → Snapshots → Take snapshot). Wait for completion.
- [ ] For prod: confirm DNS TTL is low enough (≤ 60 s recommended) for fast cutover if the replacement IP differs
- [ ] For prod: drain application traffic (update Cloudflare rule to maintenance page, or mark backend offline)
- [ ] Note the current server IP — if it changes post-replace, update Cloudflare DNS and any hardcoded references
- [ ] Confirm all secrets are present in the env's `terraform.tfvars` (cloud-init will inject them fresh)

### Replacement procedure

```bash
# Option A — terraform apply -replace (TF >= 1.2, preferred):
cd terraform/hetzner/envs/<stg|prod>
terraform apply -replace='module.vps.hcloud_server.app' -var-file=terraform.tfvars

# Option B — taint then apply (older TF or scripted pipelines):
terraform taint module.vps.hcloud_server.app
terraform apply -var-file=terraform.tfvars
```

`-replace` / `taint` marks the resource for destruction and recreation in the next apply. The apply will:
1. Destroy the existing `hcloud_server.app`
2. Create a new server with the current `user_data` (rendered from the updated template)
3. Assign firewall and labels

### Post-replacement checklist

- [ ] SSH to the new server and confirm cloud-init completed: `sudo cloud-init status --wait`
- [ ] Verify the change that triggered the rebuild is present on the new box (e.g., check `authorized_keys`, audit rules, env file)
- [ ] Confirm services are up: `docker compose -f /opt/noorinalabs-deploy/compose/docker-compose.prod.yml ps`
- [ ] Update DNS if the IP changed (Hetzner does not guarantee IP persistence across recreate)
- [ ] Re-enable production traffic / remove maintenance page
- [ ] Delete the snapshot taken before replacement once the box is confirmed healthy (retaining it costs money)

### Stg-first verification

Always perform the procedure on `stg` before `prod`. Stg is a lower-blast-radius target: smaller server, no production traffic, same cloud-init template. If the replacement produces a broken box on stg, debug there before touching prod.

## Relationship to other issues

- **#165** — canonical SSH key injection (solved by PR #217; `user_data` is now the sole path for `authorized_keys`)
- **#173** — broader cloud-init module hardening; the operator workflow here is a prerequisite for safely applying those hardening changes to existing boxes
- **#217** — PR that introduced `ignore_changes = [ssh_keys, user_data]`; the `lifecycle` comment in `main.tf` links back to this runbook

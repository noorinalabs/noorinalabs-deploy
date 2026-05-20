# ADR 0002 — Hetzner module outputs classification gate

- **Status:** Accepted
- **Date:** 2026-05-17
- **Author:** Nino Kavtaradze (Security Engineer)
- **Context issue:** [deploy#171](https://github.com/noorinalabs/noorinalabs-deploy/issues/171)
- **Related ADR:** [0001 — Terraform Hetzner per-env state strategy](./0001-tf-hetzner-per-env-state-strategy.md)
- **Supersedes:** none
- **Superseded by:** none

## Context

PR #223 (closes #169) wired the cloudflare module to consume hetzner per-env state via:

```hcl
# terraform/cloudflare/main.tf
data "terraform_remote_state" "hetzner_prod" {
  backend = "s3"
  config = { ... key = "hetzner/prod.tfstate" ... }
}

data "terraform_remote_state" "hetzner_stg" {
  backend = "s3"
  config = { ... key = "hetzner/stg.tfstate" ... }
}
```

`terraform_remote_state` is a blanket-access pattern: **every output** of the consumed state file is readable by the consuming module's plan/apply context. The `sensitive = true` attribute on outputs DOES NOT serve as an access barrier — sensitive outputs are still readable by consumers; they're only masked in CLI rendering of plans the consumer's own user runs.

This creates two real exposure surfaces:

1. **CI-side leakage.** The `plan-cloudflare` job in `.github/workflows/terraform.yml` comments the plan output on the PR. If a sensitive value flows into a `cloudflare_record.content` field (even indirectly through interpolation), it can leak into the PR comment.
2. **Apply-side leakage.** GH Actions logs are readable by anyone with read on the repo. An accidental log path from a sensitive output is harder to spot than a misconfigured secret.

Today's cloudflare module reads exactly two outputs from each hetzner state: `server_ip` (IPv4) and `server_ipv6`. Both are public information by definition (DNS A/AAAA targets). All other outputs the hetzner module exposes are incidental access surface for cloudflare.

## Decision

Adopt a **per-output classification gate** for the hetzner module. Every output in `terraform/hetzner/envs/{stg,prod}/outputs.tf` and `terraform/hetzner/modules/hetzner-vps/outputs.tf` MUST fall into one of two classes:

### Class A — Publicly safe

Output value is intrinsically public information and may be exposed to any consumer via `terraform_remote_state`. Examples:

- Public IPv4 / IPv6 addresses (DNS targets by definition)
- Hetzner labels (project/environment metadata, no secret content)
- Server status enum (running, off, etc.)
- Server name / hostname (used in DNS)
- Environment identifier (stg/prod)
- SSH target string of the form `deploy@<public-ip>` (composed entirely from public parts)

### Class B — Sensitive or internal-only

Output value contains or is derived from a token, internal-network address, key fingerprint, credential, or any other value that should not flow across a module boundary. Examples that would belong here if they existed:

- Provider API tokens
- Database connection strings with embedded credentials
- Internal/private-network IPs from a managed-network construct
- SSH key fingerprints or material
- Any value that came from `var.foo` where `foo` was declared `sensitive = true`

**Class B outputs MUST NOT be exposed via the module's public `outputs.tf`.** If a Class B value must be consumed by another module, use one of:

- **Workspace-internal locals** (don't expose; recompute in the consumer).
- **Per-output state-lite pattern** — write only the few needed Class A values to a separate state file (e.g., `terraform/_shared/<purpose>.tfstate`) that consumers read; the original state stays not-readable.
- **Out-of-band secret store** — SSM, Vault, etc.; the consumer reads via the store's data source.

Do NOT rely on `sensitive = true` as a barrier — it isn't.

### Reviewer checklist for new outputs

When a PR adds or changes an output in any hetzner-module file, the reviewer MUST:

1. Identify whether the output is Class A or Class B per the criteria above.
2. If Class B: reject; the consumer should use one of the alternatives listed above. Approving requires a documented exception in the ADR (this file) with the author + reason.
3. If Class A: ensure the output's description explains what it's for (existing convention).

## Audit attestation — 2026-05-17 (this PR)

All outputs in the hetzner module surface as of HEAD `d89f454`:

### `terraform/hetzner/modules/hetzner-vps/outputs.tf`

| Output | Value source | Class | Notes |
|---|---|---|---|
| `env` | `var.env` (literal string "stg"/"prod") | A | Identifier; no secret content |
| `server_name` | `hcloud_server.app.name` | A | Public hostname used in DNS |
| `server_ip` | `hcloud_server.app.ipv4_address` | A | Public IPv4 (DNS A target) — consumed by cloudflare |
| `server_ipv6` | `hcloud_server.app.ipv6_address` | A | Public IPv6 (DNS AAAA target) — consumed by cloudflare |
| `server_status` | `hcloud_server.app.status` | A | Public enum (running/off/etc.) |
| `ssh_target` | `"deploy@${ipv4}"` | A | Composed from public IP + public username; no secret |
| `labels` | `local.labels` = `{ project, environment }` | A | Metadata; both fields literal strings |

### `terraform/hetzner/envs/stg/outputs.tf` and `terraform/hetzner/envs/prod/outputs.tf`

Both files re-export the seven module-level outputs verbatim. Same classification — all Class A.

### Conclusion

**All 7 outputs are Class A. No Class B outputs are exposed.** The `terraform_remote_state` data source in `terraform/cloudflare/main.tf` reads `server_ip` and `server_ipv6` only; the other 5 are not currently consumed by cloudflare but are safely available to any future consumer.

No code refactor required by this audit. Future additions are gated by the reviewer checklist above.

## Consequences

### Positive

- Future output additions are explicitly classified at review time, preventing accidental exposure through the `terraform_remote_state` blanket-access pattern.
- The reviewer checklist gives a concrete reject criterion (Class B without an exception is a blocker), so the gate is enforceable rather than aspirational.
- Today's surface is documented as audited and clean, so a future reader doesn't have to redo the analysis.

### Negative / ongoing costs

- Reviewers must check the classification on every PR that touches `outputs.tf`. Mitigated by the fact that hetzner outputs change rarely; the cost is per-change, not per-PR.
- If a real Class B need emerges (e.g., a future feature legitimately requires sharing a sensitive value), the per-output state-lite pattern requires a new state file + backend config, which is more setup than `terraform_remote_state`. Accepted — the cost is proportional to the risk being mitigated.

### Failure modes explicitly considered

| Question | Answer |
|---|---|
| What happens if a reviewer misses a Class B output? | Defense-in-depth: the CI-side leakage requires the output to be referenced in a `cloudflare_record.content` (or similar) before it appears in the PR comment. The output existing in state alone doesn't leak. Adds a second gate even if the first fails. |
| What about outputs added by upstream provider upgrades? | `hcloud_server` and `hcloud_firewall` resource attributes are exposed by name in our outputs.tf; a provider upgrade doesn't auto-add new outputs to our module surface. Provider upgrades remain reviewer territory per the Terraform module review playbook. |
| What if cloudflare needs a NEW hetzner output later? | Add it to the table above with classification + justification. If Class A, append. If Class B, design the per-output state-lite pattern in the same PR. |

## Follow-up issues

None. The audit is complete; the gate is now documented.

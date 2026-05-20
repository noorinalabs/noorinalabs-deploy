# ADR 0003 — SSH-key authorization via cloud-init

- **Status:** Accepted
- **Date:** 2026-05-18
- **Author:** Nurul Hakim (Observability Engineer)
- **Context issue:** [deploy#224](https://github.com/noorinalabs/noorinalabs-deploy/issues/224)
- **Implementing PR:** [deploy#223](https://github.com/noorinalabs/noorinalabs-deploy/pull/223) (closes [deploy#222](https://github.com/noorinalabs/noorinalabs-deploy/issues/222))
- **Supersedes:** none
- **Superseded by:** none
- **Related ADRs:** [0001 — Terraform Hetzner per-env state strategy](0001-tf-hetzner-per-env-state-strategy.md)

## Context

ADR 0001 establishes that `terraform/hetzner/` is laid out as per-env root modules (`envs/stg/`, `envs/prod/`) calling a shared `modules/hetzner-vps/` child module, with the resource-naming convention `noorinalabs-${var.env}[-suffix]`. Both envs share the same canonical SSH deploy pubkey (`noorinalabs_deploy.pub`) — there is exactly one CI/operator deploy identity for the project.

The shared child module originally provisioned authorized_keys via two complementary mechanisms:

1. **`hcloud_ssh_key.deploy`** — a Hetzner-side resource registering the canonical pubkey under the project (named `noorinalabs-${var.env}-deploy`), passed to `hcloud_server.app` via `ssh_keys = [hcloud_ssh_key.deploy.id]`. This is the Hetzner-native authorization path — TF-reconcilable post-provision.
2. **cloud-init `user_data`** — the `users:` block injects the pubkey into `/home/deploy/.ssh/authorized_keys` for the `deploy` user at first-boot.

The setup looked correct on paper but had a structural collision with the Hetzner provider's uniqueness contract: **Hetzner enforces pubkey-content uniqueness within a project's SSH-key registry**. Two `hcloud_ssh_key` resources whose `public_key` content is byte-identical cannot coexist — the second one to create returns `uniqueness_error 409`.

The conflict surfaced in [deploy#217](https://github.com/noorinalabs/noorinalabs-deploy/issues/217)'s apply: stg's `hcloud_ssh_key.deploy` was created (or already present) first, prod's create then failed after the destroy of prod's old key had already succeeded. Prod was left without a Hetzner-side SSH key resource and the apply was in a partially-converged state. The original SSH-lockout bug ([deploy#216](https://github.com/noorinalabs/noorinalabs-deploy/issues/216)) was the user-visible symptom; #217 was the first-pass recovery attempt; the structural fix is what this ADR records.

We could not resolve the conflict by giving stg and prod different pubkeys without spawning a second key identity, a second CI secret to manage, and per-env rotation drift — none of which the project has appetite for. We also could not resolve it by sharing a single Hetzner-level resource across envs without breaking the per-env state isolation guarantee that ADR 0001 deliberately bought.

The decision needed to be recorded outside the inline comment in `main.tf` and outside the PR body, both of which are easy to lose track of when future work touches the cloud-init template, the lifecycle ignore_changes set, or the operator key-rotation runbook.

## Decision

**cloud-init `user_data` is the sole authorized_keys injection path for TF-provisioned Hetzner VPSes.** The `hcloud_ssh_key` resource is removed from `modules/hetzner-vps/main.tf` along with the `ssh_keys` argument on `hcloud_server.app`.

Concretely:

1. **Authorization mechanism.** The cloud-init template at `modules/hetzner-vps/cloud-init.yaml.tpl` writes the canonical pubkey to both:
   - `/home/deploy/.ssh/authorized_keys` — via the `users:` block (`ssh_authorized_keys: [${ssh_public_key}]`)
   - `/root/.ssh/authorized_keys` — via a `write_files` entry (`owner: root:root, permissions: '0600'`)

   Both entries inject the same `var.ssh_public_key_path` content (the canonical deploy pubkey), passed into the template as a `sensitive(chomp(file(...)))` variable.

2. **No Hetzner-side SSH-key resource.** `hcloud_ssh_key` is gone from the module. The Hetzner project SSH-key registry stays empty for these envs.

3. **Lifecycle protection.** `hcloud_server.app` retains `lifecycle { ignore_changes = [ssh_keys, user_data] }`:
   - `ssh_keys` is retained because the live-state for already-provisioned VPSes still carries a stale reference to a now-deleted `hcloud_ssh_key.deploy` id (an artifact of #217's partial apply). Without the ignore, every plan would attempt destructive reconciliation.
   - `user_data` is retained because cloud-init `user_data` is creation-time-only on Hetzner — TF cannot meaningfully reconcile a `user_data` diff against a running VPS. Without the ignore, any template edit would mark the server for destroy-and-recreate.

4. **Operator key-rotation procedure.** Rotation of the canonical deploy pubkey is **out-of-band operator work** (SSH into each VPS, edit `/home/deploy/.ssh/authorized_keys` and `/root/.ssh/authorized_keys`). It is not a `terraform apply` operation. The runbook procedure for forcing the cloud-init template to re-run on a live box is `taint`/`replace` of `hcloud_server.app`, documented at `docs/runbooks/cloud-init-template-changes.md`.

5. **Operator personal-key injection.** Operators who want their personal `~/.ssh/id_ed25519.pub` on `root` for a freshly-provisioned box append it manually post-provision. This matches the pre-existing pattern (see [deploy#165](https://github.com/noorinalabs/noorinalabs-deploy/issues/165) and the `reference_ssh_topology` operator memory).

## Consequences

### Positive

- **Uniqueness conflict structurally eliminated.** No `hcloud_ssh_key` resources to collide. Stg and prod can both be provisioned from the same canonical pubkey with no Hetzner-side state contention.
- **Single source of truth for authorization.** The cloud-init template is the only place that knows about authorized_keys content. PR review and runbook authoring don't have to reason about two parallel mechanisms.
- **No new CI secret surface.** The pubkey CONTENT is non-secret (pubkeys are public by definition); only the path to it is configured per-env via `var.ssh_public_key_path`.
- **Per-env state isolation preserved.** No cross-env Hetzner resource to share — ADR 0001's blast-radius guarantee is unaffected.

### Negative / ongoing costs

- **No TF reconciliation of authorization.** Once a VPS is provisioned, TF cannot detect or correct authorized_keys drift. If an operator manually appends a key and later wants TF to remove it, that's a manual SSH operation. Accepted: rotations are infrequent and operators already have the access required.
- **`user_data` edits don't propagate to live boxes.** Template changes (new pubkey, new auditd rule, new fail2ban tweak) are creation-time-only because of `ignore_changes = [user_data]`. Propagation to existing VPSes requires the `taint`/`replace` runbook OR a backfill PR that applies the same change out-of-band via Ansible-like manual steps. The lifecycle block has a comment pointing readers at the runbook; ADR 0001's apply-shape guarantees are unaffected.
- **Root-account authorization expanded.** `/root/.ssh/authorized_keys` is populated where previously root authorization went through Hetzner's `ssh_keys` server arg (functionally equivalent — both authorize the same canonical pubkey for root login). Future hardening that removes root SSH entirely would touch the `write_files` entry rather than the (now-absent) `hcloud_ssh_key` resource.
- **Operator personal-key injection stays manual.** [deploy#165](https://github.com/noorinalabs/noorinalabs-deploy/issues/165) (now closed) tracked this gap; the chosen path was to deliberately keep operator-specific pubkeys out of TF and have operators append them post-provision. If that policy changes in the future, the cloud-init template's `write_files` entry for `/root/.ssh/authorized_keys` is the place to wire a list-of-pubkeys variable.

### Failure modes explicitly considered

| Question | Answer |
|---|---|
| What happens if the canonical deploy pubkey is rotated? | Out-of-band operator work — SSH to each live VPS, edit both authorized_keys files, then update the CI secret and the file pointed at by `var.ssh_public_key_path`. Future-provisioned boxes pick up the new key at first-boot. There is no `terraform apply` for this. |
| What happens if a `user_data` template edit needs to reach a live VPS? | `taint`/`replace` `hcloud_server.app` per `docs/runbooks/cloud-init-template-changes.md`. Destructive — bounded by ADR 0001's per-env state isolation, but destructive nonetheless. The lifecycle comment in `main.tf` flags this explicitly. |
| What happens if Hetzner ever adds a `user_data` reconciliation feature? | The `ignore_changes = [user_data]` block prevents adoption automatically. Revisit this ADR before opting in. |
| What happens if the Hetzner-side uniqueness constraint changes (project-scoped → environment-scoped)? | This decision becomes a preference rather than a requirement. The cloud-init path is still preferable on the "single source of truth" axis, but per-env `hcloud_ssh_key` resources would become viable again. Revisit ADR before reintroducing. |
| What happens if an operator's personal pubkey is appended to `/root/.ssh/authorized_keys` and the box is later replaced? | The append is lost. New box has only the canonical deploy pubkey. Operator re-appends post-provision. This is the documented expectation per the operator memory and [deploy#165](https://github.com/noorinalabs/noorinalabs-deploy/issues/165). |

## References

- [deploy#216](https://github.com/noorinalabs/noorinalabs-deploy/issues/216) — original SSH-lockout bug (user-visible symptom of the registry conflict).
- [deploy#217](https://github.com/noorinalabs/noorinalabs-deploy/issues/217) — first-pass fix; the failed apply exposed the project-scoped uniqueness constraint.
- [deploy#222](https://github.com/noorinalabs/noorinalabs-deploy/issues/222) / [PR #223](https://github.com/noorinalabs/noorinalabs-deploy/pull/223) — the structural fix this ADR documents.
- [deploy#165](https://github.com/noorinalabs/noorinalabs-deploy/issues/165) — operator-personal-key injection gap (closed; stays manual post-provision).
- [deploy#118](https://github.com/noorinalabs/noorinalabs-deploy/issues/118) — cloud-init VPS baseline (cluster).
- [ADR 0001](0001-tf-hetzner-per-env-state-strategy.md) — per-env state strategy this decision composes with.
- `noorinalabs-main:ontology/repos/deploy.yaml` § `cloud_init_ssh_key_gap` — parent-repo ontology entry for the original operational gap.
- `docs/runbooks/cloud-init-template-changes.md` — the `taint`/`replace` procedure for propagating template edits to live VPSes.

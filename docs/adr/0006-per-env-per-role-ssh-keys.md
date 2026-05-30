# ADR 0006 — Per-env, per-role SSH keys

- **Status:** Accepted
- **Date:** 2026-05-25
- **Author:** Nino Kavtaradze (Security Engineer)
- **Context issue:** [deploy#164](https://github.com/noorinalabs/noorinalabs-deploy/issues/164)
- **Supersedes:** [0003 — SSH-key authorization via cloud-init](0003-ssh-key-authorization-via-cloud-init.md)
- **Superseded by:** none
- **Related ADRs:** [0001 — Terraform Hetzner per-env state strategy](0001-tf-hetzner-per-env-state-strategy.md), [0002 — Hetzner outputs classification](0002-hetzner-outputs-classification.md)

## Context

ADR 0003 established **cloud-init `user_data` as the sole authorized_keys injection path** for TF-provisioned Hetzner VPSes, after the `hcloud_ssh_key` resource was removed in [#222](https://github.com/noorinalabs/noorinalabs-deploy/issues/222). It injected **one canonical pubkey** (`var.ssh_public_key_path`) into **both** authorized_keys files on **both** envs:

- `/home/deploy/.ssh/authorized_keys` (the `deploy` user — used by CI)
- `/root/.ssh/authorized_keys` (the `root` user — operator emergency access)

The result: a single keypair authorized as **2 VPSes × 2 users = 4 authorized_keys entries**. The owner flagged this in [deploy#164](https://github.com/noorinalabs/noorinalabs-deploy/issues/164):

> "I imagine that's bad practice and I need to fix at least one of them."

The problems with one shared key:

- **Blast radius on compromise.** One private-key leak grants `root`-on-both-VPSes. There is no separation between stg and prod credentials, and no separation between the CI deploy identity and the operator root identity. The CI deploy key — which by necessity lives in a GitHub secret and is exercised on every deploy — is the same key that grants interactive `root`.
- **Rotation coupling.** Rotating the key must hit all four `authorized_keys` entries simultaneously. Any partial-rotation window locks the owner (or CI) out of one or more surfaces.
- **Auditability.** SSH logs cannot distinguish which key "should" have been used for a given box or role — there is only one.
- **The stg/prod split is half-defeated.** ADR 0001 deliberately bought per-env state isolation; two envs sharing one root credential undermines the isolation that split was meant to provide.

### Why ADR 0003 rejected per-env keys, and why that no longer applies

ADR 0003 considered giving stg and prod different pubkeys and rejected it:

> "We could not resolve the conflict by giving stg and prod different pubkeys without spawning a second key identity, a second CI secret to manage, and per-env rotation drift…"

That reasoning was anchored on the **Hetzner project-scoped pubkey-uniqueness 409** (`uniqueness_error`): two `hcloud_ssh_key` resources with byte-identical `public_key` content could not coexist, and per-env *different* keys meant extra Hetzner-side key identities to manage. **But ADR 0003 itself removed the `hcloud_ssh_key` resource** (#222) — cloud-init `user_data` is now the only injection path, and the Hetzner SSH-key registry stays empty for these envs. With no `hcloud_ssh_key` resource, **there is no Hetzner-side registry for keys to collide in**. The 409 blocker is moot.

ADR 0003 explicitly anticipated this revisit in its failure-modes table:

> "What happens if the Hetzner-side uniqueness constraint changes (project-scoped → environment-scoped)? … per-env `hcloud_ssh_key` resources would become viable again. Revisit ADR before reintroducing."

The constraint did not change at Hetzner; it became irrelevant when the resource that was subject to it was removed. The owner has weighed the residual cost (more keypairs to mint and document) against the security gain (collapsed blast radius, decoupled rotation, real per-env isolation) and **chosen the split**.

## Decision

**Split the single shared keypair into four: per-env (stg, prod) AND per-role (root, deploy).**

| Keypair | Authorizes | Private half lives where |
|---|---|---|
| `noorinalabs_stg_deploy` | `deploy@stg` | stg-scoped CI `DEPLOY_SSH_PRIVATE_KEY` secret **and** owner workstation |
| `noorinalabs_stg_root` | `root@stg` | **owner workstation only** — never in any GH secret |
| `noorinalabs_prod_deploy` | `deploy@prod` | prod-scoped CI `DEPLOY_SSH_PRIVATE_KEY` secret **and** owner workstation |
| `noorinalabs_prod_root` | `root@prod` | **owner workstation only** — never in any GH secret |

Concretely:

1. **Two cloud-init template variables.** `modules/hetzner-vps/cloud-init.yaml.tpl` no longer references a single `${ssh_public_key}`. It injects:
   - `${deploy_ssh_public_key}` → the `users:` block (`ssh_authorized_keys` for `deploy`)
   - `${root_ssh_public_key}` → the `write_files` entry for `/root/.ssh/authorized_keys`

2. **Two module variables.** `modules/hetzner-vps/variables.tf` replaces `ssh_public_key_path` with `deploy_ssh_public_key_path` and `root_ssh_public_key_path`, both wired into `local.cloud_init_vars` as `sensitive(chomp(file(...)))`.

3. **Per-env, per-role paths in the env roots.** Each of `envs/stg/` and `envs/prod/` passes its own `deploy_ssh_public_key_path` and `root_ssh_public_key_path`. The `terraform.tfvars.example` files point at the per-env per-role workstation paths (`~/.ssh/noorinalabs_{env}_{role}.pub`).

4. **CI uses the DEPLOY key only.** The env-scoped GitHub Environment secret `DEPLOY_SSH_PRIVATE_KEY` (stg → `staging` environment, prod → `production` environment, per the [deploy#155](https://github.com/noorinalabs/noorinalabs-deploy/issues/155) GH-Environments precedent) holds **only** the per-env `deploy` private key. **The root private key is owner-workstation-only and MUST NOT appear in any GH secret.** Root access is interactive operator/emergency access, not CI access.

5. **Pubkeys are non-secret; only the CONTENT goes through cloud-init.** Public keys are public by definition. There is **no GH secret for pubkeys** — their content reaches the box via cloud-init `user_data`, and the path to each pubkey is configured per-env per-role via the two `*_ssh_public_key_path` variables.

6. **Lifecycle protection preserved.** `hcloud_server.app` keeps `lifecycle { ignore_changes = [ssh_keys, user_data] }` for exactly the reasons ADR 0003 recorded: `ssh_keys` because live-state still carries a stale reference to a now-deleted `hcloud_ssh_key.deploy` id (an #217 artifact), and `user_data` because cloud-init is creation-time-only on Hetzner. Routine key rotation does **not** recreate the box — see Decision 7.

7. **Rotation is in-band-to-the-box, not `terraform apply`.** Because `user_data` is creation-time-only, the cloud-init template is the day-1 seed, not the live source of truth for an already-provisioned box. Per-env per-role rotation appends the new pubkey to the live `authorized_keys` directly (or, if the box is being recreated for another reason, `taint`/`replace` re-runs cloud-init per `docs/runbooks/cloud-init-template-changes.md`), updates the per-env GH Environment `DEPLOY_SSH_PRIVATE_KEY` secret (deploy key only), verifies one deploy, then removes the old pubkey. The full procedure is `docs/runbooks/ssh-key-rotation.md`.

### Checked-in pubkeys for CI / cold-rebuild

A canonical **deploy** pubkey remains checked in at `modules/hetzner-vps/deploy.pub` (unchanged from #217) so the cold-rebuild dry-run and CI-driven provisioning have a real pubkey to provision from without an operator-local `~/.ssh/...` path (which recreates the #216 lockout class). A **placeholder** root pubkey is checked in at `modules/hetzner-vps/root.pub` so that the module's local-default `terraform validate` and the cloud-init `templatefile` render — its private half does not exist anywhere; operators override `root_ssh_public_key_path` per-env with their real root pubkey at apply time. The `cold_rebuild_static_checks.py` guard asserts both pubkeys exist, look like valid SSH key lines, and that both env-root `*_ssh_public_key_path` defaults point at the checked-in canonical pubkeys.

## Consequences

### Positive

- **Blast radius collapsed.** A leaked `deploy` key grants `deploy`-on-one-env, not `root`-on-both-envs. The CI-resident key (the highest-exposure key, exercised on every deploy and necessarily stored in a secret) is no longer a root key, and no longer crosses the env boundary.
- **Root is never in CI.** The root private key lives only on the owner workstation. A full compromise of GitHub Actions secrets does not yield interactive root on either box.
- **Per-env isolation is real.** Stg credentials cannot reach prod and vice versa, restoring the isolation guarantee ADR 0001 bought at the state layer to the access layer as well.
- **Rotation decoupled.** Each of the four keys rotates independently. A botched stg-deploy rotation cannot lock the owner out of prod-root.
- **Auditability.** SSH logs now map a key fingerprint to a specific (env, role) pair.
- **No new Hetzner-side resource.** The split lives entirely in cloud-init template variables; no `hcloud_ssh_key` resources, so the #222 uniqueness 409 cannot recur.

### Negative / ongoing costs

- **Four keypairs to mint, store, and document instead of one.** The owner mints and holds four private keys on the workstation. This is the deliberate cost the owner accepted; the rotation runbook scopes the operational steps.
- **No TF reconciliation of authorization (unchanged from 0003).** Once a VPS is provisioned, TF cannot detect or correct authorized_keys drift. Rotation is in-band-to-the-box operator work.
- **`user_data` edits still don't propagate to live boxes (unchanged from 0003).** The cloud-init template is the day-1 seed only. Propagating a template change to an existing box requires the `taint`/`replace` runbook; routine key rotation deliberately uses direct `authorized_keys` edits instead to avoid recreating the box.
- **Two checked-in pubkeys carry a "is this the real key?" footgun.** `deploy.pub` is the real CI deploy pubkey; `root.pub` is a placeholder. The runbook and the variable descriptions are explicit that the per-env per-role real keys are operator-supplied via tfvars and that `root.pub` is not a usable key.
- **Live rotation is owner-operational, not delivered by this PR.** Minting the four keypairs, setting the GH Environment secret values, and updating the live `authorized_keys` on the running VPSes require the owner workstation and the live boxes. This ADR + the IaC + the runbook deliver the mechanism; the live cutover is a separate owner-led session (paired with [deploy#193](https://github.com/noorinalabs/noorinalabs-deploy/issues/193) secret rotation).

### Failure modes explicitly considered

| Question | Answer |
|---|---|
| What happens if the deploy private key (in a GH secret) leaks? | Attacker gains `deploy@<that-env>` only — not root, not the other env. Rotate that one key per `docs/runbooks/ssh-key-rotation.md`: mint a new deploy keypair, append the new pubkey to the live `authorized_keys`, update the env's `DEPLOY_SSH_PRIVATE_KEY` secret, verify a deploy, remove the old pubkey. The blast radius is one (env, role) pair. |
| What happens if the root private key leaks? | It is owner-workstation-only and in no GH secret, so the leak surface is the workstation itself (not CI). Rotate that env's root key the same way (no GH-secret step — root is not in CI). |
| What happens to the Hetzner uniqueness 409 that blocked per-env keys in ADR 0003? | Moot. ADR 0003 removed `hcloud_ssh_key` (#222); with no Hetzner-side key registry, there is nothing for per-env keys to collide in. Per-env keys are now feasible. |
| What happens if an operator points a `*_ssh_public_key_path` default at `~/.ssh/id_ed25519.pub`? | `cold_rebuild_static_checks.py` fails the build (the #216 lockout-class guard, extended to both per-role variables). Defaults must point at the checked-in `../../modules/hetzner-vps/{deploy,root}.pub`. |
| What happens if a box is recreated (taint/replace) and the per-env tfvars aren't supplied? | The module-local / env-root defaults seed the box with the checked-in `deploy.pub` (real) and `root.pub` (placeholder). The placeholder root key is unusable, so root would not be accessible until the operator re-provisions with the real per-env root pubkey path — fail-closed for root, which is the safe direction. Operators recreating a box MUST supply the per-env tfvars (the rotation runbook and `cloud-init-template-changes.md` both call this out). |
| What happens if Hetzner ever adds `user_data` reconciliation? | `ignore_changes = [user_data]` prevents auto-adoption (unchanged from 0003). Revisit before opting in. |

## References

- [deploy#164](https://github.com/noorinalabs/noorinalabs-deploy/issues/164) — this ADR's context issue (single shared keypair tech-debt).
- [deploy#193](https://github.com/noorinalabs/noorinalabs-deploy/issues/193) — secret rotation; the live key-mint + GH-secret-set + live-`authorized_keys` cutover for this split happens in that owner-led session.
- [deploy#155](https://github.com/noorinalabs/noorinalabs-deploy/issues/155) — GH Environments precedent for env-scoped CI secrets (`staging` / `production`).
- [deploy#222](https://github.com/noorinalabs/noorinalabs-deploy/issues/222) / [PR #223](https://github.com/noorinalabs/noorinalabs-deploy/pull/223) — removed `hcloud_ssh_key`, making the cloud-init path sole and the uniqueness 409 moot.
- [deploy#216](https://github.com/noorinalabs/noorinalabs-deploy/issues/216) / [PR #217](https://github.com/noorinalabs/noorinalabs-deploy/pull/217) — original SSH-lockout + the checked-in canonical pubkey + the cold-rebuild static guard this ADR extends.
- [ADR 0003](0003-ssh-key-authorization-via-cloud-init.md) — superseded by this ADR; the cloud-init-sole-path + lifecycle mechanism is retained, the single-shared-key decision is replaced.
- [ADR 0001](0001-tf-hetzner-per-env-state-strategy.md) — per-env state isolation this split extends to the access layer.
- `docs/runbooks/ssh-key-rotation.md` — per-env per-role rotation procedure.
- `docs/runbooks/cloud-init-template-changes.md` — `taint`/`replace` procedure for propagating template edits to live VPSes.
- `noorinalabs-main:ontology/repos/deploy.yaml` § `cloud_init_ssh_key_gap` — parent-repo ontology entry for the original operational gap.

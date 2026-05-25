# Runbook: Per-env, per-role SSH key rotation

This runbook covers rotating the SSH keypairs that authorize access to the
Hetzner VPSes. Per [ADR 0006](../adr/0006-per-env-per-role-ssh-keys.md) there
are **four** independent keypairs — one per (env, role) pair:

| Keypair | Authorizes | Private half lives where |
|---|---|---|
| `noorinalabs_stg_deploy` | `deploy@stg` | stg CI `DEPLOY_SSH_PRIVATE_KEY` secret **and** owner workstation |
| `noorinalabs_stg_root` | `root@stg` | **owner workstation only** — never in any GH secret |
| `noorinalabs_prod_deploy` | `deploy@prod` | prod CI `DEPLOY_SSH_PRIVATE_KEY` secret **and** owner workstation |
| `noorinalabs_prod_root` | `root@prod` | **owner workstation only** — never in any GH secret |

Each key rotates **independently**. Rotating one does not require touching the
other three. That decoupling is the whole point of the split (ADR 0006); do not
batch all four into one rotation unless you actually intend to rotate all four.

## TL;DR

1. Mint a new keypair on the workstation.
2. **Add** the new pubkey to the live box's `authorized_keys` (append — do not
   replace yet).
3. For a **deploy** key only: update the per-env GH Environment
   `DEPLOY_SSH_PRIVATE_KEY` secret with the new private key. (Root keys are
   owner-workstation-only — there is **no** GH-secret step for root.)
4. Verify access with the new key (one deploy for deploy keys; one interactive
   SSH for root keys).
5. **Remove** the old pubkey from `authorized_keys`.

The add-verify-remove ordering means there is never a window where you are
locked out: the old key keeps working until the new one is proven.

## Why this is not `terraform apply`

`hcloud_server.app` carries `lifecycle { ignore_changes = [ssh_keys, user_data] }`
(see [ADR 0006](../adr/0006-per-env-per-role-ssh-keys.md) and
[cloud-init-template-changes.md](cloud-init-template-changes.md)). Cloud-init
`user_data` is **creation-time-only** on Hetzner: the cloud-init template is the
day-1 seed for a fresh box, not the live source of truth for an already-running
box. Editing the checked-in pubkey and running `terraform apply` does **not**
change `authorized_keys` on a live VPS.

Therefore routine rotation edits the live `authorized_keys` directly (steps
below). The only time rotation goes through Terraform is when the box is being
**recreated anyway** (`taint`/`replace`), in which case the freshly-minted
per-env pubkey is supplied via that env's `terraform.tfvars`
(`deploy_ssh_public_key_path` / `root_ssh_public_key_path`) so the new box boots
with the new key. That path is documented in
[cloud-init-template-changes.md](cloud-init-template-changes.md).

## Pre-requisites

- Workstation SSH access to the target box with a **currently-valid** key for
  the role you are rotating (or any role that can edit the target
  `authorized_keys` — `root`, or `deploy` via `sudo`).
- For a deploy-key rotation: `gh` authenticated with permission to set the
  per-env GH Environment secret (`gh secret set ... --env staging|production`).
- The target box's IP. Available from Terraform outputs:
  `cd terraform/hetzner/envs/<env> && terraform output -raw server_ip`.

## Rotation procedure

Substitute `<env>` ∈ {`stg`, `prod`} and `<role>` ∈ {`deploy`, `root`}.
The GH Environment name is `staging` for stg and `production` for prod.

### 1. Mint a new keypair on the workstation

```bash
ssh-keygen -t ed25519 \
  -C "<role>@<env> noorinalabs $(date +%Y-%m)" \
  -f ~/.ssh/noorinalabs_<env>_<role>.new
# Produces ~/.ssh/noorinalabs_<env>_<role>.new (private) and .new.pub (public).
```

Do **not** overwrite the existing key file yet — the old private key is still
needed to authenticate until the new key is proven (step 4).

### 2. Add the new pubkey to the live box (append, do not replace)

The target file depends on the role:

- `deploy` → `/home/deploy/.ssh/authorized_keys`
- `root` → `/root/.ssh/authorized_keys`

```bash
IP=$(cd terraform/hetzner/envs/<env> && terraform output -raw server_ip)

# Append the new pubkey. Authenticate with a currently-valid key.
# For the deploy file (deploy user can write its own file):
ssh -i ~/.ssh/noorinalabs_<env>_<role> deploy@"$IP" \
  "cat >> ~/.ssh/authorized_keys" < ~/.ssh/noorinalabs_<env>_<role>.new.pub

# For the root file, write as root (via sudo or as the root user):
ssh -i ~/.ssh/noorinalabs_<env>_root root@"$IP" \
  "cat >> /root/.ssh/authorized_keys" < ~/.ssh/noorinalabs_<env>_root.new.pub
```

Confirm the new line is present and the file mode is still `0600`:

```bash
ssh ...@"$IP" "tail -n2 <authorized_keys path>; ls -l <authorized_keys path>"
```

### 3. (DEPLOY keys only) Update the per-env GH Environment secret

**Skip this step entirely for root keys** — the root private key is
owner-workstation-only and MUST NOT be placed in any GH secret (ADR 0006).

```bash
gh secret set DEPLOY_SSH_PRIVATE_KEY \
  --env <staging|production> \
  --repo noorinalabs/noorinalabs-deploy \
  < ~/.ssh/noorinalabs_<env>_deploy.new
```

Read-back-verify the secret was set (the value is write-only, but the metadata
confirms the set landed):

```bash
gh secret list --env <staging|production> --repo noorinalabs/noorinalabs-deploy \
  | grep DEPLOY_SSH_PRIVATE_KEY
```

### 4. Verify the new key works

- **Deploy key:** trigger one deploy against `<env>` and confirm the SSH step
  authenticates (e.g. `workflow_dispatch` of the env's deploy workflow, or a
  no-op deploy). The deploy SSH step uses the secret you just set.
- **Root key:** confirm interactive access with the new key:

  ```bash
  ssh -i ~/.ssh/noorinalabs_<env>_root.new root@"$IP" 'echo ok; hostname'
  ```

Do not proceed to step 5 until the new key is proven. If verification fails,
the old key still works — debug before removing anything.

### 5. Remove the old pubkey from `authorized_keys`

Edit the live file and delete the **old** pubkey line, leaving only the new one:

```bash
ssh ...@"$IP" 'sed -i "/<unique-substring-of-old-pubkey>/d" <authorized_keys path>'
# Or edit interactively: ssh ...; ${EDITOR:-vi} <authorized_keys path>
```

Verify only the new key remains and re-confirm access in a **fresh** session
(don't reuse the session you used to edit — a stale session can mask a lockout):

```bash
ssh ...@"$IP" "cat <authorized_keys path>"
# new session, new key:
ssh -i ~/.ssh/noorinalabs_<env>_<role>.new <user>@"$IP" 'echo still-in'
```

### 6. Promote the new key file to the canonical name

Once the old key is removed and the new key is confirmed:

```bash
mv ~/.ssh/noorinalabs_<env>_<role>.new     ~/.ssh/noorinalabs_<env>_<role>
mv ~/.ssh/noorinalabs_<env>_<role>.new.pub ~/.ssh/noorinalabs_<env>_<role>.pub
```

Securely delete the old private key material if it was kept anywhere else.

## Recreated-box path (taint/replace)

If the box is being recreated for another reason (template baseline shift, DR
rebuild), the freshly-minted pubkey reaches the new box via cloud-init at first
boot — supply the per-env per-role pubkey path in that env's
`terraform.tfvars`:

```hcl
deploy_ssh_public_key_path = "~/.ssh/noorinalabs_<env>_deploy.pub"
root_ssh_public_key_path   = "~/.ssh/noorinalabs_<env>_root.pub"
```

Then follow the replacement procedure in
[cloud-init-template-changes.md](cloud-init-template-changes.md). After the box
is up, for a deploy-key change still update the GH Environment secret (step 3)
and verify (step 4). The checked-in module defaults (`deploy.pub` real,
`root.pub` placeholder) are **only** for CI/cold-rebuild provisioning — a box
recreated without per-env tfvars would get the placeholder root key (unusable,
fail-closed for root) and the canonical CI deploy key. Always supply the per-env
tfvars when recreating a box you intend to access with your real keys.

## Compromise response

If a private key is suspected compromised, rotate **that** key immediately
(steps 1–6). The blast radius is bounded to one (env, role) pair by design
(ADR 0006): a leaked deploy key grants `deploy@<one-env>` only — not root, not
the other env. For a deploy-key compromise, rotating the GH Environment secret
(step 3) also invalidates the leaked CI credential. There is no need to rotate
the other three keys unless they are independently suspect.

## Relationship to other issues / docs

- [ADR 0006](../adr/0006-per-env-per-role-ssh-keys.md) — the per-env per-role
  split this runbook operationalizes (supersedes ADR 0003).
- [ADR 0003](../adr/0003-ssh-key-authorization-via-cloud-init.md) — superseded;
  the cloud-init-sole-path + lifecycle mechanism this runbook relies on.
- [cloud-init-template-changes.md](cloud-init-template-changes.md) — the
  `taint`/`replace` procedure for the recreated-box path.
- [deploy#164](https://github.com/noorinalabs/noorinalabs-deploy/issues/164) —
  the tech-debt issue that motivated the split.
- [deploy#193](https://github.com/noorinalabs/noorinalabs-deploy/issues/193) —
  the owner-led secret-rotation session that performs the initial four-keypair
  mint + GH-secret-set + live-`authorized_keys` cutover.

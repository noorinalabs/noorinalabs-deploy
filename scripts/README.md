# `scripts/` — provisioning & operations helpers

Host-side scripts run during VPS bootstrap and day-2 operations. The notable
behavioral caveats that aren't obvious from reading the code live here.

## SSH `authorized_keys` merge — options-prefix caveat

`bootstrap-vps.sh` Step 1 ("Merging /root/.ssh/authorized_keys → deploy")
copies each line of `/root/.ssh/authorized_keys` into
`/home/deploy/.ssh/authorized_keys`, but only if that key isn't already
present. The de-duplication is **fingerprint-based**: each line is run through
`ssh-keygen -lf -` and the resulting SHA256 fingerprint is compared against the
fingerprints already in deploy's file. A root line is appended only when its
fingerprint is new. This is the idempotent append-with-dedup introduced in
#112 / PR #287 (it replaced a destructive `cp` that wiped the deploy CI key on
every re-run).

**The caveat:** an `ssh-keygen` fingerprint is computed from the public-key
blob *only*. The options prefix (`from=`, `command=`, `no-pty`,
`permitopen=`, …) and the trailing comment are **not** part of the
fingerprint. Two lines that carry the same key blob fingerprint identically
even if one is restricted and the other is not.

**Implication:** tightening a restriction on a *root* key does **not**
propagate to the deploy user. For example, if an operator changes

```
ssh-ed25519 AAAA... admin
```

to

```
from="10.0.0.0/8" ssh-ed25519 AAAA... admin
```

in `/root/.ssh/authorized_keys` and re-runs bootstrap, the key already exists
in deploy's file by fingerprint, so the merge **skips** it. Deploy's
`authorized_keys` keeps the unrestricted version — the `from=` restriction is
never carried over.

**Why this is safe-by-default:** the merge never silently *tightens* (or
loosens) deploy's access based on edits to root's file. It only ever adds keys
that deploy didn't already have. This preserves operator-explicit state on the
deploy user and matches the pre-#112 intent without the destructive overwrite.
It is not a regression and not a security issue — but it is surprising if you
don't know the rule.

**If you need deploy locked down too:** edit
`/home/deploy/.ssh/authorized_keys` directly to add the `from=` (or other)
restriction. Don't expect a root-side edit plus a bootstrap re-run to do it for
you. The two files are maintained independently by design (ADR 0006 splits the
root and deploy roles onto distinct per-env keys; the merge exists only for the
residual case of personal admin keys added to root post-provision).

See also: `bootstrap-vps.sh` Step 1 inline comments, #112 (parent foot-gun),
PR #287 (the dedup fix).

## Canonical root key is excluded from the merge (ADR 0006, #352)

ADR 0006 splits SSH access into per-env **and** per-role keys: the root key
authorizes `root` only, the deploy key authorizes `deploy` only. The Step 1
merge above must therefore **never** copy the canonical per-env root key into
the deploy user — doing so would re-authorize the root key for `deploy` and
erode the role separation the split exists to create.

To make the merge honour that, supply the canonical root pubkey so the script
can fingerprint-match and skip it. Set **one** of these before running
`bootstrap-vps.sh`:

| Variable | Value |
|---|---|
| `CANONICAL_ROOT_PUBKEY` | the root pubkey line itself (e.g. `ssh-ed25519 AAAA… root@prod`) — the same `${root_ssh_public_key}` cloud-init seeded for this env |
| `CANONICAL_ROOT_PUBKEY_FILE` | path to a file whose first non-comment line is that pubkey |

```bash
CANONICAL_ROOT_PUBKEY="$(cat ~/.ssh/noorinalabs_prod_root.pub)" \
  ./bootstrap-vps.sh
```

With it set, Step 1's summary line reports `… N canonical-root excluded
(ADR 0006)`, and only **operator-personal** admin keys added to root
post-provision flow into deploy. **If neither variable is set**, the script
preserves the legacy merge-everything behaviour but prints a loud `WARNING
(ADR 0006)` — on a role-separated box, always pass the canonical root key.

This is the residual/legacy bootstrap path only — fresh Terraform-provisioned
boxes are fully cloud-init'd and never need this merge. Nothing automated calls
`bootstrap-vps.sh` (whole-tree grep at HEAD); this exclusion hardens the manual
path so it cannot quietly undo the ADR 0006 split.

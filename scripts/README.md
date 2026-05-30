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

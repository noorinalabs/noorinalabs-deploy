---
name: reference_b2_preflight_discriminator
description: rclone's B2 error text cannot tell a missing bucket from a wrongly-scoped key (identical message), and renders a read-only key's 401 as "failed to create bucket". Classify by capability probe (lsd → canary write → canary delete), never by message.
metadata:
  type: reference
---

Measured live against the B2 account on 2026-07-09 with the bucket-scoped `PIPELINE_B2_*`
write key, while building `scripts/b2_preflight.sh` (deploy#559).

**rclone's failure text does not identify the failure.** These two produce a
**byte-identical** message, because B2 refuses at key-scope before it ever evaluates the
bucket:

- bucket does not exist → `rclone copyto ... isnad:noorinalabs-bucket-that-does-not-exist-9f3a/probe.txt`
- bucket exists, key not scoped to it → `rclone copyto ... isnad:noorinalabs-terraform-state/probe.txt`

Both, rc=1:

```
CRITICAL: Failed to create file system for destination "isnad:<bucket>/":
you must use bucket(s) [{"3c86848e1e18a32490df0c15" "noorinalabs-pipeline"}] with this application key
```

Separately (see [[reference_pipeline_b2_publish_key]] in the org corpus): a **read-only
key's write 401 surfaces as "failed to create bucket"** — a message naming a problem that
does not exist.

**So never branch on the message.** Classify by capability probe:

| probe | proves |
|---|---|
| `rclone lsd <remote>:` | key validity + `listBuckets`. **A bucket-scoped key lists only its own bucket**, so target-in-listing definitively answers "is this key scoped to this bucket". |
| canary `copyto` | `writeFiles` |
| canary `deletefile` | `deleteFiles` — required by `backup.sh`'s `rclone purge` retention prune |

Verdicts: bucket visible + write fails ⇒ **read-only key, not a missing bucket** (the
bucket just listed itself). Bucket not visible ⇒ **genuinely ambiguous** (absent OR
wrongly-scoped) — report both, do not guess. Write ok + delete fails ⇒ backups accumulate
forever and the failure shows up as a bill, not an error.

`rclone lsd` on a bucket-scoped key returns rc=0 and lists exactly one bucket. A
`rclone lsf` on a full object path returns rc=0 and 0 bytes for a nonexistent object — a
**vacuous** probe. Use a real write.

**Never set `RCLONE_DUMP`** (esp. `=auth`): an ambient value once leaked base64
credentials past GitHub's log masking in a public repo. `b2_preflight.sh` hard-refuses to
run when it is set, and reports credentials by length only.

Related: [[project_backup_restore_logrotate_gap]], [[feedback_silent_zero_is_not_a_measurement]].

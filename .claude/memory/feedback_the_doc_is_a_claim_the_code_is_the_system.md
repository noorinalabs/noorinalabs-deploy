---
name: feedback_the_doc_is_a_claim_the_code_is_the_system
description: "A document is a CLAIM about the system; the code and the live account ARE the system. When they disagree, the doc is the defect — and if the doc is your only evidence FOR the doc, you have measured nothing." Our DR runbook pointed at an EMPTY bucket.
metadata:
  type: feedback
---

`RUNBOOK.md` told an operator that the B2 backup bucket was **`isnad-graph-backups`**, and asserted
that `/var/lib/noorinalabs-backups` was merely a local path and *"**not** a B2 bucket name"*.

Read against the live account:

```
B2_BUCKET on noorinalabs-stg  = noorinalabs-backups
B2_BUCKET on noorinalabs-prod = noorinalabs-backups

rclone lsf -R --files-only isnad:isnad-graph-backups/  ->   0 objects   <- the runbook's bucket
rclone lsf -R --files-only isnad:noorinalabs-backups/  ->  27 objects   <- ALL the real backups
```

**Both names are real buckets.** The one the runbook names is **empty**. The one it explicitly denies
is a bucket **is** the bucket.

## Why this is not a doc nit

The failure lands **precisely when you are already in trouble**, and it fails as a **silent zero**,
not an error. An operator doing a DR restore follows the runbook, runs
`rclone lsf isnad:isnad-graph-backups/`, gets **`rc=0` and nothing**, and concludes
**the backups do not exist.**

Bereket Tadesse ran the watchdog both ways on the stg host, at the same moment:

```
B2_ROOT=isnad:isnad-graph-backups/prod  -> status=absent  bucket_objects=0
                                           [ERROR] ALERT — no fresh restorable backup object
B2_ROOT=isnad:${B2_BUCKET}/prod         -> status=fresh   dumps=3  bucket_objects=20  age=5h
                                           [INFO] FRESH — a restorable object exists
```

A **false RED** — from the one check that reads B2 rather than a host-local gauge — in the week of a
production graph cutover. *"We have no backups"* is exactly the sentence that changes what a human
does next. And the runbook's **key-rotation procedure** said to scope a new B2 key to that same empty
bucket, which would have produced a key with **no access to where the backups actually live** → a
preflight 401 → **total backup outage on both hosts, reported as a credential fault** (deploy#650).

## The methodological error, which is the real lesson

Aisha Idrissi was told the bucket name was wrong. She did **the right thing** — she refused to change
it on assertion and went to check. **She checked `RUNBOOK.md`.**

> **She verified the document against itself.**

And the tree *did* corroborate the correction — `test_b2_preflight.py:238` and
`test_compose_project_scoping.py:828` already used `noorinalabs-backups`. The codebase's own fixtures
had it right. Only the runbook (and, by inheritance, the fixture she then wrote) named the empty one.

Her formulation, which is the file:

> **A document is a CLAIM about the system; the code and the live account ARE the system. When they
> disagree, the doc is the defect — and if the doc is your only evidence FOR the doc, you have
> measured nothing.**

## How to apply

1. **Rank your evidence.** Live system > code/fixtures > tests > docs. A doc is the *weakest* artifact
   in the repo and the *only* one nothing executes. Never let it be the sole witness for a claim about
   production — especially a claim you are about to act on during an incident.
2. **Never hardcode a name a variable already holds.** The fix is not *"name the right bucket"* — it is
   **never name a bucket**. Every runnable example uses `${B2_BUCKET}`, which is immune to this class
   *and* to a future rename. The precedent was already in the tree
   (`docs/runbooks/backup-alerts.md`, `verify-backup-artifact.yml`); the runbook had simply not followed it.
3. **Ban the class, not the instance.** The guard forbids **any** hardcoded bucket, not just the empty
   one — *"a blocklist of one would let the next wrong bucket sail straight past."* Calibrated by
   hardcoding the **CORRECT** bucket and watching it **still fail**. That is what makes it a ban and
   not a blocklist. (`feedback_an_anchor_is_a_denylist_one_level_up`.)
4. **Guard what an operator would COPY.** The detector reads only fenced `bash` blocks and `#  B2_ROOT="…`
   usage lines. The blockquote WARNING that *mentions* the legacy bucket **in order to warn about it** is
   deliberately invisible to it — because a guard that flagged the warning would force the next person to
   **delete the warning** to get green, which is the one thing that must survive.
5. **A detector that cannot spell the name of the thing it is hunting will fail the fix and pass the bug.**
   Her first regex used `[A-Z_]+` for the variable name — which **cannot match the DIGIT** in `B2_BUCKET`.
   It stopped at `${B`, read `2_BUCKET}/prod` as the path, and reported every **CORRECT** line as
   defective. Check your pattern against the thing it is supposed to *accept*, not only against the thing
   it is supposed to reject.

See [[feedback_your_own_verification_script_must_not_testify]],
[[feedback_an_anchor_is_a_denylist_one_level_up]],
[[feedback_measurement_is_the_thing_that_breaks]],
[[feedback_rc_is_not_an_authorization_signal]].
Org corpus: `feedback_silent_zero_is_not_a_measurement`, `feedback_review_against_artifact`.
deploy#636/#644/#650.

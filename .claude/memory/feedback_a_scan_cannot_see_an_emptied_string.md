---
name: feedback_a_scan_cannot_see_an_emptied_string
description: A bulk find/replace rewrote its own new definition; the source-text guard went GREEN because the file no longer contained ANY bucket path — a scan cannot see that the string it scans has been emptied of meaning. Only a different instrument (shellcheck) caught it.
metadata:
  type: feedback
---

Namespacing the B2 paths (deploy#632) meant rewriting every remote path in `restore.sh` from
`"${RCLONE_REMOTE}:${B2_BUCKET}/…` to `"${REMOTE_ROOT}/…`, where `REMOTE_ROOT` is defined once as:

```bash
REMOTE_ROOT="${RCLONE_REMOTE}:${B2_BUCKET}/${BACKUP_PREFIX}"
```

I did it with a bulk string replace. **The definition line matches the pattern it was rewriting.**
So the replace ate its own definition:

```bash
REMOTE_ROOT="${REMOTE_ROOT}/${BACKUP_PREFIX}"     # -> "/stg"
```

`REMOTE_ROOT` was empty at that point, so it resolved to `/stg`, and **every "remote" path in the
restore script then addressed the LOCAL FILESYSTEM.** A DR restore would have quietly looked for
backups in `/stg/daily/…` on the box.

**And the new test passed.** `test_every_remote_path_names_its_environment` scans for
`${B2_BUCKET}/` *not followed by a prefix* — and found **zero offenders**, because the file
contained no unprefixed bucket path. It contained no bucket path **at all**. The guard reported
the file clean, and it was correct on its own terms: the string it was hunting for was gone. What
it could not see is that the string had been emptied of *meaning*.

The only thing that noticed was **shellcheck**:

```
SC2034 (warning): RCLONE_REMOTE appears unused.
```

A completely different instrument, asking a completely different question — *is every variable you
assign actually read?* — and the answer fell out as a side effect.

**Why:** a source-text scan is a **proxy** for a property, never the property. The property here is
runtime: *"every rclone call addresses this environment's prefix in B2."* The scan approximates it
by looking for a spelling. Delete the spelling and the scan is satisfied — including when you
deleted it by *destroying the thing the spelling referred to*. A guard that only looks for the
BAD pattern is silent by construction on a file where the pattern, and the correct behaviour, are
both absent.

This is the same night's lesson arriving from a new direction: I had already been caught by an
*inert* test (an assertion no fixture could violate) and a *blind* regex (matching a substring
rather than an option). This one is neither inert nor blind — it is **correctly answering the
wrong question**.

**How to apply:**

1. **Pin the definition, not just the usages.** A test now asserts `REMOTE_ROOT` is literally built
   from `${RCLONE_REMOTE}` + `${B2_BUCKET}` + `${BACKUP_PREFIX}` and **is not defined in terms of
   itself**. Calibrated by reintroducing the exact broken form and watching it go red.
2. **A bulk find/replace must never match the definition of the thing it introduces.** Before
   running one, ask: *does my replacement's own definition match my search pattern?* If it does,
   exclude it explicitly or write the definition afterwards.
3. **Keep a second, differently-shaped instrument in the loop.** shellcheck, mypy, a type checker,
   a linter that asks "is this assigned-and-never-read?" — they are cheap and they fail on axes
   your bespoke guard does not have. **A green bespoke guard plus a green orthogonal linter is
   evidence; a green bespoke guard alone is not.** Never dismiss an SC2034 as noise: "appears
   unused" is a *semantic* claim about your program, and here it was the only true statement
   anybody made about the file.
4. **Say what the guard checks, in the guard.** The test module's docstring now says: green means
   *"no remote path is built without the prefix"* — it does **not** mean *"backups are isolated"*,
   which is a runtime property. The gap between those two sentences is where all of #613, #617,
   #623 and #632 lived.

See [[feedback_calibrate_the_mutation_before_counting_it]], [[feedback_measurement_is_the_thing_that_breaks]],
[[feedback_prod_hardening_unreachable_in_ci]], [[feedback_paraphrase_in_the_product]].
Org corpus: `feedback_lint_gate_cover_all_syntactic_forms`. deploy#632/#633.

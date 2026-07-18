---
name: feedback_guard_the_actor_not_the_stage
description: The restore-verify workflow was ONE deleted line from overwriting the prod graph on a schedule — because the destructive write was delegated to a script the guards did not cover, and the isolation guard inspected the stack the load would never touch. It would have passed, cleanly, while the graph was destroyed.
metadata:
  type: feedback
---

`restore_verify.sh` (deploy#640, PR #642) exists to prove production is restorable. It brings up a
throwaway stack, restores the real B2 artifact into it, and compares the content to live. It carried
a `guard_not_live`, an `assert_volume_isolation`, a static scan asserting **every** `docker compose`
call names its project, and a five-mutation test harness.

**It was one deleted line from loading a backup over the production graph, on a schedule,
unattended.** Caught in review by Weronika Zielinska, by execution, before it ever ran on a host.

## The mechanism

The destructive write — `neo4j-admin database load --overwrite-destination` — is **not** performed by
any of the script's own compose calls. It is performed by **`restore.sh`**, which resolves its *own*
project name (`scripts/compose_project.sh`):

```sh
COMPOSE_PROJECT="${COMPOSE_PROJECT:-${COMPOSE_PROJECT_NAME:-noorinalabs}}"
```

**The fallback is the LIVE project.** The only thing standing between the rehearsal and
`noorinalabs_neo4j_data` was a single variable assignment (`COMPOSE_PROJECT="$RV_PROJECT"`) in the
caller's var-prefix. Delete that line:

```
33 passed in 16.01s
```

Nothing noticed. The three mutations the author *did* model all died loudly (−1 test, −15 tests,
−1 test). The one line that mattered was outside the harness.

## Three lessons, each general

### 1. Guard the ACTOR, not the STAGE.

`assert_volume_isolation` resolved the volume of the container running in `$RV_PROJECT` and asserted
it was the throwaway one. **That assertion was TRUE. And irrelevant.** The load was then performed by
a *different process* addressing a *different project*.

> **The guard inspected the stack the load would not touch. It would have passed, cleanly, while the
> graph was overwritten.**

A guard is only a guard if it constrains **the thing that performs the destructive act**. When you
delegate the dangerous operation to another process, your guards on *your* process are decoration.
Ask, every time: *which process issues the irreversible call, and what constrains THAT one?*

### 2. A mutation harness cannot rule out a catastrophe it cannot REPRESENT.

The fake `restore.sh` **ignored `COMPOSE_PROJECT` entirely**, and the fake `docker` decided what the
scratch stack contained purely from a marker file. So *"restore.sh loaded into the wrong project"*
was **a state the world model could not express.** That is *why* the mutation survived — not because
the assertions were weak, but because **the simulation had no variable in which the disaster could
occur.**

Before trusting a mutation suite, ask: *can my fakes even represent the worst thing that could
happen?* If the catastrophe is inexpressible in the harness, a green harness says nothing about it.
This is the same disease as
[[feedback_prod_hardening_unreachable_in_ci]] (the whole test surface ran where the defect could not
occur) — arriving through the fakes instead of through the environment.

### 3. `${VAR:-default}` — EMPTY is as dangerous as ABSENT, and `set -a` can blank it later.

`${VAR:-default}` treats the **empty string** as unset. And `load_env` sourced the host `.env` with
`set -a` **after** the default was applied — so an `RV_PROJECT=` line in `.env` would blank it, and
`guard_not_live` would still pass, because `"" != "noorinalabs"`. Nothing writes that key today.
**Nothing refuses it either.**

Order matters: a default applied *before* an environment load is not a default, it is a suggestion.

## What a safe handoff looks like

**Verify the handoff; do not trust it.** `restore.sh` already logs `Resolved Neo4j data volume: …`.
Scrape it and **require** it to name an `rv_` volume. That converts a trusted handoff into a verified
one — the standard the rest of the script already held itself to.

And note what was done RIGHT, because it is the correct shape: the **teardown** (`docker volume rm -f`,
the largest blast radius in the file) force-removes exactly **three literal names**, each
`docker volume inspect`-gated first, passed as exact names — no glob, no `prune`, no `volume ls | grep`.
The live volumes carry no `rv_` infix, so the sets are **disjoint before `guard_not_live` even comes
into it**. *Two independent reasons it cannot reach a live volume.* That is the standard: not one
guard, but two that fail independently.

## The recurrence

This is **deploy#617's exact shape, one layer out**. #617 was Compose falling back to the directory
basename for the project name. The author *wrote the static scan* that stops it recurring inside
`restore_verify.sh` — and then handed the actual load to a script the scan does not cover. **The fix
for a bug does not travel to the code you delegate to.**

See [[feedback_prod_hardening_unreachable_in_ci]], [[feedback_measurement_is_the_thing_that_breaks]],
[[feedback_calibrate_the_mutation_before_counting_it]],
[[feedback_a_scan_cannot_see_an_emptied_string]], [[feedback_errexit_kills_assignment_guard]].
deploy#640/#642, recurrence of #617.

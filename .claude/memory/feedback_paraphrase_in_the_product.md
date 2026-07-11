---
name: feedback_paraphrase_in_the_product
description: A paraphrase of a producer's format inside a TEST goes green while the product breaks — findable. A paraphrase inside the PRODUCT is worse — a producer-side rename does not BREAK the parser, it QUIETLY NARROWS it, so the guard downstream stops guarding and the test stays green too (it paraphrases as well). Anchor on the part of the format that CANNOT change; never enumerate the part that can.
metadata:
  type: feedback
---

**deploy#589, found by Nino Kavtaradze in the deploy#584 review, 2026-07-11.**

`restore.sh::list_runs` counted the distinct backup runs in a directory by hand-parsing
`backup.sh`'s **filename** format:

```sh
sed -n 's/^isnad-[a-z0-9]\{1,\}-\([0-9]\{8\}-[0-9]\{6\}\)\.dump\(\.zst\)\{0,1\}$/\1/p'
#              ^^^^^^^^^^^^^^^ the store segment — CANNOT CONTAIN A HYPHEN
```

Correct against all three real store names today. But rename `userpg` → `user-pg` in the
**producer** and every dump of that store becomes **invisible** to the parser:

```
isnad-user-pg-20260711-030100.dump   ->   (no match)
```

`count_runs` then falls to **1** — and `1` is exactly the value that makes the
refuse-on-ambiguity gate **stand down**. **The torn restore comes back, and nothing goes red.**

## The asymmetry — this is the whole lesson

| where the paraphrase lives | how it fails |
|---|---|
| in a **test** | the test goes green while the product is broken — **loud enough to find, once someone looks** |
| in the **PRODUCT** | **the guard silently stops guarding** — and the test *still passes*, **because the test paraphrases too** |

> **A producer-side rename does not BREAK a paraphrasing parser — it QUIETLY NARROWS it.** The
> parser keeps working; it just stops seeing things. **And what it stops seeing is exactly what
> the guard downstream needed to count.**

## The fix is not "bind the parser to the producer" — it is "don't parse that part"

The tempting fix is to derive the store names from `backup.sh` (the `manifest_fixture.py` /
`*_DUMP_FILE` move). That works for **tests**, where you can import. In a **shell script on the
VPS** you cannot, and a second copy of the names is a second paraphrase.

**So parse only the part of the format that CANNOT change, and let the rest be `.*`.** Here the
**run id** is the thing we want, and its shape is strict and self-delimiting
(`%Y%m%d-%H%M%S`). Anchor on that; the store segment is `.*` and its name is irrelevant:

```sh
sed -n 's/^isnad-.*-\([0-9]\{8\}-[0-9]\{6\}\)\.dump\(\.zst\)\{0,1\}$/\1/p'
```

Sidecars still fall out for free (`.sha256` does not end in `.dump[.zst]`).

**How to apply:** for any parser over an artifact another component names, ask **"which token
am I actually extracting, and which tokens am I merely *describing* on the way to it?"** Anchor
the extraction; never enumerate a charset for a token you do not need. A character class for
someone else's identifier is a paraphrase of their naming convention, and it fails silently.

## And the test that catches it must mutate the SHAPE, not use today's names

A test that builds fixtures from the producer's **current** filenames **passes** against the
broken parser — today's names have no hyphen. It is a good test and it cannot see this. The
one that catches it introduces a **hyphenated store name** and asserts the run is still
counted. **A fixture built from today's instance cannot falsify a rule about tomorrow's.**

Related: [[feedback_stderr_is_commentary_not_data]],
[[feedback_errexit_kills_assignment_guard]], [[feedback_calibrate_the_mutation_before_counting_it]].

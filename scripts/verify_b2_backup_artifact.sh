#!/usr/bin/env bash
# =============================================================================
# verify_b2_backup_artifact.sh — assert a RESTORABLE OBJECT EXISTS IN B2.
#
# Every backup alert we have reads a HOST-LOCAL textfile gauge. The single point of
# trust that an object ever landed in the bucket is `rclone copy; echo $?` — the
# uploader's own opinion of its upload. The whole alerting stack can be green while
# the bucket is empty (deploy#583).
#
# This is the only thing that looks IN THE BUCKET. It runs from CI, never from the box
# whose backup it is checking: a host that is down cannot report that its own backup is
# missing, and a Prometheus gauge scraped from that host cannot either. That is why the
# signal here is a failing CI job and not a metric — a metric would have to be scraped
# from the very machine whose absence we are trying to detect.
#
# ---------------------------------------------------------------------------
# THE SILENT ZERO THIS EXISTS TO CLOSE — and which it must not commit itself
# ---------------------------------------------------------------------------
#   `rclone lsf` on an EMPTY prefix exits 0 and prints NOTHING.
#
# An empty listing read as "fine" is exactly the defect this issue names, and it would be
# a perfect instance of the bug landing inside its own fix. So the outcomes are kept
# strictly apart and NEVER collapsed:
#
#   bucket unreachable      -> INSTRUMENT ERROR (exit 2). Credentials, network, wrong
#                              bucket. NOT a claim about backups: we know nothing, and
#                              "no backups" here would be a fabricated measurement in the
#                              same shape as "fine".
#   reachable, no dumps     -> ABSENT (exit 1). ALERT.
#   reachable, newest old   -> STALE  (exit 1). ALERT.
#   reachable, newest fresh -> FRESH  (exit 0).
#
# ---------------------------------------------------------------------------
# WHY THE REACHABILITY PROBE IS ON THE BUCKET AND NOT THE PREFIX
# ---------------------------------------------------------------------------
# Because the B2 backend and the local backend DISAGREE, and calibrating on the wrong one
# would have shipped a scanner that cannot tell "no backups" from "wrong path".
#
# What matters is the SCOPE of the probe, not the subcommand. Measured against real B2 with
# `lsf --recursive` — the call this script actually makes (an earlier version of this
# comment described `lsd`, a probe that was measured during design and never shipped; the
# code was right and the comment was describing something else, which is its own small
# lesson about documenting the thing you ran):
#
#                                             local backend      B2 backend
#   lsf -R on a NONEXISTENT PREFIX            rc=3 (error)       rc=0, empty output  <-- !!
#   lsf -R on a NONEXISTENT BUCKET            rc=3 (error)       rc=1 (error)
#   lsf -R on a bucket with a BAD KEY         n/a                rc=1 (error)
#
# So on B2 there is NO error for a nonexistent PREFIX: a typo in the path is
# indistinguishable, by exit code, from an empty bucket. A prefix-level probe therefore
# cannot be the instrument-liveness control on the backend we actually run against — it
# would have been calibrated on a behaviour that does not exist in production.
#
# The BUCKET-level probe DOES separate, on both backends, so that is the control. After it
# passes, a zero under the prefix is a real zero: we know the credentials work, the bucket
# exists, and the scanner can see.
#
# One residual ambiguity is stated rather than hidden: a valid bucket with a MIS-TYPED
# prefix reads as ABSENT. It fails loud, which is the right direction, but the diagnosis
# would be wrong — so the result line carries `bucket_objects`, and an ABSENT verdict with
# `bucket_objects > 0` means "the bucket has contents, just not where I looked", which is
# an operator's cue to check the prefix rather than hunt for a lost backup.
#
# ---------------------------------------------------------------------------
# WHY --self-test RUNS BEFORE EVERY REAL READING
# ---------------------------------------------------------------------------
# You cannot draw a "the listing works" control from INSIDE the scope: if the bucket is
# genuinely empty, the control has nothing to see either. A zero from a broken scanner and
# a zero from an empty bucket are the same string, and only one of them is a measurement.
#
# So the scanner is calibrated against local fixtures through the same code path, and must
# SEPARATE all four classes before any real verdict is trusted. A scanner that has not been
# shown to return all four cannot be believed when it returns one.
#
# Usage:
#   B2_ROOT="isnad:isnad-graph-backups" ./scripts/verify_b2_backup_artifact.sh
#   ./scripts/verify_b2_backup_artifact.sh --self-test
#
# Env:
#   B2_ROOT          rclone path to the BUCKET (reachability probe + scan root)
#   B2_PREFIX        optional sub-path under the bucket to scan (default: whole bucket)
#   MAX_AGE_HOURS    freshness bound (default 30 — one nightly run plus slack)
# =============================================================================
set -euo pipefail

# rclone prints ModTime in LOCAL time, and this script parses it with `date -u -d`.
# Unpinned, that is a systematic error equal to the box's UTC offset — measured on this
# workstation (UTC-4), an object uploaded seconds earlier reported `newest_age_hours=4`.
#
# The direction matters: on a box BEHIND UTC the age is inflated (a fresh backup reads
# stale — a false alarm, annoying). On a box AHEAD of UTC the age is DEFLATED, and a
# stale backup reads FRESH. That is a MISSED alarm on the one check that stands between
# us and an unrecoverable delete, and it would depend on nothing but the runner's clock
# configuration.
#
# Pinning TZ makes rclone emit UTC and `date -u` parse UTC, so the two agree by
# construction. The self-test asserts the resulting AGE, not merely the class, because a
# four-hour systematic error does not move a fresh fixture out of the fresh class — the
# classes were far enough apart to hide it, which is exactly how it survived the first
# calibration.
export TZ=UTC

# This script CAPTURES AND PRINTS rclone's stderr (see instrument_error, below), and its
# output goes to a PUBLIC CI log. `RCLONE_DUMP=auth` makes rclone echo the `Authorization:
# Basic <base64(keyID:key)>` header — which GitHub's secret masking, an exact-substring
# match on the raw secret, does NOT catch. Refuse rather than redact: a leak is not
# recoverable, and there is no reason to run this under a debug dump.
if [[ -n "${RCLONE_DUMP:-}" ]]; then
    echo "ERROR: refusing to run with RCLONE_DUMP set (value redacted). rclone would echo the" >&2
    echo "  Authorization header, and GitHub's masking does not catch the base64 form." >&2
    exit 2
fi

MAX_AGE_HOURS="${MAX_AGE_HOURS:-30}"
B2_PREFIX="${B2_PREFIX:-}"

# A dump smaller than this is not restorable. `pg_dump --format=custom` of even an empty
# database is several KB; a 0-byte file is what you get when pg_dump FAILS and the shell
# redirection has already created the target. That file then uploads cleanly, rclone
# returns 0, the host gauge goes green — and, before this floor existed, THIS check said
# `fresh` too. The whole chain of trust this script exists to break was still broken one
# layer out. A plausible floor beats `> 0`: it also catches a truncated upload.
MIN_DUMP_BYTES="${MIN_DUMP_BYTES:-1024}"

# A timestamp in the future is NOT a fresh backup. It is a broken clock.
#
# rclone PRESERVES the source file's mtime, so an object's timestamp is the VPS CLOCK AT
# DUMP TIME. This script already pins TZ on the READER side; the WRITER side has the
# identical exposure and no guard at all. A skewed or NTP-failed VPS uploads future-dated
# dumps — and a one-sided `age > MAX` bound then reports them `fresh` FOREVER. It does not
# degrade, it LATCHES: because `newest` is selected by max epoch, a single bad-clock object
# masks an entire stale bucket, and this job becomes a permanent green light on the one
# signal we built it to trust.
#
# A few minutes of jitter between two machines is normal; a future timestamp beyond that is
# a fault, and the only honest verdict is "I cannot trust this reading".
FUTURE_TOLERANCE_SECONDS="${FUTURE_TOLERANCE_SECONDS:-300}"

EXIT_OK=0
EXIT_ALERT=1
EXIT_INSTRUMENT=2

# The associative-array key for a dump at the ROOT of the scanned scope. It must NOT be the
# empty string: bash rejects an empty subscript outright (`bad array subscript`), and the
# empty string is already spoken for as "nothing selected". `/` is what the result line has
# always PRINTED for the root, so this makes the key and the display agree.
ROOT_KEY="/"

# The self-test deliberately drives the instrument-error path. Without this, every PASSING
# run prints an alarming "rclone could not list ..." block from a fixture that is behaving
# exactly as intended — and a check that cries wolf on success is a check people learn to
# scroll past. The diagnostic is suppressed for the fixtures, never for a real scan.
QUIET_ERRORS=false

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) [$1] ${*:2}" >&2; }

# One machine-readable line. `status` is set explicitly on every path — it is never
# inferred from an absence of output, which is the whole point of this script.
# `undersized` is reported UNCONDITIONALLY. It used to be consulted only when `dumps == 0`,
# so a user-postgres dump that uploaded as ZERO BYTES — beside a healthy pg and neo4j —
# reported `fresh` and said nothing at all. The value was computed and then thrown away:
# the identical defect to the `-719` age that was computed and never checked. If the scan
# knows something, the result line says it.
result() {
    printf 'B2_BACKUP_ARTIFACT status=%s reason=%s dumps=%s newest_age_hours=%s bucket_objects=%s undersized=%s newest=%s\n' \
        "$1" "${2:--}" "$3" "$4" "$5" "${6:-0}" "${7:--}"
}

# scan <bucket-root> <prefix> -> result line; 0 fresh / 1 alert / 2 instrument error.
scan() {
    local root="$1" prefix="${2:-}"
    local rc=0 listing bucket_listing bucket_objects=0

    # --- 1. INSTRUMENT LIVENESS, on the bucket. -----------------------------
    # `|| rc=$?` and NOT a bare assignment: under `set -e` an assignment whose command
    # substitution fails IS a failing simple command and errexit fires AT THE ASSIGNMENT,
    # so the rc check below would be dead code on every failing path and an instrument
    # error would never be told apart from an absence. (Learned in deploy#563.)
    # `--log-level NOTICE` is pinned, not incidental: it keeps rclone's verbosity at a
    # level that carries the failure reason and nothing else. Without a diagnostic, rc alone
    # cannot separate a 401 from a typo'd bucket from a network fault — and an error state
    # that cannot say WHY it could not look is a diminished version of the third state this
    # whole script is built around. The credential cannot reach this output: it travels via
    # RCLONE_CONFIG_* env, never argv, and RCLONE_DUMP is refused above.
    local err
    err="$(mktemp)"
    bucket_listing="$(rclone lsf --recursive --log-level NOTICE "$root" 2>"$err")" || rc=$?
    if [[ $rc -ne 0 ]]; then
        if [[ "$QUIET_ERRORS" != "true" ]]; then
            log ERROR "rclone could not list ${root} (rc=${rc}). Its stderr follows:"
            sed 's/^/    /' "$err" >&2
        fi
        rm -f "$err"
        result instrument_error unreachable 0 -1 -1 0 -
        return $EXIT_INSTRUMENT
    fi
    rm -f "$err"
    bucket_objects="$(printf '%s' "$bucket_listing" | grep -c . || true)"

    # --- 2. Scan the prefix we actually care about. -------------------------
    # `tsp` = modtime; size; path. The SIZE is not decoration: a 0-byte dump lists
    # non-empty and restores nothing.
    local base="$root"
    [[ -n "$prefix" ]] && base="${root}/${prefix}"
    rc=0
    err="$(mktemp)"
    listing="$(rclone lsf --recursive --format tsp --separator ';' --log-level NOTICE "$base" 2>"$err")" || rc=$?
    if [[ $rc -ne 0 ]]; then
        if [[ "$QUIET_ERRORS" != "true" ]]; then
            log ERROR "rclone could not list ${base} (rc=${rc}). Its stderr follows:"
            sed 's/^/    /' "$err" >&2
        fi
        rm -f "$err"
        result instrument_error unreachable 0 -1 "$bucket_objects" 0 -
        return $EXIT_INSTRUMENT
    fi
    rm -f "$err"

    # --- 3. Group by RUN, and ask each RUN whether it is restorable.
    #
    # "At least one dump, over the floor, recent" is NOT what this script claims to assert. It
    # is "a RESTORABLE object exists". Those came apart badly (deploy#584 review, Nurul
    # Hakim): a bucket holding only `isnad-pg` — no user-postgres, no Neo4j — WITH a manifest
    # explicitly saying `complete=false` was reported `fresh`. `restore.sh`'s required-store
    # gate REFUSES that artifact outright, so the one check that looks inside the bucket was
    # certifying a backup our own restore path declines to restore — and the attestation was
    # sitting right there, in the bucket, saying so.
    #
    # ---------------------------------------------------------------------------
    # THE UNIT IS THE RUN, NOT THE DIRECTORY. (deploy#587)
    # ---------------------------------------------------------------------------
    # This grouped by DIRECTORY and read ONE fixed-name `_backup_manifest.txt` from each. But a
    # directory is a DAY (`<category>/<DATE>`) and the dumps in it are RUNS
    # (`isnad-<store>-<TS>`). `rclone copy` ADDS and never deletes, and backup.sh deliberately
    # uploads partials — so a day holds SEVERAL runs, and the fixed-name manifest was
    # OVERWRITTEN BY WHICHEVER RUN UPLOADED LAST.
    #
    # A good 03:01 run followed by a partial 08:00 retry therefore lost its attestation, and
    # this scanner reported `status=incomplete reason=no_complete_backup` OVER A BUCKET
    # CONTAINING A COMPLETE BACKUP. A red alert saying there is no complete backup, when there
    # is one — the same shape as the confident lie the `manifest_unreadable` fix condemns,
    # reached by a third route.
    #
    # backup.sh now writes `_backup_manifest-<TS>.txt`, one per run, never overwritten. So we
    # enumerate the RUNS and take the newest one that BOTH attests `complete=true` AND whose
    # dumps all arrived. Two instruments, at the granularity completeness actually has.
    #
    # A dump that ARRIVED but is UNATTESTED is never selectable — candidates come from the
    # MANIFESTS, not from the dumps. Accepting "all three dumps happen to be present" in place
    # of an attestation would drop the producer's word entirely and reintroduce exactly what
    # deploy#584 closed.
    local -A run_newest=() run_dumps=() run_files=() run_under=() dir_manifests=()
    local line ts size path epoch dir bn rts key
    local undersized_total=0 sized_total=0

    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        ts="${line%%;*}"
        line="${line#*;}"
        size="${line%%;*}"
        path="${line#*;}"

        # ROOT_KEY, not "". **Bash associative arrays cannot take an empty subscript** —
        # `declare -A a; a[""]=1` is `bad array subscript`, a hard error. So a dump at the
        # ROOT of the scanned scope killed the scanner outright: no result line, exit 1.
        #
        # Two live routes, neither exotic (deploy#584 review, Nurul Hakim):
        #   * the DOCUMENTED `B2_PREFIX` invocation — every dump sits at the root of the scope;
        #   * a healthy bucket plus one stray root-level dump, i.e. an operator hand-copying a
        #     dump during a DR — an entirely ordinary thing to do.
        #
        # And note the CLASS. This is not a new category, it is ROW ONE of this script's own
        # table, still live in its own code: a crash prints NO CLAIM and exits 1, and every
        # consumer reads a non-zero exit as a verdict about the BACKUPS. The one script whose
        # founding invariant is "an instrument failure is NOT a claim about your data"
        # terminated without a claim, with a failing exit code. It is the purest form of the
        # collapse this whole file exists to prevent.
        dir="$(dirname "$path")"
        if [[ "$dir" == "." ]]; then
            dir="$ROOT_KEY"
        fi
        bn="$(basename "$path")"

        # A PER-RUN MANIFEST. This is a candidate RUN, not a dump.
        #
        # `_backup_manifest-` is OUR OWN literal prefix and the run id's shape is strict, so
        # both halves of this pattern are tokens we control — nothing here paraphrases a name
        # another component may rename (deploy#589).
        if [[ "$bn" == _backup_manifest-*.txt ]]; then
            rts="$(printf '%s' "$bn" | sed -n 's/^_backup_manifest-\([0-9]\{8\}-[0-9]\{6\}\)\.txt$/\1/p')"
            if [[ -n "$rts" ]]; then
                dir_manifests["$dir"]="${dir_manifests["$dir"]:-} ${rts}"
            fi
            continue
        fi

        case "$path" in
            *.dump | *.dump.zst) ;;
            *) continue ;;
        esac

        # WHICH RUN DOES THIS DUMP BELONG TO? ANCHOR ON THE RUN ID; DO NOT PARSE THE STORE
        # NAME (deploy#589). Matching the store segment as `[a-z0-9]+` cannot contain a hyphen,
        # so renaming `userpg` to `user-pg` in the producer would not BREAK this parser — it
        # would QUIETLY NARROW it, and the dump would become invisible to the arrival check
        # while every test stayed green. The run id's format is strict and self-delimiting;
        # the store segment is `.*` and its name is irrelevant here.
        rts="$(printf '%s' "$bn" | sed -n 's/^isnad-.*-\([0-9]\{8\}-[0-9]\{6\}\)\.dump\(\.zst\)\{0,1\}$/\1/p')"
        key="${dir}|${rts}"

        # A ZERO-BYTE dump lists non-empty and restores nothing — the same sentence as the
        # checksum-only bucket above, one level in. `pg_dump` failing after the shell has
        # created the target leaves exactly this, and it uploads cleanly. Tracked PER RUN, so
        # a corrupt dump is charged to the run that produced it and not to its neighbours.
        if [[ "${size:-0}" -lt "$MIN_DUMP_BYTES" ]]; then
            undersized_total=$((undersized_total + 1))
            if [[ -n "$rts" ]]; then
                run_under["$key"]=$(( ${run_under["$key"]:-0} + 1 ))
            fi
            continue
        fi

        sized_total=$((sized_total + 1))
        # A sized dump we cannot attribute to any run. It counts as a dump (so the bucket is
        # not "absent"), but it can never satisfy a run's arrival check.
        if [[ -z "$rts" ]]; then
            continue
        fi

        run_dumps["$key"]=$(( ${run_dumps["$key"]:-0} + 1 ))
        # The NAMES, not just the count. The count cannot answer "is user-postgres here?" —
        # and that is the only question that matters to a restore.
        run_files["$key"]="${run_files["$key"]:-} ${bn}"
        epoch="$(date -u -d "$ts" +%s 2>/dev/null || echo 0)"
        if [[ "$epoch" -gt "${run_newest["$key"]:-0}" ]]; then
            run_newest["$key"]="$epoch"
        fi
    done <<< "$listing"

    # Nothing restorable anywhere.
    if [[ "$sized_total" -eq 0 ]]; then
        if [[ "$undersized_total" -gt 0 ]]; then
            result absent undersized_dumps 0 -1 "$bucket_objects" "$undersized_total" -
        else
            result absent no_dumps 0 -1 "$bucket_objects" "$undersized_total" -
        fi
        return $EXIT_ALERT
    fi

    # The RUNS, newest first — across every directory, exactly as resolve_latest walks them.
    #
    # Ordered by the run's OWN newest dump mtime, NOT by the directory's. Using the directory's
    # would let a later partial run's fresh dumps lend their freshness to the older good run we
    # actually select — a stale backup reading `fresh`, which is the deflating direction and
    # the dangerous one. Ties break on the run id, so the later run of a same-mtime pair (the
    # fixture case, and any two runs uploaded within the same second) is still considered first.
    local -a ordered=()
    while IFS= read -r key; do
        [[ -n "$key" ]] && ordered+=("$key")
    done < <(
        for dir in "${!dir_manifests[@]}"; do
            # Deliberate word-splitting: the value is a space-separated list of run ids.
            # shellcheck disable=SC2086
            for rts in ${dir_manifests["$dir"]}; do
                printf '%s\t%s\t%s|%s\n' "${run_newest["${dir}|${rts}"]:-0}" "$rts" "$dir" "$rts"
            done
        done | sort -k1,1rn -k2,2r | cut -f3-
    )

    # A run that ATTESTS complete but did NOT fully arrive is a real fault — but it is not, by
    # itself, an ALERT. If an older run is complete and intact, then a restorable backup DOES
    # exist and `restore.sh latest` will find it; saying "no complete backup" would be the
    # false alarm this whole issue is about. So we SKIP it, keep looking, and say so LOUDLY.
    # If nothing else qualifies, its specific reason becomes the verdict.
    #
    # This is what keeps the scanner and `restore.sh` answering the SAME QUESTION. They must
    # agree: a bucket the restore path can restore from must not be reported as an alert here,
    # and one it would refuse must never be reported fresh.
    local skipped=0 chosen="" chosen_ts="" chosen_epoch=0 chosen_dumps=0
    local torn_count=0 torn_dir="" torn_reason="" torn_dumps=0
    local key rts
    for key in "${ordered[@]}"; do
        dir="${key%|*}"
        rts="${key##*|}"
        local mpath="${base}"
        if [[ "$dir" != "$ROOT_KEY" ]]; then
            mpath="${base}/${dir}"
        fi

        # --- "I COULD NOT READ IT" IS NOT "IT DOES NOT ATTEST". ------------------
        #
        # This read was `rclone cat … 2>/dev/null || true` — the rc DISCARDED. A transient
        # 401, a throttle, a network blip on the manifest object then produced
        # `status=incomplete reason=no_complete_backup` over a GOOD, COMPLETE, FRESH backup:
        # the dumps are there, the backup is restorable, and the scanner says there is no
        # complete backup. That is the same confident lie the unmatched regex produced,
        # reached by a different route (deploy#584 review, Nino Kavtaradze).
        #
        # And it broke this script's OWN founding invariant — `instrument_error` is NOT a
        # claim about backups — INSIDE the change built to enforce it. Step 1's bucket probe
        # honours it (`|| rc=$?`); this read did not. "I could not read the input" is a THIRD
        # OUTCOME, not a value of the predicate, and collapsing it into either branch is a lie
        # either way.
        #
        # rc SEPARATES them, measured on both backends (and the backends DISAGREE, in exactly
        # the same shape as the `lsf` finding above — so calibrating this on local fixtures
        # alone would repeat that mistake):
        #
        #   cat existing      -> rc=0 (B2)   rc=0 (local)
        #   cat NONEXISTENT   -> rc=0, EMPTY (B2)   rc=3 (local)      <- absent, not an error
        #   cat, bad key      -> rc=1 (B2)   rc=1 (local)             <- CANNOT EVALUATE
        #
        # So: 0 => read it (empty on B2 means absent); 3 => local not-found, absent;
        # anything else => we could not look, and we must say so.
        local manifest mrc=0 merr
        merr="$(mktemp)"
        manifest="$(rclone cat --log-level NOTICE "${mpath}/_backup_manifest-${rts}.txt" 2>"$merr")" || mrc=$?
        if [[ "$mrc" -ne 0 && "$mrc" -ne 3 ]]; then
            if [[ "$QUIET_ERRORS" != "true" ]]; then
                log ERROR "rclone could not READ the manifest for run ${rts} at ${mpath} (rc=${mrc}). Its stderr follows:"
                sed 's/^/    /' "$merr" >&2
                log ERROR "  This is NOT a claim that the backup is incomplete — it is a claim that I could not look."
            fi
            rm -f "$merr"
            result instrument_error manifest_unreadable "${run_dumps["$key"]:-0}" -1 "$bucket_objects" "$undersized_total" "${dir:-/}"
            return $EXIT_INSTRUMENT
        fi
        rm -f "$merr"

        # Whole-TOKEN match, and NOT the anchored-prefix regex restore.sh shipped with —
        # that one could never match, because `complete=` is the first token after
        # `BACKUP_MANIFEST ` and the anchor's literal space consumes the only space present.
        # Fixed in restore.sh in this same change; the bug is the reason this fixture exists.
        #
        # `head -n1`: attest on the FIRST manifest line only. Unioning tokens across every
        # line lets a corrupt artifact carrying both a `complete=false` and a `complete=true`
        # line match — it FAILS OPEN, and a line-1 read costs nothing.
        if ! printf '%s\n' "$manifest" | grep '^BACKUP_MANIFEST ' | head -n1 \
                | tr ' ' '\n' | grep -qx 'complete=true'; then
            # A manifest we READ, which does not attest. (Or none at all: every
            # pre-deploy#559 backup predates user-postgres coverage entirely.) Distinct from
            # the branch above — that one is "I could not read it", this one is "I read it,
            # and it does not say complete".
            skipped=$((skipped + 1))
            continue
        fi

        # THE MANIFEST MUST BE ABOUT THE RUN IT IS NAMED FOR.
        #
        # The whole fix rests on the FILENAME's run id identifying the run. If the manifest's
        # own `timestamp=` field disagrees with the name it is filed under, then binding the
        # dumps of run X to an attestation about run Y is exactly the mis-binding this change
        # exists to prevent — so refuse to use it. Cheap to check, and it fails closed.
        local run_ts
        run_ts="$(printf '%s\n' "$manifest" | grep '^BACKUP_MANIFEST ' | tr ' ' '\n' \
            | sed -n 's/^timestamp=\(..*\)$/\1/p' | head -1)"
        if [[ "$run_ts" != "$rts" ]]; then
            log WARNING "manifest _backup_manifest-${rts}.txt declares timestamp=${run_ts:-<none>} — it does not attest the run it is named for."
            torn_count=$((torn_count + 1))
            if [[ -z "$torn_dir" ]]; then
                torn_dir="$dir"
                torn_reason="manifest_ts_mismatch"
                torn_dumps="${run_dumps["$key"]:-0}"
            fi
            continue
        fi

        # It attests complete — but attestation is the PRODUCER's word about what it WROTE,
        # not about what SURVIVED. A complete-attested run holding a zero-byte dump is corrupt,
        # and `complete=true` cannot see that. Both instruments are needed.
        if [[ "${run_under["$key"]:-0}" -gt 0 ]]; then
            log WARNING "run ${rts} in ${dir} attests complete=true but carries an UNDERSIZED dump — not restorable."
            torn_count=$((torn_count + 1))
            if [[ -z "$torn_dir" ]]; then
                torn_dir="$dir"
                torn_reason="undersized_dumps"
                torn_dumps="${run_dumps["$key"]:-0}"
            fi
            continue
        fi

        # --- THE REQUIRED-STORE GATE, bound to THIS RUN. ------------------------
        #
        # `complete=true` is the producer's account of what it DUMPED. It says nothing about
        # what UPLOADED. backup.sh writes the manifest locally and then `rclone copy`s the
        # whole directory; a copy interrupted after the manifest object lands leaves B2 with
        # `complete=true` sitting above a half-finished upload. The previous version of this
        # scanner reported that `fresh` (deploy#584: proven — `status=fresh dumps=1` over a
        # bucket with no user-postgres and no Neo4j, which restore.sh exits 1 on). Checking
        # the attestation is not the same as checking the artifact.
        #
        # The arrival test is scoped to THIS RUN's own files (`run_files`), not to the
        # directory's. A day-directory accumulates runs, so a check that asked only "is there
        # an isnad-pg somewhere in here?" would be satisfied by a dump left behind by a
        # DIFFERENT, failed run — and that is precisely how a torn restore gets certified.
        #
        # THE REQUIRED SET IS THIS SCRIPT'S OWN, deliberately NOT read from the manifest's
        # `stores=` field — the same reasoning restore.sh states for REQUIRED_STORES. A set
        # taken from the artifact lets a partial backup declare itself
        # complete-for-what-it-happens-to-hold: perfect circularity, and invisible.
        local have=" ${run_files["$key"]:-} " missing=""
        [[ "$have" == *" isnad-pg-${rts}.dump "* ]]      || missing="${missing},isnad-pg"
        [[ "$have" == *" isnad-userpg-${rts}.dump "* ]]  || missing="${missing},isnad-userpg"
        [[ "$have" == *" isnad-neo4j-${rts}.dump.zst "* || "$have" == *" isnad-neo4j-${rts}.dump "* ]] \
            || missing="${missing},isnad-neo4j"
        if [[ -n "$missing" ]]; then
            log WARNING "attested run ${rts} in ${dir} is missing required store(s): ${missing#,}"
            log WARNING "  restore.sh REFUSES this run — it must not be reported fresh."
            torn_count=$((torn_count + 1))
            if [[ -z "$torn_dir" ]]; then
                torn_dir="$dir"
                torn_reason="missing_stores${missing}"
                torn_dumps="${run_dumps["$key"]:-0}"
            fi
            continue
        fi

        chosen="$dir"
        chosen_ts="$rts"
        # The CHOSEN RUN's own newest dump — never the directory's. See the ordering note above:
        # a later partial run in the same day would otherwise lend its freshness to the older
        # run we actually selected, and a stale backup would read `fresh`.
        chosen_epoch="${run_newest["$key"]:-0}"
        chosen_dumps="${run_dumps["$key"]:-0}"
        break
    done

    if [[ -z "$chosen" ]]; then
        # An ATTESTED run that did not ARRIVE is the most specific thing we can say, so it
        # outranks the generic verdict. This is the deploy#584 diagnosis, preserved: the
        # operator is told WHICH store is missing, not merely that nothing qualified.
        if [[ "$torn_count" -gt 0 ]]; then
            result incomplete "$torn_reason" "$torn_dumps" -1 "$bucket_objects" "$undersized_total" "${torn_dir:-/}"
            return $EXIT_ALERT
        fi
        # Dumps exist, but not one RUN attests completeness.
        result incomplete no_complete_backup 0 -1 "$bucket_objects" "$undersized_total" -
        return $EXIT_ALERT
    fi

    # We found a restorable run — but a torn one existed too, and that is still a fault. It is
    # NOT an alert (an older run is restorable, and restore.sh will find it), but it must not be
    # silent: an upload that landed its attestation and not its dumps will keep happening.
    if [[ "$torn_count" -gt 0 ]]; then
        log WARNING "${torn_count} run(s) attest complete=true but did NOT fully arrive (see above)."
        log WARNING "  An older run IS restorable, so this is not an ALERT — but the torn upload is real."
    fi
    if [[ "$skipped" -gt 0 ]]; then
        log WARNING "skipped ${skipped} run(s) that do not attest complete=true"
    fi

    local now delta age_h
    now="$(date -u +%s)"
    delta=$(( now - chosen_epoch ))
    log INFO "selected run ${chosen_ts} in ${chosen} (${chosen_dumps} dump(s))"

    # --- 4. A FUTURE timestamp is a broken clock, not a fresh backup. -------
    if [[ "$delta" -lt $(( -FUTURE_TOLERANCE_SECONDS )) ]]; then
        result instrument_error future_timestamp "$chosen_dumps" $(( delta / 3600 )) "$bucket_objects" "$undersized_total" "${chosen:-/}"
        return $EXIT_INSTRUMENT
    fi

    age_h=$(( delta / 3600 ))

    if [[ "$age_h" -gt "$MAX_AGE_HOURS" ]]; then
        result stale too_old "$chosen_dumps" "$age_h" "$bucket_objects" "$undersized_total" "${chosen:-/}"
        return $EXIT_ALERT
    fi

    result fresh - "$chosen_dumps" "$age_h" "$bucket_objects" "$undersized_total" "${chosen:-/}"
    return $EXIT_OK
}

# --------------------------------------------------------------------------
# Self-test — calibrate before reading. Runs the SAME scan() against fixtures.
# --------------------------------------------------------------------------
self_test() {
    local tmp fails=0 out status rc
    QUIET_ERRORS=true
    tmp="$(mktemp -d)"
    # shellcheck disable=SC2064  # expand $tmp now, deliberately
    trap "rm -rf '$tmp'" RETURN

    _expect() { # <label> <root> <prefix> <want-status> <want-rc> [max-age-hours-reported]
        local label="$1" root="$2" prefix="$3" want="$4" want_rc="$5" want_age="${6:-}"
        rc=0
        out="$(scan "$root" "$prefix")" || rc=$?
        status="$(printf '%s' "$out" | sed -n 's/.*status=\([a-z_]*\).*/\1/p')"
        local ok=1
        [[ "$status" == "$want" && "$rc" -eq "$want_rc" ]] || ok=0

        # The AGE, not just the class — and BOTH SIDES of it.
        #
        # This assertion was `-gt` only. That catches INFLATION (a fresh backup reading
        # stale — a false alarm, the annoying direction) and is STRUCTURALLY BLIND to
        # DEFLATION (a stale backup reading fresh — the missed alarm, the dangerous
        # direction, and the one this guard was written for). Proved by removing the TZ
        # pin the guard exists to protect: at UTC-4 the self-test FAILED; at UTC+4 it
        # PASSED and the scanner then reported `newest_age_hours=-3` as `fresh`.
        #
        # A one-sided assertion on a two-sided error is half a control. If the gate
        # consumes a value, the control must bound the value — on both sides.
        if [[ -n "$want_age" && "$ok" -eq 1 ]]; then
            local got_age
            got_age="$(printf '%s' "$out" | sed -n 's/.*newest_age_hours=\(-\?[0-9]*\).*/\1/p')"
            if [[ "$got_age" -lt 0 ]]; then
                log FAIL "self-test ${label}: reported a NEGATIVE age (${got_age}h) for an object created NOW. The clock disagrees with rclone's ModTime in the DEFLATING direction — a stale backup would read fresh."
                fails=$((fails + 1))
                return
            fi
            if [[ "$got_age" -gt "$want_age" ]]; then
                log FAIL "self-test ${label}: reported age ${got_age}h for an object created NOW (max ${want_age}h). Clock/TZ skew — a fresh backup would read stale."
                fails=$((fails + 1))
                return
            fi
        fi

        if [[ "$ok" -eq 1 ]]; then
            log PASS "self-test ${label}: status=${status} rc=${rc}"
        else
            log FAIL "self-test ${label}: got status=${status} rc=${rc}; want status=${want} rc=${want_rc}"
            fails=$((fails + 1))
        fi
    }

    # A helper, because every fixture below needs the producer's attestation — the same
    # `_backup_manifest.txt` backup.sh writes and restore.sh reads.
    #
    # It builds a WHOLE RUN: all three required stores, named exactly as backup.sh names them
    # (`isnad-<store>-<TIMESTAMP>.dump`, `%Y%m%d-%H%M%S`), with a manifest declaring THAT
    # timestamp. The previous helper wrote one file called `isnad-pg-a.dump` and
    # `timestamp=t` — invented shorthand. That is why the required-store defect was
    # unreachable here: THE FIXTURE HAD NO CONCEPT OF A RUN, so no fixture could express "the
    # user-postgres dump never uploaded", and the scanner was never asked. A fixture that
    # paraphrases the producer's format tests the paraphrase.
    RUN_TS=20260711-030100
    # A LATER run on the SAME DAY. The day-directory accumulates, so this is what a retry after
    # a failed nightly leaves beside the run that already succeeded (deploy#587).
    LATE_TS=20260711-080000
    _mkbackup() { # <dir> <complete> [dump-bytes] [run-ts]
        local d="$1" complete="$2" bytes="${3:-$(( MIN_DUMP_BYTES * 2 ))}" ts="${4:-$RUN_TS}"
        mkdir -p "$d"
        head -c "$bytes" /dev/zero > "${d}/isnad-pg-${ts}.dump"
        head -c "$bytes" /dev/zero > "${d}/isnad-userpg-${ts}.dump"
        head -c "$bytes" /dev/zero > "${d}/isnad-neo4j-${ts}.dump.zst"
        printf 'BACKUP_MANIFEST complete=%s stores=postgres,user-postgres,neo4j timestamp=%s category=daily\n' \
            "$complete" "$ts" > "${d}/_backup_manifest-${ts}.txt"
    }
    # A PARTIAL run: the isnad pg dump made it, user-postgres and Neo4j did not. backup.sh
    # produces exactly this BY DESIGN ("a partial backup beats none") and uploads it into the
    # SAME day-directory as the good run. Its manifest no longer overwrites the good run's.
    _mkpartial() { # <dir> [run-ts]
        local d="$1" ts="${2:-$LATE_TS}"
        mkdir -p "$d"
        head -c "$(( MIN_DUMP_BYTES * 2 ))" /dev/zero > "${d}/isnad-pg-${ts}.dump"
        printf 'BACKUP_MANIFEST complete=false stores=postgres timestamp=%s category=daily\n' \
            "$ts" > "${d}/_backup_manifest-${ts}.txt"
    }
    # Age is a property of the RUN, not of one file: `newest` is the max mtime in the
    # directory, so ageing a single dump while its siblings stay fresh does not age the
    # backup. Re-dating must move the whole run or the stale/future fixtures assert nothing.
    _agebackup() { # <dir> <when>
        touch -d "$2" "${1}"/*.dump "${1}"/*.dump.zst
    }

    # FRESH — the POSITIVE control. Without a fixture the scanner can pass, every refusal
    # below is vacuous: a scanner that only ever says ALERT proves every bucket is empty.
    #
    # It must be a REAL-SIZED dump AND attest completeness. The first version of this fixture
    # was `: > file` — zero bytes, no manifest — so the scanner had never been shown to tell
    # a restorable backup from an unrestorable one, BECAUSE NO FIXTURE COULD PRODUCE THE BAD
    # CONDITION. The guard and the fixture were blind together.
    _mkbackup "${tmp}/fresh/daily/2026-07-11" true
    _expect fresh "${tmp}/fresh" "" fresh "$EXIT_OK" 1

    # ROOT-LEVEL DUMPS — the DOCUMENTED `B2_PREFIX` invocation, which was never exercised.
    #
    # With a prefix, every dump sits at the ROOT of the scanned scope, so `dirname` returns
    # "." and the directory key was set to the EMPTY STRING — which bash rejects outright as an
    # associative-array subscript (`bad array subscript`). The scanner DIED: no result line,
    # exit 1, which every consumer reads as a verdict about the backups. The one script whose
    # founding invariant is "an instrument failure is NOT a claim about your data" terminated
    # without a claim, with a failing exit code.
    #
    # Same shape via a stray root-level dump beside a healthy bucket — an operator hand-copying
    # a dump during a DR. Both are covered here because both are ordinary.
    _mkbackup "${tmp}/prefix/daily/2026-07-11" true
    _expect fresh-under-prefix "${tmp}/prefix" "daily/2026-07-11" fresh "$EXIT_OK" 1

    _mkbackup "${tmp}/rootdump/daily/2026-07-11" true
    head -c "$(( MIN_DUMP_BYTES * 2 ))" /dev/zero > "${tmp}/rootdump/isnad-pg-${RUN_TS}.dump"
    _expect fresh-beside-a-root-dump "${tmp}/rootdump" "" fresh "$EXIT_OK" 1

    # INCOMPLETE — the artifact SAYS it is incomplete, and restore.sh refuses it. This is a
    # backup.sh output by design ("a partial backup beats none"), and it was reported fresh.
    _mkbackup "${tmp}/incomplete/daily/2026-07-11" false
    _expect incomplete-manifest "${tmp}/incomplete" "" incomplete "$EXIT_ALERT"

    # -----------------------------------------------------------------------------------
    # deploy#587 — A GOOD RUN FOLLOWED BY A PARTIAL ONE, IN THE SAME DAY-DIRECTORY.
    #
    # THE DEFECT THIS CHANGE EXISTS TO CLOSE. The 03:01 nightly succeeds completely; an 08:00
    # retry fails halfway and uploads its `complete=false` manifest. With a FIXED-NAME manifest
    # the partial one overwrote the good one, and this scanner reported
    #
    #     status=incomplete reason=no_complete_backup ... bucket_objects=7
    #
    # — a red alert saying THERE IS NO COMPLETE BACKUP, over a bucket containing one.
    #
    # Both orderings are here, and they are NOT the same test. Only the good-then-partial one
    # reproduces #587; partial-then-good is the ordering deploy#584 already handled, and it is
    # here so that fixing the first cannot regress the second.
    _mkbackup "${tmp}/good_then_partial/daily/2026-07-11" true
    _mkpartial "${tmp}/good_then_partial/daily/2026-07-11"
    _expect good-run-then-partial-run "${tmp}/good_then_partial" "" fresh "$EXIT_OK" 1

    _mkpartial "${tmp}/partial_then_good/daily/2026-07-11" 20260711-020000
    _mkbackup "${tmp}/partial_then_good/daily/2026-07-11" true
    _expect partial-run-then-good-run "${tmp}/partial_then_good" "" fresh "$EXIT_OK" 1

    # THE GOOD RUN IS NEITHER FIRST NOR LAST. A selection that took the newest manifest, or the
    # oldest, or "the only one", passes both fixtures above and fails this one.
    _mkpartial "${tmp}/three_runs/daily/2026-07-11" 20260711-020000
    _mkbackup "${tmp}/three_runs/daily/2026-07-11" true
    _mkpartial "${tmp}/three_runs/daily/2026-07-11" 20260711-080000
    _expect three-runs-good-in-the-middle "${tmp}/three_runs" "" fresh "$EXIT_OK" 1

    # A DAY WITH ZERO MANIFESTS — the enumeration LEGITIMATELY MATCHES NOTHING.
    #
    # Under `set -euo pipefail` this is the shape that CRASHES rather than returning empty. The
    # scanner must return a VERDICT here, not die: a crash prints no claim and exits non-zero,
    # and every consumer reads a non-zero exit as a verdict about the backups.
    mkdir -p "${tmp}/no_manifests/daily/2026-07-11"
    head -c "$(( MIN_DUMP_BYTES * 2 ))" /dev/zero \
        > "${tmp}/no_manifests/daily/2026-07-11/isnad-pg-${RUN_TS}.dump"
    head -c "$(( MIN_DUMP_BYTES * 2 ))" /dev/zero \
        > "${tmp}/no_manifests/daily/2026-07-11/isnad-userpg-${RUN_TS}.dump"
    head -c "$(( MIN_DUMP_BYTES * 2 ))" /dev/zero \
        > "${tmp}/no_manifests/daily/2026-07-11/isnad-neo4j-${RUN_TS}.dump.zst"
    _expect all-three-dumps-but-no-manifest "${tmp}/no_manifests" "" incomplete "$EXIT_ALERT"

    # MISSING REQUIRED STORE — `complete=true` above a HALF-FINISHED UPLOAD.
    #
    # THE DEFECT THIS SCANNER SHIPPED WITH. backup.sh writes the manifest locally and then
    # `rclone copy`s the directory; a copy interrupted after the manifest object lands leaves
    # exactly this in B2. The scanner reported `status=fresh dumps=1 exit=0` over a bucket
    # holding NO user-postgres and NO Neo4j — the two stores no pipeline artifact can rebuild
    # — while restore.sh's required-store gate exits 1 on it. The attestation was checked and
    # the ARTIFACT was not, which is not the same thing, though the comment claimed it was.
    mkdir -p "${tmp}/missing_store/daily/2026-07-11"
    head -c "$(( MIN_DUMP_BYTES * 2 ))" /dev/zero \
        > "${tmp}/missing_store/daily/2026-07-11/isnad-pg-${RUN_TS}.dump"
    printf 'BACKUP_MANIFEST complete=true stores=postgres,user-postgres,neo4j timestamp=%s category=daily\n' \
        "$RUN_TS" > "${tmp}/missing_store/daily/2026-07-11/_backup_manifest-${RUN_TS}.txt"
    _expect incomplete-missing-store "${tmp}/missing_store" "" incomplete "$EXIT_ALERT"

    # STALE DUMP MASQUERADING AS THE ATTESTED RUN — the store is PRESENT, from the WRONG RUN.
    #
    # B2's day-directory accumulates across runs: `rclone copy` adds and never deletes, while
    # the fixed-name manifest is overwritten by whichever ran last. So a dump left behind by
    # an earlier FAILED run sits beside the good one, and a check that asked only "is there an
    # isnad-pg here?" would be satisfied by it. Binding to the attested run is what separates
    # them — and it is also what stops restore.sh picking the wrong one (fixed in this change).
    _mkbackup "${tmp}/stale_run/daily/2026-07-11" true
    rm -f "${tmp}/stale_run/daily/2026-07-11/isnad-pg-${RUN_TS}.dump"
    head -c "$(( MIN_DUMP_BYTES * 2 ))" /dev/zero \
        > "${tmp}/stale_run/daily/2026-07-11/isnad-pg-20260711-020000.dump"
    _expect incomplete-stale-run "${tmp}/stale_run" "" incomplete "$EXIT_ALERT"

    # NO MANIFEST AT ALL — every pre-deploy#559 backup predates user-postgres coverage.
    # A directory that does not attest its own completeness is not one we can promise a
    # restore from.
    mkdir -p "${tmp}/nomanifest/daily/2026-07-11"
    head -c "$(( MIN_DUMP_BYTES * 2 ))" /dev/zero \
        > "${tmp}/nomanifest/daily/2026-07-11/isnad-pg-${RUN_TS}.dump"
    _expect no-manifest "${tmp}/nomanifest" "" incomplete "$EXIT_ALERT"

    # COMPLETE-ATTESTED, BUT CORRUPT — the producer's word about what it WROTE is not a claim
    # about what SURVIVED. A zero-byte user-postgres dump inside a complete-attested directory
    # reported `fresh` and said nothing: `undersized` was computed and thrown away.
    _mkbackup "${tmp}/corrupt/daily/2026-07-11" true
    : > "${tmp}/corrupt/daily/2026-07-11/isnad-userpg-${RUN_TS}.dump"
    _expect complete-but-undersized "${tmp}/corrupt" "" incomplete "$EXIT_ALERT"

    # ZERO-BYTE DUMP — lists non-empty, restores nothing.
    mkdir -p "${tmp}/zerobyte/daily/2026-07-11"
    : > "${tmp}/zerobyte/daily/2026-07-11/isnad-pg-${RUN_TS}.dump"
    _expect absent-zero-byte-dump "${tmp}/zerobyte" "" absent "$EXIT_ALERT"

    # FUTURE TIMESTAMP — a broken clock, NOT a fresh backup. rclone preserves the source
    # mtime, so this is the VPS clock at dump time; the writer side has no pin and no guard.
    _mkbackup "${tmp}/future/daily/2026-07-11" true
    _agebackup "${tmp}/future/daily/2026-07-11" "+30 days"
    _expect instrument-error-future "${tmp}/future" "" instrument_error "$EXIT_INSTRUMENT"

    # ...and it must not MASK a stale bucket.
    _mkbackup "${tmp}/future_masks/daily/2026-06-01" true
    _agebackup "${tmp}/future_masks/daily/2026-06-01" "40 days ago"
    _mkbackup "${tmp}/future_masks/daily/2026-08-10" true
    _agebackup "${tmp}/future_masks/daily/2026-08-10" "+30 days"
    _expect instrument-error-future-masking "${tmp}/future_masks" "" instrument_error "$EXIT_INSTRUMENT"

    # ABSENT — the silent zero itself: rclone exits 0 and prints nothing.
    mkdir -p "${tmp}/empty"
    _expect absent-empty "${tmp}/empty" "" absent "$EXIT_ALERT"

    # ABSENT — lists NON-empty, restores nothing. Checksums and a manifest, no dumps.
    mkdir -p "${tmp}/nodumps"
    : > "${tmp}/nodumps/isnad-pg-old.dump.sha256"
    : > "${tmp}/nodumps/_backup_manifest-${RUN_TS}.txt"
    _expect absent-no-dumps "${tmp}/nodumps" "" absent "$EXIT_ALERT"

    # STALE — a real, COMPLETE backup, far too old.
    _mkbackup "${tmp}/stale/daily/2026-06-01" true
    _agebackup "${tmp}/stale/daily/2026-06-01" "$(( MAX_AGE_HOURS + 24 )) hours ago"
    _expect stale "${tmp}/stale" "" stale "$EXIT_ALERT"

    # INSTRUMENT ERROR — an unreachable bucket must NOT be reported as "absent".
    _expect instrument-error "${tmp}/no-such-bucket" "" instrument_error "$EXIT_INSTRUMENT"

    QUIET_ERRORS=false
    if [[ "$fails" -ne 0 ]]; then
        log ERROR "self-test FAILED (${fails} case(s)). The scanner does not separate the"
        log ERROR "classes, so its verdict on the real bucket would mean nothing. Refusing"
        log ERROR "to report one — an uncalibrated instrument's zero is not a zero."
        return 1
    fi
    log INFO "self-test PASSED — scanner separates fresh / absent / stale / instrument-error."
    return 0
}

main() {
    if [[ "${1:-}" == "--self-test" ]]; then
        self_test
        exit $?
    fi

    : "${B2_ROOT:?B2_ROOT must be set (rclone path to the backups bucket)}"

    # Calibrate first, ALWAYS. A reading from an instrument that has not been shown to
    # separate the classes is not a reading.
    if ! self_test; then
        log ERROR "refusing to scan ${B2_ROOT} with an uncalibrated scanner."
        exit $EXIT_INSTRUMENT
    fi

    log INFO "scanning ${B2_ROOT}${B2_PREFIX:+/$B2_PREFIX} (max age ${MAX_AGE_HOURS}h)"
    local rc=0
    scan "$B2_ROOT" "$B2_PREFIX" || rc=$?

    case "$rc" in
        "$EXIT_OK")
            log INFO "FRESH — a restorable object exists in the bucket and is recent."
            ;;
        "$EXIT_ALERT")
            log ERROR "ALERT — no fresh restorable backup object in ${B2_ROOT}${B2_PREFIX:+/$B2_PREFIX}."
            log ERROR "  The host-local gauges may well be green: they say backup.sh RAN and rclone"
            log ERROR "  returned 0. They do not look in the bucket. This does."
            log ERROR "  If bucket_objects > 0 above, the bucket has contents but no dump under this"
            log ERROR "  prefix — check the prefix before concluding the backups are gone."
            ;;
        "$EXIT_INSTRUMENT")
            log ERROR "INSTRUMENT ERROR — could not list ${B2_ROOT} (credentials, network, bucket)."
            log ERROR "  This is NOT a claim that backups are missing. We do not know. Fix the scan."
            ;;
    esac
    exit "$rc"
}

main "$@"

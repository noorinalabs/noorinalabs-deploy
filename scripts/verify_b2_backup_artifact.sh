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

    # --- 3. Group by BACKUP DIRECTORY, and ask each one whether it is complete.
    #
    # "At least one dump, over the floor, recent" is NOT what this PR claims to assert. It
    # is "a RESTORABLE object exists". Those came apart badly (deploy#584 review, Nurul
    # Hakim): a bucket holding only `isnad-pg` — no user-postgres, no Neo4j — WITH a
    # `_backup_manifest.txt` explicitly saying `complete=false` was reported `fresh`.
    #
    # `restore.sh`'s required-store gate REFUSES that artifact outright. So the one check
    # that looks inside the bucket was certifying a backup our own restore path declines to
    # restore — and the attestation was sitting right there, in the bucket, saying so.
    #
    # And backup.sh MANUFACTURES these by design: it deliberately uploads a partial when a
    # leg fails ("a partial backup beats none"). This is not a hypothetical artifact.
    #
    # So this now mirrors restore.sh's resolve_latest(): walk the backup directories
    # newest-first and take the first one that ATTESTS its own completeness. Same
    # `_backup_manifest.txt`, same `complete=true` predicate. If the consumer would refuse
    # it, this must not call it fresh.
    local -A dir_newest=() dir_dumps=() dir_undersized=() dir_files=()
    local line ts size path epoch dir
    local undersized_total=0

    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        ts="${line%%;*}"
        line="${line#*;}"
        size="${line%%;*}"
        path="${line#*;}"
        case "$path" in
            *.dump | *.dump.zst) ;;
            *) continue ;;
        esac

        dir="$(dirname "$path")"
        [[ "$dir" == "." ]] && dir=""

        # A ZERO-BYTE dump lists non-empty and restores nothing — the same sentence as the
        # checksum-only bucket above, one level in. `pg_dump` failing after the shell has
        # created the target leaves exactly this, and it uploads cleanly.
        if [[ "${size:-0}" -lt "$MIN_DUMP_BYTES" ]]; then
            undersized_total=$((undersized_total + 1))
            dir_undersized["$dir"]=$(( ${dir_undersized["$dir"]:-0} + 1 ))
            continue
        fi

        dir_dumps["$dir"]=$(( ${dir_dumps["$dir"]:-0} + 1 ))
        # The NAMES, not just the count. The count cannot answer "is user-postgres here?" —
        # and that is the only question that matters to a restore.
        dir_files["$dir"]="${dir_files["$dir"]:-} $(basename "$path")"
        epoch="$(date -u -d "$ts" +%s 2>/dev/null || echo 0)"
        if [[ "$epoch" -gt "${dir_newest["$dir"]:-0}" ]]; then
            dir_newest["$dir"]="$epoch"
        fi
    done <<< "$listing"

    # Nothing restorable anywhere.
    if [[ ${#dir_newest[@]} -eq 0 ]]; then
        if [[ "$undersized_total" -gt 0 ]]; then
            result absent undersized_dumps 0 -1 "$bucket_objects" "$undersized_total" -
        else
            result absent no_dumps 0 -1 "$bucket_objects" "$undersized_total" -
        fi
        return $EXIT_ALERT
    fi

    # Newest-first, exactly as resolve_latest walks them.
    local -a ordered=()
    while IFS= read -r dir; do ordered+=("$dir"); done < <(
        for dir in "${!dir_newest[@]}"; do
            printf '%s\t%s\n' "${dir_newest["$dir"]}" "$dir"
        done | sort -rn | cut -f2-
    )

    local skipped=0 chosen="" chosen_epoch=0
    for dir in "${ordered[@]}"; do
        local mpath="${base}"
        [[ -n "$dir" ]] && mpath="${base}/${dir}"

        # The attestation the producer wrote about ITSELF. Same predicate restore.sh uses.
        local manifest
        manifest="$(rclone cat --log-level NOTICE "${mpath}/_backup_manifest.txt" 2>/dev/null || true)"
        # Whole-TOKEN match, and NOT the anchored-prefix regex restore.sh shipped with —
        # that one could never match, because `complete=` is the first token after
        # `BACKUP_MANIFEST ` and the anchor's literal space consumes the only space present.
        # Fixed in restore.sh in this same change; the bug is the reason this fixture exists.
        if ! printf '%s\n' "$manifest" | grep '^BACKUP_MANIFEST ' | tr ' ' '\n' | grep -qx 'complete=true'; then
            # No manifest, or it says complete=false. A directory that does not attest its
            # own completeness is not one we can promise a restore from — and every
            # pre-deploy#559 backup predates user-postgres coverage entirely.
            skipped=$((skipped + 1))
            continue
        fi

        # It attests complete — but attestation is the PRODUCER's word about what it wrote,
        # not about what survived. A complete-attested directory holding a zero-byte dump is
        # corrupt, and `complete=true` cannot see that. Both checks are needed.
        if [[ "${dir_undersized["$dir"]:-0}" -gt 0 ]]; then
            result incomplete undersized_dumps "${dir_dumps["$dir"]}" -1 "$bucket_objects" "$undersized_total" "${dir:-/}"
            return $EXIT_ALERT
        fi

        # --- THE REQUIRED-STORE GATE, bound to the ATTESTED RUN. ----------------
        #
        # `complete=true` is the producer's account of what it DUMPED. It says nothing about
        # what UPLOADED. backup.sh writes the manifest locally and then `rclone copy`s the
        # whole directory; a copy interrupted after the manifest object lands leaves B2 with
        # `complete=true` sitting above a half-finished upload. The previous version of this
        # scanner reported that `fresh` (deploy#584: proven — `status=fresh dumps=1` over a
        # bucket with no user-postgres and no Neo4j, which restore.sh exits 1 on). Checking
        # the attestation is not the same as checking the artifact, and I had only done the
        # first while the comment above claimed the second.
        #
        # Bind to the RUN, not merely to the store names. The manifest declares
        # `timestamp=<TS>`, and every dump of that run is named `isnad-<store>-<TS>.*`. So the
        # producer names the run and the consumer verifies THAT RUN arrived intact. Store
        # names alone would be satisfied by a stale dump left behind by an earlier failed run
        # in the same day-directory — B2 accumulates those, because `rclone copy` adds and
        # never deletes, while the fixed-name manifest is overwritten by whichever ran last.
        #
        # THE REQUIRED SET IS THIS SCRIPT'S OWN, deliberately NOT read from the manifest's
        # `stores=` field — the same reasoning restore.sh states for REQUIRED_STORES. A set
        # taken from the artifact lets a partial backup declare itself
        # complete-for-what-it-happens-to-hold: perfect circularity, and invisible.
        local run_ts
        run_ts="$(printf '%s\n' "$manifest" | grep '^BACKUP_MANIFEST ' | tr ' ' '\n' \
            | sed -n 's/^timestamp=\(..*\)$/\1/p' | head -1)"
        if [[ -z "$run_ts" ]]; then
            # backup.sh always writes it. Absent ⇒ not a manifest we know how to verify.
            result incomplete manifest_no_timestamp "${dir_dumps["$dir"]}" -1 "$bucket_objects" "$undersized_total" "${dir:-/}"
            return $EXIT_ALERT
        fi

        local have=" ${dir_files["$dir"]:-} " missing=""
        [[ "$have" == *" isnad-pg-${run_ts}.dump "* ]]      || missing="${missing},isnad-pg"
        [[ "$have" == *" isnad-userpg-${run_ts}.dump "* ]]  || missing="${missing},isnad-userpg"
        [[ "$have" == *" isnad-neo4j-${run_ts}.dump.zst "* || "$have" == *" isnad-neo4j-${run_ts}.dump "* ]] \
            || missing="${missing},isnad-neo4j"
        if [[ -n "$missing" ]]; then
            log ERROR "attested run ${run_ts} is missing required store(s): ${missing#,}"
            log ERROR "  restore.sh REFUSES this artifact — it must not be reported fresh."
            result incomplete "missing_stores${missing}" "${dir_dumps["$dir"]}" -1 "$bucket_objects" "$undersized_total" "${dir:-/}"
            return $EXIT_ALERT
        fi

        chosen="$dir"
        chosen_epoch="${dir_newest["$dir"]}"
        break
    done

    if [[ -z "$chosen" && "$chosen_epoch" -eq 0 ]]; then
        # Dumps exist, but not one backup directory attests completeness.
        result incomplete no_complete_backup 0 -1 "$bucket_objects" "$undersized_total" -
        return $EXIT_ALERT
    fi

    [[ "$skipped" -gt 0 ]] && log WARNING "skipped ${skipped} backup director(ies) that do not attest complete=true"

    local now delta age_h dumps
    dumps="${dir_dumps["$chosen"]}"
    now="$(date -u +%s)"
    delta=$(( now - chosen_epoch ))

    # --- 4. A FUTURE timestamp is a broken clock, not a fresh backup. -------
    if [[ "$delta" -lt $(( -FUTURE_TOLERANCE_SECONDS )) ]]; then
        result instrument_error future_timestamp "$dumps" $(( delta / 3600 )) "$bucket_objects" "$undersized_total" "${chosen:-/}"
        return $EXIT_INSTRUMENT
    fi

    age_h=$(( delta / 3600 ))

    if [[ "$age_h" -gt "$MAX_AGE_HOURS" ]]; then
        result stale too_old "$dumps" "$age_h" "$bucket_objects" "$undersized_total" "${chosen:-/}"
        return $EXIT_ALERT
    fi

    result fresh - "$dumps" "$age_h" "$bucket_objects" "$undersized_total" "${chosen:-/}"
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
    _mkbackup() { # <dir> <complete> [dump-bytes] [run-ts]
        local d="$1" complete="$2" bytes="${3:-$(( MIN_DUMP_BYTES * 2 ))}" ts="${4:-$RUN_TS}"
        mkdir -p "$d"
        head -c "$bytes" /dev/zero > "${d}/isnad-pg-${ts}.dump"
        head -c "$bytes" /dev/zero > "${d}/isnad-userpg-${ts}.dump"
        head -c "$bytes" /dev/zero > "${d}/isnad-neo4j-${ts}.dump.zst"
        printf 'BACKUP_MANIFEST complete=%s stores=postgres,user-postgres,neo4j timestamp=%s category=daily\n' \
            "$complete" "$ts" > "${d}/_backup_manifest.txt"
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

    # INCOMPLETE — the artifact SAYS it is incomplete, and restore.sh refuses it. This is a
    # backup.sh output by design ("a partial backup beats none"), and it was reported fresh.
    _mkbackup "${tmp}/incomplete/daily/2026-07-11" false
    _expect incomplete-manifest "${tmp}/incomplete" "" incomplete "$EXIT_ALERT"

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
        "$RUN_TS" > "${tmp}/missing_store/daily/2026-07-11/_backup_manifest.txt"
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
    : > "${tmp}/nodumps/_backup_manifest.txt"
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

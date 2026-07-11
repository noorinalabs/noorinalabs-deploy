# Project Memory — noorinalabs-deploy

In-repo, version-controlled memory index for the deployment-orchestration repo. One
line per memory; the topic files (`.claude/memory/*.md`) are read on demand when a
line looks relevant. Seeded from `noorinalabs-main` in the org/repo memory split
(deploy#479, from main#740 / driver main#732) — these are the deploy-specific
memories that previously lived in the org-level corpus.

Some bodies carry `[[wikilinks]]` to org-level memories that stay in
`noorinalabs-main`; those are soft cross-repo pointers and may dangle here (this
repo imports only its own `.claude/memory/`).

- [Compose env change rollback safety](feedback_compose_env_change_rollback_safety.md) — compose config migration MUST keep old env form; rollback.yml rewrites only image tag. deploy#403.
- [compose up --wait: non-app service aborts rollout](feedback_compose_up_wait_non_app_abort.md) — prod compose up --wait health-gates WHOLE stack; unhealthy kafka → 521; tier the rollout.
- [db-migrate gate EXPECTED_MERGE_HEAD drift](feedback_db_migrate_gate_expected_head_drift.md) — gate fails on any compose PR when pin lags us stg-latest alembic head; pre-existing. PR#409.
- [stg deploy per-service tag routing](feedback_stg_deploy_per_service_tag_routing.md) — deploy-stg must route dispatch sha to ONLY that service's tag; shared IMAGE_TAG → wrong image. deploy#418.
- [CF plan doesn't validate expr; close-on-verified-live](feedback_cf_ruleset_apply_validation.md) — ruleset exprs validate at APPLY not plan; close after live-verify.
- [Ruleset empty required_status_checks 422](feedback_ruleset_empty_checks_422.md) — rulesets API 422s on empty checks; path-filtered-CI repos must OMIT the rule. #322.
- [Grafana forward_auth credential-carry](project_grafana_forward_auth.md) — SSO 3-repo; /grafana carries no credential (token localStorage-only). deploy#460.
- [Promote gate stg-verify refresh](project_promote_gate_stg_verify_refresh.md) — promote.yml v2 honors only workflow_run-triggered verify; refresh via deploy-stg. deploy#423.
- [Streaming E2E prereqs](project_streaming_e2e_prereqs.md) — Kafka pipeline E2E on stg deferred past P4W6 (#601 met via batch); dispatch-needs-main. deploy#443.
- [Staging Neo4j/frontend unreachable from sandbox](project_staging_unreachable_from_sandbox.md) — bolt/frontend resolve only in-cluster; ssh stg → cypher-shell or -L tunnel. da#73.
- [B2 preflight discriminator](reference_b2_preflight_discriminator.md) — rclone's text CANNOT tell missing-bucket from wrongly-scoped-key (identical msg); read-only 401 reads as "failed to create bucket". Classify by capability probe (lsd → canary write → canary delete), never by message. Never set RCLONE_DUMP. deploy#559.
- [errexit kills the assignment guard](feedback_errexit_kills_assignment_guard.md) — under `set -e`, `OUT="$(fn)"; RC=$?` is DEAD on every failing path. **RECURRED #584:** under `-e`+`pipefail`, ANY `VAR="$(grep/sed/find…)"` that legitimately finds NOTHING is a CRASH, not an empty string — needs `|| true`. A harness running prod code under a weaker `set -…` is not running prod code. deploy#563/#584.
- [stderr is COMMENTARY, not DATA](feedback_stderr_is_commentary_not_data.md) — `2>/dev/null`/`|| true` DISCARDS the diagnostic; `2>&1` PROMOTES it to DATA and **fires on the SUCCESS path** (tools write to stderr when nothing is wrong — rclone's config NOTICE became a "backup directory"). **The rule is about the CONSUMER, not the redirect:** fatal when the reader ITERATES every line as a record; harmless/required when it SEARCHES for a known token (pg_restore's "errors ignored" count). Variable or file is a red herring. deploy#584.
- [A paraphrase in the PRODUCT](feedback_paraphrase_in_the_product.md) — a paraphrase in a TEST goes green while the product breaks (findable); in the PRODUCT a producer-side rename does NOT break the parser, it QUIETLY NARROWS it → the guard downstream stops guarding, and the test stays green too. Anchor on the token you're extracting (strict run-id); never enumerate a charset for someone else's identifier. deploy#589.
- [Calibrate the mutation before counting it](feedback_calibrate_the_mutation_before_counting_it.md) — an INERT mutation is not evidence, kill or gap; an oracle that only says BLOCKED proves every guard holds. Baseline + positive control + prove each mutation effective. deploy#574.

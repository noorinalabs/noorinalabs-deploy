---
name: feedback_cf_ruleset_apply_validation
description: Cloudflare ruleset expressions validate at apply-time not plan-time; and close prod-apply-gated issues on verified-live not on merge
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 255c7ede-88bb-41ac-864c-67035ac0d582
---

Two coupled lessons from deploy#348 (P3W11 CF canonical-redirect import, 2026-05-24):

**1. A clean `terraform plan` does NOT validate Cloudflare ruleset expressions.** CF validates `target_url` / filter (wirefilter) expressions only at APPLY time, via the API. So plan-green + two-reviewer-green can still fail at apply. deploy#349's plan was clean (`2 import, 0 add, 2 change, 0 destroy`) but apply failed: the `target_url` used `if()`/`len()` (unsupported in CF's redirect expression language — "unknown identifier"). That expression came from the original #166 code and had never been API-validated because every prior apply died earlier at the "ruleset already exists" stage. Fix was `concat("https://noorinalabs.com", http.request.uri)` (`http.request.uri` = path+query together; no conditional needed).

**Why:** plan only checks TF-side schema/graph, not provider-API semantic validity of expression strings. **How to apply:** for CF ruleset / expression changes, treat APPLY as the validation gate — don't claim "verified" on a clean plan; sanity-check expressions against CF Rules-language docs (supported functions/fields) before apply; expect that the first real apply is where expression bugs surface. Sibling to [[feedback_runtime_gate_scoping]].

**2. For issues whose acceptance is a runtime/apply-time outcome (prod apply succeeds + live behavior), use `Refs #N` on the PR, NOT `Closes #N`; close the issue manually only after the live verification.** deploy#349 merged with "Closes #348" → #348 auto-closed on merge — but the apply then failed, leaving it closed-but-not-done. Had to reopen. The fix PR #350 used "Refs #348" and #348 was closed only after `curl` confirmed live 301s.

**Why:** `Closes #N` fires on default-branch merge, which is BEFORE the prod apply runs (apply is a separate, environment-gated main-push run). Merge ≠ live. **How to apply:** when an issue's real acceptance is a gated prod apply or live behavior, PR body says "Refs #N"; orchestrator closes #N after the post-merge apply + verification (e.g. curl). Extends [[feedback_wave_branch_issue_close]] (which is about wave-branch vs default-branch) to the runtime-acceptance dimension.

Bonus pattern: CF phase-entrypoint rulesets are named `"default"` (fixed convention) and `name` is ForceNew — adopting an existing entrypoint via `import {}` requires `name = "default"` in config, else the plan shows destroy+recreate (`-/+`, "2 to destroy") instead of in-place update.

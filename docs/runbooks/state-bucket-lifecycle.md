# Runbook — `noorinalabs-terraform-state` lifecycle policy

> **SPEC-OF-RECORD ONLY (as of #331).** The implementation now lives in
> [`terraform/backblaze-bootstrap/`](../../terraform/backblaze-bootstrap/README.md)
> (ADR 0004 Part-2): the `b2_bucket.terraform_state` resource declares the
> lifecycle rule below as a reviewable IaC artifact, and that module is the
> apply-path. This document remains the **canonical spec** the module's
> lifecycle block must match verbatim, and a **DR fallback** for applying the
> lifecycle by hand if the bootstrap module is unavailable. Keep the two in
> sync — if you change the rule here, change it in the module (and vice versa).

Source: [deploy#194](https://github.com/noorinalabs/noorinalabs-deploy/issues/194).
Composes with: [deploy#172](https://github.com/noorinalabs/noorinalabs-deploy/issues/172)
(SSE-B2 at-rest), [ADR 0004](../adr/0004-b2-state-bucket-and-key-management.md)
(IaC-management strategy), [`terraform/backblaze-bootstrap/`](../../terraform/backblaze-bootstrap/README.md)
(the implementation, #331).

## Why this console procedure is the DR fallback (not the primary path)

The `noorinalabs-terraform-state` bucket is the load-bearing root of every
Terraform apply in this repo — it holds the `*.tfstate` for `terraform/hetzner/`,
`terraform/cloudflare/`, and `terraform/backblaze/`. The bucket was created
out-of-band in the B2 console during initial repo bootstrap.

[ADR 0004 Decision A](../adr/0004-b2-state-bucket-and-key-management.md#decision-a--noorinalabs-terraform-state-bucket-iac-management)
accepts the chicken-and-egg problem (the bucket holds the state for the modules
that would otherwise manage it) and adopts **Option A2**: a
`terraform/backblaze-bootstrap/` root module with a `local` backend, executed
once-per-DR-event. **That module now exists (#331)** and is the primary
apply-path. The B2-console / `b2`-CLI procedure documented below is retained as
the **DR fallback** — use it to apply bucket-config (lifecycle, versioning,
object-lock, SSE-B2 toggle, etc.) by hand when the bootstrap module is
unavailable (e.g. mid-DR before the repo is cloned, or if the module's local
state is lost and not yet re-imported).

Adding a `b2_bucket` resource for the state bucket to the existing
`terraform/backblaze/main.tf` would be wrong: that module manages the
**pipeline bucket** (`noorinalabs-pipeline`) and its scoped keys; introducing
the state bucket there crosses domains and re-introduces the chicken-and-egg
without the bounded once-per-cycle posture that ADR 0004 chose to make it
acceptable.

## Canonical lifecycle spec

```json
[
  {
    "fileNamePrefix": "",
    "daysFromHidingToDeleting": 7,
    "daysFromUploadingToHiding": null
  }
]
```

Semantics:

- `daysFromHidingToDeleting: 7` — once a version is hidden (because a newer
  version was uploaded over it, or because `b2 rm` / hide-marker was applied),
  the hidden version is permanently deleted after 7 days.
- `daysFromUploadingToHiding: null` — never auto-hide based on upload age.
  The current version of every `*.tfstate` object stays accessible forever;
  only previously-superseded versions are aged out.
- `fileNamePrefix: ""` — applies to the entire bucket (all `hetzner/*.tfstate`,
  `cloudflare/*.tfstate`, `backblaze/*.tfstate` keys).

### Rationale for 7 days

- **Recovery window.** 7 days is long enough that an operator who notices a
  bad `terraform apply` within a normal on-call rotation can still pull the
  prior version via `terraform state pull` against the pre-apply object.
- **Bounded plaintext-history accumulation.** State files contain
  infrastructure-shaped metadata (resource IDs, IPs, tags). Pre-#172, the
  bucket had 13 prior versions across 3 path families, each retaining the
  same secret-ish content. SSE-B2 (#172) protects at-rest but doesn't bound
  retention; the lifecycle rule does.
- **No conflict with active applies.** The rule only acts on **hidden**
  versions. A version becomes hidden when a newer one is written; the
  current head of every key remains unaffected indefinitely.

## Apply procedure (B2 console)

This is operator-only — there is no CI path until the `backblaze-bootstrap/`
module ships per ADR 0004.

1. Log in to the [B2 console → Buckets](https://secure.backblaze.com/b2_buckets.htm).
2. Click **`noorinalabs-terraform-state`** → **Lifecycle Settings**.
3. Pick **Use custom lifecycle rules**.
4. Configure a single rule with:
   - **File name prefix:** *(empty — applies to entire bucket)*
   - **Days from hiding to deleting:** `7`
   - **Days from uploading to hiding:** *(leave blank / not set)*
5. **Save**.

### Equivalent via `b2` CLI

If the operator has the [b2 CLI](https://www.backblaze.com/docs/cloud-storage-command-line-tools)
installed and authenticated with the master key:

```bash
b2 update-bucket noorinalabs-terraform-state allPrivate \
  --lifecycleRules '[{"fileNamePrefix":"","daysFromHidingToDeleting":7,"daysFromUploadingToHiding":null}]'
```

## Verification

After applying via either path:

```bash
b2 bucket get noorinalabs-terraform-state | jq .lifecycleRules
```

Expected output:

```json
[
  {
    "fileNamePrefix": "",
    "daysFromHidingToDeleting": 7,
    "daysFromUploadingToHiding": null
  }
]
```

## Acceptance over time

- **T+0 (apply):** `b2 bucket get` returns the rule above.
- **T+1 apply cycle:** the next `terraform apply` against any backend
  (`hetzner/stg`, `hetzner/prod`, `cloudflare`, `backblaze`) hides the prior
  version of the relevant `*.tfstate` object — visible via
  `b2 ls --versions noorinalabs-terraform-state hetzner/stg.tfstate`
  (look for an `action: hide` entry).
- **T+7 days (per object):** any version hidden by a subsequent apply is
  permanently removed. Re-running `b2 ls --versions` shows only the current
  head plus any not-yet-7-day-old hidden versions.

## The `backblaze-bootstrap/` module (landed — Part-2 of #180, #331)

The `b2_bucket "terraform_state"` resource in
[`terraform/backblaze-bootstrap/`](../../terraform/backblaze-bootstrap/) declares
a `lifecycle_rules` block matching the spec above verbatim:

```hcl
lifecycle_rules {
  file_name_prefix             = ""
  days_from_hiding_to_deleting = 7
  # days_from_uploading_to_hiding intentionally omitted — keep current
  # versions accessible forever; only age out hidden (superseded) versions.
}
```

The bootstrap module is now the **apply-path** (operator-run, once-per-cycle —
see its README); this runbook is the **spec-of-record** (the rule the module
must match) and the **DR fallback** (the § "Apply procedure (B2 console)" and
§ "Equivalent via `b2` CLI" steps above, for applying the lifecycle by hand if
the module is unavailable). If you change the rule, change it in BOTH places.

## Parent-ontology coupling

The parent `noorinalabs-main:ontology/repos/deploy.yaml` § `state_backend`
entry currently records `{ type: s3, bucket: noorinalabs-terraform-state,
endpoint: backblaze-b2, region: us-east-005 }`. Once this rule is applied,
that entry should grow a `lifecycle: { days_from_hiding_to_deleting: 7 }`
sub-field so the parent ontology reflects the bucket's real configuration.
Orchestrator follow-up (cross-repo; not in this PR's scope).

## Refs

- [deploy#194](https://github.com/noorinalabs/noorinalabs-deploy/issues/194) — this issue.
- [deploy#172](https://github.com/noorinalabs/noorinalabs-deploy/issues/172) — SSE-B2 verification (the at-rest control this composes with).
- [ADR 0004](../adr/0004-b2-state-bucket-and-key-management.md) — IaC-management strategy that makes this a runbook (today) and a `backblaze-bootstrap` resource block (after Part-2 lands).
- [deploy#180](https://github.com/noorinalabs/noorinalabs-deploy/issues/180) — parent ADR issue (Part-2 implementation follow-up to be filed).

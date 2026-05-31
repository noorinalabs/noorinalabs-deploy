# Runbook — Per-env OAuth provisioning (Google + GitHub)

**Scope:** how to provision and rotate OAuth provider apps (Google Cloud OAuth client + GitHub OAuth app) for each deployment environment, and how the four `AUTH_*` secrets resolve at deploy time.

**NOT in scope:**
- The user-service OAuth flow itself (handler code, callback parsing) — see `noorinalabs-user-service` repo.
- JWT signing keys — see `user-service-alembic.md` and the `JWT_PRIVATE_KEY`/`JWT_PUBLIC_KEY` custodial paths in `~/.ssh/`.
- Frontend redirect logic post-callback — see deploy#248 and the user-service auth router.

## The convention

**Each deployment environment gets its own Google OAuth client and its own GitHub OAuth app. They are never shared across environments.** The four secrets resolving the credentials live at GitHub Environments env-scope, not org-scope, so each env's deploy step reads only its own env's values.

The four secret names are identical across envs (env-scope, not name-scope, encodes which env you're in):

| Secret name | Provider | Role |
|---|---|---|
| `AUTH_GOOGLE_CLIENT_ID` | Google | OAuth client ID, public, surfaces in browser |
| `AUTH_GOOGLE_CLIENT_SECRET` | Google | OAuth client secret, server-side only |
| `AUTH_GITHUB_CLIENT_ID` | GitHub | OAuth app client ID, public |
| `AUTH_GITHUB_CLIENT_SECRET` | GitHub | OAuth app client secret, server-side only |

## Why per-env, not shared

1. **Google publishing-status is per-app.** Prod must run `In Production` (consent screen reviewed, public). Stg should run `Testing` (no review needed, fast iteration, only allowlisted test users can log in). The same app cannot be in both modes.
2. **Credential isolation.** A leaked stg credential (CI logs, dev laptop, transcript paste — see 2026-05-02 emergency restore session) does not affect prod login. Separate `CLIENT_ID`/`CLIENT_SECRET` per env makes this guarantee structural, not procedural.
3. **Metrics & quota separation.** Prod login analytics are not polluted by stg test traffic. Rate-limit buckets are independent. Revoking a stg secret does not page prod-on-call.
4. **Redirect URI hygiene.** Each app's allowed redirect URI list contains only its env's callback URL — no shared wildcard.

## Redirect URI conventions

User-service's OAuth callback path is `/auth/oauth/{provider}/callback` (`POST`, see `ontology/services.yaml` user-service entry). The full callback URL per env follows the `users.{base}` subdomain convention (deploy#241):

| Env | Base | Google authorized redirect URI | GitHub authorization callback URL |
|---|---|---|---|
| Production | `noorinalabs.com` | `https://users.noorinalabs.com/auth/oauth/google/callback` | `https://users.noorinalabs.com/auth/oauth/github/callback` |
| Staging | `stg.noorinalabs.com` | `https://users.stg.noorinalabs.com/auth/oauth/google/callback` | `https://users.stg.noorinalabs.com/auth/oauth/github/callback` |
| Future env (e.g., `dev`, `canary`) | `<env>.noorinalabs.com` | `https://users.<env>.noorinalabs.com/auth/oauth/google/callback` | `https://users.<env>.noorinalabs.com/auth/oauth/github/callback` |

**Only the env's own callback URL goes in that env's app.** Do not mix prod and stg URIs in one app — that re-creates the shared-app posture this runbook exists to prevent.

> **Adding a new OAuth provider:** follow the same per-env pattern — provision one provider app per env, set `AUTH_<PROVIDER>_CLIENT_ID` + `AUTH_<PROVIDER>_CLIENT_SECRET` at the env-scope for each env, and add the callback URI to the redirect URI conventions table above.

## Provisioning a new env (4 steps)

Follow when adding any new env (e.g., `dev` between stg and prod, or a `canary` post-prod). Each step is owner-only because OAuth provider consoles require interactive auth that is out-of-band of CI.

### 1. Provision the Google OAuth client

1. Go to [Google Cloud Console → APIs & Services → Credentials](https://console.cloud.google.com/apis/credentials) for the `noorinalabs` project.
2. Click `Create Credentials → OAuth client ID`. Application type: `Web application`. Name: `noorinalabs-<env>` (e.g., `noorinalabs-stg`).
3. **Authorized JavaScript origins:** `https://users.<env-base>` (no trailing slash).
4. **Authorized redirect URIs:** `https://users.<env-base>/auth/oauth/google/callback` (one entry only).
5. Save. Copy the displayed `Client ID` and `Client Secret` — the secret is only shown once.

**Publishing status:**

- For **prod**: configure the OAuth consent screen with full branding (app name, logo, support email, terms-of-service URL, privacy-policy URL), submit for verification, switch to `In Production` once approved. Required scopes — only `openid`, `email`, `profile` — do not require sensitive-scope review, so verification is typically same-week.
- For **stg / dev / canary**: leave the consent screen in `Testing` mode. Add allowlisted test-user emails (owner + any teammates needing OAuth access). No verification needed. Only allowlisted users can OAuth into a `Testing`-mode app.

### 2. Provision the GitHub OAuth app

1. Go to [GitHub → Settings → Developer settings → OAuth Apps](https://github.com/settings/developers) (under the `noorinalabs` org if you have org-app permissions, otherwise under your personal account is acceptable for stg-class envs).
2. Click `New OAuth App`. Application name: `noorinalabs-<env>` (e.g., `noorinalabs-stg`).
3. **Homepage URL:** `https://users.<env-base>`.
4. **Authorization callback URL:** `https://users.<env-base>/auth/oauth/github/callback` (one entry — GitHub allows only one callback URL per app, which is part of why per-env apps are mandatory).
5. Generate a client secret. Copy the `Client ID` and the freshly generated `Client Secret`.

GitHub OAuth apps have no `Testing`/`Production` distinction — the only switch is `public` vs `private` (irrelevant for this app class). All envs use the same shape; only the callback URL differs.

### 3. Set the four env-scope secrets

The `<env-name>` below is the GitHub Environments name (`staging` or `production` today; lowercase env name for any future env added via `gh api repos/.../environments`):

```bash
gh secret set AUTH_GOOGLE_CLIENT_ID     --repo noorinalabs/noorinalabs-deploy --env <env-name>
gh secret set AUTH_GOOGLE_CLIENT_SECRET --repo noorinalabs/noorinalabs-deploy --env <env-name>
gh secret set AUTH_GITHUB_CLIENT_ID     --repo noorinalabs/noorinalabs-deploy --env <env-name>
gh secret set AUTH_GITHUB_CLIENT_SECRET --repo noorinalabs/noorinalabs-deploy --env <env-name>
```

`gh secret set` will prompt for the value on stdin — paste the value from steps 1 and 2.

Verify all four are set (names only — values are write-only):

```bash
gh secret list --repo noorinalabs/noorinalabs-deploy --env <env-name> | grep AUTH_
```

Expect 4 rows. If any are missing, the user-service container will boot with empty `AUTH_*_CLIENT_ID/SECRET` env vars and the OAuth flow will return a generic 500 from the provider on first attempted login.

### 4. Redeploy the env

Env-scope secrets are read by the deploy workflow at SSH-step time and written to `/opt/noorinalabs-deploy/.env` on the VPS. The user-service container reads them at container start. So a redeploy is required for the new credentials to take effect:

```bash
# stg auto-deploys on push to wave branch via repository_dispatch
# but a manual run can be forced:
gh workflow run deploy-stg.yml --repo noorinalabs/noorinalabs-deploy

# prod requires manual approval through the production GH Environment.
# Default invocation: promote.yml resolves stg-latest at plan-time and threads
# the resolved sha-<short> through to retag — no input needed.
gh workflow run promote.yml --repo noorinalabs/noorinalabs-deploy

# Or pin to a specific immutable SHA (rollback / specific-build promotion):
gh workflow run promote.yml --repo noorinalabs/noorinalabs-deploy -f source_sha=<short-sha>
```

**Do not pass `-f source_sha=stg-latest` or any other floating-tag string.** The `promote-no-floating-source` static guard added by [deploy#260](https://github.com/noorinalabs/noorinalabs-deploy/pull/260) (cold-rebuild dryrun) catches floating-source values as a regression. `source_sha` must be an immutable `sha-<short>` (or bare `<short>`) — leaving it empty is the correct way to mean "whatever stg-latest currently points at."

After the redeploy completes, smoke-test the OAuth flow end-to-end on the env's frontend:

```bash
# Replace <env-base> with the env's base domain (e.g., stg.noorinalabs.com)
curl -sSI "https://users.<env-base>/auth/oauth/google/login"   # expect 302 to accounts.google.com
curl -sSI "https://users.<env-base>/auth/oauth/github/login"   # expect 302 to github.com/login/oauth/authorize
```

Then complete the flow in a real browser — `redirect_uri_mismatch` from the provider means step 1 or 2's allowed redirect URI is wrong; check it character-for-character against the table in §Redirect URI conventions.

## Rotating credentials for an existing env

Steps are the same as §3 above, except you re-issue the secret in the OAuth provider console first:

- **Google:** Credentials → click the OAuth client → `Reset secret`. Old secret becomes invalid immediately. Copy the new value, re-run `gh secret set` for `AUTH_GOOGLE_CLIENT_SECRET` only, redeploy. The `Client ID` is unchanged.
- **GitHub:** OAuth Apps → click the app → `Generate a new client secret`. The old secret continues to work for 7 days (GitHub's grace period) — useful for zero-downtime rotation. Copy the new value, re-run `gh secret set` for `AUTH_GITHUB_CLIENT_SECRET` only, redeploy.

`CLIENT_ID` is public and only ever rotates if you delete and recreate the entire app — which is the same procedure as §Provisioning above.

## Current state (2026-05-03)

| Scope | `AUTH_*_CLIENT_ID/SECRET` set? | Notes |
|---|---|---|
| Org-scope (`noorinalabs` org, `vis=selected`) | yes — 4 secrets, set 2026-04-26 | Prod fallback. The 4 prod-app values live here today. |
| `staging` env-scope | yes — 4 secrets, set 2026-05-02 | Separate stg OAuth app per env. Overrides org-scope at deploy. |
| `production` env-scope | empty | Prod resolves from org-scope fallback. |

Both envs run their own Google + GitHub OAuth apps end-to-end (provisioning verified by owner 2026-05-03 — see [deploy#244](https://github.com/noorinalabs/noorinalabs-deploy/issues/244#issuecomment-4366806318)). The remaining row is whether prod's secrets stay at org-scope (current) or move to env-scope.

### Optional cleanup — move prod secrets org-scope → env-scope

This is **owner-action** (GitHub secret values are write-only — Aisha cannot read existing org-scope values to copy them). Recommended to schedule as a 5-minute owner step:

```bash
# Owner re-pastes the four prod-app values (from Google Cloud Console + GitHub OAuth App console — same values that were
# pasted at org-scope on 2026-04-26):
gh secret set AUTH_GOOGLE_CLIENT_ID     --repo noorinalabs/noorinalabs-deploy --env production
gh secret set AUTH_GOOGLE_CLIENT_SECRET --repo noorinalabs/noorinalabs-deploy --env production
gh secret set AUTH_GITHUB_CLIENT_ID     --repo noorinalabs/noorinalabs-deploy --env production
gh secret set AUTH_GITHUB_CLIENT_SECRET --repo noorinalabs/noorinalabs-deploy --env production

# Verify production env-scope is now non-empty:
gh secret list --repo noorinalabs/noorinalabs-deploy --env production | grep AUTH_   # expect 4 rows

# Then delete the org-scope copies (the env-scope values now resolve directly, no fallback needed):
gh secret delete AUTH_GOOGLE_CLIENT_ID     --org noorinalabs
gh secret delete AUTH_GOOGLE_CLIENT_SECRET --org noorinalabs
gh secret delete AUTH_GITHUB_CLIENT_ID     --org noorinalabs
gh secret delete AUTH_GITHUB_CLIENT_SECRET --org noorinalabs

# Verify org-scope is empty:
gh secret list --org noorinalabs | grep AUTH_   # expect zero rows
```

Redeploy prod (`promote.yml` with manual approval) to confirm env-scope resolution works end-to-end before deleting org-scope. Order matters — if org-scope is deleted before env-scope is verified, the prod OAuth flow breaks until env-scope is populated.

**Why bother:** symmetry with stg (both envs resolve from env-scope, not a mix of env+org), and an empty org-scope row that future `AUTH_*` audits don't have to reason about. The current org-scope-fallback posture is correct and working — this cleanup is hygiene, not a bug fix.

## Failure modes and recovery

### 1. `redirect_uri_mismatch` from Google or GitHub

Provider console's allowed redirect URI list does not contain the URL the user-service sent. Cause: env's OAuth app was provisioned with a wrong/stale URI, or someone rotated the env's base domain without updating the app.

**Recovery:**

1. Inspect the failing request — Google returns the offending URI in the redirect URL's `error_description`. GitHub returns it in the body.
2. Open the OAuth provider's app settings (Google Cloud Console → Credentials, or GitHub → OAuth Apps).
3. Add the correct URI per §Redirect URI conventions. Save. The change is effective within ~30 seconds for both providers.
4. Retry the OAuth flow — no redeploy needed, this is a provider-side change.

### 2. Login returns 500 from user-service immediately

Most common cause: env's `AUTH_*` secrets are empty. The user-service starts but emits a generic 500 when the provider redirects back because `CLIENT_SECRET` is the empty string and the token-exchange POST returns `invalid_client`.

**Recovery:**

1. `gh secret list --repo noorinalabs/noorinalabs-deploy --env <env-name> | grep AUTH_` — expect 4 rows. If org-scope fallback is in play (production today): also check `gh secret list --org noorinalabs | grep AUTH_` returns 4 rows.
2. If any are missing, redo §3 for the missing names.
3. Redeploy the env. The container reads the env vars at start, not per-request.

### 3. Stg test users can log in but get "App is not verified"

Google's `Testing` mode posts a generic warning interstitial before redirecting back. This is expected for stg/dev/canary and is the price of skipping verification. If a teammate complains, add their email to the test-user allowlist in Google Cloud Console (`OAuth consent screen → Test users → Add users`).

If the warning is reaching prod users: the prod app is not in `In Production` status. Re-check Google Cloud Console — verification may have been revoked, or the consent screen was edited after publication (which kicks the app back to `Testing`).

### 4. Prod app suddenly demands re-verification

Google occasionally requires re-verification when sensitive scopes change or the policy URL becomes unreachable. Recover by:

1. Checking Google Cloud Console for the OAuth client's status.
2. Confirming the privacy-policy and terms-of-service URLs in the consent screen still 200 (`curl -sSI <url>`).
3. Re-submitting for verification. Until approved, prod logs in via the stg-class `Testing` interstitial — degraded UX but not broken.

### 5. `invalid_scope` from Google or GitHub

User-service requested a scope the OAuth app's allow-list does not cover — e.g., user-service code added `https://www.googleapis.com/auth/userinfo.email` but the app was provisioned with only `openid`. The provider returns `invalid_scope` with the offending scope name in `error_description` (Google) or in the redirect body (GitHub).

**Recovery:** open the OAuth app's settings (Google: `OAuth consent screen → Scopes for Google APIs → Add or remove scopes`; GitHub: scopes are requested per-call, no console-side allow-list — symptom there means user-service is requesting a scope the provider doesn't recognize). Add the missing scope or correct the user-service request. Save. No redeploy needed for the provider-side change; user-service code change requires a redeploy.

### 6. User session ends mid-flow / 401 with `invalid_grant`

The provider revoked the refresh token. Triggers vary by provider:

- **Google:** user changed their Google password, user revoked grant via [myaccount.google.com → Security → Third-party access](https://myaccount.google.com/permissions), admin policy revoked, or the OAuth app was deleted/recreated.
- **GitHub:** user revoked via Settings → Applications → Authorized OAuth Apps, or the env's `CLIENT_SECRET` was rotated past the 7-day grace window (see §Rotating credentials).

Symptom: user-service emits `401 invalid_grant` on the token-refresh exchange; the user is forced back to the login button.

**Recovery is per-user:** the user re-completes the OAuth flow from the SPA. No on-call action for individual occurrences. **If a fleet-wide pattern emerges** (multiple users hit `invalid_grant` in a short window), check whether the env's `AUTH_GITHUB_CLIENT_SECRET` was rotated without honoring the 7-day grace window — a same-day double-rotate invalidates all in-flight tokens. Cross-reference §Rotating credentials.

### 7. Prod OAuth broken right after the org→env-scope cleanup

If the optional cleanup (§"Optional cleanup — move prod secrets org-scope → env-scope") was executed and prod login broke immediately after, the org-scope row was deleted before the `production` env-scope row was populated and verified. This is the order-of-operations hazard called out at §"Optional cleanup" — calling it out here too because §Failure modes is where on-call greps first.

**Recovery:**

1. Re-set the four `AUTH_*` secrets at `production` env-scope (`gh secret set ... --env production` per §"Optional cleanup" recipe). The owner is the only one who can do this — see §Escalation row "OAuth `AUTH_*` cleanup ordering issue."
2. Redeploy prod via `promote.yml`.
3. Verify the OAuth flow recovers end-to-end before declaring resolved.

## Escalation

| Failure | Primary | Secondary | Tertiary |
|---|---|---|---|
| `redirect_uri_mismatch` | Aisha.Idrissi (SRE — owns this runbook) | Lucas.Ferreira (SRE) | — |
| 500 from user-service after OAuth callback | Aisha.Idrissi | Anya.Kowalczyk (user-service) | — |
| `invalid_scope` from provider | Aisha.Idrissi | Anya.Kowalczyk (user-service — owns scope-request code) | — |
| Fleet-wide `invalid_grant` post-rotation | Aisha.Idrissi | Lucas.Ferreira | Nino.Kavtaradze (Security) |
| Google verification revoked / consent screen broken | Aisha.Idrissi (self-resolve **once** the `roles/oauthconfig.editor` binding is live — see Pre-staged IAM binding note below; until then owner liaison only) | Lucas.Ferreira / Bereket.Tadesse (deploy-team-on-call, same binding) | parametrization (owner — sole Google Cloud project admin; fallback while binding is pending or for project-IAM changes) |
| OAuth `AUTH_*` cleanup ordering issue (org→env-scope) | parametrization (owner — only one who has the secret values) | Aisha.Idrissi (procedural support) | — |
| Suspected leaked secret | Nino.Kavtaradze (deploy-team Senior Security Engineer — see `roster/security_engineer_nino.md`) | Bereket.Tadesse (IM) | — |

**Roster disambiguation:** `Nino.Kavtaradze` is the deploy team's Senior Security Engineer (`noorinalabs-deploy/.claude/team/roster/security_engineer_nino.md`) — escalation-class for OAuth-incident scope. Reviewers from outside the deploy team may not recognize the name; the roster file is the source of truth.

### Pre-staged IAM binding (Google verification revoked row) — deploy#269

This row used to be a hard single point of failure: only the owner held Google
Cloud admin, so a verification-revocation or consent-screen break was
owner-blocked until the owner was reachable. deploy#269 removes that SPOF by
pre-staging an IAM binding that lets a deploy-team teammate self-resolve
OAuth-app emergencies (verification revoked, consent screen broken,
`invalid_scope` allow-list edit) directly in the console.

**Target posture (once the binding is live):** Aisha (primary), Lucas / Bereket
(secondary) hold `roles/oauthconfig.editor` on the `noorinalabs` Google Cloud
project and can edit the OAuth client + OAuth consent screen without
owner-roundtrip. The owner remains the fallback for project-IAM changes and
while the binding is pending.

**Binding shape — per-teammate Google account (recommended, shape (a)):** bind
the role to each teammate's Google identity rather than minting a shared
service-account key. Shape (b) (service account + key via `gh secret`) is
**deliberately rejected** here: piping a shared admin credential through a
secret re-introduces the exact shared-key blast-radius that the per-env
OAuth-app discipline (this whole runbook) exists to avoid. Per-account bindings
add no new credential surface.

**Owner grant recipe (the one owner-gated step).** Run once per teammate from a
shell authenticated as a project admin (`gcloud auth login` as owner):

```bash
PROJECT=noorinalabs   # the Google Cloud project id holding the OAuth apps
for acct in \
  parametrization+Aisha.Idrissi@gmail.com \
  parametrization+Lucas.Ferreira@gmail.com \
  parametrization+Bereket.Tadesse@gmail.com ; do
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="user:${acct}" \
    --role="roles/oauthconfig.editor"
done
# Confirm the bindings landed:
gcloud projects get-iam-policy "$PROJECT" \
  --flatten="bindings[].members" \
  --filter="bindings.role=roles/oauthconfig.editor" \
  --format="table(bindings.members)"
```

> `roles/oauthconfig.editor` grants edit on the OAuth client + brand/consent
> screen only — it does NOT grant broader project IAM, billing, or compute
> admin. It is the minimal role for this self-resolve scope.

**Acceptance gate — do NOT mark this SPOF closed until live-verified.** IAM-list
confirmation (the `get-iam-policy` table above) is necessary but NOT
sufficient. The bound teammate must perform an end-to-end edit:

1. Open the OAuth **client** settings in Google Cloud Console and make a
   no-op-safe edit (e.g. re-save an authorized redirect URI) — confirm Save
   succeeds, no permission error.
2. Open the **OAuth consent screen** and confirm Scopes / branding fields are
   editable and saveable.

Record the verification date + teammate in this section when done.

**Current status (as of deploy#269 landing):** binding **pending owner grant** —
the recipe above is staged but not yet applied, and the live edit-capability
test has not been run. Until both are complete, the row above is still
owner-blocked in practice and Aisha/Lucas/Bereket act as owner liaison only.
This is honest framing — flip the Escalation-row primary to true self-resolve
only after the live verification lands.

## Refs

- Issue: [deploy#244](https://github.com/noorinalabs/noorinalabs-deploy/issues/244) (this runbook closes the docs sub-task).
- SPOF remediation: [deploy#269](https://github.com/noorinalabs/noorinalabs-deploy/issues/269) — pre-staged `roles/oauthconfig.editor` binding (§"Pre-staged IAM binding" above).
- Surfacing PR: [deploy#241](https://github.com/noorinalabs/noorinalabs-deploy/pull/241) — `users.{base}` vhost carve-out, where the redirect URI convention was first established.
- Charter codification: tracked in `noorinalabs-main` (see PR body for follow-up issue link).
- Adjacent secret patterns: `user-service-alembic.md` § Environment protection (env-scope `USER_POSTGRES_*`, `JWT_*`).

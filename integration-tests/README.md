# Cross-Repo Integration Tests

End-to-end validation of the user-service extraction across
`noorinalabs-user-service`, `noorinalabs-isnad-graph`, and `noorinalabs-deploy`.

Lives in this repo (`noorinalabs-deploy`) because it exercises the full
production stack — the same docker-compose topology that
`compose/docker-compose.prod.yml` deploys to the VPS.

Part of [noorinalabs-main#49](https://github.com/noorinalabs/noorinalabs-main/issues/49).

## What this covers

| Scenario | Status | Notes |
|----------|--------|-------|
| Token issuance (auth-code grant) → isnad-graph API access | Covered | Seeds auth code via Redis shim (bypasses provider HTTP exchange) |
| Token refresh across service boundary | Covered | `/auth/token/refresh` → rotated refresh token |
| Session management through user-service | Covered | Create / list / revoke sessions |
| RBAC on isnad-graph endpoints | Covered | Admin-role JWT → `/admin/*`; non-admin → 403 |
| Subscription enforcement on premium features | Covered | Free-tier JWT on premium endpoint → 402/403; paid → 200 |
| Email verification flow | Covered | `/api/v1/verification` issue → confirm |
| 2FA login flow | Covered | TOTP setup → verify → login |
| Health checks | Covered | All services `/health` probed before tests run |
| Network isolation (user-postgres unreachable from isnad-graph) | Covered | In-container TCP probe |
| Performance baseline (auth < 100ms) | Covered | Measured in `test_performance.py` |

### Scenarios deliberately stubbed / deferred

| Scenario | Reason | Follow-up |
|----------|--------|-----------|
| Real OAuth provider code-exchange | Hardcoded provider URLs in `src/app/services/oauth.py`; needs a `OAUTH_PROVIDER_BASE_URL` override before a fake-provider container can sit in front | noorinalabs-main#135 |
| Pipeline worker scenarios | Pipeline (#105–#108) not yet live on wave-9 at the time this harness was written | noorinalabs-main#136 |

## Run modes

The harness supports two run modes via `RUN_MODE`:

| Mode | Default? | What it does |
|------|----------|--------------|
| `hermetic` | yes | Local docker-compose stack, builds from sibling checkouts, full DB+Redis seeding via `seeded_user_factory`. Used by PR CI. |
| `remote` | no | Skip stack-up; run pytest against env-supplied stg URLs. DB/Redis-direct fixtures auto-skip. With a provisioned stg test-user (see below), the remote-runnable set is health + JWKS **plus** the auth/session/RBAC-negative/free-tier/validate-latency flows reachable via the `auth_session` fixture (deploy#204). |

### Remote-mode coverage beyond health (deploy#204)

`seeded_user_factory` shapes an arbitrary user (roles, subscription,
verification state) by writing directly to the local DB+Redis — impossible
in remote mode (stg DB/Redis are not reachable from outside the VPS, and
**user-service exposes no password-login / test-seed / dev-bypass
endpoint**: the only token-minting paths are the Redis-seeded `/auth/token`
one-time code and the real OAuth callback, neither CI-drivable against stg).

The CI-safe, HTTP-only lever that *does* exist is `/auth/token/refresh`. A
long-lived refresh token for a **pre-provisioned, stable stg test-user** is
exchanged for a fresh access+refresh pair without any DB access. Tests that
need *an* authenticated user — but not a specific role / subscription tier /
verification state — use the mode-aware **`auth_session`** fixture:

* hermetic → seeds a fresh free-tier user DB-direct, exactly as before.
* remote → returns the stg test-user's freshly-rotated token pair.

Promoted to remote-runnable (run in both modes):

| Test | What it asserts |
|------|-----------------|
| `test_auth_flow::test_auth_code_grants_jwt_that_isnad_graph_accepts` | cross-service JWT acceptance |
| `test_auth_flow::test_refresh_token_rotation` | refresh rotation + old-token revocation |
| `test_auth_flow::test_token_validation_endpoint` | `/auth/token/validate` shape |
| `test_sessions::test_session_lifecycle` | session create / list / revoke |
| `test_rbac::test_non_admin_jwt_lacks_admin_role` | admin-role absent (negative control) |
| `test_subscription::test_free_tier_not_promoted_to_premium` | free tier stays free |
| `test_performance::test_token_validate_latency_baseline` | validate latency baseline |

Intentionally **hermetic-only** (need DB-direct shaping a single fixed
test-user cannot provide, or mutate durable account state unsafe to repeat
on the shared stg user — each carries an explicit `pytest.skip` with the
reason):

| Test | Why hermetic-only |
|------|-------------------|
| `test_rbac::test_admin_jwt_carries_admin_role_across_boundary` | needs an admin-role user |
| `test_subscription::test_subscription_status_reflected_in_jwt` | needs an active paid subscription |
| `test_subscription::test_trial_start_flow` | consumes the user's one-time trial (durable) |
| `test_2fa::*` | enrolls durable TOTP state on the user |
| `test_verification::test_verification_issue_and_confirm` | needs `email_verified=False` shaping |
| `test_auth_flow::test_oauth_callback_to_token_issue` | needs `fake_oauth` container (hermetic-only) |
| `test_performance::test_token_issuance_latency_baseline` | mints 20 one-time codes (DB-direct) |
| `test_network_isolation::*` | topology assertion, not a runtime one |

> **Why not more?** Closing the remaining hermetic-only gap requires either
> a test-only seeding endpoint in user-service (a new auth-bypass surface —
> out of scope for a deploy-repo change and a security decision for the
> user-service team) or direct stg DB access (deliberately not exposed).
> Tracked as the natural follow-up to deploy#204.

#### Provisioning the stg test-user (runbook)

The remote-promoted tests are **runtime-gated**: they skip cleanly unless
`STG_TEST_USER_REFRESH_TOKEN` is present (so local dev and hermetic PR-CI
are unaffected). To enable them against stg:

1. **Create a stable test-user** in stg user-service via a one-time real
   OAuth login (Google) with a dedicated test mailbox, e.g.
   `integration-test@stg.noorinalabs.com`. It is created as a plain
   free-tier, non-admin, email-verified account — do **not** grant it admin
   or a paid subscription (the hermetic-only tests above rely on it staying
   plain).
2. **Capture its refresh token.** The OAuth callback sets the refresh token
   as an httpOnly cookie and returns the access token on the redirect; grab
   the refresh token from the `Set-Cookie: refresh_token=…` header of the
   `/auth/oauth/google/callback` 302 (path-scoped to `/auth`).
3. **Store secrets** on the `integration-tests` workflow's stg environment:
   - `STG_TEST_USER_REFRESH_TOKEN` — the captured refresh token.
   - `STG_TEST_USER_EMAIL` — the test-user's email (enables the
     `token/validate` email-equality assertion; optional but recommended).
4. **Rotation.** `/auth/token/refresh` rotates the refresh token on every
   call, but the test only consumes the secret as a *bootstrap* seed for the
   first hop in a run, so a single captured token survives many runs until
   user-service expires it (`JWT_REFRESH_TOKEN_EXPIRE_DAYS`). When the
   exchange starts failing, repeat steps 1–3 to re-provision.

### Running locally — hermetic (PR-CI default)

```bash
cd integration-tests
./run-tests.sh
```

This expects `noorinalabs-user-service` and `noorinalabs-isnad-graph` to be
checked out as siblings to this repo (same layout as `noorinalabs-main/` uses).

The script will:
1. Build and start the test stack via `docker-compose.test.yml`
2. Wait for all health checks to pass
3. Run the pytest suite inside the `test-runner` container
4. Tear down the stack on exit (pass `KEEP_STACK=1` to leave it up for debugging)

### Running remotely against stg

```bash
cd integration-tests
RUN_MODE=remote \
  ISNAD_BASE_URL=https://isnad.stg.noorinalabs.com \
  USER_SERVICE_BASE_URL=https://auth.stg.noorinalabs.com \
  ./run-tests.sh
```

Remote mode:
- Refuses to run against `ENVIRONMENT=prod` (hard guardrail — refuses with
  exit code 3). Defaults `ENVIRONMENT=stg` if unset.
- Requires `ISNAD_BASE_URL` and `USER_SERVICE_BASE_URL` (both validated up
  front via /health probes).
- Builds the runner image and `docker run`s it directly — no compose
  stack-up, no sibling-repo checkouts needed.
- Auto-skips tests that depend on `user_pg`, `user_redis`, or
  `seeded_user_factory` because direct stg DB/Redis access is not
  available from outside the VPS.
- With a provisioned stg test-user (`STG_TEST_USER_REFRESH_TOKEN`), the
  `auth_session`-based tests (auth / sessions / RBAC-negative / free-tier /
  validate-latency) also run remotely — see
  "Remote-mode coverage beyond health (deploy#204)" above. Without the
  secret they skip cleanly, so the baseline remote set is still
  `test_health.py` (health endpoints + JWKS publication).

### Two-layer prod-environment guard

The `RUN_MODE=remote` + `ENVIRONMENT in {prod, production}` refusal is
enforced at **two layers** to cover the case where pytest is invoked
directly (interactive debugging inside the runner image, future workflows
that skip `run-tests.sh`, `python -m pytest`, etc.):

| Layer | Where | When it fires | How it aborts |
|------|------|------|------|
| Shell | `run-tests.sh` lines ~46-56 | Before pytest starts via the documented entrypoint | `exit 3` |
| Python | `tests/conftest.py` module-load | At pytest collection time, regardless of invocation path | `RuntimeError` raised before any fixture or test runs |

Both layers check the same predicate (`RUN_MODE=remote` AND
`ENVIRONMENT in {prod, production}`). If you change one, change the
other — they are intentionally coupled. See deploy#203.

## Architecture

- All services run on isolated Docker networks: `backend`, `user-backend`, `testnet`
- `user-postgres` sits on `user-backend` only; `isnad-graph-api` is on `backend` + `testnet`, and cannot reach `user-postgres` directly — enforced by `test_network_isolation.py`
- JWT keys are generated once per test run and injected into both services
- Test runner container joins `testnet` and reaches services via their Compose service names

## Environment variables

See `.env.test` for the full set. All credentials are test-only and generated fresh each run.

## CI

`.github/workflows/integration-tests.yml` checks out `noorinalabs-deploy`,
`noorinalabs-user-service`, and `noorinalabs-isnad-graph` as peer directories
and runs the suite on every PR to `main`.

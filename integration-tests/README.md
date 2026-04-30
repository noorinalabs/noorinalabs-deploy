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
| `remote` | no | Skip stack-up; run pytest against env-supplied stg URLs. DB/Redis-direct fixtures auto-skip — effective remote-runnable set today is health + JWKS. |

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
  available from outside the VPS. The current effective remote set is
  `test_health.py` (health endpoints + JWKS publication).
- Per-test refactors to use stg test-user creds delivered via secrets
  (so RBAC / sessions / 2FA / subscription flows can run remotely too)
  are downstream follow-up work — see deploy#178 body.

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

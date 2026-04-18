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

### Known work-arounds (revert once upstream fix lands)

| Work-around | Reason | Revert-path |
|-------------|--------|-------------|
| `alembic upgrade heads` (plural) in the user-service command | user-service has three unmerged migration heads (0003, 0020, 0030 off 0001); `upgrade head` refuses with "Multiple head revisions" | `noorinalabs/noorinalabs-user-service#63` — once a merge migration lands, revert to `head` (singular) |

## Running locally

```bash
# One command
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

# fake_oauth

Minimal FastAPI impersonation of Google's OAuth 2.0 / OpenID Connect endpoints
for the integration-test stack. Exists so the real `/auth/oauth/google/callback`
code path in `noorinalabs-user-service` can be exercised end-to-end — closing
out the `#135` follow-up that `integration-tests/tests/conftest.py` used to
name explicitly.

Activated by wiring two env vars into the `user-service` container:

- `OAUTH_PROVIDER_BASE_URL_OVERRIDE=http://fake_oauth:8080` — landed in
  `noorinalabs-user-service#77`, rewrites every provider's authorize / token /
  userinfo scheme+host to this base while preserving paths.
- `ENVIRONMENT=test` — required by the production-guard validator in
  `user-service#78` before HTTP overrides are accepted.

## Endpoints

- `GET /health` — liveness.
- `GET /o/oauth2/v2/auth` — authorize; 302s back to `redirect_uri` with a fake
  `code=FAKE_CODE_<random>` and the caller's `state`.
- `POST /token` — accepts any form-posted `code`, returns canned
  `access_token` / `id_token` / `refresh_token`.
- `GET /oauth2/v3/userinfo` — returns a deterministic fake user keyed to the
  access-token suffix so `sub` and `email` stay stable within one flow.

## Scope — Google only

One provider is enough to prove the plumbing and unblock #135. GitHub / Apple
/ Facebook test paths still use the pre-#135 Redis shim in `conftest.py`. Add
more provider fixtures here if/when those flows need end-to-end coverage.

## Local exercise

```bash
# From this directory
docker build -t fake_oauth:dev .
docker run --rm -p 8080:8080 -e FAKE_OAUTH_AUDIENCE=test-client-id fake_oauth:dev

# In another terminal
curl -i 'http://localhost:8080/o/oauth2/v2/auth?redirect_uri=http://x/cb&state=abc'
curl -s -X POST http://localhost:8080/token -d 'code=abc' | jq
curl -s http://localhost:8080/oauth2/v3/userinfo -H 'Authorization: Bearer fake-access-deadbeef' | jq
```

## Image size

Reconciled per #147 (PR #146 body said "55 MB unpacked single-arch", earlier
draft of this README said "~160 MB"). Authoritative breakdown for the current
Dockerfile (`python:3.12-slim` + `fastapi==0.115.0` + `uvicorn[standard]==0.32.0`
+ `python-multipart==0.0.12`):

- `python:3.12-slim` amd64 base: **~119 MB** on-disk (per
  [docker-library/repo-info](https://github.com/docker-library/repo-info/blob/master/repos/python/local/3.12-slim.md)
  Virtual Size for `3.12.13-slim-trixie`).
- Pip deps installed into site-packages: **~39 MB** (measured by replaying
  `pip install -r requirements.txt` into a venv; dominated by `uvloop`
  ~15 MB, `pip`/`setuptools` are NOT included since `pip install -r` doesn't
  add them).
- App code (`main.py`): negligible (<1 KB).
- No `__pycache__` overhead — `PYTHONDONTWRITEBYTECODE=1` is set in the
  Dockerfile.

**Total: ~158 MB unpacked** (estimated, not measured via `docker image
inspect` — see #147 for the reconciliation context). The 55 MB figure in PR
#146's body was the compressed/registry size, not the on-disk size; this
matters for pull bandwidth but not for the docker host's disk footprint or
the `<100 MB target` discussion. Don't add DBs, Redis clients, or crypto
libs — id_token signature verification is not required for Google's flow.

The 58 MB over the 100 MB target is dominated by `uvicorn[standard]` extras
(`httptools` ~1.6 MB, `uvloop` ~15 MB, `watchfiles` ~1.2 MB, `websockets`
~1.4 MB) and `python:3.12-slim` itself. Dropping `[standard]` would shave
~18-20 MB but is a future option, not a current change.

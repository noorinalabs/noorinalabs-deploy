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

Target under 100 MB. `python:3.12-slim` + fastapi + uvicorn[standard] +
python-multipart weighs in around ~160 MB uncompressed; that's acceptable
for a test-only container. Don't add DBs, Redis clients, or crypto libs —
id_token signature verification is not required for Google's flow.

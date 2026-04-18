#!/usr/bin/env bash
# Emit shell-eval-able env declarations for ephemeral test secrets.
# Written to .env.test.secrets per run; never committed.

set -euo pipefail

tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT

openssl genrsa -out "$tmpdir/jwt.key" 2048 2>/dev/null
openssl rsa -in "$tmpdir/jwt.key" -pubout -out "$tmpdir/jwt.pub" 2>/dev/null

priv=$(awk 'BEGIN{ORS="\\n"} {print}' "$tmpdir/jwt.key")
pub=$(awk 'BEGIN{ORS="\\n"} {print}' "$tmpdir/jwt.pub")

# 32-byte url-safe key for Fernet (TOTP encryption)
totp_key=$(python3 -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())")

cat <<EOF
JWT_PRIVATE_KEY="$priv"
JWT_PUBLIC_KEY="$pub"
TOTP_ENCRYPTION_KEY="$totp_key"
EOF

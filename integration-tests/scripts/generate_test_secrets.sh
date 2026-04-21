#!/usr/bin/env bash
# Generate ephemeral PEM keys and a Fernet key for the test run.
# Writes three files into the directory given as $1 (default: ./secrets/).
# The caller is expected to read those files directly (e.g. KEY=$(cat ...))
# so that real newlines survive — trying to marshal a PEM through a dotenv
# file with escaped \n sequences breaks python-jose at load time.

set -euo pipefail

outdir="${1:-secrets}"
mkdir -p "$outdir"

openssl genrsa -out "$outdir/jwt.key" 2048 2>/dev/null
openssl rsa -in "$outdir/jwt.key" -pubout -out "$outdir/jwt.pub" 2>/dev/null

python3 -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())" \
    > "$outdir/totp.key"

chmod 600 "$outdir/jwt.key" "$outdir/jwt.pub" "$outdir/totp.key"
echo "Wrote jwt.key, jwt.pub, totp.key to $outdir"

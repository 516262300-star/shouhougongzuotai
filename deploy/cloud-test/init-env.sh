#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if [[ -e .env ]]; then
  echo "Existing test environment preserved."
  exit 0
fi
command -v openssl >/dev/null || { echo "openssl is required" >&2; exit 1; }
umask 077
db_password=$(openssl rand -hex 24)
root_password=$(openssl rand -hex 24)
set -o noclobber
{
  printf 'MYSQL_PASSWORD=%s\n' "$db_password"
  printf 'MYSQL_ROOT_PASSWORD=%s\n' "$root_password"
} > .env
echo "Created cloud-test/.env with random database passwords (mode 600)."

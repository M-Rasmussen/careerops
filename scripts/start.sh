#!/usr/bin/env bash
# Build and start CareerOps (dashboard + API + PostgreSQL), then wait until healthy.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [[ ! -f .env ]]; then
  echo "No .env found. Create one first:  cp .env.example .env  (then set a password)" >&2
  exit 1
fi

docker compose up -d --build --wait
port="$(grep -E '^API_PORT=' .env | cut -d= -f2 || true)"
web_port="$(grep -E '^WEB_PORT=' .env | cut -d= -f2 || true)"
echo "CareerOps dashboard: http://127.0.0.1:${web_port:-5173}"
echo "CareerOps API:       http://127.0.0.1:${port:-8000}  (docs: /docs, health: /health)"

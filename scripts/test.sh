#!/usr/bin/env bash
# Run the test suite inside the running API container against the test database.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
docker compose exec -T api pytest "$@"

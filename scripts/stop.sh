#!/usr/bin/env bash
# Stop CareerOps containers. Data in the named volume is preserved.
# This script intentionally never removes volumes (no `down -v`).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
docker compose stop
echo "Stopped. Database volume 'careerops_pgdata' is preserved."

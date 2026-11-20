#!/usr/bin/env bash
# Write a compressed pg_dump of the live database to backups/ (git-ignored).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
mkdir -p backups
file="backups/careerops_$(date -u +%Y%m%dT%H%M%SZ).dump"
docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom' > "$file"
if [[ ! -s "$file" ]]; then
  rm -f "$file"
  echo "Backup failed: empty dump" >&2
  exit 1
fi
echo "Backup written: $file"

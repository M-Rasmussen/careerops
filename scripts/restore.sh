#!/usr/bin/env bash
# Restore a pg_dump file into a database. Overwriting the live database
# requires typing its name to confirm. Usage:
#   scripts/restore.sh backups/careerops_YYYYMMDDTHHMMSSZ.dump [target_db]
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

file="${1:?usage: scripts/restore.sh <dump-file> [target_db]}"
[[ -f "$file" ]] || { echo "No such file: $file" >&2; exit 1; }
live_db="$(docker compose exec -T db sh -c 'printf %s "$POSTGRES_DB"' </dev/null)"
target="${2:-$live_db}"
[[ "$target" =~ ^[A-Za-z0-9_]+$ ]] || { echo "Invalid database name: $target" >&2; exit 1; }

if [[ "$target" == "$live_db" ]]; then
  echo "WARNING: this will REPLACE all data in the live database '$live_db'."
  echo "Consider taking a fresh backup first (scripts/backup.sh)."
  read -r -p "Type the database name to confirm: " answer
  [[ "$answer" == "$live_db" ]] || { echo "Aborted."; exit 1; }
  # Stop the API so it holds no connections during the restore.
  docker compose stop api
else
  docker compose exec -T db sh -c "createdb -U \"\$POSTGRES_USER\" '$target'" 2>/dev/null || true
fi

docker compose exec -T db sh -c "pg_restore -U \"\$POSTGRES_USER\" -d '$target' --clean --if-exists --no-owner" < "$file"
echo "Restored $file into '$target'."
if [[ "$target" == "$live_db" ]]; then
  docker compose up -d --wait api
fi

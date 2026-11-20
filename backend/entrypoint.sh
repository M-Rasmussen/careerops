#!/bin/sh
# Apply migrations deterministically before starting the API.
set -e
alembic upgrade head
exec "$@"

"""Merge a duplicate company record into another (development data). Dry run by default. Refuses records with
different domains. Back up first (./scripts/backup.sh).

    docker compose exec api python -m scripts.merge_companies --source <uuid> --target <uuid> --reason "evidence..."
    docker compose exec api python -m scripts.merge_companies ... --apply
"""

import argparse
import json
import uuid

from app.db import SessionLocal
from app.services.companies import merge_companies


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", type=uuid.UUID, required=True, help="Record to merge away")
    parser.add_argument("--target", type=uuid.UUID, required=True, help="Record to keep")
    parser.add_argument("--reason", required=True, help="Evidence that both records are the same employer")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    with SessionLocal() as session:
        print(json.dumps(merge_companies(session, args.source, args.target, args.reason, args.apply), indent=2))
    if not args.apply:
        print("Dry run: nothing changed. Re-run with --apply.")


if __name__ == "__main__":
    main()

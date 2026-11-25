"""Start an empty Job Discovery generation (development reset). Nothing is deleted: archived postings keep their
history, review decisions, saved-job links, and duplicate links, and stay available for deduplication. Saved jobs
and applications are not touched. Back up first (./scripts/backup.sh).

    docker compose exec api python -m scripts.archive_discovery --confirm "Employer-source pilot"
"""

import argparse

from app.db import SessionLocal
from app.services.discovery import archive_active_generation


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--confirm", metavar="LABEL", required=True, help="Label for the new, empty generation")
    args = parser.parse_args()
    with SessionLocal() as session:
        result = archive_active_generation(session, args.confirm)
    print(f"Archived generation {result['archived_generation']} ({result['archived_jobs']} postings kept). "
          f"Active generation is now {result['active_generation']} (empty).")


if __name__ == "__main__":
    main()

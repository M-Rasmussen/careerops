"""Re-check stored discovered postings for non-vacancies (interest forms, talent pools). Offline: contacts nothing.
Dry run by default; --apply stores the result. Excluded postings are hidden from Job Discovery, never deleted.

    docker compose exec api python -m scripts.classify_vacancies           # show what would change
    docker compose exec api python -m scripts.classify_vacancies --apply
"""

import argparse
from collections import Counter

from app.db import SessionLocal
from app.services.discovery import active_generation, reclassify_non_vacancies


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    with SessionLocal() as session:
        active = active_generation(session)
        changes = reclassify_non_vacancies(session, args.apply)
        for job, old, new in changes:
            state = "active" if job.generation == active else "archived"
            print(f"{'EXCLUDE' if new else 'RESTORE'} [{state}] {job.company} | {job.title} | {new or old}")
        print(Counter(("exclude" if new else "restore", "active" if j.generation == active else "archived") for j, _, new in changes))
        print("Applied." if args.apply else "Dry run: nothing changed. Re-run with --apply.")


if __name__ == "__main__":
    main()

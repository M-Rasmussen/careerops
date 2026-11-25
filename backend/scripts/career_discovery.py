"""Bounded employer career-source discovery for named companies (official site -> careers page -> ATS board ->
job import). Never scans the whole registry: companies must be listed explicitly.

    docker compose exec api python -m scripts.career_discovery --company "GitLab" --company "Stripe"
    docker compose exec api python -m scripts.career_discovery --company-id <uuid> --max-tasks 20
"""

import argparse
import json
import sys

from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import Company
from app.services.career_discovery import DEEP, STANDARD, SiteFetcher, enqueue_discovery, run_tasks
from app.services.job_sources import HttpClient


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--company", action="append", default=[], help="Exact company name (case-insensitive)")
    parser.add_argument("--company-id", action="append", default=[])
    parser.add_argument("--max-tasks", type=int, default=40, help="Upper bound on discovery + sync tasks run")
    parser.add_argument("--force", action="store_true", help="Re-check companies checked in the last 7 days")
    parser.add_argument("--deep", action="store_true", help="Deeper scan (one company only; up to 150 site requests)")
    args = parser.parse_args()
    if not args.company and not args.company_id:
        sys.exit("Name at least one --company or --company-id; the whole registry is never scanned.")
    if args.deep and len(args.company) + len(args.company_id) != 1:
        sys.exit("--deep scans exactly one company.")
    with SessionLocal() as session:
        ids = list(session.scalars(select(Company.id).where(
            func.lower(Company.name).in_([n.lower() for n in args.company]) | Company.id.in_(args.company_id))))
        if len(ids) < len(args.company) + len(args.company_id):
            print(f"Warning: matched {len(ids)} companies for {len(args.company) + len(args.company_id)} names/ids", file=sys.stderr)
        queued = enqueue_discovery(session, ids, force=args.force)
        session.commit()
    client, fetcher = HttpClient(), SiteFetcher()
    results = run_tasks(SessionLocal, client, fetcher, args.max_tasks, company_ids=ids, limits=DEEP if args.deep else STANDARD)
    print(json.dumps({"companies": len(ids), "queued": queued, "tasks": results,
                      "requests": {"ats_api": client.requests, "official_sites": fetcher.requests}}, indent=2))


if __name__ == "__main__":
    main()

"""Import the curated employer registry (company records only). Idempotent; contacts no external service.

    docker compose exec api python -m scripts.seed_companies
"""

from app.db import SessionLocal
from app.services.companies import seed_companies


def main() -> None:
    with SessionLocal() as session:
        result = seed_companies(session)
    print(
        f"Curated employers: {result['seed_entries']} in seed, {result['inserted']} added, "
        f"{result['promoted']} auto-discovered records promoted, {result['already_present']} already present (left unchanged)."
    )


if __name__ == "__main__":
    main()

"""Regenerate public/demo/sample-applications.xlsx from the fictional demo seed.

Uses the backend's own exporter so the sample matches the real export format. Run from the repo root
with the stack up (fictional seed data only; never point this at real records):
  docker compose exec -T api sh -c 'cat > /tmp/gen.py' < frontend/scripts/demo_workbook.py
  docker compose exec -T -e PYTHONPATH=/app api python /tmp/gen.py \
    < frontend/src/demo/seed.json > frontend/public/demo/sample-applications.xlsx
"""
import json
import sys
from datetime import date, timedelta
from types import SimpleNamespace

from app.services.excel import applications_to_xlsx

seed = json.load(sys.stdin)
anchor = date(2026, 11, 20)  # Fixed so the committed sample is reproducible.
jobs = {j["key"]: SimpleNamespace(**j) for j in seed["jobs"]}
apps = [
    SimpleNamespace(
        job=jobs[a["job"]],
        status=a["status"],
        applied_date=anchor - timedelta(days=a["applied_days_ago"]),
        follow_up_date=None if a["follow_up_in_days"] is None else anchor + timedelta(days=a["follow_up_in_days"]),
        notes=a["notes"],
    )
    for a in sorted(seed["applications"], key=lambda a: a["applied_days_ago"])
]
sys.stdout.buffer.write(applications_to_xlsx(apps))

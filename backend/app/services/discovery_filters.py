"""Deterministic, explainable filtering of discovered jobs. No AI.

Each configured criterion yields pass / unknown / reject with a human-readable reason.
Any reject => "rejected"; otherwise any unknown => "unknown"; otherwise "match".
Missing job data is "unknown", never a rejection. Nothing (seniority, salary, location) is inferred when absent.
"""

import re
from typing import Any

# Role family -> (label, search phrase for aggregators, title phrases that count as that family).
ROLE_FAMILIES: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "backend": ("Backend", "backend engineer", ("backend", "back-end", "back end", "server-side", "api engineer", "api developer")),
    "software": ("Software engineering / SWE II", "software engineer", (
        "software engineer", "software developer", "software development engineer", "swe", "application developer",
        "applications developer", "programmer", "python developer", "java developer", "developer",
    )),
    "fullstack": ("Full-stack", "full stack engineer", ("full stack", "full-stack", "fullstack")),
    "platform": ("Platform", "platform engineer", ("platform engineer", "platform engineering", "platform developer")),
    "devops": ("DevOps / infrastructure", "devops engineer", (
        "devops", "dev ops", "site reliability", "sre", "infrastructure engineer", "infrastructure", "cloud engineer",
        "build engineer", "release engineer", "reliability engineer",
    )),
    "support": ("Production / application support", "production support engineer", (
        "production support", "application support", "applications support", "support engineer",
        "l2 support", "l3 support", "systems support",
    )),
    "sdet": ("SDET / QA automation", "sdet", (
        "sdet", "software engineer in test", "software development engineer in test", "qa automation",
        "test automation", "automation engineer", "quality engineer", "qa engineer", "quality assurance engineer",
    )),
}
ROLE_FAMILY_KEYS = tuple(ROLE_FAMILIES)
# Engineering-ish titles outside the chosen families are "unknown", not rejected (titles vary a lot).
ADJACENT_TITLE_WORDS = ("engineer", "developer", "engineering", "programmer", "architect")

# Title words -> seniority level. Titles without these words (e.g. "Software Engineer") are unknown.
SENIORITY_WORDS = {
    "intern": ("intern", "internship", "co-op"),
    "entry": ("junior", "jr", "entry level", "entry-level", "graduate", "new grad", "associate"),
    "mid": ("ii", "mid-level", "mid level", "intermediate"),
    "senior": ("senior", "sr", "iii"),
    "staff": ("staff",),
    "principal": ("principal", "distinguished"),
    "lead": ("lead", "tech lead"),
    "manager": ("manager", "director", "head of", "vp", "vice president"),
}
SENIORITY_LEVELS = tuple(SENIORITY_WORDS)

GEOGRAPHIES = ("nj", "nyc", "remote_us")
GEOGRAPHY_LABELS = {"nj": "New Jersey", "nyc": "New York City area", "remote_us": "US-eligible remote"}
NJ_WORDS = ("new jersey", "nj", "jersey city", "newark", "hoboken", "princeton", "parsippany", "edison",
            "morristown", "iselin", "piscataway", "basking ridge", "warren", "red bank", "holmdel", "berkeley heights")
# No bare "ny": "Albany, NY" is New York State, not the NYC area.
NYC_WORDS = ("new york", "nyc", "manhattan", "brooklyn", "queens", "bronx", "staten island",
             "long island city", "white plains", "stamford")
US_WORDS = ("united states", "usa", "us", "u.s.", "america", "worldwide", "anywhere", "global")


def _has_word(text: str, phrase: str) -> bool:
    # Word boundaries so "go" doesn't match "Google" and "sr" doesn't match "Israel".
    return re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text, re.IGNORECASE) is not None


def seniority_levels(title: str) -> set[str]:
    return {level for level, words in SENIORITY_WORDS.items() if any(_has_word(title, w) for w in words)}


def _geography(job: Any, wanted: list[str]) -> tuple[str, str]:
    """Returns (outcome, reason) for the location preference: pass, unknown, or reject."""
    loc = job.location or ""
    in_nj = any(_has_word(loc, w) for w in NJ_WORDS)
    in_nyc = any(_has_word(loc, w) for w in NYC_WORDS)
    if job.workplace_type == "remote":
        if "remote_us" not in wanted:
            return "reject", "Remote, and remote isn't in your geographies"
        if not loc:
            return "unknown", "Remote, but eligible countries aren't stated"
        if any(_has_word(loc, w) for w in US_WORDS) or in_nj or in_nyc:
            return "pass", f"Remote, open to the US ({loc})"
        return "reject", f"Remote, limited to {loc}"
    if not loc:
        return "unknown", "Location not stated"
    for geo, hit in (("nj", in_nj), ("nyc", in_nyc)):
        if hit and geo in wanted:
            return "pass", f"Location in {GEOGRAPHY_LABELS[geo]} ({loc})"
    if "remote_us" in wanted and job.workplace_type is None and _has_word(loc, "remote"):
        return "unknown", f"Possibly remote ({loc}); workplace type not stated"
    return "reject", f"Location {loc} is outside your geographies"


def evaluate(job: Any, criteria: dict[str, Any]) -> tuple[str, list[str]]:
    """`job` has title, location, workplace_type, salary_*, seniority attributes; `criteria` is a
    DiscoveryFilters dump."""
    rejects: list[str] = []
    unknowns: list[str] = []
    passes: list[str] = []
    title = job.title or ""

    families = criteria.get("role_families") or []
    keywords = criteria.get("title_keywords") or []
    if families or keywords:
        hit = next(
            ((ROLE_FAMILIES[f][0], w) for f in families for w in ROLE_FAMILIES[f][2] if _has_word(title, w)), None
        ) or next((("keyword", k) for k in keywords if _has_word(title, k)), None)
        if hit:
            passes.append(f'Role: {hit[0]} ("{hit[1]}")' if hit[0] != "keyword" else f'Title matches "{hit[1]}"')
        elif any(_has_word(title, w) for w in ADJACENT_TITLE_WORDS):
            unknowns.append("Engineering title outside your role families")
        else:
            rejects.append("Title doesn't match your role families or keywords")

    if excluded := criteria.get("excluded_keywords"):
        hit = next((k for k in excluded if _has_word(title, k)), None)
        if hit:
            rejects.append(f'Title contains excluded keyword "{hit}"')

    workplaces = criteria.get("workplace_types") or []
    if workplaces:
        if job.workplace_type is None:
            unknowns.append("Workplace type not stated")
        elif job.workplace_type in workplaces:
            passes.append(f"Workplace type is {job.workplace_type}")
        else:
            rejects.append(f"Workplace type is {job.workplace_type}")

    if geos := criteria.get("geographies"):
        outcome, reason = _geography(job, geos)
        {"pass": passes, "unknown": unknowns, "reject": rejects}[outcome].append(reason)

    # Free-text locations (in addition to geographies); an accepted remote job passes regardless.
    remote_ok = job.workplace_type == "remote" and ("remote" in workplaces or "remote_us" in (geos or []))
    if (locations := criteria.get("locations")) and not remote_ok:
        if not job.location:
            unknowns.append("Location not stated")
        elif hit := next((loc for loc in locations if loc.lower() in job.location.lower()), None):
            passes.append(f'Location matches "{hit}"')
        else:
            rejects.append(f"Location {job.location} is not in your preferred locations")

    if allowed := criteria.get("seniority"):
        levels = seniority_levels(title) | ({job.seniority} if getattr(job, "seniority", None) else set())
        if not levels:
            unknowns.append("Seniority not stated")
        elif levels & set(allowed):
            passes.append(f"Seniority: {', '.join(sorted(levels & set(allowed)))}")
        else:
            rejects.append(f"Seniority is {', '.join(sorted(levels))}")

    if (minimum := criteria.get("min_salary")) is not None:
        currency = criteria.get("salary_currency") or "USD"
        top = job.salary_max if job.salary_max is not None else job.salary_min
        interval = (job.salary_interval or "").lower()
        # Only annual figures in the configured currency are comparable; anything else is unknown.
        if top is None or job.salary_currency != currency or not ("year" in interval or "annual" in interval):
            unknowns.append("Salary not stated or not comparable")
        elif top >= minimum:
            passes.append(f"Salary up to {top:,} {currency}")
        else:
            rejects.append(f"Salary up to {top:,} {currency} is below {minimum:,}")

    if rejects:
        return "rejected", rejects + unknowns
    if unknowns:
        return "unknown", unknowns + passes
    return "match", passes or ["No filters configured"]

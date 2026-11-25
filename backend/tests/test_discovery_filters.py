from types import SimpleNamespace

import pytest

from app.schemas import DiscoveryFilters
from app.services.discovery_filters import evaluate, seniority_levels


def job(**kw: object) -> SimpleNamespace:
    base = dict(title="Backend Engineer", location=None, workplace_type=None,
                salary_min=None, salary_max=None, salary_currency=None, salary_interval=None)
    return SimpleNamespace(**{**base, **kw})


def criteria(**kw: object) -> dict:
    # Role families and geographies default to "all" in the app; these tests opt in explicitly.
    return DiscoveryFilters(**{"role_families": [], "geographies": [], **kw}).model_dump()


def test_no_filters_matches_everything() -> None:
    assert evaluate(job(), criteria()) == ("match", ["No filters configured"])


def test_title_keywords_use_word_boundaries() -> None:
    c = criteria(title_keywords=["Go", "Python"])
    assert evaluate(job(title="Python Developer"), c) == ("match", ['Title matches "Python"'])
    assert evaluate(job(title="Google Ads Specialist"), c)[0] == "rejected"
    assert evaluate(job(title="Go Engineer"), c)[0] == "match"


def test_excluded_keyword_rejects_with_reason() -> None:
    result, reasons = evaluate(job(title="Senior Java Engineer"), criteria(excluded_keywords=["java"]))
    assert result == "rejected"
    assert reasons == ['Title contains excluded keyword "java"']


def test_missing_workplace_and_location_are_unknown_not_rejected() -> None:
    result, reasons = evaluate(job(), criteria(workplace_types=["remote"], locations=["Denver"]))
    assert result == "unknown"
    assert set(reasons) == {"Workplace type not stated", "Location not stated"}


def test_workplace_type_and_location() -> None:
    c = criteria(workplace_types=["remote", "hybrid"], locations=["Denver"])
    assert evaluate(job(workplace_type="onsite", location="Denver, CO"), c)[0] == "rejected"
    assert evaluate(job(workplace_type="hybrid", location="Denver, CO"), c)[0] == "match"
    assert evaluate(job(workplace_type="hybrid", location="Austin, TX"), c)[0] == "rejected"
    # An accepted remote job passes regardless of its listed location.
    assert evaluate(job(workplace_type="remote", location="Remote - Canada"), c)[0] == "match"


def test_seniority() -> None:
    assert seniority_levels("Sr. Software Engineer") == {"senior"}
    assert seniority_levels("Software Engineer, Israel") == set()
    assert seniority_levels("Staff Engineer") == {"staff"}
    c = criteria(seniority=["entry", "senior"])
    assert evaluate(job(title="Senior Backend Engineer"), c)[0] == "match"
    assert evaluate(job(title="Engineering Manager"), c) == ("rejected", ["Seniority is manager"])
    assert evaluate(job(title="Backend Engineer"), c) == ("unknown", ["Seniority not stated"])


@pytest.mark.parametrize(
    ("salary", "expected"),
    [
        (dict(salary_min=100_000, salary_max=140_000, salary_currency="USD", salary_interval="per-year-salary"), "match"),
        (dict(salary_min=70_000, salary_max=90_000, salary_currency="USD", salary_interval="per-year-salary"), "rejected"),
        (dict(), "unknown"),  # not stated
        (dict(salary_min=60, salary_max=80, salary_currency="USD", salary_interval="per-hour-wage"), "unknown"),
        (dict(salary_min=150_000, salary_max=200_000, salary_currency="EUR", salary_interval="per-year-salary"), "unknown"),
    ],
)
def test_salary_minimum_only_uses_comparable_data(salary: dict, expected: str) -> None:
    assert evaluate(job(**salary), criteria(min_salary=120_000))[0] == expected


def test_rejection_lists_all_reasons() -> None:
    result, reasons = evaluate(
        job(title="Java Manager", workplace_type="onsite"),
        criteria(excluded_keywords=["Java"], workplace_types=["remote"], min_salary=1),
    )
    assert result == "rejected"
    assert len(reasons) == 3  # excluded keyword, workplace type, unknown salary


def test_geographies() -> None:
    c = criteria(geographies=["nj", "nyc", "remote_us"])
    assert evaluate(job(location="Jersey City, NJ", workplace_type="hybrid"), c)[0] == "match"
    assert evaluate(job(location="Brooklyn, NY"), c)[0] == "match"
    assert evaluate(job(location="Albany, NY"), c)[0] == "rejected"  # New York State, not the NYC area
    assert evaluate(job(location="Remote - United States", workplace_type="remote"), c)[0] == "match"
    assert evaluate(job(location="Remote - Canada", workplace_type="remote"), c)[0] == "rejected"
    assert evaluate(job(location=None, workplace_type="remote"), c)[0] == "unknown"  # eligibility not stated
    assert evaluate(job(location=None), c)[0] == "unknown"
    only_nj = criteria(geographies=["nj"])
    assert evaluate(job(location="Remote - United States", workplace_type="remote"), only_nj)[0] == "rejected"


def test_role_families_are_flexible() -> None:
    c = criteria(role_families=["backend", "sdet"])
    assert evaluate(job(title="Back-End Developer"), c)[0] == "match"
    assert evaluate(job(title="Software Development Engineer in Test"), c)[0] == "match"
    # Close-but-different engineering titles are unknown, not rejected.
    assert evaluate(job(title="Data Engineer"), c) == ("unknown", ["Engineering title outside your role families"])
    assert evaluate(job(title="Account Executive"), c)[0] == "rejected"
    # Provider-stated seniority counts; a missing one is unknown, never guessed.
    s = criteria(seniority=["mid"])
    assert evaluate(job(title="Software Engineer II"), s)[0] == "match"
    assert evaluate(job(title="Backend Engineer", seniority="mid"), s)[0] == "match"
    assert evaluate(job(title="Backend Engineer"), s)[0] == "unknown"

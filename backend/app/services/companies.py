"""Company registry: curated seed import, manual edits, identity resolution, and ATS board verification.

Identity rules (strongest first): a known ATS board, an exact normalized domain, a provider's own company id
(alias). A display name alone never links a job to a curated/manual/verified company; name-only employers are
grouped into an "unresolved" discovered record instead.
"""

import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import Select, and_, case, func, literal_column, nulls_last, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Company, CompanyAlias, DiscoveredJob, DiscoveryGeneration, DiscoveryTask, JobSource
from app.schemas import CompanyCreate, CompanyRead, CompanyUpdate, JobSourceRead
from app.seed.companies import SEED
from app.services.aggregators import normalize_domain
from app.services.job_sources import AtsRef, HttpClient, SourceError, parse_ats_url, verify_board

FAILED_VERIFICATION_TTL = timedelta(days=30)  # don't re-probe a board that answered "not found" recently


class NotFoundError(Exception):
    pass


class ConflictError(Exception):
    pass


class VerificationError(Exception):
    pass


def name_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()[:300]


_GENERIC_WORDS = {"inc", "llc", "ltd", "limited", "corp", "corporation", "company", "co", "group", "holdings", "the", "and",
                  "plc", "gmbh", "technologies", "technology", "labs", "careers", "jobs", "prospects", "global", "usa"}


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def names_consistent(company_name: str, domain: str | None, reported: str | None) -> bool:
    """False only when a name reported by a provider (board name, JSON-LD hiring organization) clearly belongs to
    someone else. Used to flag conflicts, never to prove ownership: equal names don't make equal companies."""
    rep = _squash(reported or "")
    if not rep:
        return True  # nothing reported: no conflict (a provider not exposing a name never disables a board)
    label = (domain or "").split(".")[0]
    ours = {_squash(company_name)} | ({_squash(label)} if len(label) >= 3 else set())
    ours |= {t for t in name_key(company_name).split() if len(t) >= 4 and t not in _GENERIC_WORDS}
    theirs = {t for t in name_key(reported or "").split() if len(t) >= 4 and t not in _GENERIC_WORDS}
    return any(o and o in rep for o in ours) or any(t in _squash(company_name) or t == _squash(label) for t in theirs)


MANUAL_OWNERSHIP = ("Confirmed by the user", "Confirmed manually by the user", "Added by the user")


# --- Seed ---------------------------------------------------------------------------------------


def seed_companies(session: Session) -> dict[str, int]:
    """Idempotent: inserts curated employers whose domain isn't registered yet. Existing curated or manual rows
    are never changed, so user edits survive re-imports. A row that discovery created for the same domain is
    marked as a curated suggestion (its notes are kept if set)."""
    rows = [
        {"id": uuid.uuid4(), "name": name, "domain": domain, "origin": "curated_seed", "watchlist": False,
         "verification_status": "verified", "website": f"https://{domain}",
         "notes": "Curated suggestion. Not evidence of current hiring or ATS verification."}
        for name, domain in SEED
    ]
    stmt = pg_insert(Company).values(rows)
    result = session.execute(
        stmt.on_conflict_do_update(
            index_elements=["domain"],
            set_={"origin": "curated_seed", "notes": func.coalesce(Company.notes, stmt.excluded.notes)},
            where=Company.origin == "discovered",
        ).returning(literal_column("xmax = 0"))  # PostgreSQL: true for inserted rows, false for promoted ones
    ).all()
    session.commit()
    inserted = sum(1 for (new,) in result if new)
    return {"seed_entries": len(rows), "inserted": inserted, "promoted": len(result) - inserted,
            "already_present": len(rows) - len(result)}


# --- Registry CRUD ------------------------------------------------------------------------------


def _domain_or_error(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    domain = normalize_domain(value)
    if domain is None:
        raise ValueError("domain: not a valid domain name")
    return domain


def create_company(session: Session, data: CompanyCreate) -> Company:
    values = data.model_dump(mode="json")
    values["domain"] = _domain_or_error(data.domain)
    company = Company(**values, origin="manual", verification_status="verified" if values["domain"] else "unresolved")
    session.add(company)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise ConflictError("A company with this domain already exists") from None
    return company


def get_company(session: Session, company_id: uuid.UUID) -> Company:
    company = session.get(Company, company_id)
    if company is None:
        raise NotFoundError("Company not found")
    return company


def update_company(session: Session, company_id: uuid.UUID, data: CompanyUpdate) -> Company:
    company = get_company(session, company_id)
    values = data.model_dump(mode="json", exclude_unset=True)
    if "domain" in values:
        values["domain"] = _domain_or_error(values["domain"])
        if values["domain"] and company.verification_status == "unresolved":
            values["verification_status"] = "verified"  # the user vouched for the domain
        elif not values["domain"] and company.verification_status == "verified" and not session.scalar(
            select(JobSource.id).where(JobSource.company_id == company.id, JobSource.verification_status == "verified").limit(1)
        ):
            values["verification_status"] = "unresolved"  # nothing verifies its identity any more
    if "domain" in values and values["domain"] != company.domain:
        if company.domain:  # the old domain stays linked to this company as history (and for identity matching)
            session.execute(pg_insert(CompanyAlias).values(id=uuid.uuid4(), company_id=company.id, kind="former_domain",
                                                           value=company.domain).on_conflict_do_nothing(constraint="uq_company_aliases_kind_value"))
        company.discovery_evidence = [*(company.discovery_evidence or []), {
            "step": "domain_change", "detail": f"Official domain changed by the user from {company.domain or 'none'} to "
            f"{values['domain'] or 'none'}", "url": None, "at": datetime.now(timezone.utc).isoformat()}]
    for field, value in values.items():
        setattr(company, field, value)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise ConflictError("A company with this domain already exists") from None
    session.refresh(company)
    return company


ACTIVE_JOBS = and_(DiscoveredJob.generation == select(func.max(DiscoveryGeneration.id)).scalar_subquery(),
                   DiscoveredJob.non_vacancy_reason.is_(None), DiscoveredJob.closed_at.is_(None))

ATS_STATUS = case(
    (func.bool_or(JobSource.verification_status == "verified"), "verified"),
    (func.bool_or(JobSource.verification_status == "needs_review"), "needs_review"),
    (func.bool_or(JobSource.verification_status == "unverified"), "unverified"),
    (func.count(JobSource.id) > 0, "failed"),
    else_="none",
)


def list_companies(
    session: Session,
    q: str | None = None,
    ats_status: str | None = None,
    origin: str | None = None,
    watchlist: bool | None = None,
    sort: str = "name",
    limit: int = 50,
    offset: int = 0,
) -> list[CompanyRead]:
    boards = (
        select(JobSource.company_id.label("cid"), ATS_STATUS.label("ats_status"),
               func.max(JobSource.last_checked_at).label("last_checked_at"),
               func.max(JobSource.last_success_at).label("last_sync_at"))
        .group_by(JobSource.company_id).subquery()
    )
    jobs = (
        select(DiscoveredJob.company_id.label("cid"), func.count().label("job_count"))
        .where(ACTIVE_JOBS).group_by(DiscoveredJob.company_id).subquery()
    )
    ats = func.coalesce(boards.c.ats_status, "none")
    count = func.coalesce(jobs.c.job_count, 0)
    stmt: Select[Any] = (
        select(Company, ats, boards.c.last_checked_at, count, boards.c.last_sync_at)
        .outerjoin(boards, boards.c.cid == Company.id)
        .outerjoin(jobs, jobs.c.cid == Company.id)
    )
    if q:
        stmt = stmt.where(or_(Company.name.icontains(q, autoescape=True), Company.domain.icontains(q, autoescape=True)))
    if ats_status:
        stmt = stmt.where(ats == ats_status)
    if origin:
        stmt = stmt.where(Company.origin == origin)
    if watchlist is not None:
        stmt = stmt.where(Company.watchlist.is_(watchlist))
    order = {
        "name": (func.lower(Company.name),),
        "jobs": (count.desc(), func.lower(Company.name)),
        "last_seen": (nulls_last(Company.last_seen_at.desc()), func.lower(Company.name)),
    }[sort]
    rows = session.execute(stmt.order_by(*order, Company.id).limit(limit).offset(offset)).all()
    by_company: dict[uuid.UUID, list[JobSourceRead]] = {}
    ids = [c.id for c, *_ in rows]
    found = list(session.scalars(select(JobSource).where(JobSource.company_id.in_(ids)).order_by(JobSource.created_at))) if ids else []
    for read in board_reads(session, found):
        by_company.setdefault(read.company_id, []).append(read)  # type: ignore[arg-type]
    return [
        CompanyRead.model_validate(c).model_copy(
            update={"ats_status": a, "last_checked_at": lc, "job_count": n, "boards": by_company.get(c.id, []),
                    "last_sync_at": ls}
        )
        for c, a, lc, n, ls in rows
    ]


def board_reads(session: Session, boards: list[JobSource]) -> list[JobSourceRead]:
    """Boards with their sync state and active posting counts (two grouped queries, not one per board)."""
    ids = [b.id for b in boards]
    if not ids:
        return []
    active = dict(session.execute(select(DiscoveredJob.source_id, func.count()).where(DiscoveredJob.source_id.in_(ids), ACTIVE_JOBS)
                                  .group_by(DiscoveredJob.source_id)).all())
    running = dict(session.execute(select(DiscoveryTask.source_id, func.max(DiscoveryTask.status)).where(
        DiscoveryTask.source_id.in_(ids), DiscoveryTask.kind == "sync", DiscoveryTask.status.in_(("queued", "running")))
        .group_by(DiscoveryTask.source_id)).all())
    out = []
    for b in boards:
        read = JobSourceRead.model_validate(b)
        status = ("not_imported" if not read.ingestion_supported or not b.ownership_verified_at
                  else {"running": "syncing", "queued": "queued"}.get(running.get(b.id, ""))
                  or ("failed" if b.last_error else "ok" if b.last_success_at else "never"))
        out.append(read.model_copy(update={"sync_status": status, "active_jobs": active.get(b.id, 0)}))
    return out


def company_detail(session: Session, company_id: uuid.UUID) -> CompanyRead:
    company = get_company(session, company_id)
    boards = list(session.scalars(select(JobSource).where(JobSource.company_id == company_id).order_by(JobSource.created_at)))
    statuses = {b.verification_status for b in boards}
    ats = next((s for s in ("verified", "needs_review", "unverified", "failed") if s in statuses), "none")
    count = session.scalar(select(func.count()).select_from(DiscoveredJob).where(DiscoveredJob.company_id == company_id, ACTIVE_JOBS))
    checked = [b.last_checked_at for b in boards if b.last_checked_at]
    synced = [b.last_success_at for b in boards if b.last_success_at]
    return CompanyRead.model_validate(company).model_copy(update={
        "boards": board_reads(session, boards), "ats_status": ats,
        "job_count": count or 0, "last_checked_at": max(checked) if checked else None,
        "last_sync_at": max(synced) if synced else None,
    })


# --- Identity resolution (used by discovery runs) -----------------------------------------------


class Resolver:
    """Resolves employers for one batch inside one session. Caches lookups; counts created companies."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.created = 0
        self._cache: dict[tuple[str, str], uuid.UUID] = {}

    def _alias(self, kind: str, value: str) -> uuid.UUID | None:
        key = (kind, value)
        if key not in self._cache:
            found = self.session.scalar(select(CompanyAlias.company_id).where(CompanyAlias.kind == kind, CompanyAlias.value == value))
            if found:
                self._cache[key] = found
        return self._cache.get(key)

    def _create(self, name: str, domain: str | None, status: str, alias: tuple[str, str] | None) -> uuid.UUID:
        now = datetime.now(timezone.utc)
        company = Company(name=name[:200], domain=domain, origin="discovered", watchlist=False,
                          verification_status=status, first_seen_at=now, last_seen_at=now,
                          notes="Discovered automatically from a job listing.")
        self.session.add(company)
        self.session.flush()
        if alias:
            # ON CONFLICT: another alias row may already exist for this key; keep the first.
            self.session.execute(
                pg_insert(CompanyAlias).values(id=uuid.uuid4(), company_id=company.id, kind=alias[0], value=alias[1])
                .on_conflict_do_nothing(constraint="uq_company_aliases_kind_value")
            )
            self._cache[alias] = company.id
        self.created += 1
        return company.id

    def resolve(self, name: str, domain: str | None, alias: tuple[str, str] | None, board_company: uuid.UUID | None) -> uuid.UUID:
        if board_company:
            return board_company
        if domain:
            key = ("domain", domain)
            if key not in self._cache:
                found = self.session.scalar(select(Company.id).where(Company.domain == domain))
                if found:
                    self._cache[key] = found
            if key in self._cache:
                return self._cache[key]
        if alias and (found := self._alias(*alias)):
            return found
        if domain:  # provider-reported website: a new company keyed by that domain
            if self.session.scalar(select(Company.id).where(Company.domain == domain)) is None:
                cid = self._create(name, domain, "provider_reported", alias)
                self._cache[("domain", domain)] = cid
                return cid
        if alias:
            return self._create(name, None, "provider_reported", alias)
        # Name only: group with other unresolved name-only records, never with a curated/verified company.
        key = name_key(name)
        return self._alias("name", key) or self._create(name, None, "unresolved", ("name", key))


def touch_companies(session: Session, company_ids: set[uuid.UUID]) -> None:
    if company_ids:
        session.execute(
            Company.__table__.update().where(Company.id.in_(company_ids))
            .values(last_seen_at=func.now(), first_seen_at=func.coalesce(Company.first_seen_at, func.now()))
        )


# --- ATS board verification ---------------------------------------------------------------------


def known_board(session: Session, ref: AtsRef) -> JobSource | None:
    return session.scalar(
        select(JobSource).where(JobSource.provider == ref.provider, func.lower(JobSource.board_token) == ref.board.lower())
    )


def record_verification(
    session: Session, ref: AtsRef, company_id: uuid.UUID | None, evidence: str, error: SourceError | None,
    ownership: str | None = None,
) -> JobSource:
    """Stores the outcome on the board row (the verification cache). Never deletes or unlinks companies.
    `ownership` is evidence that the employer itself points to this board (its official site links to it, or the
    user confirmed it). Without it, an answering board is only "needs_review" and is never synced into Discovery."""
    board = known_board(session, ref)
    now = datetime.now(timezone.utc)
    if board is None:
        company = session.get(Company, company_id) if company_id else None
        board = JobSource(provider=ref.provider, board_token=ref.board, company_id=company_id,
                          company_name=company.name if company else ref.board, enabled=True)
        session.add(board)
    elif board.company_id is None and company_id:
        board.company_id = company_id
    board.last_checked_at = now
    board.verification_evidence = evidence[:2048]
    if error is None:
        owned = ownership is not None and board.company_id == company_id and company_id is not None
        if owned:
            board.ownership_verified_at, board.ownership_evidence = now, ownership[:2048]  # type: ignore[index]
        status = "verified" if board.ownership_verified_at else "needs_review"
        board.verification_status, board.verified_at, board.last_error = status, now, None
        if owned and board.company_id:
            company = session.get(Company, board.company_id)
            if company and company.verification_status != "verified":
                company.verification_status = "verified"  # a verified board is trustworthy identity
    elif "404" in str(error) or "did not answer" in str(error):
        board.verification_status, board.last_error, board.last_error_at = "failed", str(error), now
    else:
        board.last_error, board.last_error_at = str(error), now  # transient: stays unverified, retried later
    session.flush()
    return board


def needs_verification(session: Session, ref: AtsRef) -> bool:
    board = known_board(session, ref)
    if board is None:
        return True
    if board.verification_status == "failed":
        return board.last_checked_at is None or board.last_checked_at < datetime.now(timezone.utc) - FAILED_VERIFICATION_TTL
    return board.verification_status == "unverified" and board.last_success_at is None


def verify_url(session: Session, client: HttpClient, company_id: uuid.UUID, url: str) -> JobSource:
    """Manual verification from a pasted ATS job or board URL. Arbitrary career sites are never crawled."""
    company = get_company(session, company_id)
    ref = parse_ats_url(url)
    if ref is None:
        raise VerificationError(
            "Not a supported ATS link. Paste a Greenhouse, Lever, or Ashby job or board URL "
            "(e.g. https://jobs.lever.co/acme). Company career sites aren't crawled."
        )
    existing = known_board(session, ref)
    if existing and existing.company_id and existing.company_id != company.id:
        raise ConflictError("That board is already linked to another company")
    session.commit()  # release the transaction while we call the provider
    try:
        verify_board(client, ref.provider, ref.board)
        error = None
    except SourceError as exc:
        error = exc
    board = record_verification(session, ref, company.id, url, error, ownership=f"Confirmed by the user: {url}")
    if board.ownership_verified_at and error is None:
        company.discovery_status, company.discovery_error = "verified", None
        enqueue_sync(session, board.id)
    session.commit()
    session.refresh(board)
    return board


def enqueue_sync(session: Session, source_id: uuid.UUID) -> None:
    """Queues a job import for an ownership-verified board; a no-op while one is already queued or running."""
    session.execute(pg_insert(DiscoveryTask).values(id=uuid.uuid4(), kind="sync", source_id=source_id).on_conflict_do_nothing())


# --- Merging duplicate records ------------------------------------------------------------------


def merge_companies(session: Session, source_id: uuid.UUID, target_id: uuid.UUID, reason: str, apply: bool) -> dict[str, Any]:
    """Moves everything that references `source` to `target`, then removes the source row. Refuses companies with
    different domains (same name is never enough). The source's id and name stay as aliases of the target, and the
    reason is recorded, so history can be traced. Jobs, saved jobs, and applications are never touched beyond the
    company link on discovered postings (saved jobs and applications don't reference companies)."""
    src, dst = get_company(session, source_id), get_company(session, target_id)
    if src.id == dst.id:
        raise ValueError("Source and target are the same company")
    if src.domain and dst.domain and src.domain != dst.domain:
        raise ConflictError(f"Different domains ({src.domain} vs {dst.domain}): separate identities, not merged")
    if not reason.strip():
        raise ValueError("A reason (the evidence) is required")
    counts = {
        "boards": session.scalar(select(func.count()).select_from(JobSource).where(JobSource.company_id == src.id)) or 0,
        "discovered_jobs": session.scalar(select(func.count()).select_from(DiscoveredJob).where(DiscoveredJob.company_id == src.id)) or 0,
        "aliases": session.scalar(select(func.count()).select_from(CompanyAlias).where(CompanyAlias.company_id == src.id)) or 0,
        "tasks": session.scalar(select(func.count()).select_from(DiscoveryTask).where(DiscoveryTask.company_id == src.id)) or 0,
    }
    if not apply:
        return {"source": f"{src.name} ({src.id})", "target": f"{dst.name} ({dst.id})", **counts}
    now = datetime.now(timezone.utc)
    session.execute(JobSource.__table__.update().where(JobSource.company_id == src.id).values(company_id=dst.id, company_name=dst.name))
    session.execute(DiscoveredJob.__table__.update().where(DiscoveredJob.company_id == src.id).values(company_id=dst.id))
    session.execute(CompanyAlias.__table__.update().where(CompanyAlias.company_id == src.id).values(company_id=dst.id))
    # A queued/running discovery task of the source would duplicate the target's: drop it; keep finished ones.
    session.execute(DiscoveryTask.__table__.delete().where(DiscoveryTask.company_id == src.id, DiscoveryTask.status.in_(("queued", "running"))))
    session.execute(DiscoveryTask.__table__.update().where(DiscoveryTask.company_id == src.id).values(company_id=dst.id))
    for kind, value in (("merged_company", str(src.id)), ("name", name_key(src.name))):
        session.execute(pg_insert(CompanyAlias).values(id=uuid.uuid4(), company_id=dst.id, kind=kind, value=value)
                        .on_conflict_do_nothing(constraint="uq_company_aliases_kind_value"))
    seen = [t for t in (src.first_seen_at, dst.first_seen_at) if t]
    dst.first_seen_at = min(seen) if seen else None
    dst.watchlist = dst.watchlist or src.watchlist
    dst.notes = f"{dst.notes or ''}\nMerged duplicate record {src.name!r} ({src.id}, {src.origin}) on {now:%Y-%m-%d}: {reason}".strip()
    dst.discovery_evidence = [*(dst.discovery_evidence or []), {
        "step": "merge", "detail": f"Merged duplicate {src.name!r} ({src.id}): {reason}"[:500], "url": None, "at": now.isoformat()}]
    session.delete(src)
    session.commit()
    return {"source": f"{src.name} ({src.id})", "target": f"{dst.name} ({dst.id})", **counts, "merged": True}

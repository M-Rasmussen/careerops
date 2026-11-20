"""Candidate profile and AI job-match persistence/policy. Limits and reuse are deterministic code.

A match request runs in three phases so no DB connection is held while the model works:
1. prepare  - one short transaction: validate, reuse check, rate limit, insert a 'running' run
2. agent    - the tool loop on an in-memory snapshot (no DB access)
3. finalize - one short transaction: record the outcome and, on success, the assessment, atomically
"""

import hashlib
import json
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agent.orchestrator import PROMPT_VERSION, SYSTEM_PROMPT, run_agent
from app.agent.provider import ModelProvider
from app.agent.tools import RunContext, job_snapshot, tool_specs
from app.config import AISettings
from app.models import AgentRun, CandidateProfile, Job, JobMatch
from app.schemas import MatchRequest, ProfileUpdate

# Serializes the rate-limit check with the run insert across concurrent requests (transaction-scoped).
RUN_LIMIT_LOCK = 4_041_001


class JobNotFoundError(Exception):
    pass


class ProfileMissingError(Exception):
    pass


class RateLimitedError(Exception):
    pass


class AgentFailedError(Exception):
    def __init__(self, run_id: uuid.UUID, error_type: str, error: str) -> None:
        super().__init__(error)
        self.run_id, self.error_type, self.error = run_id, error_type, error


def get_profile(session: Session) -> CandidateProfile | None:
    return session.get(CandidateProfile, 1)


def save_profile(session: Session, data: ProfileUpdate) -> CandidateProfile:
    """Only called from an explicit user request; the agent has no tool that writes the profile."""
    profile = get_profile(session)
    if profile is None:
        profile = CandidateProfile(id=1, resume_text=data.resume_text)
        session.add(profile)
    else:
        profile.resume_text = data.resume_text
    session.commit()
    session.refresh(profile)
    return profile


def input_hash(ctx: RunContext, provider: ModelProvider, settings: AISettings) -> str:
    """Fingerprint of everything that can change the result: exactly what the model sees (prompt, tool
    schemas, job snapshot, description, resume) plus which model/endpoint evaluates it."""
    payload = {
        "version": PROMPT_VERSION,
        "system": SYSTEM_PROMPT,
        "tools": tool_specs(),
        "provider": provider.name,
        "model": provider.model,
        "base_url": settings.base_url,
        "job": ctx.job,
        "job_description": ctx.job_description,
        "resume": ctx.resume_text,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def request_match(
    factory: Callable[[], Session], provider: ModelProvider, settings: AISettings, data: MatchRequest
) -> tuple[uuid.UUID, bool]:
    """Returns (match_id, reused). Raises the errors above."""
    with factory() as session, session.begin():
        job = session.get(Job, data.job_id)
        if job is None:
            raise JobNotFoundError
        profile = get_profile(session)
        if profile is None:
            raise ProfileMissingError
        ctx = RunContext(job=job_snapshot(job), resume_text=profile.resume_text, job_description=data.job_description)
        digest = input_hash(ctx, provider, settings)
        if not data.force:
            # Only successful runs produce matches; the status filter is a second guard.
            existing = session.scalars(
                select(JobMatch.id)
                .join(JobMatch.run)
                .where(JobMatch.input_hash == digest, AgentRun.status == "succeeded")
                .order_by(JobMatch.created_at.desc())
                .limit(1)
            ).first()
            if existing is not None:
                return existing, True
        session.execute(select(func.pg_advisory_xact_lock(RUN_LIMIT_LOCK)))
        since = datetime.now(timezone.utc) - timedelta(days=1)
        recent = session.scalar(select(func.count()).select_from(AgentRun).where(AgentRun.started_at >= since))
        if recent >= settings.max_runs_per_day:
            raise RateLimitedError
        run = AgentRun(
            kind="job_match", job_id=job.id, status="running", provider=provider.name,
            model=provider.model, steps=0, tool_calls=[],
        )
        session.add(run)
        session.flush()
        run_id = run.id

    outcome = run_agent(provider, settings.max_steps, ctx)

    # shortcut: if this final write itself fails the run stays 'running'; a stale-run sweep can fix that later.
    with factory() as session, session.begin():
        run = session.get_one(AgentRun, run_id)
        run.status = "succeeded" if outcome.assessment else "failed"
        run.steps = outcome.steps
        run.tool_calls = list(outcome.tool_calls)
        run.input_tokens, run.output_tokens = outcome.input_tokens, outcome.output_tokens
        run.error_type, run.error = outcome.error_type, outcome.error and outcome.error[:2000]
        run.finished_at = datetime.now(timezone.utc)
        match_id = None
        if outcome.assessment is not None:
            match = JobMatch(
                run_id=run_id,
                job_id=uuid.UUID(ctx.job["id"]),
                overall_match=outcome.assessment.overall_match,
                assessment=outcome.assessment.model_dump(mode="json"),
                job_description=ctx.job_description,
                input_hash=digest,
            )
            session.add(match)
            session.flush()
            match_id = match.id
    if match_id is None:
        raise AgentFailedError(run_id, outcome.error_type or "internal_error", outcome.error or "Agent failed")
    return match_id, False


def list_matches(session: Session, job_id: uuid.UUID | None, limit: int, offset: int) -> list[JobMatch]:
    stmt = select(JobMatch).order_by(JobMatch.created_at.desc(), JobMatch.id.desc())
    if job_id is not None:
        stmt = stmt.where(JobMatch.job_id == job_id)
    return list(session.scalars(stmt.limit(limit).offset(offset)))


def get_match(session: Session, match_id: uuid.UUID) -> JobMatch | None:
    return session.get(JobMatch, match_id)

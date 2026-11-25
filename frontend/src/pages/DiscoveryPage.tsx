import { useEffect, useState, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router";
import { useData } from "../api/context";
import { useAsync } from "../hooks/useAsync";
import { Empty, ErrorBox, ExternalLink, Loading, safeExternalUrl } from "../components/ui";
import { formatTimestamp } from "../dates";
import {
  DISCOVERY_SORTS, EMPLOYMENT_TYPES, GEOGRAPHIES, JOB_PROVIDERS, PROVIDER_LABELS, ROLE_FAMILIES, SENIORITY_LEVELS, WORKPLACE_TYPES,
} from "../types";
import type {
  DiscoveredJob, DiscoveredJobQuery, DiscoveryFilters, DiscoveryRun, DiscoverySort, EmploymentTypeFilter, ExperienceMatch, FilterResult,
  JobProvider, ReviewStatus,
} from "../types";

const PAGE_SIZE = 20;
const RESULT_LABELS: Record<FilterResult, string> = { match: "Matches preferences", unknown: "Unknown / not enough info", rejected: "Doesn't match preferences" };
const REVIEW_LABELS: Record<ReviewStatus, string> = { new: "Needs review", later: "Review later", saved: "Saved", skipped: "Passed" };
const EXPERIENCE_LABELS: Record<ExperienceMatch["status"], string> = {
  not_evaluated: "Not evaluated", evaluation_available: "Evaluation available", previously_evaluated: "Previously evaluated",
};
const WORKPLACE_LABELS: Record<string, string> = { remote: "Remote", hybrid: "Hybrid", onsite: "On-site" };
const providerLabel = (p: string) => PROVIDER_LABELS[p as JobProvider] ?? p;
const ATS_PROVIDERS: string[] = ["greenhouse", "lever", "lever_eu", "ashby"];
const RUN_POLL_MS = 2000;

// Filters live in the URL, so they survive review actions, paging, and navigating away and back.
// Defaults: "Needs review" and possible duplicates hidden.
function readQuery(p: URLSearchParams): DiscoveredJobQuery {
  const get = (k: string) => p.get(k) ?? "";
  const num = (k: string) => (/^\d+$/.test(get(k)) ? Number(get(k)) : "");
  return {
    q: get("q"), role_family: get("role_family") as DiscoveredJobQuery["role_family"], location: get("location"),
    workplace: get("workplace") as DiscoveredJobQuery["workplace"], seniority: get("seniority") as DiscoveredJobQuery["seniority"],
    min_salary: num("min_salary"), posted_within_days: num("posted_within_days"),
    filter_result: get("filter_result") as FilterResult | "", provider: get("provider") as JobProvider | "",
    saved_state: get("saved_state") as DiscoveredJobQuery["saved_state"],
    // Comma-separated in the page URL; sent to the API as repeated parameters.
    employment_type: get("employment_type").split(",").filter((t): t is EmploymentTypeFilter =>
      t === "unknown" || (EMPLOYMENT_TYPES as readonly string[]).includes(t)),
    review_status: p.has("review_status") ? (get("review_status") === "all" ? "" : (get("review_status") as ReviewStatus)) : "new",
    hide_duplicates: get("show_duplicates") !== "1",
    sort: (get("sort") || "discovered") as DiscoverySort,
    company_id: get("company") || undefined,
  };
}

/** "125,000–150,000 USD per year". Only what the source states; nothing is estimated. */
function salaryText(j: Pick<DiscoveredJob, "salary_min" | "salary_max" | "salary_currency" | "salary_interval">): string | null {
  if (j.salary_min === null && j.salary_max === null) return null;
  const range = j.salary_min !== null && j.salary_max !== null && j.salary_min !== j.salary_max
    ? `${j.salary_min.toLocaleString()}–${j.salary_max.toLocaleString()}`
    : (j.salary_max ?? j.salary_min)!.toLocaleString();
  const i = (j.salary_interval ?? "").toLowerCase();
  const period = /year|annual/.test(i) ? "per year" : /hour/.test(i) ? "per hour" : /month/.test(i) ? "per month" : /week/.test(i) ? "per week" : "pay period not stated";
  return `${range} ${j.salary_currency ?? "(currency not stated)"} ${period}`;
}

export function DiscoveryPage() {
  const data = useData();
  const [params, setParams] = useSearchParams();
  const query = readQuery(params);
  const page = Number(params.get("page") ?? "0") || 0;
  const key = params.toString();
  const [busy, setBusy] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [lastRun, setLastRun] = useState<DiscoveryRun | null>(null);
  const [tick, setTick] = useState(0); // bumps after review actions so counts refresh with the list
  const { data: rows, error, loading, reload } = useAsync(
    () => data.listDiscoveredJobs(query, PAGE_SIZE + 1, page * PAGE_SIZE), [data, key, tick],
  );
  const summary = useAsync(() => data.getDiscoverySummary(query), [data, key, tick]);
  const runs = useAsync(() => data.listDiscoveryRuns(1, 0), [data, lastRun?.id, lastRun?.status]);
  const coverage = useAsync(() => data.getDiscoveryCoverage(), [data, lastRun?.id, lastRun?.status]);
  const company = useAsync(() => (query.company_id ? data.getCompany(query.company_id) : Promise.resolve(null)), [data, query.company_id]);
  const hasNext = (rows?.length ?? 0) > PAGE_SIZE;
  const jobs = rows?.slice(0, PAGE_SIZE) ?? [];
  const run = lastRun ?? runs.data?.[0] ?? null;
  const running = run?.status === "running";

  // A run is processed in the background: poll its progress, and refresh counts and cards as boards finish.
  useEffect(() => {
    if (!running || !run) return;
    let done = run.tasks_done + run.tasks_failed;
    const timer = window.setInterval(async () => {
      try {
        const next = await data.getDiscoveryRun(run.id);
        setLastRun(next);
        if (next.tasks_done + next.tasks_failed !== done || next.status !== "running") {
          done = next.tasks_done + next.tasks_failed;
          setTick((t) => t + 1);
        }
      } catch {
        // transient: try again on the next tick
      }
    }, RUN_POLL_MS);
    return () => window.clearInterval(timer);
  }, [data, running, run?.id]);  // eslint-disable-line react-hooks/exhaustive-deps
  const filtersActive = [...params.keys()].some((k) => k !== "page");

  // Reviewing the last row on a later page empties it: step back.
  useEffect(() => {
    if (!loading && !error && rows?.length === 0 && page > 0) setPage(page - 1);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loading, error, rows, page]);

  function update(patch: Record<string, string | number | boolean | undefined>) {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(patch)) {
      if (v === undefined || v === "" || v === false) next.delete(k);
      else next.set(k, String(v));
    }
    if (!("page" in patch)) next.delete("page");
    setParams(next, { replace: true });
  }
  const setPage = (p: number) => update({ page: p || undefined });
  const reset = () => setParams({}, { replace: true });
  const refresh = () => { setTick((t) => t + 1); reload(); };

  async function act(id: string, fn: () => Promise<unknown>, done?: string) {
    setBusy(id);
    setActionError(null);
    setNotice(null);
    try {
      await fn();
      if (done) setNotice(done);
      refresh();
    } catch (e) {
      setActionError((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  const s = summary.data;
  const start = page * PAGE_SIZE + 1;
  const end = page * PAGE_SIZE + jobs.length;

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Job discovery</h1>
          {s && (
            <p className="job-count" role="status" aria-live="polite">
              <strong>{s.matching === s.total && !filtersActive ? `${s.total} jobs discovered` : `${s.matching} of ${s.total} jobs shown`}</strong>
              {!query.hide_duplicates && <span className="muted"> (including possible duplicates)</span>}
              {s.matching > 0 && jobs.length > 0 && <span className="muted"> · Showing {start}–{end} of {s.matching} matching jobs</span>}
              <span className="muted small count-breakdown">
                {" "}· {s.by_result.match} match preferences · {s.by_result.unknown} unknown · {s.by_result.rejected} don't match
              </span>
            </p>
          )}
          {s && (s.total > 0 || s.archived > 0) && (
            <p className="small muted source-breakdown" aria-label="Jobs by source">
              {s.total > 0 && <>Sources: {Object.entries(s.by_source).sort(([a], [b]) => a.localeCompare(b)).map(([p, n]) => `${providerLabel(p)} ${n}`).join(" · ")}</>}
              {s.archived > 0 && <>{s.total > 0 ? " · " : ""}{s.archived} earlier postings archived (kept for history and duplicate checks)</>}
              {s.non_vacancies > 0 && <> · {s.non_vacancies} non-vacancy listings hidden (interest forms, talent pools)</>}
            </p>
          )}
        </div>
        <button type="button" className="btn btn-primary" disabled={busy !== null || running}
          onClick={() => act("run", async () => setLastRun(await data.startDiscoveryRun()))}>
          {busy === "run" ? "Starting…" : running ? "Discovery running…" : "Run discovery"}
        </button>
      </div>

      {data.mode === "demo" && (
        <p className="notice">Demo: employers, boards, and postings are fictional and runs are simulated. No job board, provider, or AI is contacted; filter results and technologies are prerecorded.</p>
      )}
      {coverage.data && (
        <details className="card coverage">
          <summary><h2>Sources and coverage</h2></summary>
          <ul className="reasons">
            {coverage.data.providers.map((p) => (
              <li key={p.provider}>
                <strong>{p.provider === "ats_boards" ? "Employer ATS boards" : providerLabel(p.provider)}</strong>{" "}
                <span className={p.available ? "ok-text" : "muted"}>{p.available ? "available" : "unavailable"}</span> — {p.detail}
              </li>
            ))}
          </ul>
          {coverage.data.warnings.map((w) => <p key={w} className="small warn-text">{w}</p>)}
          <p className="small"><Link to="/companies">Manage employers and boards in the Company directory</Link></p>
        </details>
      )}
      {run && <RunSummary run={run} />}
      {actionError && <ErrorBox message={actionError} />}
      {notice && <p className="notice" role="status">{notice} <Link to="/jobs">View saved jobs</Link></p>}
      {query.company_id && (
        <p className="notice">
          Showing jobs for <strong>{company.data?.name ?? "one employer"}</strong>.{" "}
          <button type="button" className="btn btn-small" onClick={() => update({ company: undefined })}>Show all employers</button>
        </p>
      )}

      <form className="toolbar filters" role="search" aria-label="Filter discovered jobs" onSubmit={(e) => e.preventDefault()}>
        <label>Search<input type="search" value={query.q} onChange={(e) => update({ q: e.target.value })} placeholder="Title, company, or location" /></label>
        <label>Role family
          <select value={query.role_family} onChange={(e) => update({ role_family: e.target.value })}>
            <option value="">Any</option>
            {Object.entries(ROLE_FAMILIES).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        </label>
        <label>Location<input value={query.location} onChange={(e) => update({ location: e.target.value })} placeholder="e.g. NJ, Remote" /></label>
        <label>Work arrangement
          <select value={query.workplace} onChange={(e) => update({ workplace: e.target.value })}>
            <option value="">Any</option><option value="remote">Remote</option><option value="hybrid">Hybrid</option><option value="onsite">On-site</option><option value="unknown">Not stated</option>
          </select>
        </label>
        <label>Seniority
          <select value={query.seniority} onChange={(e) => update({ seniority: e.target.value })}>
            <option value="">Any</option>
            {SENIORITY_LEVELS.map((l) => <option key={l} value={l}>{l}</option>)}
          </select>
        </label>
        <label>Min annual salary (USD)
          <input type="number" min={0} step={5000} value={query.min_salary} onChange={(e) => update({ min_salary: e.target.value })} />
        </label>
        <label>Posted within
          <select value={query.posted_within_days} onChange={(e) => update({ posted_within_days: e.target.value })}>
            <option value="">Any time</option><option value="1">1 day</option><option value="7">7 days</option><option value="14">14 days</option><option value="30">30 days</option>
          </select>
        </label>
        <label>Preference match
          <select value={query.filter_result} onChange={(e) => update({ filter_result: e.target.value })}>
            <option value="">Any</option>
            {Object.entries(RESULT_LABELS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        </label>
        <label>Review
          <select value={params.get("review_status") ?? "new"} onChange={(e) => update({ review_status: e.target.value === "new" ? undefined : e.target.value })}>
            {Object.entries(REVIEW_LABELS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
            <option value="all">All</option>
          </select>
        </label>
        <label>Source
          <select value={query.provider} onChange={(e) => update({ provider: e.target.value })}>
            <option value="">Any</option>
            {JOB_PROVIDERS.map((p) => <option key={p} value={p}>{PROVIDER_LABELS[p]}</option>)}
          </select>
        </label>
        <label>Saved history
          <select value={query.saved_state} onChange={(e) => update({ saved_state: e.target.value })}>
            <option value="">Any</option><option value="saved">Saved now</option><option value="unsaved">Previously saved, removed</option><option value="never">Never saved</option>
          </select>
        </label>
        <label>Sort
          <select value={query.sort} onChange={(e) => update({ sort: e.target.value === "discovered" ? undefined : e.target.value })}>
            {Object.entries(DISCOVERY_SORTS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        </label>
        <fieldset className="checks employment-types">
          <legend>Employment type</legend>
          {[...EMPLOYMENT_TYPES, "unknown" as const].map((t) => (
            <label key={t} className="inline-check">
              <input type="checkbox" checked={query.employment_type!.includes(t)}
                onChange={(e) => {
                  const current = query.employment_type!;
                  const next = e.target.checked ? [...current, t] : current.filter((x) => x !== t);
                  update({ employment_type: next.join(",") });
                }} />
              {t === "unknown" ? "Not stated" : t}
            </label>
          ))}
        </fieldset>
        <label className="inline-check">
          <input type="checkbox" checked={!query.hide_duplicates} onChange={(e) => update({ show_duplicates: e.target.checked ? "1" : undefined })} />
          Show possible duplicates
        </label>
        <button type="button" className="btn btn-small" onClick={reset} disabled={!filtersActive}>Reset filters</button>
      </form>
      <p className="muted small filter-note">Jobs missing a salary, posting date, or seniority aren't hidden by those filters, and jobs without a stated employment type stay unless you pick specific types without "Not stated". Salaries are only compared when they're annual USD.</p>

      {loading && !rows ? <Loading what="discovered jobs" /> : error ? <ErrorBox message={error} onRetry={refresh} /> :
        jobs.length === 0 ? (
          filtersActive || page > 0 ? (
            <Empty action={<button type="button" className="btn btn-small" onClick={reset}>Reset filters</button>}>
              No discovered jobs match these filters.
            </Empty>
          ) : (
            <Empty>
              No jobs discovered yet. Jobs appear here once an employer's official job board is verified: use{" "}
              <strong>Retry discovery</strong> in the <Link to="/companies">Company directory</Link>, then <strong>Run discovery</strong> to refresh verified boards.
            </Empty>
          )
        ) : (
          <ul className="job-cards" aria-label="Discovered jobs">
            {jobs.map((j) => <li key={j.id}><JobCard job={j} busy={busy !== null} act={act} /></li>)}
          </ul>
        )}
      {(page > 0 || jobs.length > 0) && (
        <nav className="pager" aria-label="Pagination">
          <button type="button" className="btn btn-small" disabled={page === 0 || loading} onClick={() => setPage(page - 1)}>Previous</button>
          <span>Page {page + 1}{s && s.matching > 0 ? ` of ${Math.max(1, Math.ceil(s.matching / PAGE_SIZE))}` : ""}</span>
          <button type="button" className="btn btn-small" disabled={!hasNext || loading} onClick={() => setPage(page + 1)}>Next</button>
        </nav>
      )}

      <FiltersPanel onSaved={refresh} />
      <p className="muted small attribution">
        Remote job data from <ExternalLink href="https://himalayas.app">Himalayas</ExternalLink>. Applying always happens on the posting's own page.
      </p>
    </>
  );
}

type Act = (id: string, fn: () => Promise<unknown>, done?: string) => void;

function JobCard({ job: j, busy, act }: { job: DiscoveredJob; busy: boolean; act: Act }) {
  const data = useData();
  const [open, setOpen] = useState(false);
  const posting = j.posting_link && safeExternalUrl(j.posting_link.url) ? j.posting_link : null;
  const application = j.application_url && safeExternalUrl(j.application_url) ? j.application_url : null;
  const employerSource = ATS_PROVIDERS.includes(j.provider);
  const website = safeExternalUrl(j.company_info?.website);
  const salary = salaryText(j);
  const facts = [
    j.location ?? "Location not stated",
    j.workplace_type ? WORKPLACE_LABELS[j.workplace_type] ?? j.workplace_type : "Work arrangement not stated",
    j.seniority && `${j.seniority[0].toUpperCase()}${j.seniority.slice(1)} level`,
  ].filter(Boolean);
  const experience = j.experience_required.slice(0, 2);
  const unsaved = j.unsaved_at && j.review_status !== "saved";
  const label = `${j.title} at ${j.company}`;

  return (
    <article className={`job-card job-${j.filter_result}`} aria-labelledby={`job-${j.id}`}>
      <div className="job-card-head">
        <div className="job-identity">
          <div className="job-title-row">
            <h3 id={`job-${j.id}`}>{j.title}</h3>
            <span className={`badge badge-employment${j.employment_type ? "" : " muted"}`}>{j.employment_type ?? "Employment type not stated"}</span>
          </div>
          <div className="job-company">
            {j.company_id ? <Link to={`/companies?company=${encodeURIComponent(j.company_id)}`}>{j.company}<span className="sr-only"> company profile</span></Link> : j.company}
            {j.company_info?.watchlist && <span className="badge badge-match">Watched</span>}
          </div>
        </div>
        <div className="job-badges">
          <span className={`badge badge-${j.filter_result}`}>{RESULT_LABELS[j.filter_result]}</span>
          <span className="badge badge-review">{unsaved ? "Removed from saved" : REVIEW_LABELS[j.review_status]}</span>
        </div>
      </div>

      <p className="job-facts">{facts.join(" · ")}</p>
      <p className="job-salary">{salary ?? <span className="muted">Salary not stated</span>}</p>
      <p className="job-experience small">
        <span className="muted">Experience required: </span>
        {experience.length ? experience.join(" · ") : <span className="muted">Not specified</span>}
        {j.experience_required.length > 2 && <span className="muted"> (+{j.experience_required.length - 2} more in details)</span>}
      </p>
      <p className="job-dates small">
        <span>{j.posted_at ? `Posted ${formatTimestamp(j.posted_at)}` : "Posting date not provided"}</span>
        <span>First discovered {formatTimestamp(j.first_seen_at)}</span>
        <span>Last seen {formatTimestamp(j.last_seen_at)}</span>
      </p>
      <p className="job-reason small">
        {j.filter_reasons[0]}
        {j.filter_reasons.length > 1 && <span className="muted"> (+{j.filter_reasons.length - 1} more in details)</span>}
      </p>
      {j.technologies.length > 0 && (
        <ul className="chips" aria-label="Technologies mentioned">
          {j.technologies.slice(0, 8).map((t) => <li key={t}>{t}</li>)}
          {j.technologies.length > 8 && <li className="muted">+{j.technologies.length - 8}</li>}
        </ul>
      )}
      <p className="job-meta small muted">
        Experience match (AI, optional): {EXPERIENCE_LABELS[j.experience_match.status]}
        {" · "}Source: {providerLabel(j.provider)}{employerSource ? " (employer job board)" : " (job feed)"}
        {j.duplicate_of_id && <span className="warn-text"> · Possible duplicate of a posting seen earlier</span>}
      </p>

      <div className="job-actions">
        <div className="actions review-actions">
          {j.review_status === "saved" ? (
            <Link className="btn btn-small btn-saved" to="/jobs">Saved · View in Saved Jobs</Link>
          ) : (
            <>
              <button type="button" className="btn btn-small btn-primary" disabled={busy}
                onClick={() => act(j.id, () => data.saveDiscoveredJob(j.id), `Saved “${j.title}” to your saved jobs.`)}>
                {unsaved ? "Save again" : "Save"}<span className="sr-only"> {label}</span>
              </button>
              {j.review_status === "new" && (
                <>
                  <button type="button" className="btn btn-small" disabled={busy} onClick={() => act(j.id, () => data.reviewDiscoveredJob(j.id, "later"))}>
                    Review later<span className="sr-only"> {label}</span>
                  </button>
                  <button type="button" className="btn btn-small btn-pass" disabled={busy} onClick={() => act(j.id, () => data.reviewDiscoveredJob(j.id, "skipped"))}>
                    Pass<span className="sr-only"> on {label}</span>
                  </button>
                </>
              )}
              {(j.review_status === "skipped" || j.review_status === "later") && (
                <button type="button" className="btn btn-small" disabled={busy} onClick={() => act(j.id, () => data.reviewDiscoveredJob(j.id, "new"))}>
                  Restore<span className="sr-only"> {label}</span>
                </button>
              )}
            </>
          )}
        </div>
        <div className="actions link-actions">
          {application && (
            <ExternalLink className="btn btn-small btn-primary" href={application}>
              Open employer application<span className="sr-only"> for {label}</span>
            </ExternalLink>
          )}
          {posting ? (
            <ExternalLink className="btn btn-small" href={posting.url}>
              {posting.kind === "employer" ? "Open employer job posting" : `Open posting (via ${providerLabel(posting.via)})`}<span className="sr-only"> for {label}</span>
            </ExternalLink>
          ) : <span className="btn btn-small btn-disabled" aria-disabled="true">No posting link</span>}
          {website ? (
            <ExternalLink className="btn btn-small" href={website}>Company website<span className="sr-only"> for {j.company}</span></ExternalLink>
          ) : <span className="btn btn-small btn-disabled" aria-disabled="true" title="No verified company website">Website unavailable</span>}
          <button type="button" className="btn btn-small btn-ghost" aria-expanded={open} aria-controls={`details-${j.id}`} onClick={() => setOpen(!open)}>
            {open ? "Hide details" : "Details"}<span className="sr-only"> for {label}</span>
          </button>
        </div>
      </div>
      {open && <JobDetails id={j.id} />}
    </article>
  );
}

function JobDetails({ id }: { id: string }) {
  const data = useData();
  const { data: d, error, loading, reload } = useAsync(() => data.getDiscoveredJob(id), [data, id]);
  if (loading) return <div id={`details-${id}`}><Loading what="details" /></div>;
  if (error || !d) return <div id={`details-${id}`}><ErrorBox message={error ?? "Details unavailable"} onRetry={reload} /></div>;
  const r = d.requirements;
  const em = d.experience_match;
  const salary = salaryText(d);
  const posting = safeExternalUrl(d.posting_url);
  const website = safeExternalUrl(d.company_info?.website);
  return (
    <div className="job-details" id={`details-${id}`}>
      <section aria-label="Overview">
        <h4>Overview</h4>
        <dl className="facts">
          <dt>Company</dt><dd>{d.company}{d.company_info && <span className="muted"> · {d.company_info.verification_status === "verified" ? "verified identity" : d.company_info.verification_status === "provider_reported" ? "reported by job feed" : "name only"}</span>}</dd>
          <dt>Company website</dt><dd>{website ? <ExternalLink href={website}>{website}</ExternalLink> : "Not available"}</dd>
          <dt>Employment type</dt><dd>{d.employment_type ?? "Not stated"}</dd>
          <dt>Work arrangement</dt><dd>{d.workplace_type ? WORKPLACE_LABELS[d.workplace_type] ?? d.workplace_type : "Not stated"}</dd>
          <dt>Location</dt><dd>{d.location ?? "Not stated"}</dd>
          <dt>Salary</dt><dd>{salary ?? "Not stated"}</dd>
          <dt>Originally posted</dt><dd>{d.posted_at ? formatTimestamp(d.posted_at) : "Not provided by the source"}</dd>
          <dt>First discovered</dt><dd>{formatTimestamp(d.first_seen_at)}</dd>
          <dt>Last seen</dt><dd>{formatTimestamp(d.last_seen_at)} <span className="muted">(not proof it's still open)</span></dd>
          <dt>Source</dt><dd>{providerLabel(d.provider)}</dd>
          <dt>Original posting</dt><dd>{posting ? <ExternalLink href={posting}>{posting}</ExternalLink> : "No link"}</dd>
          <dt>Preference match</dt><dd>{RESULT_LABELS[d.filter_result]}<ul className="reasons">{d.filter_reasons.map((x) => <li key={x}>{x}</li>)}</ul></dd>
        </dl>
        <h5>Job description</h5>
        {/* Rendered as plain text: markup from external sources is never interpreted. */}
        {d.description ? <div className="description">{d.description}</div> : <p className="muted">The source didn't include a description.</p>}
      </section>
      <section aria-label="Requirements">
        <h4>Requirements</h4>
        {r && r.sections_found ? (
          <>
            {r.required.length > 0 && <><h5>Listed under requirements / qualifications</h5><ul>{r.required.map((x, i) => <li key={i}>{x}</li>)}</ul></>}
            {r.preferred.length > 0 && <><h5>Listed as preferred / nice to have</h5><ul>{r.preferred.map((x, i) => <li key={i}>{x}</li>)}</ul></>}
          </>
        ) : <p className="muted">The posting doesn't separate required and preferred qualifications. Read the full description above.</p>}
        <h5>Experience mentioned</h5>
        {r && r.experience.length > 0 ? <ul>{r.experience.map((x, i) => <li key={i}>{x}</li>)}</ul> : <p className="muted">No years of experience stated.</p>}
        <h5>Technologies mentioned</h5>
        {r && r.technologies.length > 0 ? <ul className="chips">{r.technologies.map((t) => <li key={t}>{t}</li>)}</ul> : <p className="muted">None recognized in the description.</p>}
        <p className="muted small">Read directly from the posting text, without AI. Nothing is inferred.</p>
      </section>
      <section aria-label="Experience match">
        <h4>Experience match</h4>
        <p><strong>{EXPERIENCE_LABELS[em.status]}</strong></p>
        {em.status === "not_evaluated" ? (
          <p className="muted small">
            Experience match compares the job's requirements with your resume using AI, and only runs when you ask.
            {d.review_status === "saved" ? <> Run it from <Link to="/matching">AI matching</Link>.</> : " Save the job first, then run it from AI matching."}
          </p>
        ) : (
          <>
            <p>Overall: {em.overall_match?.replaceAll("_", " ")}{em.evaluated_at && <span className="muted"> · {formatTimestamp(em.evaluated_at)}</span>}</p>
            {em.recommendation && <p className="small">{em.recommendation}</p>}
            {em.status === "previously_evaluated" && <p className="muted small">Your profile changed after this evaluation.</p>}
            <p className="small"><Link to="/matching">See the full assessment in AI matching</Link></p>
          </>
        )}
        <p className="muted small">Preference match (above) uses only your search preferences; experience match uses only your resume. Neither uses company prestige.</p>
      </section>
    </div>
  );
}

const RUN_STATUS: Record<DiscoveryRun["status"], string> = {
  running: "running", succeeded: "completed", partial: "partially completed (some sources failed)", failed: "failed",
};

function RunSummary({ run }: { run: DiscoveryRun }) {
  const problems = run.sources.filter((s) => s.status === "failed");
  const skipped = run.sources.filter((s) => s.status === "skipped");
  const deferred = run.sources.filter((s) => s.status === "deferred");
  const closed = run.sources.reduce((n, s) => n + (s.closed ?? 0), 0);
  const n = (v: number, word: string) => `${v} ${word}${v === 1 ? "" : "s"}`;
  const finished = run.tasks_done + run.tasks_failed;
  return (
    <section className={run.status === "succeeded" || run.status === "running" ? "notice" : "notice notice-warn"}
      aria-label="Discovery run status" aria-live="polite">
      <strong>{run.trigger === "scheduled" ? "Scheduled sync" : "Discovery run"} {formatTimestamp(run.started_at)} — {RUN_STATUS[run.status]}.</strong>{" "}
      {run.tasks_total > 0 && (
        <span className="run-progress">
          {n(run.tasks_total, "source")} queued · {finished} completed · {run.tasks_remaining} remaining
          {run.tasks_failed > 0 && <> · {run.tasks_failed} failed</>}.{" "}
        </span>
      )}
      {run.status === "running" ? <>{run.jobs_inserted} new jobs so far.</> : (
        <>
          {n(run.sources_checked, "source")} checked, {n(run.requests, "API request")}, {run.jobs_retrieved} postings retrieved
          ({run.jobs_unique} unique): {run.jobs_inserted} new, {run.jobs_updated} already known, {n(run.duplicates, "possible duplicate")}.
          {" "}{run.jobs_matched} match your filters, {run.jobs_unknown} unknown.
          {closed > 0 && <> {n(closed, "posting")} no longer listed by the employer (hidden, kept).</>}
          {run.companies_created > 0 && <> {n(run.companies_created, "new employer")} added.</>}
          {run.boards_verified > 0 && <> {n(run.boards_verified, "employer board")} verified.</>}
          {run.sources_failed > 0 && <> {run.sources_failed} failed.</>}
        </>
      )}
      {(problems.length > 0 || skipped.length > 0 || deferred.length > 0 || run.warnings.length > 0) && (
        <ul className="reasons">
          {problems.map((f, i) => <li key={`f${i}`}>Error — {f.company} ({providerLabel(f.provider)}): {f.error}</li>)}
          {deferred.length > 0 && <li>Deferred — {deferred.map((f) => f.company).join(", ")}: {deferred[0].error}</li>}
          {skipped.map((f, i) => <li key={`s${i}`}>Skipped — {providerLabel(f.provider)}: {f.error}</li>)}
          {run.warnings.map((w) => <li key={w}>{w}</li>)}
        </ul>
      )}
    </section>
  );
}

const list = (v: string) => v.split(",").map((s) => s.trim()).filter(Boolean);
type Checks = Pick<DiscoveryFilters, "workplace_types" | "seniority" | "role_families" | "geographies">;

function FiltersPanel({ onSaved }: { onSaved: () => void }) {
  const data = useData();
  const { data: saved, error, loading, reload } = useAsync(() => data.getDiscoveryFilters(), [data]);
  const [form, setForm] = useState<Record<string, string> | null>(null);
  const [checks, setChecks] = useState<Checks>({ workplace_types: [], seniority: [], role_families: [], geographies: [] });
  const [status, setStatus] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);

  useEffect(() => {
    if (!saved) return;
    setForm({
      title_keywords: saved.title_keywords.join(", "), excluded_keywords: saved.excluded_keywords.join(", "),
      locations: saved.locations.join(", "), min_salary: saved.min_salary?.toString() ?? "", salary_currency: saved.salary_currency,
    });
    setChecks({ workplace_types: saved.workplace_types, seniority: saved.seniority, role_families: saved.role_families, geographies: saved.geographies });
  }, [saved]);

  function toggle<K extends keyof Checks>(key: K, value: Checks[K][number], on: boolean) {
    const current = checks[key] as string[];
    setChecks({ ...checks, [key]: on ? [...current, value] : current.filter((v) => v !== value) });
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!form) return;
    setSaveError(null);
    setStatus(null);
    const min = form.min_salary.trim();
    if (min && !/^\d+$/.test(min)) {
      setSaveError("Minimum salary must be a whole number.");
      return;
    }
    try {
      await data.saveDiscoveryFilters({
        title_keywords: list(form.title_keywords), excluded_keywords: list(form.excluded_keywords), locations: list(form.locations),
        ...checks, min_salary: min ? Number(min) : null, salary_currency: form.salary_currency.trim().toUpperCase() || "USD",
      });
      setStatus("Preferences saved. Discovered jobs were re-checked against them.");
      onSaved();
    } catch (err) {
      setSaveError((err as Error).message);
    }
  }

  const text = (k: string) => ({ value: form?.[k] ?? "", onChange: (e: { target: { value: string } }) => setForm({ ...form!, [k]: e.target.value }) });
  const group = <K extends keyof Checks>(key: K, legend: string, options: [Checks[K][number], string][]) => (
    <fieldset className="checks">
      <legend>{legend}</legend>
      {options.map(([value, label]) => (
        <label key={value} className="inline-check">
          <input type="checkbox" checked={(checks[key] as string[]).includes(value)} onChange={(e) => toggle(key, value, e.target.checked)} />{label}
        </label>
      ))}
    </fieldset>
  );

  return (
    <details className="card panel">
      <summary><h2>Search preferences</h2></summary>
      <p className="muted small">Plain rules, no AI. Titles that are close to your role families are marked Unknown rather than filtered out, and missing data (location, seniority, salary) is never guessed. Separate multiple values with commas.</p>
      {loading ? <Loading what="preferences" /> : error ? <ErrorBox message={error} onRetry={reload} /> : (
        <form className="form" onSubmit={submit} aria-label="Search preferences" noValidate>
          {saveError && <ErrorBox message={saveError} />}
          {status && <p className="notice" role="status">{status}</p>}
          {group("role_families", "Role families", Object.entries(ROLE_FAMILIES) as [Checks["role_families"][number], string][])}
          {group("geographies", "Geographies", Object.entries(GEOGRAPHIES) as [Checks["geographies"][number], string][])}
          <div className="form-grid">
            <label>Extra title keywords<input {...text("title_keywords")} placeholder="python, golang" /></label>
            <label>Excluded title keywords<input {...text("excluded_keywords")} placeholder="sales, clearance" /></label>
            <label>Other locations<input {...text("locations")} placeholder="Philadelphia" /></label>
            <label>Minimum annual salary<input inputMode="numeric" {...text("min_salary")} /></label>
            <label>Salary currency<input maxLength={3} {...text("salary_currency")} /></label>
          </div>
          {group("workplace_types", "Workplace", WORKPLACE_TYPES.map((w) => [w, w]))}
          {group("seniority", "Seniority", SENIORITY_LEVELS.map((s) => [s, s]))}
          <div className="form-actions"><button type="submit" className="btn btn-primary">Save preferences</button></div>
        </form>
      )}
    </details>
  );
}

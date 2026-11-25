import { Fragment, useState, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router";
import { useData } from "../api/context";
import { useAsync } from "../hooks/useAsync";
import { Empty, ErrorBox, ExternalLink, Loading, safeExternalUrl } from "../components/ui";
import { formatTimestamp } from "../dates";
import { PROVIDER_LABELS } from "../types";
import type { Company, CompanyQuery, JobSource } from "../types";

const PAGE_SIZE = 25;
const ORIGIN_LABELS: Record<Company["origin"], string> = { curated_seed: "Suggested", manual: "Added by you", discovered: "Discovered" };
const ATS_LABELS: Record<Company["ats_status"], string> = { verified: "Verified", needs_review: "Needs review", unverified: "Unverified", failed: "Not found", none: "No board" };
const SYNC_LABELS: Record<NonNullable<JobSource["sync_status"]>, string> = {
  queued: "queued", syncing: "syncing now", failed: "last attempt failed", ok: "up to date", never: "not synced yet", not_imported: "not imported",
};
const DISCOVERY_LABELS: Record<Company["discovery_status"], string> = {
  not_checked: "Not checked", discovering: "Discovering", verified: "Verified", needs_review: "Needs review",
  unsupported_ats: "Unsupported ATS", not_found: "Not found", failed: "Failed",
};
const IDENTITY_LABELS: Record<Company["verification_status"], string> = {
  verified: "Verified domain", provider_reported: "Reported by job feed", unresolved: "Unresolved (name only)",
};
const DEFAULT_QUERY: CompanyQuery = { q: "", ats_status: "", origin: "", sort: "name" };

export function CompaniesPage() {
  const data = useData();
  const [params, setParams] = useSearchParams();
  const focusId = params.get("company");
  const [query, setQuery] = useState(DEFAULT_QUERY);
  const [page, setPage] = useState(0);
  const [open, setOpen] = useState<string | null>(focusId);
  const [showForm, setShowForm] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const { data: rows, error, loading, reload } = useAsync(
    () => (focusId ? data.getCompany(focusId).then((c) => [c]) : data.listCompanies(query, PAGE_SIZE + 1, page * PAGE_SIZE)),
    [data, query, page, focusId],
  );
  const companies = rows?.slice(0, PAGE_SIZE) ?? [];
  const hasNext = (rows?.length ?? 0) > PAGE_SIZE;
  const set = (patch: Partial<CompanyQuery>) => { setQuery({ ...query, ...patch }); setPage(0); };

  async function act(fn: () => Promise<unknown>) {
    setActionError(null);
    try {
      await fn();
      reload();
    } catch (e) {
      setActionError((e as Error).message);
    }
  }

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Company directory</h1>
          <p className="page-sub">Employers you watch or that discovery found, and their job boards. Every employer is treated the same; a verified board means its official job API answered.</p>
        </div>
        <button type="button" className="btn btn-primary" onClick={() => setShowForm((v) => !v)} aria-expanded={showForm}>
          {showForm ? "Close form" : "Add company"}
        </button>
      </div>
      {showForm && <CompanyForm onCreated={() => { setShowForm(false); reload(); }} />}
      {actionError && <ErrorBox message={actionError} />}

      {focusId ? (
        <p className="notice">Showing one employer. <button type="button" className="btn btn-small" onClick={() => { setParams({}); setOpen(null); }}>Show all companies</button></p>
      ) : (
        <div className="toolbar" role="search">
          <label>Search<input type="search" value={query.q} onChange={(e) => set({ q: e.target.value })} placeholder="Name or domain" /></label>
          <label>ATS board
            <select value={query.ats_status} onChange={(e) => set({ ats_status: e.target.value as CompanyQuery["ats_status"] })}>
              <option value="">All</option>
              {Object.entries(ATS_LABELS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
            </select>
          </label>
          <label>Status
            <select value={query.origin} onChange={(e) => set({ origin: e.target.value as CompanyQuery["origin"] })}>
              <option value="">All</option>
              {Object.entries(ORIGIN_LABELS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
            </select>
          </label>
          <label>Sort
            <select value={query.sort} onChange={(e) => set({ sort: e.target.value as CompanyQuery["sort"] })}>
              <option value="name">Name</option><option value="jobs">Most jobs</option><option value="last_seen">Recently seen</option>
            </select>
          </label>
        </div>
      )}

      {loading ? <Loading what="companies" /> : error ? <ErrorBox message={error} onRetry={reload} /> :
        companies.length === 0 ? (
          JSON.stringify(query) !== JSON.stringify(DEFAULT_QUERY) ? (
            <Empty action={<button type="button" className="btn btn-small" onClick={() => set(DEFAULT_QUERY)}>Clear filters</button>}>No companies match these filters.</Empty>
          ) : (
            <Empty>No companies yet. Add one, run discovery, or import the curated list with <code>python -m scripts.seed_companies</code>.</Empty>
          )
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr><th scope="col">Company</th><th scope="col">Focus</th><th scope="col">Status</th><th scope="col">ATS</th><th scope="col">Last sync</th><th scope="col">Jobs</th><th scope="col"><span className="sr-only">Actions</span></th></tr>
              </thead>
              <tbody>
                {companies.map((c) => (
                  <Fragment key={c.id}>
                    <tr>
                      <td data-label="Company">
                        <strong>{c.name}</strong>
                        {c.verification_status === "verified" && safeExternalUrl(c.website)
                          ? <div className="small"><ExternalLink href={c.website!}>{c.domain ?? c.website}</ExternalLink></div>
                          : <div className="muted small">{IDENTITY_LABELS[c.verification_status]}</div>}
                      </td>
                      <td data-label="Focus" className="small">{c.engineering_focus ?? "—"}<div className="muted">{c.target_geography ?? ""}</div></td>
                      <td data-label="Status">
                        {c.watchlist ? <span className="badge badge-match">Watched</span> : <span className="muted">{ORIGIN_LABELS[c.origin]}</span>}
                      </td>
                      <td data-label="ATS">
                        <span className={`badge badge-ats-${c.ats_status}`}>{ATS_LABELS[c.ats_status]}</span>
                        {c.boards.length > 0 && <div className="muted small">{[...new Set(c.boards.map((b) => PROVIDER_LABELS[b.provider]))].join(", ")}</div>}
                        <div className="small">Careers: {DISCOVERY_LABELS[c.discovery_status]}{c.unsupported_ats ? ` (${c.unsupported_ats})` : ""}</div>
                      </td>
                      <td data-label="Last sync">{c.last_sync_at ? formatTimestamp(c.last_sync_at) : "—"}</td>
                      <td data-label="Jobs">
                        {c.job_count > 0 ? <Link to={`/discovery?company=${encodeURIComponent(c.id)}`}>{c.job_count}<span className="sr-only"> jobs at {c.name}</span></Link> : 0}
                      </td>
                      <td className="row-actions">
                        <button type="button" className="btn btn-small" onClick={() => act(() => data.updateCompany(c.id, { watchlist: !c.watchlist }))}>
                          {c.watchlist ? "Unwatch" : "Watch"}<span className="sr-only"> {c.name}</span>
                        </button>
                        <button type="button" className="btn btn-small" aria-expanded={open === c.id} onClick={() => setOpen(open === c.id ? null : c.id)}>
                          Details<span className="sr-only"> for {c.name}</span>
                        </button>
                      </td>
                    </tr>
                    {open === c.id && (
                      <tr className="editing"><td colSpan={7}><CompanyDetails company={c} onChanged={reload} /></td></tr>
                    )}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        )}
      {!focusId && (
        <nav className="pager" aria-label="Pagination">
          <button type="button" className="btn btn-small" disabled={page === 0 || loading} onClick={() => setPage(page - 1)}>Previous</button>
          <span>Page {page + 1}</span>
          <button type="button" className="btn btn-small" disabled={!hasNext || loading} onClick={() => setPage(page + 1)}>Next</button>
        </nav>
      )}
    </>
  );
}

function CompanyDetails({ company: c, onChanged }: { company: Company; onChanged: () => void }) {
  const data = useData();
  const [url, setUrl] = useState("");
  const [careers, setCareers] = useState(c.careers_url ?? "");
  // The latest candidate while the company needs review; never applied without a click.
  const last = [...c.discovery_evidence].reverse().find((e) => e.candidate);
  const candidates = c.discovery_status === "needs_review" && last && last.candidate !== c.domain && last.candidate !== c.careers_url ? [last] : [];
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);

  async function run(fn: () => Promise<unknown>, ok?: string) {
    setBusy(true);
    setMessage(null);
    try {
      await fn();
      if (ok) setMessage({ ok: true, text: ok });
      onChanged();
    } catch (e) {
      setMessage({ ok: false, text: (e as Error).message });
    } finally {
      setBusy(false);
    }
  }

  async function verify(e: FormEvent) {
    e.preventDefault();
    if (!url.trim()) return;
    await run(async () => {
      const board = await data.verifyCompanyBoard(c.id, url.trim());
      setUrl("");
      if (board.verification_status !== "verified") throw new Error(`Board ${board.board_token} could not be verified: ${board.last_error ?? "unknown error"}`);
    }, "Board verified. Its jobs are imported on Retry discovery or the next discovery run.");
  }

  async function retry(e?: FormEvent) {
    e?.preventDefault();
    await run(async () => {
      const next = careers.trim() || null;
      if (next !== c.careers_url) await data.updateCompany(c.id, { careers_url: next });
      await data.discoverCompany(c.id);  // the outcome (or lookup problem) shows in the refreshed details
    });
  }

  return (
    <section className="details" aria-label={`Details for ${c.name}`}>
      <p className="small muted">
        {IDENTITY_LABELS[c.verification_status]} · {ORIGIN_LABELS[c.origin]}
        {c.first_seen_at && <> · first seen {formatTimestamp(c.first_seen_at)}</>}
        {c.last_seen_at && <> · last seen {formatTimestamp(c.last_seen_at)}</>}
      </p>
      {c.notes && <p className="small">{c.notes}</p>}
      <h3>Career source</h3>
      <p className="small">
        <strong>{DISCOVERY_LABELS[c.discovery_status]}</strong>
        {c.discovery_checked_at && <> · checked {formatTimestamp(c.discovery_checked_at)}</>}
        {c.website_verified_at && <> · website answered {formatTimestamp(c.website_verified_at)}</>}
        {c.unsupported_ats && <> · uses {c.unsupported_ats} (not supported yet)</>}
      </p>
      <p className="small">Careers page: {c.careers_url && safeExternalUrl(c.careers_url) ? <ExternalLink href={c.careers_url}>{c.careers_url}</ExternalLink> : <span className="muted">not found yet</span>}</p>
      {c.discovery_error && <p className="danger-text small">Lookup problem: {c.discovery_error}</p>}
      {candidates.map((e) => (
        <p key={`${e.step}-${e.candidate}`} className="small">
          {e.step === "domain_candidate" ? <>The website redirects to <strong>{e.candidate}</strong>. Accept it only if it's this company's new official domain.</>
            : <>Careers link to another domain: <strong>{e.candidate}</strong>. Use it only if it's this company's official careers site.</>}{" "}
          <button type="button" className="btn btn-small" disabled={busy} onClick={() => run(async () => {
            await data.updateCompany(c.id, e.step === "domain_candidate" ? { domain: e.candidate! } : { careers_url: e.candidate! });
            await data.discoverCompany(c.id);
          })}>
            {e.step === "domain_candidate" ? `Use ${e.candidate} as official domain` : "Use as careers page"}
          </button>
        </p>
      ))}
      {c.discovery_evidence.length > 0 && (
        <details className="small">
          <summary>Evidence ({c.discovery_evidence.length})</summary>
          <ul className="reasons">
            {c.discovery_evidence.slice(-10).map((e, i) => <li key={i}>{e.detail}{e.url && <span className="muted"> — {e.url}</span>}</li>)}
          </ul>
        </details>
      )}
      <form className="form-grid add-source" onSubmit={retry} aria-label={`Career discovery for ${c.name}`} noValidate>
        <label>Official careers page URL (optional)
          <input type="url" maxLength={2048} value={careers} onChange={(e) => setCareers(e.target.value)} placeholder="https://example.com/careers" />
        </label>
        <div className="form-actions">
          <button type="submit" className="btn" disabled={busy}>{busy ? "Working…" : "Retry discovery"}</button>
          <button type="button" className="btn btn-ghost" disabled={busy} title="Reads more pages and scripts of this company's site (up to 150 requests)"
            onClick={() => run(() => data.discoverCompany(c.id, true))}>Deep scan</button>
        </div>
      </form>
      <p className="muted small">Reads the official website (or the careers URL you enter) for Greenhouse, Lever, or Ashby links, checks each board with the provider's official API, and imports its jobs. Domains are never guessed from the name.</p>
      <h3>ATS boards</h3>
      {c.boards.length === 0 ? <p className="muted small">No verified board yet.</p> : (
        <ul className="list">
          {c.boards.map((b) => (
            <li key={b.id}>
              <div className="list-main">
                <strong>{PROVIDER_LABELS[b.provider]} · {b.board_token}</strong>
                {b.board_role && <span className="muted small">{b.board_role === "talent_pool" ? "Prospect / talent pool board (interest forms are hidden from Job discovery)" : "General hiring board"}</span>}
                {safeExternalUrl(b.board_url) && <ExternalLink className="small" href={b.board_url}>{b.board_url}</ExternalLink>}
                <span className="muted small">
                  {b.verification_status === "verified" ? `Verified ${b.verified_at ? formatTimestamp(b.verified_at) : ""}` : ATS_LABELS[b.verification_status]}
                  {b.region !== "Global" && <> · {b.region} region</>}
                  {b.last_checked_at && <> · last checked {formatTimestamp(b.last_checked_at)}</>}
                  {b.last_success_at && <> · last synced {formatTimestamp(b.last_success_at)}</>}
                </span>
                {b.provider_name && <span className="muted small">Name reported by {PROVIDER_LABELS[b.provider]}: {b.provider_name}</span>}
                {b.sync_status && b.sync_status !== "not_imported" && (
                  <span className={`small ${b.sync_status === "failed" ? "danger-text" : "muted"}`}>
                    Sync: {SYNC_LABELS[b.sync_status]} · {b.active_jobs ?? 0} active jobs
                    {b.next_sync_at && <> · next {formatTimestamp(b.next_sync_at)}</>}
                    {(b.sync_failures ?? 0) > 0 && <> · {b.sync_failures} failed attempts in a row</>}
                  </span>
                )}
                {b.ownership_evidence ? <span className="muted small">Ownership: {b.ownership_evidence}</span>
                  : <span className="warn-text small">Ownership not verified: this board's jobs are not imported.</span>}
                {b.verification_evidence && <span className="muted small">Evidence: {b.verification_evidence}</span>}
                {b.last_error && <span className="danger-text small">Last error: {b.last_error}</span>}
              </div>
              {b.ingestion_supported === false ? (
                <span className="muted small">Job import not supported yet for {PROVIDER_LABELS[b.provider]}</span>
              ) : (
                <label className="inline-check">
                  <input type="checkbox" checked={b.enabled} disabled={busy} onChange={(e) => run(() => data.updateJobSource(b.id, { enabled: e.target.checked }))} />
                  Monitor<span className="sr-only"> {b.provider} board {b.board_token}</span>
                </label>
              )}
            </li>
          ))}
        </ul>
      )}
      <form className="form-grid add-source" onSubmit={verify} aria-label={`Verify a job board for ${c.name}`} noValidate>
        <label>Greenhouse, Lever, or Ashby URL
          <input type="url" maxLength={2048} value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://jobs.lever.co/company/…" />
        </label>
        <div className="form-actions"><button type="submit" className="btn btn-primary" disabled={busy || !url.trim()}>{busy ? "Verifying…" : "Verify board"}</button></div>
      </form>
      <p className="muted small">Pasting a board confirms it belongs to this employer. Only the board name is read from the link; the provider's official job API is checked once.</p>
      {message && (message.ok ? <p className="notice" role="status">{message.text}</p> : <ErrorBox message={message.text} />)}
    </section>
  );
}

function CompanyForm({ onCreated }: { onCreated: () => void }) {
  const data = useData();
  const [form, setForm] = useState({ name: "", domain: "" });
  const [error, setError] = useState<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!form.name.trim()) {
      setError("Company name is required.");
      return;
    }
    try {
      await data.createCompany({ name: form.name.trim(), domain: form.domain.trim() || null, watchlist: true });
      onCreated();
    } catch (err) {
      setError((err as Error).message);
    }
  }

  return (
    <form className="card form" onSubmit={submit} aria-labelledby="add-company-h" noValidate>
      <h2 id="add-company-h">Add an employer</h2>
      {error && <ErrorBox message={error} />}
      <div className="form-grid">
        <label>Name *<input maxLength={200} value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} /></label>
        <label>Domain<input maxLength={253} value={form.domain} onChange={(e) => setForm({ ...form, domain: e.target.value })} placeholder="example.com" /></label>
      </div>
      <div className="form-actions"><button type="submit" className="btn btn-primary">Add company</button></div>
    </form>
  );
}

import { useEffect, useState, type FormEvent } from "react";
import { useSearchParams } from "react-router";
import { useData } from "../api/context";
import { fetchAll } from "../api/fetchAll";
import { useAsync } from "../hooks/useAsync";
import { Empty, ErrorBox, Loading, PostingLink, StatusBadge, saveBlob, statusLabel } from "../components/ui";
import { addDays, formatDate, today } from "../dates";
import { STATUSES, type Application, type ApplicationFilters, type ApplicationStatus } from "../types";

const PAGE_SIZE = 20;
const NO_FILTERS: ApplicationFilters = { status: "", company: "", applied_from: "", applied_to: "" };

/** Mirrors the backend rules so users see problems before submitting. The server still validates. */
function dateError(applied: string, followUp: string): string | null {
  if (!applied) return "Application date is required.";
  if (applied > addDays(today(), 1)) return "Application date cannot be in the future.";
  if (followUp && followUp < applied) return "Follow-up date cannot be before the application date.";
  return null;
}

export function ApplicationsPage() {
  const data = useData();
  const [params, setParams] = useSearchParams();
  const recordJobId = params.get("record");
  const [draft, setDraft] = useState(NO_FILTERS);
  const [filters, setFilters] = useState(NO_FILTERS);
  const [page, setPage] = useState(0);
  const [exportError, setExportError] = useState<string | null>(null);
  const [exporting, setExporting] = useState(false);
  const [showCreate, setShowCreate] = useState(recordJobId !== null);

  // Fetch one extra row to know whether a next page exists (the API has no total count).
  const { data: rows, error, loading, reload } = useAsync(
    () => data.listApplications(filters, PAGE_SIZE + 1, page * PAGE_SIZE),
    [data, filters, page],
  );
  const hasNext = (rows?.length ?? 0) > PAGE_SIZE;
  const apps = rows?.slice(0, PAGE_SIZE) ?? [];
  const filtersActive = Object.values(filters).some(Boolean);

  // A delete (or an edit that leaves the filter) can empty a later page: step back until rows appear or page 1.
  useEffect(() => {
    if (!loading && !error && rows?.length === 0 && page > 0) setPage((p) => p - 1);
  }, [loading, error, rows, page]);

  function applyFilters(e: FormEvent) {
    e.preventDefault();
    setFilters(draft);
    setPage(0);
  }

  function clearFilters() {
    setDraft(NO_FILTERS);
    setFilters(NO_FILTERS);
    setPage(0);
  }

  function closeCreate() {
    setShowCreate(false);
    if (recordJobId) setParams({}, { replace: true });
  }

  async function exportXlsx() {
    setExporting(true);
    setExportError(null);
    try {
      const { blob, filename } = await data.exportApplications(filters);
      saveBlob(blob, filename);
    } catch (err) {
      setExportError((err as Error).message);
    } finally {
      setExporting(false);
    }
  }

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Applications</h1>
          <p className="page-sub">Applications you've confirmed as submitted.</p>
        </div>
        <div className="actions">
          <button type="button" className="btn" onClick={exportXlsx} disabled={exporting}>
            {exporting ? "Preparing…" : data.mode === "demo" ? "Download sample Excel" : "Download Excel"}
          </button>
          <button type="button" className="btn btn-primary" onClick={() => (showCreate ? closeCreate() : setShowCreate(true))} aria-expanded={showCreate}>
            {showCreate ? "Close form" : "Record application"}
          </button>
        </div>
      </div>
      {data.mode === "demo" && <p className="muted small">Demo export is a pre-generated sample workbook with fictional data.</p>}
      {data.mode === "live" && filtersActive && <p className="muted small">Excel export includes all applications matching the current filters.</p>}
      {exportError && <ErrorBox message={`Export failed: ${exportError}`} />}

      {showCreate && (
        <CreateApplicationForm
          initialJobId={recordJobId ?? ""}
          onCancel={closeCreate}
          onCreated={() => { closeCreate(); setPage(0); reload(); }}
        />
      )}

      <form className="toolbar" onSubmit={applyFilters} aria-label="Filter applications">
        <label>
          Status
          <select value={draft.status} onChange={(e) => setDraft({ ...draft, status: e.target.value as ApplicationStatus | "" })}>
            <option value="">All statuses</option>
            {STATUSES.map((s) => <option key={s} value={s}>{statusLabel(s)}</option>)}
          </select>
        </label>
        <label>
          Company
          <input type="search" maxLength={200} value={draft.company} onChange={(e) => setDraft({ ...draft, company: e.target.value })} />
        </label>
        <label>
          Applied from
          <input type="date" value={draft.applied_from} onChange={(e) => setDraft({ ...draft, applied_from: e.target.value })} />
        </label>
        <label>
          Applied to
          <input type="date" value={draft.applied_to} onChange={(e) => setDraft({ ...draft, applied_to: e.target.value })} />
        </label>
        <div className="toolbar-actions">
          <button type="submit" className="btn">Apply filters</button>
          <button type="button" className="btn btn-ghost" onClick={clearFilters}>Clear</button>
        </div>
      </form>

      {loading ? <Loading what="applications" /> : error ? <ErrorBox message={error} onRetry={reload} /> :
        apps.length === 0 ? (
          filtersActive || page > 0 ? (
            <Empty action={<button type="button" className="btn btn-small" onClick={clearFilters}>Clear filters</button>}>
              No applications match these filters.
            </Empty>
          ) : (
            <Empty action={!showCreate && <button type="button" className="btn btn-primary btn-small" onClick={() => setShowCreate(true)}>Record application</button>}>
              No applications recorded yet. Record one once you've actually submitted it.
            </Empty>
          )
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr><th scope="col">Company</th><th scope="col">Title</th><th scope="col">Status</th><th scope="col">Applied</th><th scope="col">Follow-up</th><th scope="col">Notes</th><th scope="col"><span className="sr-only">Actions</span></th></tr>
              </thead>
              <tbody>
                {apps.map((a) => <ApplicationRow key={a.id} app={a} onChanged={reload} />)}
              </tbody>
            </table>
          </div>
        )}

      <nav className="pager" aria-label="Pagination">
        <button type="button" className="btn btn-small" disabled={page === 0 || loading} onClick={() => setPage(page - 1)}>Previous</button>
        <span>Page {page + 1}</span>
        <button type="button" className="btn btn-small" disabled={!hasNext || loading} onClick={() => setPage(page + 1)}>Next</button>
      </nav>
    </>
  );
}

function ApplicationRow({ app, onChanged }: { app: Application; onChanged: () => void }) {
  const data = useData();
  const [editing, setEditing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const label = `${app.job.title} at ${app.job.company}`;

  async function remove() {
    if (!window.confirm(`Delete the application for ${label}? The saved job is kept. This cannot be undone.`)) return;
    try {
      await data.deleteApplication(app.id);
      onChanged();
    } catch (err) {
      setError((err as Error).message);
    }
  }

  if (editing) {
    return (
      <tr className="editing">
        <td colSpan={7}>
          <EditApplicationForm app={app} onDone={(changed) => { setEditing(false); if (changed) onChanged(); }} />
        </td>
      </tr>
    );
  }

  return (
    <tr>
      <td data-label="Company"><strong>{app.job.company}</strong></td>
      <td data-label="Title">{app.job.title}<div className="small"><PostingLink url={app.job.posting_url} /></div></td>
      <td data-label="Status"><StatusBadge status={app.status} /></td>
      <td data-label="Applied">{formatDate(app.applied_date)}</td>
      <td data-label="Follow-up">{formatDate(app.follow_up_date)}</td>
      <td data-label="Notes" className="notes">{app.notes ?? "—"}</td>
      <td className="row-actions">
        <button type="button" className="btn btn-small" onClick={() => setEditing(true)}>Edit<span className="sr-only"> {label}</span></button>
        <button type="button" className="btn btn-small btn-danger" onClick={remove}>Delete<span className="sr-only"> {label}</span></button>
        {error && <ErrorBox message={error} />}
      </td>
    </tr>
  );
}

function EditApplicationForm({ app, onDone }: { app: Application; onDone: (changed: boolean) => void }) {
  const data = useData();
  const [status, setStatus] = useState(app.status);
  const [applied, setApplied] = useState(app.applied_date);
  const [followUp, setFollowUp] = useState(app.follow_up_date ?? "");
  const [notes, setNotes] = useState(app.notes ?? "");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    const invalid = dateError(applied, followUp);
    if (invalid) return setError(invalid);
    setSaving(true);
    setError(null);
    try {
      await data.updateApplication(app.id, {
        status, applied_date: applied, follow_up_date: followUp || null, notes: notes.trim() || null,
      });
      onDone(true);
    } catch (err) {
      setError((err as Error).message);
      setSaving(false);
    }
  }

  return (
    <form className="form" onSubmit={submit} noValidate aria-label={`Edit application for ${app.job.title} at ${app.job.company}`}>
      <strong>{app.job.company} — {app.job.title}</strong>
      {error && <ErrorBox message={error} />}
      <div className="form-grid">
        <label>Status
          <select value={status} onChange={(e) => setStatus(e.target.value as ApplicationStatus)}>
            {STATUSES.map((s) => <option key={s} value={s}>{statusLabel(s)}</option>)}
          </select>
        </label>
        <label>Application date<input type="date" required value={applied} onChange={(e) => setApplied(e.target.value)} /></label>
        <label>Follow-up date<input type="date" min={applied} value={followUp} onChange={(e) => setFollowUp(e.target.value)} /></label>
      </div>
      <label>Notes<textarea rows={2} value={notes} onChange={(e) => setNotes(e.target.value)} /></label>
      <div className="form-actions">
        <button type="submit" className="btn btn-primary" disabled={saving}>{saving ? "Saving…" : "Save changes"}</button>
        <button type="button" className="btn btn-ghost" onClick={() => onDone(false)}>Cancel</button>
      </div>
    </form>
  );
}

function CreateApplicationForm(props: { initialJobId: string; onCreated: () => void; onCancel: () => void }) {
  const data = useData();
  const [jobId, setJobId] = useState(props.initialJobId);
  const [status, setStatus] = useState<ApplicationStatus>("applied");
  const [applied, setApplied] = useState(today());
  const [followUp, setFollowUp] = useState("");
  const [notes, setNotes] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  // Only jobs without an application are eligible (one application per saved job).
  const jobs = useAsync(async () => {
    const [all, apps] = await Promise.all([fetchAll(data.listJobs), fetchAll((l, o) => data.listApplications({}, l, o))]);
    const taken = new Set(apps.map((a) => a.job_id));
    return all.filter((j) => !taken.has(j.id));
  }, [data]);
  // A preselected ID from the URL counts only if that job is still eligible.
  const selectedJob = jobs.data?.some((j) => j.id === jobId) ? jobId : "";

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!selectedJob) return setError("Choose the saved job you applied to.");
    const invalid = dateError(applied, followUp);
    if (invalid) return setError(invalid);
    if (!confirmed) return setError("Confirm that you actually submitted this application.");
    setSaving(true);
    setError(null);
    try {
      await data.createApplication({
        job_id: selectedJob, submission_confirmed: true, status, applied_date: applied,
        follow_up_date: followUp || null, notes: notes.trim() || null,
      });
      props.onCreated();
    } catch (err) {
      setError((err as Error).message);
      setSaving(false);
    }
  }

  return (
    <form className="card form" onSubmit={submit} aria-labelledby="create-app-h" noValidate>
      <h2 id="create-app-h">Record a submitted application</h2>
      {error && <ErrorBox message={error} />}
      {jobs.loading ? <Loading what="saved jobs" /> : jobs.error ? <ErrorBox message={jobs.error} onRetry={jobs.reload} /> :
        jobs.data?.length === 0 ? <Empty>Every saved job already has an application. Add a new saved job first.</Empty> : (
          <>
            <label>Saved job *
              <select required value={selectedJob} onChange={(e) => setJobId(e.target.value)}>
                <option value="">Select a saved job…</option>
                {jobs.data?.map((j) => <option key={j.id} value={j.id}>{j.company} — {j.title}</option>)}
              </select>
            </label>
            <div className="form-grid">
              <label>Status
                <select value={status} onChange={(e) => setStatus(e.target.value as ApplicationStatus)}>
                  {STATUSES.map((s) => <option key={s} value={s}>{statusLabel(s)}</option>)}
                </select>
              </label>
              <label>Application date *<input type="date" required value={applied} onChange={(e) => setApplied(e.target.value)} /></label>
              <label>Follow-up date<input type="date" min={applied} value={followUp} onChange={(e) => setFollowUp(e.target.value)} /></label>
            </div>
            <label>Notes<textarea rows={2} value={notes} onChange={(e) => setNotes(e.target.value)} /></label>
            <label className="confirm">
              <input type="checkbox" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} />
              <span>I confirm I actually submitted this application to the employer. Saving a job, drafting, or clicking an apply link does not count.</span>
            </label>
            <div className="form-actions">
              <button type="submit" className="btn btn-primary" disabled={saving || !confirmed}>{saving ? "Saving…" : "Record application"}</button>
              <button type="button" className="btn btn-ghost" onClick={props.onCancel}>Cancel</button>
            </div>
          </>
        )}
    </form>
  );
}

import { useState, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router";
import { useData } from "../api/context";
import { fetchAll } from "../api/fetchAll";
import { useAsync } from "../hooks/useAsync";
import { Empty, ErrorBox, Loading, PostingLink, StatusBadge } from "../components/ui";
import { formatTimestamp } from "../dates";
import type { JobCreate } from "../types";

const EMPTY_JOB = { company: "", title: "", location: "", posting_url: "", description: "", notes: "" };

export function JobsPage() {
  const data = useData();
  const [search, setSearch] = useState("");
  const [appliedFilter, setAppliedFilter] = useState<"all" | "open" | "applied">("all");
  const [params] = useSearchParams();
  const [showForm, setShowForm] = useState(params.has("add")); // Dashboard "Add job" links here with ?add=1.
  const [actionError, setActionError] = useState<string | null>(null);
  const [removed, setRemoved] = useState<string | null>(null);
  const { data: result, error, loading, reload } = useAsync(
    () => Promise.all([fetchAll(data.listJobs), fetchAll((l, o) => data.listApplications({}, l, o))]),
    [data],
  );

  async function unsave(id: string, title: string) {
    if (!window.confirm(`Remove “${title}” from Saved Jobs? Nothing is deleted, and you can save it again later.`)) return;
    setActionError(null);
    setRemoved(null);
    try {
      await data.unsaveJob(id);
      setRemoved(title);
      reload();
    } catch (e) {
      setActionError((e as Error).message);
    }
  }

  const appByJob = new Map(result?.[1].map((a) => [a.job_id, a]));
  const q = search.trim().toLowerCase();
  const jobs = (result?.[0] ?? []).filter((j) => {
    const applied = appByJob.has(j.id);
    if (appliedFilter === "open" && applied) return false;
    if (appliedFilter === "applied" && !applied) return false;
    return !q || [j.company, j.title, j.location ?? ""].some((v) => v.toLowerCase().includes(q));
  });

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Saved jobs</h1>
          <p className="page-sub">Opportunities you're tracking. Saving a job doesn't mark it as applied.</p>
        </div>
        <button type="button" className="btn btn-primary" onClick={() => setShowForm((v) => !v)} aria-expanded={showForm}>
          {showForm ? "Close form" : "Add job"}
        </button>
      </div>

      {showForm && <JobForm onCreated={() => { setShowForm(false); reload(); }} />}
      {actionError && <ErrorBox message={actionError} />}
      {removed && (
        <p className="notice" role="status">
          Removed “{removed}” from Saved Jobs. It's still in <Link to="/discovery?saved_state=unsaved&review_status=all">Job discovery</Link> if it came from there, and you can save it again.
        </p>
      )}

      <div className="toolbar" role="search">
        <label>
          Search
          <input type="search" value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Company, title, or location" />
        </label>
        <label>
          Show
          <select value={appliedFilter} onChange={(e) => setAppliedFilter(e.target.value as typeof appliedFilter)}>
            <option value="all">All saved jobs</option>
            <option value="open">Not yet applied</option>
            <option value="applied">Applied</option>
          </select>
        </label>
      </div>

      {loading ? <Loading what="saved jobs" /> : error ? <ErrorBox message={error} onRetry={reload} /> :
        jobs.length === 0 ? (
          result?.[0].length ? (
            <Empty action={<button type="button" className="btn btn-small" onClick={() => { setSearch(""); setAppliedFilter("all"); }}>Clear filters</button>}>
              No jobs match your filters.
            </Empty>
          ) : (
            <Empty action={!showForm && <button type="button" className="btn btn-primary btn-small" onClick={() => setShowForm(true)}>Add your first job</button>}>
              No saved jobs yet. Add an opportunity you're interested in to start tracking it.
            </Empty>
          )
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr><th scope="col">Company</th><th scope="col">Title</th><th scope="col">Location</th><th scope="col">Saved</th><th scope="col">Posting</th><th scope="col">Application</th><th scope="col"><span className="sr-only">Actions</span></th></tr>
              </thead>
              <tbody>
                {jobs.map((j) => {
                  const app = appByJob.get(j.id);
                  return (
                    <tr key={j.id}>
                      <td data-label="Company"><strong>{j.company}</strong></td>
                      <td data-label="Title">{j.title}{j.notes && <div className="muted small">{j.notes}</div>}</td>
                      <td data-label="Location">{j.location ?? "—"}</td>
                      <td data-label="Saved">{formatTimestamp(j.created_at)}</td>
                      <td data-label="Posting"><PostingLink url={j.posting_url} /></td>
                      <td data-label="Application">
                        {app ? <StatusBadge status={app.status} /> : (
                          <Link className="btn btn-small" to={`/applications?record=${j.id}`}>
                            Record application<span className="sr-only"> for {j.title} at {j.company}</span>
                          </Link>
                        )}
                      </td>
                      <td className="row-actions">
                        {app ? <span className="muted small">Has an application</span> : (
                          <button type="button" className="btn btn-small btn-danger" onClick={() => unsave(j.id, j.title)}>
                            Remove from saved<span className="sr-only"> {j.title} at {j.company}</span>
                          </button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
    </>
  );
}

function JobForm({ onCreated }: { onCreated: () => void }) {
  const data = useData();
  const [form, setForm] = useState(EMPTY_JOB);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const set = (k: keyof typeof EMPTY_JOB) => (e: { target: { value: string } }) => setForm({ ...form, [k]: e.target.value });

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!form.company.trim() || !form.title.trim()) {
      setError("Company and title are required.");
      return;
    }
    const payload: JobCreate = {
      company: form.company.trim(),
      title: form.title.trim(),
      location: form.location.trim() || null,
      posting_url: form.posting_url.trim() || null,
      description: form.description.trim() || null,
      notes: form.notes.trim() || null,
    };
    setSaving(true);
    setError(null);
    try {
      await data.createJob(payload);
      setForm(EMPTY_JOB);
      onCreated();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <form className="card form" onSubmit={submit} aria-labelledby="add-job-h" noValidate>
      <h2 id="add-job-h">Add a job opportunity</h2>
      <p className="muted small">Saving a job does not mark it as applied.</p>
      {error && <ErrorBox message={error} />}
      <div className="form-grid">
        <label>Company *<input required maxLength={200} value={form.company} onChange={set("company")} /></label>
        <label>Title *<input required maxLength={300} value={form.title} onChange={set("title")} /></label>
        <label>Location<input maxLength={200} value={form.location} onChange={set("location")} /></label>
        <label>Posting URL<input type="url" maxLength={2048} placeholder="https://" value={form.posting_url} onChange={set("posting_url")} /></label>
      </div>
      <label>Description<textarea rows={3} value={form.description} onChange={set("description")} /></label>
      <label>Notes<textarea rows={2} value={form.notes} onChange={set("notes")} /></label>
      <div className="form-actions">
        <button type="submit" className="btn btn-primary" disabled={saving}>{saving ? "Saving…" : "Save job"}</button>
      </div>
    </form>
  );
}

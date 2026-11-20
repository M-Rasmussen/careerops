import { useState, type FormEvent } from "react";
import { Link } from "react-router";
import { useData } from "../api/context";
import { fetchAll } from "../api/fetchAll";
import { useAsync } from "../hooks/useAsync";
import { Empty, ErrorBox, Loading } from "../components/ui";
import { formatTimestamp } from "../dates";
import type { CandidateProfile, Job, JobMatch, OverallMatch, Qualification } from "../types";

// Reuses the status badge colors; the text label is always shown alongside.
const OVERALL: Record<OverallMatch, { label: string; badge: string }> = {
  strong: { label: "Strong match", badge: "badge-offer" },
  moderate: { label: "Moderate match", badge: "badge-interviewing" },
  weak: { label: "Weak match", badge: "badge-rejected" },
  insufficient_information: { label: "Insufficient information", badge: "badge-withdrawn" },
};

export function MatchingPage() {
  const data = useData();
  const demo = data.mode === "demo";
  // Page load only reads data. The AI runs solely from the Analyze match button.
  const base = useAsync(() => Promise.all([data.getAIStatus(), data.getProfile(), fetchAll(data.listJobs)]), [data]);
  const history = useAsync(() => data.listMatches(50, 0), [data]);
  const [savedProfile, setSavedProfile] = useState<CandidateProfile | null>(null);
  const [shown, setShown] = useState<JobMatch | null>(null);

  if (base.loading) return <Loading what="AI matching" />;
  if (base.error || !base.data) return <ErrorBox message={base.error ?? "Failed to load"} onRetry={base.reload} />;
  const [status, loadedProfile, jobs] = base.data;
  const profile = savedProfile ?? loadedProfile;

  return (
    <>
      <div className="page-head">
        <div>
          <h1>AI matching</h1>
          <p className="page-sub">Compare your profile with a saved job. Analysis runs only when you click Analyze match.</p>
        </div>
      </div>

      {demo ? (
        <div className="notice" role="note">
          <strong>Demo:</strong> the profile and assessments are fictional, and results are <strong>recorded agent traces</strong> replayed in your browser. No AI provider is contacted.
        </div>
      ) : !status.configured && (
        <div className="notice" role="note">
          <strong>No AI provider configured.</strong> The tracker works normally; analysis is unavailable until you set <code>AI_PROVIDER</code>, <code>AI_MODEL</code>, and <code>AI_BASE_URL</code> (plus <code>AI_API_KEY</code> if your provider needs one) in <code>.env</code> and restart.
        </div>
      )}

      <ProfileForm profile={profile} onSaved={setSavedProfile} />
      <AnalyzeForm
        jobs={jobs}
        blocked={!status.configured ? "AI provider not configured." : !profile ? "Save your profile first." : null}
        onResult={(m) => { setShown(m); history.reload(); }}
      />
      {shown && <AssessmentView match={shown} demo={demo} />}

      <section className="card" aria-labelledby="history-h">
        <div className="card-head"><h2 id="history-h">Previous assessments</h2></div>
        {history.loading ? <Loading what="assessments" /> : history.error ? <ErrorBox message={history.error} onRetry={history.reload} /> :
          !history.data?.length ? <Empty>No assessments yet. Run your first analysis above.</Empty> : (
            <ul className="list">
              {history.data.map((m) => (
                <li key={m.id}>
                  <div className="list-main">
                    <strong>{m.job.title} at {m.job.company}</strong>
                    <span className="muted">{formatTimestamp(m.created_at)} · {m.run.provider}/{m.run.model}</span>
                  </div>
                  <div className="list-meta">
                    <OverallBadge value={m.overall_match} />
                    <button type="button" className="btn btn-small" onClick={() => setShown(m)}>
                      View<span className="sr-only"> assessment for {m.job.title} at {m.job.company}</span>
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          )}
      </section>
    </>
  );
}

function OverallBadge({ value }: { value: OverallMatch }) {
  return <span className={`badge ${OVERALL[value].badge}`}>{OVERALL[value].label}</span>;
}

function ProfileForm({ profile, onSaved }: { profile: CandidateProfile | null; onSaved: (p: CandidateProfile) => void }) {
  const data = useData();
  const [text, setText] = useState(profile?.resume_text ?? "");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const unchanged = text.trim() === (profile?.resume_text ?? "");

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!text.trim()) return setError("Paste your resume or profile text first.");
    setSaving(true);
    setError(null);
    try {
      onSaved(await data.saveProfile(text));
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <form className="card form" onSubmit={submit} aria-labelledby="profile-h" noValidate>
      <h2 id="profile-h">Your professional profile</h2>
      <p className="muted small">
        Paste resume text. It is saved only when you click Save profile{data.mode === "live" ? ", in your local database" : ""}; the AI can read it but never changes it.
      </p>
      {error && <ErrorBox message={error} />}
      <label>Resume / profile text<textarea rows={8} maxLength={50000} value={text} onChange={(e) => setText(e.target.value)} /></label>
      <div className="form-actions">
        <button type="submit" className="btn" disabled={saving || unchanged}>{saving ? "Saving…" : "Save profile"}</button>
        <span className="muted small" role="status">
          {profile ? `Last saved ${formatTimestamp(profile.updated_at)}${unchanged ? "" : " · unsaved changes"}` : "Not saved yet"}
        </span>
      </div>
    </form>
  );
}

function AnalyzeForm({ jobs, blocked, onResult }: { jobs: Job[]; blocked: string | null; onResult: (m: JobMatch) => void }) {
  const data = useData();
  const [jobId, setJobId] = useState("");
  const [description, setDescription] = useState("");
  const [force, setForce] = useState(false);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!jobs.length) {
    return (
      <section className="card" aria-labelledby="analyze-h">
        <h2 id="analyze-h">Analyze a saved job</h2>
        <Empty action={<Link className="btn btn-primary btn-small" to="/jobs?add=1">Add a job</Link>}>Save a job first, then analyze it here.</Empty>
      </section>
    );
  }

  function pick(id: string) {
    setJobId(id);
    setDescription(jobs.find((j) => j.id === id)?.description ?? "");
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!jobId || !description.trim()) return setError("Select a job and paste its job description.");
    setRunning(true);
    setError(null);
    try {
      onResult(await data.requestMatch({ job_id: jobId, job_description: description.trim(), force }));
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setRunning(false);
    }
  }

  return (
    <form className="card form" onSubmit={submit} aria-labelledby="analyze-h" noValidate>
      <h2 id="analyze-h">Analyze a saved job</h2>
      {error && <ErrorBox message={error} />}
      <label>
        Saved job *
        <select value={jobId} onChange={(e) => pick(e.target.value)}>
          <option value="">Select a job…</option>
          {jobs.map((j) => <option key={j.id} value={j.id}>{j.company} — {j.title}</option>)}
        </select>
      </label>
      <label>Job description *<textarea rows={6} maxLength={30000} value={description} onChange={(e) => setDescription(e.target.value)} placeholder="Paste the full job description" /></label>
      <label className="inline-check">
        <input type="checkbox" checked={force} onChange={(e) => setForce(e.target.checked)} />
        Run a new analysis even if one exists for these exact inputs
      </label>
      <div className="form-actions">
        <button type="submit" className="btn btn-primary" disabled={running || blocked !== null}>{running ? "Analyzing…" : "Analyze match"}</button>
        {blocked && <span className="muted small">{blocked}</span>}
      </div>
      {running && <p className="muted small" role="status">The agent is reading the job and your profile. This can take up to a minute.</p>}
    </form>
  );
}

function QualificationList({ title, items }: { title: string; items: Qualification[] }) {
  return (
    <div className="qual">
      <h3>{title} ({items.length})</h3>
      {!items.length ? <p className="muted small">None identified.</p> : (
        <ul>
          {items.map((q, i) => (
            <li key={i}>
              <strong>{q.requirement}</strong> <span className="muted small">{q.category.replaceAll("_", " ")}</span>
              {q.evidence.map((ev, j) => <blockquote key={j}>“{ev}”</blockquote>)}
              {q.note && <p className="small">{q.note}</p>}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function AssessmentView({ match, demo }: { match: JobMatch; demo: boolean }) {
  const { assessment: a, run } = match;
  const tokens = run.input_tokens === null && run.output_tokens === null ? "not reported" : `${run.input_tokens ?? "?"} in / ${run.output_tokens ?? "?"} out`;
  return (
    <section className="card assessment" aria-labelledby="result-h">
      <div className="card-head">
        <h2 id="result-h">{match.job.title} at {match.job.company}</h2>
        <OverallBadge value={match.overall_match} />
      </div>
      <p className="muted small">
        {demo && <><strong>Recorded demo trace, fictional data.</strong> · </>}
        Assessed {formatTimestamp(match.created_at)}
        {match.reused && " · Reused an earlier assessment for identical inputs (no new AI call)"}
      </p>
      <h3>Recommendation</h3>
      <p>{a.recommendation}</p>
      <QualificationList title="Clearly supported" items={a.supported} />
      <QualificationList title="Partially supported" items={a.partial} />
      <QualificationList title="Missing or unverified" items={a.missing} />
      <h3>Uncertainty</h3>
      {a.uncertainties.length ? <ul>{a.uncertainties.map((u, i) => <li key={i}>{u}</li>)}</ul> : <p className="muted small">None noted.</p>}
      <details>
        <summary>Agent trace: {run.steps} steps · {run.provider}/{run.model} · tokens {tokens}</summary>
        <ol className="trace">
          {run.tool_calls.map((c, i) => (
            <li key={i}>
              Step {c.step}: <code>{c.tool}</code> — {c.ok ? "ok" : <span className="danger-text">rejected: {c.error}</span>}
            </li>
          ))}
        </ol>
      </details>
    </section>
  );
}

import { Link } from "react-router";
import { useData } from "../api/context";
import { fetchAll } from "../api/fetchAll";
import { useAsync } from "../hooks/useAsync";
import { Empty, ErrorBox, Loading, StatusBadge, statusLabel } from "../components/ui";
import { formatDate, today } from "../dates";
import { STATUSES } from "../types";

const ACTIVE = new Set(["applied", "interviewing", "offer"]);

/** "Today", "Tomorrow", or "In N days" for a future YYYY-MM-DD date. */
function dueIn(iso: string, now: string): string {
  const days = Math.round((Date.parse(iso) - Date.parse(now)) / 86_400_000);
  return days === 0 ? "Today" : days === 1 ? "Tomorrow" : `In ${days} days`;
}

export function DashboardPage() {
  const data = useData();
  const { data: result, error, loading, reload } = useAsync(
    () => Promise.all([fetchAll(data.listJobs), fetchAll((l, o) => data.listApplications({}, l, o))]),
    [data],
  );

  const head = (
    <div className="page-head">
      <div>
        <h1>Dashboard</h1>
        <p className="page-sub">Your job search at a glance.</p>
      </div>
      <div className="actions">
        <Link className="btn" to="/applications">View applications</Link>
        <Link className="btn btn-primary" to="/jobs?add=1">Add job</Link>
      </div>
    </div>
  );

  if (loading) return <>{head}<Loading what="dashboard" /></>;
  if (error || !result) return <>{head}<ErrorBox message={error ?? "No data"} onRetry={reload} /></>;

  const [jobs, apps] = result;
  const now = today();
  const appliedJobs = new Set(apps.map((a) => a.job_id));
  const notApplied = jobs.filter((j) => !appliedJobs.has(j.id)).length;
  const active = apps.filter((a) => ACTIVE.has(a.status)).length;
  const counts = STATUSES.map((s) => ({ status: s, count: apps.filter((a) => a.status === s).length }));
  const upcoming = apps
    .filter((a) => a.follow_up_date && a.follow_up_date >= now)
    .sort((a, b) => a.follow_up_date!.localeCompare(b.follow_up_date!))
    .slice(0, 5);
  const recent = apps.slice(0, 5); // API returns newest-applied first.

  return (
    <>
      {head}
      <section className="kpis" aria-label="Totals">
        <div className="card stat">
          <span className="stat-label">Saved jobs</span>
          <span className="stat-value">{jobs.length}</span>
          <span className="stat-sub">{notApplied} not yet applied · <Link to="/jobs">View jobs</Link></span>
        </div>
        <div className="card stat">
          <span className="stat-label">Applications submitted</span>
          <span className="stat-value">{apps.length}</span>
          <span className="stat-sub">{active} active · {apps.length - active} closed</span>
        </div>
        <section className="card status-card" aria-labelledby="by-status-h">
          <h2 id="by-status-h">By status</h2>
          <ul className="meters">
            {counts.map(({ status, count }) => (
              <li key={status} className="meter-row" data-status={status}>
                <span className="meter-name"><span className={`dot dot-${status}`} aria-hidden="true" />{statusLabel(status)}</span>
                <span className="meter-track" aria-hidden="true">
                  <span className="meter-fill" style={{ width: apps.length ? `${(count / apps.length) * 100}%` : 0 }} />
                </span>
                <span className="meter-value">{count}</span>
              </li>
            ))}
          </ul>
        </section>
      </section>

      <div className="grid-2">
        <section className="card" aria-labelledby="upcoming-h">
          <div className="card-head">
            <h2 id="upcoming-h">Upcoming follow-ups</h2>
          </div>
          {upcoming.length === 0 ? (
            <Empty action={apps.length > 0 && <Link className="btn btn-small" to="/applications">Go to applications</Link>}>
              No upcoming follow-ups.{apps.length > 0 && " Add a follow-up date when editing an application."}
            </Empty>
          ) : (
            <ul className="list">
              {upcoming.map((a) => (
                <li key={a.id}>
                  <span className="list-main"><strong>{a.job.company}</strong><span className="muted">{a.job.title}</span></span>
                  <span className="list-meta">
                    <span className="due">{dueIn(a.follow_up_date!, now)}</span>
                    <span className="muted small">{formatDate(a.follow_up_date)}</span>
                  </span>
                </li>
              ))}
            </ul>
          )}
        </section>
        <section className="card" aria-labelledby="recent-h">
          <div className="card-head">
            <h2 id="recent-h">Recent applications</h2>
            {recent.length > 0 && <Link className="card-link" to="/applications">View all</Link>}
          </div>
          {recent.length === 0 ? (
            jobs.length === 0 ? (
              <Empty action={<Link className="btn btn-primary btn-small" to="/jobs?add=1">Add your first job</Link>}>
                No applications yet. Start by saving a job you're interested in.
              </Empty>
            ) : (
              <Empty action={<Link className="btn btn-small" to="/jobs">Choose a saved job</Link>}>
                No applications recorded yet. Record one after you've submitted it.
              </Empty>
            )
          ) : (
            <ul className="list">
              {recent.map((a) => (
                <li key={a.id}>
                  <span className="list-main"><strong>{a.job.company}</strong><span className="muted">{a.job.title}</span></span>
                  <span className="list-meta"><StatusBadge status={a.status} /><span className="muted small">{formatDate(a.applied_date)}</span></span>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
    </>
  );
}

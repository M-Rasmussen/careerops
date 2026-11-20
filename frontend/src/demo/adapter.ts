// Browser-only adapter for the public demo. It never calls the API or the real database; the only
// network request is for its own static sample workbook.
import type { Application, ApplicationFilters, DataAdapter, Job } from "../types";
import { STATUSES } from "../types";
import { addDays, today } from "../dates";
import seed from "./seed.json";

const KEY = "careerops-demo-v1";
type StoredApp = Omit<Application, "job">;
interface State { jobs: Job[]; apps: StoredApp[] }

function seedState(): State {
  const now = today();
  const ts = (daysAgo: number) => `${addDays(now, -daysAgo)}T12:00:00Z`;
  const ids = new Map<string, string>();
  const jobs: Job[] = seed.jobs.map((j) => {
    const id = crypto.randomUUID();
    ids.set(j.key, id);
    const { key: _key, created_days_ago, ...rest } = j;
    return { ...rest, id, created_at: ts(created_days_ago), updated_at: ts(created_days_ago) };
  });
  const apps: StoredApp[] = seed.applications.map((a) => {
    const applied = addDays(now, -a.applied_days_ago);
    return {
      id: crypto.randomUUID(),
      job_id: ids.get(a.job)!,
      status: a.status as StoredApp["status"],
      applied_date: applied,
      follow_up_date: a.follow_up_in_days === null ? null : addDays(now, a.follow_up_in_days),
      notes: a.notes,
      created_at: `${applied}T12:00:00Z`,
      updated_at: `${applied}T12:00:00Z`,
    };
  });
  return { jobs, apps };
}

function load(): State {
  try {
    const raw = localStorage.getItem(KEY);
    if (raw) return JSON.parse(raw) as State;
  } catch {
    // Unavailable or corrupt storage: fall back to fresh seed data.
  }
  const state = seedState();
  save(state);
  return state;
}

function save(state: State): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(state));
  } catch {
    // Private mode / blocked storage: changes just won't survive a reload.
  }
}

const clean = (v: string | null | undefined) => (v ?? "").trim() || null;
const nowIso = () => new Date().toISOString();

function checkDates(applied: string, followUp: string | null): void {
  if (applied > addDays(today(), 1)) throw new Error("applied_date: applied_date cannot be in the future");
  if (followUp && followUp < applied) throw new Error("follow_up_date cannot be before applied_date");
}

function matches(f: ApplicationFilters, a: Application): boolean {
  return (
    (!f.status || a.status === f.status) &&
    (!f.company || a.job.company.toLowerCase().includes(f.company.toLowerCase())) &&
    (!f.applied_from || a.applied_date >= f.applied_from) &&
    (!f.applied_to || a.applied_date <= f.applied_to)
  );
}

// Same ordering as the backend: newest applied first, then newest created.
const byApplied = (a: Application, b: Application) =>
  b.applied_date.localeCompare(a.applied_date) || b.created_at.localeCompare(a.created_at);

export function createDemoAdapter(sampleWorkbookUrl = "/demo/sample-applications.xlsx"): DataAdapter {
  let state = load();
  const commit = () => save(state);
  const withJob = (a: StoredApp): Application => ({ ...a, job: state.jobs.find((j) => j.id === a.job_id)! });
  const getApp = (id: string) => {
    const app = state.apps.find((a) => a.id === id);
    if (!app) throw new Error("Application not found");
    return app;
  };

  return {
    mode: "demo",
    async listJobs(limit, offset) {
      return [...state.jobs].sort((a, b) => b.created_at.localeCompare(a.created_at)).slice(offset, offset + limit);
    },
    async createJob(p) {
      const company = clean(p.company);
      const title = clean(p.title);
      if (!company || !title) throw new Error("company and title are required");
      if (p.posting_url && !/^https?:\/\/\S+$/.test(p.posting_url)) throw new Error("posting_url: must be an http(s) URL");
      const at = nowIso();
      const job: Job = {
        id: crypto.randomUUID(), company, title, location: clean(p.location), posting_url: clean(p.posting_url),
        description: clean(p.description), notes: clean(p.notes), created_at: at, updated_at: at,
      };
      state = { ...state, jobs: [...state.jobs, job] };
      commit();
      return job;
    },
    async listApplications(filters, limit, offset) {
      return state.apps.map(withJob).filter((a) => matches(filters, a)).sort(byApplied).slice(offset, offset + limit);
    },
    async createApplication(p) {
      if (p.submission_confirmed !== true) throw new Error("submission_confirmed must be true");
      if (!STATUSES.includes(p.status)) throw new Error("Invalid status");
      checkDates(p.applied_date, p.follow_up_date);
      if (!state.jobs.some((j) => j.id === p.job_id)) throw new Error("Job not found");
      if (state.apps.some((a) => a.job_id === p.job_id)) throw new Error("An application for this job already exists");
      const at = nowIso();
      const app: StoredApp = {
        id: crypto.randomUUID(), job_id: p.job_id, status: p.status, applied_date: p.applied_date,
        follow_up_date: p.follow_up_date, notes: clean(p.notes), created_at: at, updated_at: at,
      };
      state = { ...state, apps: [...state.apps, app] };
      commit();
      return withJob(app);
    },
    async updateApplication(id, p) {
      const current = getApp(id);
      if (p.status !== undefined && !STATUSES.includes(p.status)) throw new Error("Invalid status");
      const next: StoredApp = { ...current, ...p, updated_at: nowIso() };
      if ("notes" in p) next.notes = clean(p.notes);
      checkDates(next.applied_date, next.follow_up_date);
      state = { ...state, apps: state.apps.map((a) => (a.id === id ? next : a)) };
      commit();
      return withJob(next);
    },
    async deleteApplication(id) {
      getApp(id);
      state = { ...state, apps: state.apps.filter((a) => a.id !== id) };
      commit();
    },
    async exportApplications() {
      // shortcut: static pre-generated workbook (ignores filters and demo edits); generate in-browser if it matters.
      const res = await fetch(sampleWorkbookUrl);
      if (!res.ok) throw new Error("Sample workbook is unavailable");
      return { blob: await res.blob(), filename: "careerops-demo-sample.xlsx" };
    },
    reset() {
      state = seedState();
      commit();
    },
  };
}

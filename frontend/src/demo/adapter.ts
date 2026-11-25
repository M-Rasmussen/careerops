// Browser-only adapter for the public demo. It never calls the API or the real database; the only
// network request is for its own static sample workbook. AI matching replays recorded, fictional
// assessments: no AI provider is ever contacted. Job discovery and the company directory are simulated over
// fictional employers (see ./discovery.ts): no job board or provider is contacted.
import type { Application, ApplicationFilters, Assessment, CandidateProfile, DataAdapter, Job, JobMatch, OverallMatch } from "../types";
import { STATUSES } from "../types";
import { addDays, today } from "../dates";
import seed from "./seed.json";
import matching from "./matching.json";
import { discoveryMethods, seedDiscovery, type DiscoveryState } from "./discovery";

const KEY = "careerops-demo-v1";
type StoredApp = Omit<Application, "job">;
type StoredMatch = Omit<JobMatch, "job">;
type Discovery = DiscoveryState;
interface State extends Discovery { jobs: Job[]; apps: StoredApp[]; profile: CandidateProfile; matches: StoredMatch[] }
type Recorded = (typeof matching.recorded_matches)[number];

const seedJob = (r: Recorded) => seed.jobs.find((j) => j.key === r.job)!;
// Demo jobs get fresh IDs per seed, so recorded analyses are matched by company + title.
const recordedFor = (job: Job) =>
  matching.recorded_matches.find((r) => seedJob(r).company === job.company && seedJob(r).title === job.title);

function replay(r: Recorded, jobId: string, at: string): StoredMatch {
  return {
    id: crypto.randomUUID(), job_id: jobId, overall_match: r.assessment.overall_match as OverallMatch,
    assessment: r.assessment as Assessment, job_description: r.job_description, created_at: at, reused: false,
    run: {
      id: crypto.randomUUID(), kind: "job_match", status: "succeeded", provider: "demo", model: "recorded-trace",
      steps: Math.max(...r.tool_calls.map((c) => c.step)), tool_calls: r.tool_calls, error_type: null, error: null,
      input_tokens: r.input_tokens, output_tokens: r.output_tokens, started_at: at, finished_at: at,
    },
  };
}

/** Adds AI-matching seed data; also upgrades demo state saved before AI matching existed. */
function withMatching(s: Pick<State, "jobs" | "apps"> & Partial<State>): Omit<State, keyof Discovery> & Partial<Discovery> {
  const at = `${today()}T12:00:00Z`;
  const matches = matching.recorded_matches.flatMap((r) => {
    const job = s.jobs.find((j) => recordedFor(j) === r);
    return job && r.seeded_days_ago !== null ? [replay(r, job.id, `${addDays(today(), -r.seeded_days_ago)}T12:00:00Z`)] : [];
  });
  return { profile: { resume_text: matching.profile, created_at: at, updated_at: at }, matches, ...s };
}

/** Adds discovery seed data; also replaces demo discovery state saved by an older demo version. */
function withDiscovery(s: Omit<State, keyof Discovery> & Partial<Discovery>): State {
  if (s.discovered?.every((d) => "review_history" in d && "experience_required" in d && "application_url" in d) &&
      s.companies?.every((c) => "discovery_status" in c) && s.runs?.every((r) => "tasks_total" in r)) return s as State;
  return { ...s, ...seedDiscovery() };
}

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
  return withDiscovery(withMatching({ jobs, apps }));
}

function load(): State {
  try {
    const raw = localStorage.getItem(KEY);
    if (raw) return withDiscovery(withMatching(JSON.parse(raw) as State));
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
      // Unsaved jobs are kept (for discovery links and history) but hidden from Saved Jobs, like the API.
      return state.jobs.filter((j) => !j.unsaved_at).sort((a, b) => b.created_at.localeCompare(a.created_at)).slice(offset, offset + limit);
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
    async getAIStatus() {
      return { configured: true, provider: "demo", model: "recorded-trace", max_steps: 6, max_runs_per_day: 20 };
    },
    async getProfile() {
      return state.profile;
    },
    async saveProfile(text) {
      const resume_text = text.trim();
      if (!resume_text) throw new Error("resume_text: String should have at least 1 character");
      state = { ...state, profile: { ...state.profile, resume_text, updated_at: nowIso() } };
      commit();
      return state.profile;
    },
    async requestMatch(p) {
      const job = state.jobs.find((j) => j.id === p.job_id);
      if (!job) throw new Error("Job not found");
      if (!p.job_description.trim()) throw new Error("job_description: String should have at least 1 character");
      const recorded = recordedFor(job);
      if (!recorded) {
        const names = matching.recorded_matches.map((r) => seedJob(r).company).join(", ");
        throw new Error(`Demo mode only has recorded analyses for ${names}. The private app runs the real agent on any saved job.`);
      }
      const existing = state.matches.find((m) => m.job_id === job.id);
      if (existing && !p.force) return { ...existing, reused: true, job };
      const match = replay(recorded, job.id, nowIso());
      state = { ...state, matches: [...state.matches, match] };
      commit();
      return { ...match, job };
    },
    async listMatches(limit, offset) {
      return [...state.matches]
        .sort((a, b) => b.created_at.localeCompare(a.created_at))
        .slice(offset, offset + limit)
        .map((m) => ({ ...m, job: state.jobs.find((j) => j.id === m.job_id)! }));
    },
    ...discoveryMethods(() => state, (patch) => {
      state = { ...state, ...patch };
      commit();
    }),
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

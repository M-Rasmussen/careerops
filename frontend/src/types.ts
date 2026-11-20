// Mirrors backend/app/schemas.py. UUIDs and timestamps arrive as strings; dates as YYYY-MM-DD.

export const STATUSES = ["applied", "interviewing", "offer", "rejected", "withdrawn"] as const;
export type ApplicationStatus = (typeof STATUSES)[number];

export interface Job {
  id: string;
  company: string;
  title: string;
  location: string | null;
  posting_url: string | null;
  description: string | null;
  notes: string | null;
  created_at: string;
  updated_at: string;
}

export interface JobCreate {
  company: string;
  title: string;
  location?: string | null;
  posting_url?: string | null;
  description?: string | null;
  notes?: string | null;
}

export interface Application {
  id: string;
  job_id: string;
  status: ApplicationStatus;
  applied_date: string;
  follow_up_date: string | null;
  notes: string | null;
  created_at: string;
  updated_at: string;
  job: Job;
}

export interface ApplicationCreate {
  job_id: string;
  submission_confirmed: true;
  status: ApplicationStatus;
  applied_date: string;
  follow_up_date: string | null;
  notes: string | null;
}

export interface ApplicationUpdate {
  status?: ApplicationStatus;
  applied_date?: string;
  follow_up_date?: string | null;
  notes?: string | null;
}

export interface ApplicationFilters {
  status?: ApplicationStatus | "";
  company?: string;
  applied_from?: string;
  applied_to?: string;
}

export interface CandidateProfile {
  resume_text: string;
  created_at: string;
  updated_at: string;
}

export type OverallMatch = "strong" | "moderate" | "weak" | "insufficient_information";

export interface Qualification {
  requirement: string;
  category: string;
  evidence: string[];
  note: string;
}

export interface Assessment {
  overall_match: OverallMatch;
  recommendation: string;
  supported: Qualification[];
  partial: Qualification[];
  missing: Qualification[];
  uncertainties: string[];
}

export interface ToolCallLog {
  step: number;
  tool: string;
  ok: boolean;
  error: string | null;
}

export interface AgentRun {
  id: string;
  kind: string;
  status: "running" | "succeeded" | "failed";
  provider: string;
  model: string;
  steps: number;
  tool_calls: ToolCallLog[];
  error_type: string | null;
  error: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
  started_at: string;
  finished_at: string | null;
}

export interface JobMatch {
  id: string;
  job_id: string;
  overall_match: OverallMatch;
  assessment: Assessment;
  job_description: string;
  created_at: string;
  job: Job;
  run: AgentRun;
  reused: boolean;
}

export interface MatchRequest {
  job_id: string;
  job_description: string;
  force: boolean;
}

export interface AIStatus {
  configured: boolean;
  provider: string | null;
  model: string | null;
  max_steps: number;
  max_runs_per_day: number;
}

/** One data-access contract shared by the live API and the browser-only demo. */
export interface DataAdapter {
  mode: "live" | "demo";
  listJobs(limit: number, offset: number): Promise<Job[]>;
  createJob(payload: JobCreate): Promise<Job>;
  listApplications(filters: ApplicationFilters, limit: number, offset: number): Promise<Application[]>;
  createApplication(payload: ApplicationCreate): Promise<Application>;
  updateApplication(id: string, payload: ApplicationUpdate): Promise<Application>;
  deleteApplication(id: string): Promise<void>;
  exportApplications(filters: ApplicationFilters): Promise<{ blob: Blob; filename: string }>;
  getAIStatus(): Promise<AIStatus>;
  getProfile(): Promise<CandidateProfile | null>;
  /** Explicit user save only; nothing else writes the profile. */
  saveProfile(resumeText: string): Promise<CandidateProfile>;
  /** Starts an AI analysis (may cost money in live mode); only call from a user action. */
  requestMatch(payload: MatchRequest): Promise<JobMatch>;
  listMatches(limit: number, offset: number): Promise<JobMatch[]>;
  /** Demo only: restore the seed data. */
  reset?(): void;
}

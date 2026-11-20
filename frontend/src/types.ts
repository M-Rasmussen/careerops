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
  /** Demo only: restore the seed data. */
  reset?(): void;
}

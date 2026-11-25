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
  /** Set when removed from Saved Jobs (the record is kept). */
  unsaved_at?: string | null;
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

// --- Job discovery (Phase 4B) ---

export const PROVIDERS = ["greenhouse", "lever", "lever_eu", "ashby"] as const;
export type Provider = (typeof PROVIDERS)[number];
export const JOB_PROVIDERS = [...PROVIDERS, "himalayas", "jsearch"] as const;
export type JobProvider = (typeof JOB_PROVIDERS)[number];
/** Boards can also be Workday: ownership is recorded, but jobs can't be imported yet. */
export type BoardProvider = Provider | "workday";
export const PROVIDER_LABELS: Record<JobProvider | "workday", string> = {
  greenhouse: "Greenhouse", lever: "Lever", lever_eu: "Lever (EU)", ashby: "Ashby", himalayas: "Himalayas", jsearch: "JSearch", workday: "Workday",
};
export const ROLE_FAMILIES = {
  backend: "Backend", software: "Software engineering / SWE II", fullstack: "Full-stack", platform: "Platform",
  devops: "DevOps / infrastructure", support: "Production / application support", sdet: "SDET / QA automation",
} as const;
export type RoleFamily = keyof typeof ROLE_FAMILIES;
export const GEOGRAPHIES = { nj: "New Jersey", nyc: "New York City area", remote_us: "US-eligible remote" } as const;
export type Geography = keyof typeof GEOGRAPHIES;

export type FilterResult = "match" | "unknown" | "rejected";
export type ReviewStatus = "new" | "saved" | "skipped" | "later";
export const WORKPLACE_TYPES = ["remote", "hybrid", "onsite"] as const;
export const SENIORITY_LEVELS = ["intern", "entry", "mid", "senior", "staff", "principal", "lead", "manager"] as const;

export interface JobSource {
  id: string;
  provider: BoardProvider;
  /** Workday only: host and career site (the tenant is board_token). */
  ats_host?: string | null;
  career_site?: string | null;
  /** Whether CareerOps can import this board's jobs; separate from ownership verification. */
  ingestion_supported?: boolean;
  /** Employer name the ATS itself reports for this board (Greenhouse), used as an ownership signal. */
  provider_name?: string | null;
  /** general hiring board vs prospect/talent pool; set only from evidence. */
  board_role?: "general" | "talent_pool" | null;
  next_sync_at?: string | null;
  sync_failures?: number;
  sync_status?: "queued" | "syncing" | "failed" | "ok" | "never" | "not_imported" | null;
  active_jobs?: number;
  company_name: string;
  board_token: string;
  enabled: boolean;
  last_success_at: string | null;
  last_error: string | null;
  last_error_at: string | null;
  created_at: string;
  updated_at: string;
  company_id: string | null;
  /** verified = official API answered and employer ownership is proven; needs_review = API answered, ownership unproven. */
  verification_status: "unverified" | "verified" | "needs_review" | "failed";
  verification_evidence: string | null;
  verified_at: string | null;
  last_checked_at: string | null;
  ownership_verified_at: string | null;
  ownership_evidence: string | null;
  region: string;
  board_url: string;
}

export interface DiscoveryFilters {
  role_families: RoleFamily[];
  geographies: Geography[];
  title_keywords: string[];
  excluded_keywords: string[];
  locations: string[];
  workplace_types: (typeof WORKPLACE_TYPES)[number][];
  seniority: (typeof SENIORITY_LEVELS)[number][];
  min_salary: number | null;
  salary_currency: string;
}

export interface CompanyBrief {
  id: string;
  name: string;
  watchlist: boolean;
  verification_status: string;
  /** Only present when the company's identity is verified. */
  website: string | null;
}

export interface PostingLink {
  url: string;
  /** employer = the employer's own ATS posting; aggregator = a job feed's page. */
  kind: "employer" | "aggregator";
  via: string;
}

export interface ExperienceMatch {
  status: "not_evaluated" | "evaluation_available" | "previously_evaluated";
  match_id: string | null;
  overall_match: OverallMatch | null;
  evaluated_at: string | null;
  recommendation: string | null;
}

export interface DiscoveredJob {
  id: string;
  source_id: string | null;
  company_id: string | null;
  company_info: CompanyBrief | null;
  ats_job_key: string | null;
  requisition_id: string | null;
  seniority: string | null;
  provider: JobProvider;
  source_posting_id: string;
  company: string;
  title: string;
  location: string | null;
  workplace_type: string | null;
  employment_type: string | null;
  posting_url: string | null;
  /** Apply-form URL supplied by the provider (Lever/Ashby). Null when not supplied; never constructed. */
  application_url: string | null;
  salary_min: number | null;
  salary_max: number | null;
  salary_currency: string | null;
  salary_interval: string | null;
  source_updated_at: string | null;
  /** The source's original posting date; null when the source doesn't give one. */
  posted_at: string | null;
  first_seen_at: string;
  last_seen_at: string;
  filter_result: FilterResult;
  filter_reasons: string[];
  review_status: ReviewStatus;
  saved_job_id: string | null;
  unsaved_at: string | null;
  duplicate_of_id: string | null;
  technologies: string[];
  /** Years of experience the posting explicitly asks for; empty = not specified. Not the AI experience match. */
  experience_required: string[];
  posting_link: PostingLink | null;
  experience_match: ExperienceMatch;
}

export interface DiscoveredJobDetail extends DiscoveredJob {
  description: string | null;
  review_history: { action: string; at: string }[];
  requirements: { sections_found: boolean; required: string[]; preferred: string[]; experience: string[]; technologies: string[] } | null;
}

export interface DiscoverySummary {
  total: number;
  matching: number;
  by_result: Record<FilterResult, number>;
  /** Distinct active jobs per source provider. */
  by_source: Partial<Record<JobProvider, number>>;
  /** Postings kept from earlier (archived) generations for history and deduplication. */
  archived: number;
  /** Active postings hidden because they aren't a specific opening (interest forms, talent pools). */
  non_vacancies: number;
  /** Postings their employer stopped listing (confirmed by two complete syncs); kept, hidden. */
  closed: number;
}

export const EMPLOYMENT_TYPES = ["Full-time", "Contract", "Contract-to-hire", "Part-time", "Temporary", "Internship"] as const;
export type EmploymentTypeFilter = (typeof EMPLOYMENT_TYPES)[number] | "unknown";

export const DISCOVERY_SORTS = { discovered: "Newest discovered", posted: "Newest posted", salary: "Highest salary", title: "Job title", company: "Company name" } as const;
export type DiscoverySort = keyof typeof DISCOVERY_SORTS;

export interface DiscoveredJobQuery {
  filter_result?: FilterResult | "";
  review_status?: ReviewStatus | "";
  provider?: JobProvider | "";
  q?: string;
  hide_duplicates?: boolean;
  company_id?: string;
  role_family?: RoleFamily | "";
  location?: string;
  workplace?: "remote" | "hybrid" | "onsite" | "unknown" | "";
  seniority?: (typeof SENIORITY_LEVELS)[number] | "";
  min_salary?: number | "";
  posted_within_days?: number | "";
  saved_state?: "saved" | "unsaved" | "never" | "";
  /** Any of EMPLOYMENT_TYPES and/or "unknown"; empty = no filter. */
  employment_type?: EmploymentTypeFilter[];
  sort?: DiscoverySort;
}

export interface DiscoverySourceOutcome {
  kind: "aggregator" | "board";
  source_id?: string | null;
  provider: JobProvider;
  company: string;
  board_token?: string;
  /** deferred: synced recently or already being synced; it's checked on its own schedule. */
  status: "succeeded" | "failed" | "skipped" | "deferred";
  retrieved: number;
  unique?: number;
  inserted: number;
  updated: number;
  duplicates: number;
  matched?: number;
  unknown?: number;
  requests?: number;
  duration_ms: number;
  error: string | null;
  warnings?: string[];
  /** Board syncs: postings no longer listed (missing) and closed after 2 complete syncs without them. */
  missing?: number;
  closed?: number;
}

export interface DiscoveryRun {
  id: string;
  status: "running" | "succeeded" | "partial" | "failed";
  sources_checked: number;
  sources_failed: number;
  jobs_retrieved: number;
  jobs_inserted: number;
  jobs_updated: number;
  duplicates: number;
  requests: number;
  jobs_unique: number;
  jobs_matched: number;
  jobs_unknown: number;
  companies_created: number;
  boards_verified: number;
  warnings: string[];
  sources: DiscoverySourceOutcome[];
  started_at: string;
  finished_at: string | null;
  duration_ms: number | null;
  trigger: "manual" | "scheduled";
  /** Queue progress: boards (and job feeds) queued for this run. */
  tasks_total: number;
  tasks_done: number;
  tasks_failed: number;
  tasks_remaining: number;
}

export interface Company {
  id: string;
  name: string;
  domain: string | null;
  website: string | null;
  careers_url: string | null;
  industry: string | null;
  engineering_focus: string | null;
  target_geography: string | null;
  origin: "curated_seed" | "manual" | "discovered";
  watchlist: boolean;
  verification_status: "verified" | "provider_reported" | "unresolved";
  notes: string | null;
  first_seen_at: string | null;
  last_seen_at: string | null;
  created_at: string;
  updated_at: string;
  discovery_status: CareerDiscoveryStatus;
  /** `candidate`: a new official domain or careers URL found by discovery, applied only if you accept it. */
  discovery_evidence: { step: string; detail: string; url: string | null; at: string; candidate?: string }[];
  discovery_error: string | null;
  discovery_checked_at: string | null;
  website_verified_at: string | null;
  unsupported_ats: string | null;
  ats_status: "verified" | "unverified" | "needs_review" | "failed" | "none";
  boards: JobSource[];
  /** Distinct jobs in the active Job Discovery generation. */
  job_count: number;
  last_checked_at: string | null;
  last_sync_at: string | null;
}

export type CareerDiscoveryStatus = "not_checked" | "discovering" | "verified" | "needs_review" | "unsupported_ats" | "not_found" | "failed";

export interface CompanyCreate {
  name: string;
  domain?: string | null;
  watchlist?: boolean;
  notes?: string | null;
}

export interface CompanyUpdate {
  watchlist?: boolean;
  notes?: string | null;
  engineering_focus?: string | null;
  target_geography?: string | null;
  careers_url?: string | null;
  domain?: string | null;
}

export interface CompanyQuery {
  q?: string;
  ats_status?: Company["ats_status"] | "";
  origin?: Company["origin"] | "";
  sort?: "name" | "jobs" | "last_seen";
}

export interface DiscoveryCoverage {
  providers: { provider: string; kind: "aggregator" | "ats_boards"; available: boolean; detail: string }[];
  boards_total: number;
  boards_verified: number;
  boards_per_run: number;
  warnings: string[];
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
  /** Enables or disables monitoring of one employer ATS board. */
  updateJobSource(id: string, payload: { enabled?: boolean }): Promise<JobSource>;
  listCompanies(query: CompanyQuery, limit: number, offset: number): Promise<Company[]>;
  getCompany(id: string): Promise<Company>;
  createCompany(payload: CompanyCreate): Promise<Company>;
  updateCompany(id: string, payload: CompanyUpdate): Promise<Company>;
  /** Verifies a pasted Greenhouse/Lever/Ashby URL through the provider's API (one read-only request in live mode). */
  verifyCompanyBoard(id: string, url: string): Promise<JobSource>;
  /** Re-runs career-source discovery for one company (bounded) and imports jobs from verified boards. */
  discoverCompany(id: string, deep?: boolean): Promise<Company>;
  getDiscoveryCoverage(): Promise<DiscoveryCoverage>;
  getDiscoveryFilters(): Promise<DiscoveryFilters>;
  saveDiscoveryFilters(filters: DiscoveryFilters): Promise<DiscoveryFilters>;
  /** Checks all enabled sources (real network requests in live mode); only call from a user action. */
  startDiscoveryRun(): Promise<DiscoveryRun>;
  listDiscoveryRuns(limit: number, offset: number): Promise<DiscoveryRun[]>;
  getDiscoveryRun(id: string): Promise<DiscoveryRun>;
  listDiscoveredJobs(query: DiscoveredJobQuery, limit: number, offset: number): Promise<DiscoveredJob[]>;
  /** Counts over all discovered jobs (not just one page) for the same filters. */
  getDiscoverySummary(query: DiscoveredJobQuery): Promise<DiscoverySummary>;
  getDiscoveredJob(id: string): Promise<DiscoveredJobDetail>;
  /** Removes a job from Saved Jobs without deleting it. Refused if an application is recorded. */
  unsaveJob(jobId: string): Promise<Job>;
  reviewDiscoveredJob(id: string, status: "new" | "skipped" | "later"): Promise<DiscoveredJob>;
  /** Adds the lead to Saved Jobs (idempotent). Never records an application. */
  saveDiscoveredJob(id: string): Promise<DiscoveredJob>;
  /** Demo only: restore the seed data. */
  reset?(): void;
}

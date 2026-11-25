// Demo-only job discovery and company directory over fictional data in discovery.json.
// Nothing here makes a network request: runs, verification, and providers are simulated, and technologies,
// requirement sections, and filter results are prerecorded or read from the fictional descriptions.
import type {
  Application, Company, CompanyQuery, DataAdapter, DiscoveredJob, DiscoveredJobDetail, DiscoveredJobQuery,
  DiscoveryFilters, DiscoveryRun, DiscoverySourceOutcome, DiscoverySummary, EmploymentTypeFilter, Job, JobMatch, JobSource, Provider,
} from "../types";
import { addDays, today } from "../dates";
import data from "./discovery.json";

type StoredCompany = Omit<Company, "ats_status" | "boards" | "job_count" | "last_checked_at" | "last_sync_at">;
type StoredLead = DiscoveredJobDetail;
export interface DiscoveryState {
  companies: StoredCompany[];
  sources: JobSource[];
  discovered: StoredLead[];
  runs: DiscoveryRun[];
  filters: DiscoveryFilters;
}
type Posting = (typeof data.jobs)[number];
type Context = DiscoveryState & { jobs: Job[]; apps: Omit<Application, "job">[]; matches: Omit<JobMatch, "job">[] };
type DiscoveryMethods = Pick<DataAdapter,
  "updateJobSource" | "listCompanies" | "getCompany" | "createCompany" | "updateCompany" | "verifyCompanyBoard" | "discoverCompany" |
  "getDiscoveryCoverage" | "getDiscoveryFilters" | "saveDiscoveryFilters" | "startDiscoveryRun" | "listDiscoveryRuns" | "getDiscoveryRun" |
  "listDiscoveredJobs" | "getDiscoverySummary" | "getDiscoveredJob" | "reviewDiscoveredJob" | "saveDiscoveredJob" | "unsaveJob">;

const ts = (daysAgo: number) => `${addDays(today(), -daysAgo)}T12:00:00Z`;
const nowIso = () => new Date().toISOString();
const ATS: string[] = ["greenhouse", "lever", "lever_eu", "ashby"];
const BOARD_URLS: Record<Provider, string> = {
  greenhouse: "https://job-boards.greenhouse.io/", lever: "https://jobs.lever.co/", lever_eu: "https://jobs.eu.lever.co/", ashby: "https://jobs.ashbyhq.com/",
};
// Same exact-host rule as the backend: anything else is "not a supported ATS link".
const ATS_HOSTS: Record<string, Provider> = {
  "boards.greenhouse.io": "greenhouse", "job-boards.greenhouse.io": "greenhouse", "jobs.lever.co": "lever",
  "jobs.eu.lever.co": "lever_eu", "jobs.ashbyhq.com": "ashby",
};
const ROLE_WORDS: Record<string, RegExp> = {
  backend: /\b(backend|back-end)\b/i, software: /\b(software engineer|developer)\b/i, fullstack: /\b(full[- ]?stack)\b/i,
  platform: /\bplatform\b/i, devops: /\b(devops|site reliability|sre|infrastructure)\b/i,
  support: /\b(production support|application support|support engineer)\b/i, sdet: /\b(sdet|test|qa)\b/i,
};

/** Demo-only mirror of the backend's conservative section reader (headings must say "requirements" etc.). */
function readRequirements(text: string | null, technologies: string[]): StoredLead["requirements"] {
  const required: string[] = [];
  const preferred: string[] = [];
  let section: "required" | "preferred" | "other" | null = null;
  let found = false;
  const lines = (text ?? "").split("\n").map((l) => l.trim()).filter(Boolean);
  for (const line of lines) {
    const h = line.replace(/:$/, "").toLowerCase();
    if (h.length <= 60 && /preferred|nice to have/.test(h)) { section = "preferred"; found = true; continue; }
    if (h.length <= 60 && /^(requirements|what you'll need)/.test(h)) { section = "required"; found = true; continue; }
    if (h.length <= 60 && /^(about|benefits)/.test(h)) { section = "other"; continue; }
    if (section === "required") required.push(line);
    if (section === "preferred") preferred.push(line);
  }
  return { sections_found: found, required, preferred, experience: lines.filter((l) => /\d+\+?\s*years?/i.test(l)), technologies };
}

function company(c: Partial<StoredCompany> & { name: string }, at: string): StoredCompany {
  return {
    id: crypto.randomUUID(), domain: null, website: null, careers_url: null, industry: null, engineering_focus: null,
    target_geography: null, origin: "manual", watchlist: false, verification_status: "unresolved",
    notes: null, first_seen_at: null, last_seen_at: null, created_at: at, updated_at: at, discovery_status: "not_checked",
    discovery_evidence: [], discovery_error: null, discovery_checked_at: null, website_verified_at: null, unsupported_ats: null, ...c,
  };
}

function board(provider: Provider, token: string, companyId: string, status: JobSource["verification_status"], at: string): JobSource {
  return {
    id: crypto.randomUUID(), provider, company_name: "", board_token: token, enabled: true, company_id: companyId,
    verification_status: status, verification_evidence: `${BOARD_URLS[provider]}${token}`,
    verified_at: status === "verified" ? at : null, last_checked_at: at, last_success_at: status === "verified" ? at : null,
    last_error: null, last_error_at: null, created_at: at, updated_at: at, board_url: `${BOARD_URLS[provider]}${token}`,
    ownership_verified_at: status === "verified" ? at : null,
    ownership_evidence: status === "verified" ? "Linked from the employer's careers page (simulated)" : null,
    region: provider === "lever_eu" ? "EU" : "Global",
  };
}

function toLead(p: Posting, companyId: string | null, sourceId: string | null, at: string): StoredLead {
  return {
    id: crypto.randomUUID(), source_id: sourceId, company_id: companyId, company_info: null, ats_job_key: null,
    requisition_id: null, seniority: p.seniority, provider: p.provider as DiscoveredJob["provider"],
    source_posting_id: p.source_posting_id, company: "", title: p.title, location: p.location,
    workplace_type: p.workplace_type, employment_type: p.employment_type, description: p.description, posting_url: p.posting_url,
    application_url: "application_url" in p ? (p.application_url as string) : null,
    salary_min: p.salary?.[0] ?? null, salary_max: p.salary?.[1] ?? null,
    salary_currency: p.salary ? "USD" : null, salary_interval: p.salary ? "year" : null,
    source_updated_at: null, posted_at: p.posted_days_ago === null ? null : ts(p.posted_days_ago), first_seen_at: at, last_seen_at: at,
    filter_result: p.filter_result as DiscoveredJob["filter_result"], filter_reasons: p.filter_reasons,
    review_status: p.review_status as DiscoveredJob["review_status"], saved_job_id: null, unsaved_at: null, duplicate_of_id: null,
    technologies: p.technologies, experience_required: p.experience_required, posting_link: null, experience_match: { status: "not_evaluated", match_id: null, overall_match: null, evaluated_at: null, recommendation: null },
    review_history: [], requirements: null,
  };
}

export function seedDiscovery(): DiscoveryState {
  const ids = new Map<string, string>();
  const companies = data.companies.map(({ key, ...c }) => {
    const row = company({ ...c, origin: c.origin as StoredCompany["origin"],
      verification_status: c.verification_status as StoredCompany["verification_status"],
      website: c.domain ? `https://${c.domain}` : null, first_seen_at: ts(5), last_seen_at: ts(1) }, ts(10));
    ids.set(key, row.id);
    return row;
  });
  const boardIds = new Map<string, string>();
  const sources = data.boards.map((b) => {
    const row = { ...board(b.provider as Provider, b.board_token, ids.get(b.company)!, b.verification_status as JobSource["verification_status"], ts(b.checked_days_ago)),
      enabled: b.enabled, last_error: b.last_error, last_error_at: b.last_error ? ts(b.checked_days_ago) : null,
      company_name: companies.find((c) => c.id === ids.get(b.company))!.name };
    boardIds.set(b.key, row.id);
    return row;
  });
  for (const c of companies) {
    if (sources.some((b) => b.company_id === c.id && b.verification_status === "verified")) {
      Object.assign(c, { discovery_status: "verified", discovery_checked_at: ts(1), careers_url: c.domain ? `https://${c.domain}/careers` : null });
    }
  }
  const discovered: StoredLead[] = [];
  for (const p of data.jobs.filter((j) => j.seeded)) {
    const row = toLead(p, ids.get(p.company)!, p.board ? boardIds.get(p.board)! : null, ts(p.first_seen_days_ago));
    row.last_seen_at = ts(Math.min(p.first_seen_days_ago, 1));
    if (p.duplicate_of) row.duplicate_of_id = discovered.find((d) => d.source_posting_id === p.duplicate_of)?.id ?? null;
    discovered.push(row);
  }
  const run: DiscoveryRun = {
    id: crypto.randomUUID(), status: "partial", sources_checked: 4, sources_failed: 1, jobs_retrieved: 11,
    jobs_inserted: discovered.length, jobs_updated: 0, duplicates: 1, requests: 10, jobs_unique: discovered.length,
    jobs_matched: 2, jobs_unknown: 3, companies_created: 2, boards_verified: 0,
    trigger: "scheduled", tasks_total: 4, tasks_done: 3, tasks_failed: 1, tasks_remaining: 0,
    warnings: ["Simulated: geographic coverage is limited to remote feeds and monitored boards."],
    sources: [], started_at: ts(1), finished_at: ts(1), duration_ms: 4120,
  };
  return { companies, sources, discovered, runs: [run], filters: data.filters as DiscoveryFilters };
}

const comparable = (d: DiscoveredJob) =>
  d.salary_currency === "USD" && /year|annual/i.test(d.salary_interval ?? "") ? d.salary_max ?? d.salary_min : null;

function matches(d: DiscoveredJob, f: DiscoveredJobQuery, includeResult = true): boolean {
  const q = (f.q ?? "").trim().toLowerCase();
  const level = f.seniority;
  const minSalary = f.min_salary === "" ? undefined : f.min_salary;
  const days = f.posted_within_days === "" ? undefined : f.posted_within_days;
  const salary = comparable(d);
  return (
    (!includeResult || !f.filter_result || d.filter_result === f.filter_result) &&
    (!f.review_status || d.review_status === f.review_status) &&
    (!f.provider || d.provider === f.provider) &&
    (!f.hide_duplicates || !d.duplicate_of_id) &&
    (!f.company_id || d.company_id === f.company_id) &&
    (!q || [d.title, d.company, d.location ?? ""].some((v) => v.toLowerCase().includes(q))) &&
    (!f.role_family || ROLE_WORDS[f.role_family].test(d.title)) &&
    (!f.location || (d.location ?? "").toLowerCase().includes(f.location.toLowerCase())) &&
    (!f.workplace || (f.workplace === "unknown" ? d.workplace_type === null : d.workplace_type === f.workplace)) &&
    (!level || d.seniority === level || new RegExp(`\\b${level === "senior" ? "senior|sr" : level}\\b`, "i").test(d.title)) &&
    (minSalary === undefined || salary === null || salary >= minSalary) &&
    (days === undefined || d.posted_at === null || d.posted_at >= `${addDays(today(), -days)}`) &&
    (!f.employment_type?.length || f.employment_type.includes(d.employment_type === null ? "unknown" : (d.employment_type as EmploymentTypeFilter))) &&
    (f.saved_state !== "saved" || d.review_status === "saved") &&
    (f.saved_state !== "unsaved" || (!!d.unsaved_at && d.review_status !== "saved")) &&
    (f.saved_state !== "never" || (!d.saved_job_id && d.review_status !== "saved"))
  );
}

const byNewest = (a: DiscoveredJob, b: DiscoveredJob) => b.first_seen_at.localeCompare(a.first_seen_at) || b.id.localeCompare(a.id);
const SORTS: Record<string, (a: DiscoveredJob, b: DiscoveredJob) => number> = {
  discovered: byNewest,
  posted: (a, b) => (b.posted_at ?? "").localeCompare(a.posted_at ?? "") || byNewest(a, b),
  salary: (a, b) => (comparable(b) ?? -1) - (comparable(a) ?? -1) || byNewest(a, b),
  title: (a, b) => a.title.toLowerCase().localeCompare(b.title.toLowerCase()) || byNewest(a, b),
  company: (a, b) => a.company.toLowerCase().localeCompare(b.company.toLowerCase()) || a.title.localeCompare(b.title),
};

export function discoveryMethods(get: () => Context, set: (patch: Partial<Context>) => void): DiscoveryMethods {
  const findCompany = (id: string) => {
    const c = get().companies.find((x) => x.id === id);
    if (!c) throw new Error("Company not found");
    return c;
  };
  const enrichCompany = (c: StoredCompany): Company => {
    const s = get();
    const boards = s.sources.filter((b) => b.company_id === c.id);
    const statuses = new Set(boards.map((b) => b.verification_status));
    const checked = boards.map((b) => b.last_checked_at).filter(Boolean).sort().at(-1) ?? null;
    const synced = boards.map((b) => b.last_success_at).filter(Boolean).sort().at(-1) ?? null;
    return {
      ...c, boards, last_checked_at: checked, last_sync_at: synced, job_count: s.discovered.filter((d) => d.company_id === c.id).length,
      ats_status: (["verified", "needs_review", "unverified", "failed"] as const).find((x) => statuses.has(x)) ?? "none",
    };
  };
  /** Same derived card fields as the API: company info, posting link, and experience-match state. */
  const enrich = (d: StoredLead): StoredLead => {
    const s = get();
    const c = d.company_id ? s.companies.find((x) => x.id === d.company_id) : undefined;
    const m = d.saved_job_id ? [...s.matches].filter((x) => x.job_id === d.saved_job_id).sort((a, b) => b.created_at.localeCompare(a.created_at))[0] : undefined;
    return {
      ...d, company: c?.name ?? d.company,
      company_info: c ? { id: c.id, name: c.name, watchlist: c.watchlist, verification_status: c.verification_status,
        website: c.verification_status === "verified" ? c.website : null } : null,
      posting_link: d.posting_url ? { url: d.posting_url, kind: ATS.includes(d.provider) ? "employer" : "aggregator", via: d.provider } : null,
      experience_match: m
        ? { status: "evaluation_available", match_id: m.id, overall_match: m.overall_match, evaluated_at: m.created_at, recommendation: m.assessment.recommendation }
        : { status: "not_evaluated", match_id: null, overall_match: null, evaluated_at: null, recommendation: null },
    };
  };
  const compact = (d: StoredLead): DiscoveredJob => {
    const { description: _d, review_history: _h, requirements: _r, ...card } = enrich(d);
    return card;
  };
  const findLead = (id: string) => {
    const d = get().discovered.find((x) => x.id === id);
    if (!d) throw new Error("Discovered job not found");
    return d;
  };
  const putLead = (next: StoredLead) => {
    set({ discovered: get().discovered.map((d) => (d.id === next.id ? next : d)) });
    return compact(next);
  };
  const logged = (d: StoredLead, action: string): StoredLead => ({ ...d, review_history: [...d.review_history, { action, at: nowIso() }] });

  return {
    async updateJobSource(id, p) {
      const b = get().sources.find((x) => x.id === id);
      if (!b) throw new Error("Job source not found");
      const next = { ...b, ...p, updated_at: nowIso() };
      set({ sources: get().sources.map((x) => (x.id === id ? next : x)) });
      return next;
    },
    async listCompanies(q: CompanyQuery, limit, offset) {
      const text = (q.q ?? "").trim().toLowerCase();
      const rows = get().companies.map(enrichCompany).filter((c) =>
        (!text || c.name.toLowerCase().includes(text) || (c.domain ?? "").includes(text)) &&
        (!q.ats_status || c.ats_status === q.ats_status) && (!q.origin || c.origin === q.origin));
      const byName = (a: Company, b: Company) => a.name.toLowerCase().localeCompare(b.name.toLowerCase());
      const sorters: Record<string, (a: Company, b: Company) => number> = {
        name: byName,
        jobs: (a, b) => b.job_count - a.job_count || byName(a, b),
        last_seen: (a, b) => (b.last_seen_at ?? "").localeCompare(a.last_seen_at ?? "") || byName(a, b),
      };
      return rows.sort(sorters[q.sort ?? "name"]).slice(offset, offset + limit);
    },
    async getCompany(id) {
      return enrichCompany(findCompany(id));
    },
    async createCompany(p) {
      const name = p.name.trim();
      if (!name) throw new Error("name: String should have at least 1 character");
      const domain = (p.domain ?? "").trim().toLowerCase().replace(/^https?:\/\//, "").replace(/^www\./, "").split("/")[0] || null;
      if (domain && !/^([a-z0-9-]+\.)+[a-z]{2,}$/.test(domain)) throw new Error("domain: not a valid domain name");
      if (domain && get().companies.some((c) => c.domain === domain)) throw new Error("A company with this domain already exists");
      const row = company({ name, domain, website: domain ? `https://${domain}` : null,
        watchlist: p.watchlist ?? false, notes: p.notes ?? null, verification_status: domain ? "verified" : "unresolved" }, nowIso());
      set({ companies: [...get().companies, row] });
      return enrichCompany(row);
    },
    async updateCompany(id, p) {
      const next = { ...findCompany(id), ...p, updated_at: nowIso() };
      set({ companies: get().companies.map((c) => (c.id === id ? next : c)) });
      return enrichCompany(next);
    },
    async verifyCompanyBoard(id, url) {
      const c = findCompany(id);
      let parsed: URL;
      try {
        parsed = new URL(url);
      } catch {
        parsed = new URL("about:blank");
      }
      const provider = ATS_HOSTS[parsed.hostname];
      const token = parsed.pathname.split("/").filter(Boolean)[0];
      if (!provider || !["http:", "https:"].includes(parsed.protocol) || parsed.username || !token || !/^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$/.test(token)) {
        throw new Error("Not a supported ATS link. Paste a Greenhouse, Lever, or Ashby job or board URL. Company career sites aren't crawled.");
      }
      if (get().sources.some((b) => b.provider === provider && b.board_token.toLowerCase() === token.toLowerCase() && b.company_id !== id)) {
        throw new Error("That board is already linked to another company");
      }
      // Simulated: only the fictional boards listed in discovery.json "exist".
      const ok = data.verifiable_boards.includes(token.toLowerCase());
      const at = nowIso();
      const row = { ...board(provider, token, id, ok ? "verified" : "failed", at), company_name: c.name, verification_evidence: url,
        last_error: ok ? null : "Not found (HTTP 404) (simulated)", last_error_at: ok ? null : at };
      set({ sources: [...get().sources.filter((b) => !(b.provider === provider && b.board_token === token)), row] });
      return row;
    },
    async discoverCompany(id) {
      // Simulated: the demo never contacts websites. Outcome follows the fictional boards already on record.
      const c = findCompany(id);
      const verified = get().sources.some((b) => b.company_id === id && b.verification_status === "verified");
      const next: StoredCompany = {
        ...c, discovery_checked_at: nowIso(), updated_at: nowIso(),
        discovery_status: verified ? "verified" : c.domain ? "not_found" : "needs_review",
        discovery_error: verified ? null : c.domain
          ? "Simulated: no Greenhouse, Lever, or Ashby link found. Paste the board URL to add it."
          : "No verified official website. Add the company's domain or careers URL; websites are never guessed from the name.",
        discovery_evidence: [...c.discovery_evidence, { step: "website", detail: "Simulated in the demo; no website was contacted", url: null, at: nowIso() }],
      };
      set({ companies: get().companies.map((x) => (x.id === id ? next : x)) });
      return enrichCompany(next);
    },
    async getDiscoveryCoverage() {
      const boards = get().sources.filter((b) => b.enabled);
      return {
        providers: [
          { provider: "himalayas", kind: "aggregator", available: true, detail: "Simulated remote-jobs feed (fictional postings)." },
          { provider: "jsearch", kind: "aggregator", available: false, detail: "Not configured in the demo (optional, metered)." },
          { provider: "ats_boards", kind: "ats_boards", available: boards.length > 0,
            detail: `${boards.filter((b) => b.verification_status === "verified").length} verified of ${boards.length} enabled employer boards; up to 10 checked per run.` },
        ],
        boards_total: boards.length, boards_verified: boards.filter((b) => b.verification_status === "verified").length, boards_per_run: 10,
        warnings: ["Geographic coverage is limited: without JSearch, on-site and hybrid NJ/NYC jobs come only from monitored employer boards."],
      };
    },
    async getDiscoveryFilters() {
      return get().filters;
    },
    async saveDiscoveryFilters(filters) {
      // Demo filter results are prerecorded; the private app re-evaluates every lead when filters change.
      set({ filters });
      return filters;
    },
    async startDiscoveryRun() {
      const started = nowIso();
      const s = get();
      let { discovered, companies, sources } = s;
      const outcomes: DiscoverySourceOutcome[] = [];
      const companyIds = new Map(data.companies.map((c) => [c.key, companies.find((x) => x.name === c.name)?.id]));
      let created = 0;
      const ingest = (pool: Posting[], o: DiscoverySourceOutcome, sourceId: string | null) => {
        for (const p of pool) {
          let cid = companyIds.get(p.company);
          if (!cid) {
            const info = (data.discovered_on_run as Record<string, { name: string; verification_status: string }>)[p.company];
            const existing = companies.find((c) => c.name === info.name && c.origin === "discovered");
            cid = existing?.id;
            if (!cid) {
              const row = company({ name: info.name, origin: "discovered", verification_status: info.verification_status as StoredCompany["verification_status"],
                first_seen_at: started, last_seen_at: started, notes: "Discovered automatically from a job listing." }, started);
              companies = [...companies, row];
              cid = row.id;
              created += 1;
            }
            companyIds.set(p.company, cid);
          }
          const found = discovered.find((d) => d.provider === p.provider && d.source_posting_id === p.source_posting_id);
          if (found) {
            // Re-found: refresh last seen only. Review decisions (including "unsaved") are never changed.
            o.updated += 1;
            discovered = discovered.map((d) => (d === found ? { ...d, last_seen_at: started } : d));
          } else {
            o.inserted += 1;
            discovered = [...discovered, { ...toLead(p, cid, sourceId, started), review_status: "new" }];
          }
        }
        o.retrieved = o.unique = pool.length;
      };
      const base = (kind: "aggregator" | "board", provider: DiscoverySourceOutcome["provider"], name: string): DiscoverySourceOutcome => ({
        kind, provider, company: name, status: "succeeded", retrieved: 0, unique: 0, inserted: 0, updated: 0, duplicates: 0,
        requests: 1, duration_ms: 300, error: null,
      });
      const feed = base("aggregator", "himalayas", "Himalayas (simulated)");
      ingest(data.jobs.filter((p) => p.provider === "himalayas"), feed, null);
      outcomes.push(feed, { ...base("aggregator", "jsearch", "jsearch"), status: "skipped", requests: 0, error: "Not configured in the demo" });
      sources = sources.map((b) => {
        if (!b.enabled || b.verification_status === "failed" || b.provider === "workday") return b;
        const o = { ...base("board", b.provider, b.company_name), board_token: b.board_token, source_id: b.id };
        outcomes.push(o);
        const error = (data.simulated_errors as Record<string, string>)[b.board_token];
        if (error) {
          Object.assign(o, { status: "failed", error });
          return { ...b, last_error: error, last_error_at: started, last_checked_at: started };
        }
        const key = data.boards.find((x) => x.board_token === b.board_token)?.key;
        ingest(key ? data.jobs.filter((p) => p.board === key) : [], o, b.id);
        return { ...b, last_success_at: started, last_checked_at: started, last_error: null, last_error_at: null };
      });
      const checked = outcomes.filter((o) => o.status !== "skipped");
      const failed = checked.filter((o) => o.status === "failed").length;
      const sum = (k: "retrieved" | "inserted" | "updated") => outcomes.reduce((n, o) => n + o[k], 0);
      const fresh = discovered.filter((d) => d.first_seen_at === started);
      const run: DiscoveryRun = {
        id: crypto.randomUUID(), status: failed === checked.length ? "failed" : failed ? "partial" : "succeeded",
        sources_checked: checked.length, sources_failed: failed, jobs_retrieved: sum("retrieved"), jobs_unique: sum("retrieved"),
        jobs_inserted: sum("inserted"), jobs_updated: sum("updated"), duplicates: 0, requests: checked.length,
        jobs_matched: fresh.filter((d) => d.filter_result === "match").length, jobs_unknown: fresh.filter((d) => d.filter_result === "unknown").length,
        companies_created: created, boards_verified: 0,
        warnings: ["Simulated run: no real job board or provider was contacted."],
        sources: outcomes, started_at: started, finished_at: nowIso(), duration_ms: 300 * checked.length,
        // Simulated runs finish at once (the live app queues them for a background worker).
        trigger: "manual", tasks_total: checked.length, tasks_done: checked.length - failed, tasks_failed: failed, tasks_remaining: 0,
      };
      set({ discovered, companies, sources, runs: [...s.runs, run] });
      return run;
    },
    async getDiscoveryRun(id) {
      const r = get().runs.find((x) => x.id === id);
      if (!r) throw new Error("Discovery run not found");
      return r;
    },
    async listDiscoveryRuns(limit, offset) {
      return [...get().runs].sort((a, b) => b.started_at.localeCompare(a.started_at)).slice(offset, offset + limit);
    },
    async listDiscoveredJobs(f: DiscoveredJobQuery, limit, offset) {
      return get().discovered.map(enrich).filter((d) => matches(d, f))
        .sort(SORTS[f.sort ?? "discovered"]).slice(offset, offset + limit).map(compact);
    },
    async getDiscoverySummary(f) {
      const all = get().discovered.map(enrich);
      const base = all.filter((d) => matches(d, f, false));
      const count = (r: string) => base.filter((d) => d.filter_result === r).length;
      const distinct = all.filter((d) => !d.duplicate_of_id);
      const bySource: DiscoverySummary["by_source"] = {};
      for (const d of distinct) bySource[d.provider] = (bySource[d.provider] ?? 0) + 1;
      return {
        total: distinct.length,
        matching: all.filter((d) => matches(d, f)).length,
        by_source: bySource,
        archived: 0,
        non_vacancies: 0,
        closed: 0,
        by_result: { match: count("match"), unknown: count("unknown"), rejected: count("rejected") },
      };
    },
    async getDiscoveredJob(id) {
      const d = enrich(findLead(id));
      return { ...d, requirements: readRequirements(d.description, d.technologies) };
    },
    async reviewDiscoveredJob(id, review_status) {
      const found = findLead(id);
      if (found.review_status === "saved") throw new Error("This job is in Saved Jobs. Remove it from Saved Jobs first.");
      if (!["new", "skipped", "later"].includes(review_status)) throw new Error("review_status: Invalid status");
      return putLead(found.review_status === review_status ? found : logged({ ...found, review_status }, review_status));
    },
    async saveDiscoveredJob(id) {
      const found = enrich(findLead(id));
      const jobs = get().jobs;
      // Reuse the job this lead was saved as before (even if unsaved), else one with the same URL, else create.
      let job = jobs.find((j) => j.id === found.saved_job_id) ?? (found.posting_url ? jobs.find((j) => j.posting_url === found.posting_url) : undefined);
      if (!job) {
        const at = nowIso();
        job = {
          id: crypto.randomUUID(), company: found.company, title: found.title, location: found.location,
          posting_url: found.posting_url, description: found.description,
          notes: `Found via job discovery (${found.provider}).`, unsaved_at: null, created_at: at, updated_at: at,
        };
        set({ jobs: [...jobs, job] });
      } else if (job.unsaved_at) {
        const restored = { ...job, unsaved_at: null };
        set({ jobs: get().jobs.map((j) => (j.id === restored.id ? restored : j)) });
        job = restored;
      } else if (found.review_status === "saved") {
        return compact(findLead(id));
      }
      return putLead(logged({ ...findLead(id), review_status: "saved", saved_job_id: job.id }, "saved"));
    },
    async unsaveJob(jobId) {
      const s = get();
      const job = s.jobs.find((j) => j.id === jobId);
      if (!job) throw new Error("Job not found");
      if (s.apps.some((a) => a.job_id === jobId)) {
        throw new Error("This job has a recorded application, so it stays in your tracker. Manage it on the Applications page.");
      }
      if (job.unsaved_at) return job;
      const at = nowIso();
      const next = { ...job, unsaved_at: at };
      set({
        jobs: s.jobs.map((j) => (j.id === jobId ? next : j)),
        discovered: s.discovered.map((d) =>
          d.saved_job_id === jobId && d.review_status === "saved" ? logged({ ...d, review_status: "new", unsaved_at: at }, "unsaved") : d),
      });
      return next;
    },
  };
}

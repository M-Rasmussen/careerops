import type { ApplicationFilters, CompanyQuery, DataAdapter, DiscoveredJobQuery } from "../types";

/** FastAPI errors: `detail` is a string, or a list of {loc, msg} for 422 validation errors. */
function errorMessage(status: number, body: unknown): string {
  const detail = (body as { detail?: unknown } | null)?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((d: { loc?: unknown[]; msg?: string }) => {
        const field = d.loc?.filter((p) => p !== "body").join(".");
        return field ? `${field}: ${d.msg}` : d.msg;
      })
      .join("; ");
  }
  return `Request failed (HTTP ${status})`;
}

async function request(path: string, init?: RequestInit): Promise<Response> {
  let res: Response;
  try {
    res = await fetch(path, {
      ...init,
      headers: init?.body ? { "Content-Type": "application/json" } : undefined,
    });
  } catch {
    throw new Error("Cannot reach the CareerOps API. Is the backend running?");
  }
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new Error(errorMessage(res.status, body));
  }
  return res;
}

const json = async <T>(path: string, init?: RequestInit) => (await request(path, init)).json() as Promise<T>;
const send = (method: string, body: unknown): RequestInit => ({ method, body: JSON.stringify(body) });

function query(filters: ApplicationFilters | DiscoveredJobQuery | CompanyQuery, extra: Record<string, number> = {}): string {
  // Empty strings and false are "no filter" and are omitted.
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries({ ...filters, ...extra })) {
    if (Array.isArray(v)) v.forEach((item) => params.append(k, String(item)));
    else if (v !== undefined && v !== "" && v !== false) params.set(k, String(v));
  }
  const s = params.toString();
  return s ? `?${s}` : "";
}

export const liveAdapter: DataAdapter = {
  mode: "live",
  listJobs: (limit, offset) => json(`/api/jobs?limit=${limit}&offset=${offset}`),
  createJob: (payload) => json("/api/jobs", send("POST", payload)),
  listApplications: (filters, limit, offset) => json(`/api/applications${query(filters, { limit, offset })}`),
  createApplication: (payload) => json("/api/applications", send("POST", payload)),
  updateApplication: (id, payload) => json(`/api/applications/${encodeURIComponent(id)}`, send("PATCH", payload)),
  deleteApplication: async (id) => {
    await request(`/api/applications/${encodeURIComponent(id)}`, { method: "DELETE" });
  },
  getAIStatus: () => json("/api/ai/status"),
  getProfile: () => json("/api/profile"),
  saveProfile: (resume_text) => json("/api/profile", send("PUT", { resume_text })),
  requestMatch: (payload) => json("/api/matches", send("POST", payload)),
  listMatches: (limit, offset) => json(`/api/matches?limit=${limit}&offset=${offset}`),
  updateJobSource: (id, payload) => json(`/api/discovery/sources/${encodeURIComponent(id)}`, send("PATCH", payload)),
  listCompanies: (q, limit, offset) => json(`/api/companies${query(q, { limit, offset })}`),
  getCompany: (id) => json(`/api/companies/${encodeURIComponent(id)}`),
  createCompany: (payload) => json("/api/companies", send("POST", payload)),
  updateCompany: (id, payload) => json(`/api/companies/${encodeURIComponent(id)}`, send("PATCH", payload)),
  verifyCompanyBoard: (id, url) => json(`/api/companies/${encodeURIComponent(id)}/verify`, send("POST", { url })),
  discoverCompany: (id, deep = false) => json(`/api/companies/${encodeURIComponent(id)}/discover${deep ? "?deep=true" : ""}`, send("POST", {})),
  getDiscoveryCoverage: () => json("/api/discovery/coverage"),
  getDiscoveryFilters: () => json("/api/discovery/filters"),
  saveDiscoveryFilters: (filters) => json("/api/discovery/filters", send("PUT", filters)),
  startDiscoveryRun: () => json("/api/discovery/runs", { method: "POST" }),
  listDiscoveryRuns: (limit, offset) => json(`/api/discovery/runs?limit=${limit}&offset=${offset}`),
  getDiscoveryRun: (id) => json(`/api/discovery/runs/${encodeURIComponent(id)}`),
  listDiscoveredJobs: (q, limit, offset) => json(`/api/discovery/jobs${query(q, { limit, offset })}`),
  getDiscoverySummary: (q) => json(`/api/discovery/jobs/summary${query(q)}`),
  getDiscoveredJob: (id) => json(`/api/discovery/jobs/${encodeURIComponent(id)}`),
  unsaveJob: (id) => json(`/api/jobs/${encodeURIComponent(id)}/unsave`, { method: "POST" }),
  reviewDiscoveredJob: (id, review_status) =>
    json(`/api/discovery/jobs/${encodeURIComponent(id)}`, send("PATCH", { review_status })),
  saveDiscoveredJob: (id) => json(`/api/discovery/jobs/${encodeURIComponent(id)}/save`, { method: "POST" }),
  exportApplications: async (filters) => {
    const res = await request(`/api/applications/export.xlsx${query(filters)}`);
    const match = /filename="([^"]+)"/.exec(res.headers.get("Content-Disposition") ?? "");
    return { blob: await res.blob(), filename: match?.[1] ?? "applications.xlsx" };
  },
};

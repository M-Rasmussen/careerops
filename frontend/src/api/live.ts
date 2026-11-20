import type { ApplicationFilters, DataAdapter } from "../types";

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

function query(filters: ApplicationFilters, extra: Record<string, number> = {}): string {
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries({ ...filters, ...extra })) {
    if (v !== undefined && v !== "") params.set(k, String(v));
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
  exportApplications: async (filters) => {
    const res = await request(`/api/applications/export.xlsx${query(filters)}`);
    const match = /filename="([^"]+)"/.exec(res.headers.get("Content-Disposition") ?? "");
    return { blob: await res.blob(), filename: match?.[1] ?? "applications.xlsx" };
  },
};

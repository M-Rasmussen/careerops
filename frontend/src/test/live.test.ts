import { liveAdapter } from "../api/live";

const reply = (status: number, body: unknown, headers: Record<string, string> = {}) =>
  vi.fn(async () => new Response(typeof body === "string" ? body : JSON.stringify(body), { status, headers }));

afterEach(() => vi.unstubAllGlobals());

describe("live API adapter", () => {
  it("sends filters as query params and omits empty ones", async () => {
    const fetch = reply(200, []);
    vi.stubGlobal("fetch", fetch);
    await liveAdapter.listApplications({ status: "offer", company: "", applied_from: "2026-01-01" }, 21, 40);
    expect(fetch).toHaveBeenCalledWith("/api/applications?status=offer&applied_from=2026-01-01&limit=21&offset=40", expect.anything());
  });

  it("turns FastAPI validation errors into readable messages", async () => {
    vi.stubGlobal("fetch", reply(422, { detail: [{ loc: ["body", "submission_confirmed"], msg: "Input should be True" }] }));
    await expect(liveAdapter.createApplication({} as never)).rejects.toThrow("submission_confirmed: Input should be True");
  });

  it("passes string error details through", async () => {
    vi.stubGlobal("fetch", reply(409, { detail: "An application for this job already exists" }));
    await expect(liveAdapter.createApplication({} as never)).rejects.toThrow("An application for this job already exists");
  });

  it("reports an unreachable backend", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new TypeError("Failed to fetch"))));
    await expect(liveAdapter.listJobs(50, 0)).rejects.toThrow("Cannot reach the CareerOps API");
  });

  it("downloads the export with the server-provided filename", async () => {
    const fetch = reply(200, "xlsx-bytes", { "Content-Disposition": 'attachment; filename="applications_20261120.xlsx"' });
    vi.stubGlobal("fetch", fetch);
    const { filename } = await liveAdapter.exportApplications({ company: "Acme" });
    expect(fetch).toHaveBeenCalledWith("/api/applications/export.xlsx?company=Acme", expect.anything());
    expect(filename).toBe("applications_20261120.xlsx");
  });
});

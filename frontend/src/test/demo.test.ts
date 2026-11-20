import { createDemoAdapter } from "../demo/adapter";
import { today } from "../dates";

afterEach(() => vi.unstubAllGlobals());

describe("demo adapter isolation", () => {
  it("performs all CRUD without any network request", async () => {
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    const demo = createDemoAdapter();
    const job = await demo.createJob({ company: "Test Co", title: "Dev" });
    const app = await demo.createApplication({
      job_id: job.id, submission_confirmed: true, status: "applied", applied_date: today(), follow_up_date: null, notes: null,
    });
    await demo.updateApplication(app.id, { status: "offer" });
    await demo.listApplications({ status: "offer" }, 50, 0);
    await demo.deleteApplication(app.id);
    expect(fetch).not.toHaveBeenCalled();
    expect(demo.mode).toBe("demo");
  });

  it("export fetches only the bundled sample workbook, never the API", async () => {
    const fetch = vi.fn(async () => new Response("bytes"));
    vi.stubGlobal("fetch", fetch);
    const { filename } = await createDemoAdapter().exportApplications({});
    expect(fetch).toHaveBeenCalledWith("/demo/sample-applications.xlsx");
    expect(filename).toMatch(/demo-sample/);
  });

  it("enforces the same rules as the backend", async () => {
    const demo = createDemoAdapter();
    const [app] = await demo.listApplications({}, 1, 0);
    const base = { status: "applied" as const, applied_date: today(), follow_up_date: null, notes: null };
    await expect(demo.createApplication({ ...base, job_id: app.job_id, submission_confirmed: false as never }))
      .rejects.toThrow("submission_confirmed");
    await expect(demo.createApplication({ ...base, job_id: app.job_id, submission_confirmed: true }))
      .rejects.toThrow("already exists");
    await expect(demo.updateApplication(app.id, { follow_up_date: "2000-01-01" })).rejects.toThrow("follow_up_date");
    await expect(demo.createJob({ company: " ", title: "x" })).rejects.toThrow("required");
  });

  it("persists changes in localStorage and Reset restores the seed", async () => {
    const first = createDemoAdapter();
    await first.createJob({ company: "Persisted Co", title: "Dev" });
    const second = createDemoAdapter();
    expect((await second.listJobs(200, 0)).map((j) => j.company)).toContain("Persisted Co");
    second.reset!();
    const jobs = await createDemoAdapter().listJobs(200, 0);
    expect(jobs).toHaveLength(10);
    expect(jobs.map((j) => j.company)).not.toContain("Persisted Co");
  });

  it("seed data uses only reserved example.com posting URLs", async () => {
    const jobs = await createDemoAdapter().listJobs(200, 0);
    for (const j of jobs) if (j.posting_url) expect(new URL(j.posting_url).hostname).toBe("example.com");
  });
});

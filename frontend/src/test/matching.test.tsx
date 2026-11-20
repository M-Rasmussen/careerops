import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderAt, spiedAdapter } from "./render";
import { createDemoAdapter } from "../demo/adapter";
import { liveAdapter } from "../api/live";

afterEach(() => vi.unstubAllGlobals());

const analyzeForm = () => screen.findByRole("form", { name: "Analyze a saved job" });

describe("AI matching page", () => {
  it("is reachable from the sidebar and never starts an analysis on load", async () => {
    const adapter = spiedAdapter();
    renderAt("/", adapter);
    await userEvent.click(screen.getByRole("link", { name: "AI matching" }));
    expect(await screen.findByRole("heading", { name: "AI matching" })).toBeInTheDocument();
    await screen.findByText("Backend Engineer (Python) at Lumen Harbor Labs"); // seeded previous assessment
    expect(adapter.getAIStatus).toHaveBeenCalled();
    expect(adapter.requestMatch).not.toHaveBeenCalled();
  });

  it("saves the profile only on explicit save, then analyzes a selected job", async () => {
    const adapter = spiedAdapter();
    renderAt("/matching", adapter);
    const profile = await screen.findByRole("form", { name: "Your professional profile" });
    const save = within(profile).getByRole("button", { name: "Save profile" });
    expect(save).toBeDisabled(); // nothing changed yet
    await userEvent.type(within(profile).getByLabelText("Resume / profile text"), "\nAlso: AWS certified.");
    expect(adapter.saveProfile).not.toHaveBeenCalled();
    await userEvent.click(save);
    expect(adapter.saveProfile).toHaveBeenCalledWith(expect.stringContaining("AWS certified."));
    await waitFor(() => expect(save).toBeDisabled());

    const form = await analyzeForm();
    await userEvent.selectOptions(within(form).getByLabelText("Saved job *"), "Kestrel Data Co. — Python Developer");
    // Prefilled from the saved job; the user replaces it with the full description.
    const jd = within(form).getByLabelText("Job description *");
    expect(jd).toHaveValue("ETL tooling and internal APIs.");
    await userEvent.clear(jd);
    await userEvent.type(jd, "Required: Python, ETL, SQL.");
    await userEvent.click(within(form).getByRole("button", { name: "Analyze match" }));
    expect(adapter.requestMatch).toHaveBeenCalledWith({ job_id: expect.any(String), job_description: "Required: Python, ETL, SQL.", force: false });

    const result = await screen.findByRole("region", { name: "Python Developer at Kestrel Data Co." });
    expect(within(result).getByText("Moderate match")).toBeInTheDocument();
    expect(within(result).getByRole("heading", { name: "Clearly supported (2)" })).toBeInTheDocument();
    expect(within(result).getByRole("heading", { name: "Partially supported (1)" })).toBeInTheDocument();
    expect(within(result).getByRole("heading", { name: "Missing or unverified (2)" })).toBeInTheDocument();
    expect(within(result).getByText("“4 years of Python experience”")).toBeInTheDocument();
    expect(within(result).getByText(/Agent trace: 4 steps/)).toBeInTheDocument();
    expect(within(result).getByText(/rejected: Assessment rejected/)).toBeInTheDocument();
    // History refreshes with the new assessment.
    const history = screen.getByRole("region", { name: "Previous assessments" });
    await within(history).findByText("Python Developer at Kestrel Data Co.");
  });

  it("validates input before calling the AI and shows analysis errors", async () => {
    const adapter = spiedAdapter({ requestMatch: () => Promise.reject(new Error("AI analysis failed: Provider timed out after 60s")) });
    renderAt("/matching", adapter);
    const form = await analyzeForm();
    await userEvent.click(within(form).getByRole("button", { name: "Analyze match" }));
    expect(within(form).getByRole("alert")).toHaveTextContent("Select a job and paste its job description.");
    expect(adapter.requestMatch).not.toHaveBeenCalled();
    await userEvent.selectOptions(within(form).getByLabelText("Saved job *"), "Kestrel Data Co. — Python Developer");
    await userEvent.click(within(form).getByRole("button", { name: "Analyze match" }));
    expect(await within(form).findByRole("alert")).toHaveTextContent("Provider timed out");
  });

  it("explains a missing AI provider and disables analysis", async () => {
    const adapter = spiedAdapter({
      getAIStatus: async () => ({ configured: false, provider: null, model: null, max_steps: 6, max_runs_per_day: 20 }),
    });
    renderAt("/matching", adapter);
    expect(await screen.findByText("No AI provider configured.")).toBeInTheDocument();
    const form = await analyzeForm();
    expect(within(form).getByRole("button", { name: "Analyze match" })).toBeDisabled();
    expect(within(form).getByText("AI provider not configured.")).toBeInTheDocument();
  });

  it("requires a saved profile and shows empty states", async () => {
    const adapter = spiedAdapter({ getProfile: async () => null, listMatches: async () => [], listJobs: async () => [] });
    renderAt("/matching", adapter);
    expect(await screen.findByText("Not saved yet")).toBeInTheDocument();
    expect(screen.getByText("No assessments yet. Run your first analysis above.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Add a job" })).toHaveAttribute("href", "/jobs?add=1");
  });

  it("views a previous assessment and labels demo traces", async () => {
    renderAt("/matching", createDemoAdapter());
    await userEvent.click(await screen.findByRole("button", { name: /View assessment for Backend Engineer \(Python\)/ }));
    const result = screen.getByRole("region", { name: "Backend Engineer (Python) at Lumen Harbor Labs" });
    expect(within(result).getByText("Strong match")).toBeInTheDocument();
    expect(within(result).getByText("Recorded demo trace, fictional data.")).toBeInTheDocument();
    expect(screen.getAllByRole("note").some((n) => /No AI provider is contacted/.test(n.textContent ?? ""))).toBe(true);
  });
});

describe("demo AI matching isolation", () => {
  it("never makes a network request and reuses recorded results", async () => {
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    const demo = createDemoAdapter();
    expect((await demo.getAIStatus()).provider).toBe("demo");
    expect((await demo.getProfile())?.resume_text).toMatch(/fictional demo candidate/);
    const jobs = await demo.listJobs(50, 0);
    const kestrel = jobs.find((j) => j.company === "Kestrel Data Co.")!;
    const first = await demo.requestMatch({ job_id: kestrel.id, job_description: "x", force: false });
    const again = await demo.requestMatch({ job_id: kestrel.id, job_description: "x", force: false });
    expect(first.run.provider).toBe("demo");
    expect(again.reused).toBe(true);
    const other = jobs.find((j) => j.company === "Fernway Systems")!;
    await expect(demo.requestMatch({ job_id: other.id, job_description: "x", force: false })).rejects.toThrow("recorded analyses");
    await demo.saveProfile("Edited");
    expect(fetch).not.toHaveBeenCalled();
  });

  it("recorded evidence quotes appear verbatim in the fictional profile", async () => {
    const demo = createDemoAdapter();
    const resume = (await demo.getProfile())!.resume_text;
    for (const job of await demo.listJobs(50, 0)) {
      const match = await demo.requestMatch({ job_id: job.id, job_description: "x", force: true }).catch(() => null);
      for (const q of match ? [...match.assessment.supported, ...match.assessment.partial] : []) {
        for (const ev of q.evidence) expect(resume).toContain(ev);
      }
    }
  });

  it("upgrades demo state saved before AI matching existed", async () => {
    const old = createDemoAdapter();
    const raw = JSON.parse(localStorage.getItem("careerops-demo-v1")!);
    delete raw.profile;
    delete raw.matches;
    localStorage.setItem("careerops-demo-v1", JSON.stringify(raw));
    const demo = createDemoAdapter();
    expect((await demo.listJobs(50, 0)).length).toBe((await old.listJobs(50, 0)).length);
    expect(await demo.getProfile()).not.toBeNull();
    expect(await demo.listMatches(50, 0)).toHaveLength(1);
  });
});

describe("live adapter AI endpoints", () => {
  it("calls the matching API only with explicit methods", async () => {
    const fetch = vi.fn(async (_url: string, _init?: RequestInit) => new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetch);
    await liveAdapter.saveProfile("resume");
    await liveAdapter.requestMatch({ job_id: "j", job_description: "d", force: true });
    await liveAdapter.listMatches(10, 0);
    expect(fetch.mock.calls.map((c) => [c[0], c[1]?.method])).toEqual([
      ["/api/profile", "PUT"], ["/api/matches", "POST"], ["/api/matches?limit=10&offset=0", undefined],
    ]);
    expect(JSON.parse(fetch.mock.calls[1][1]!.body as string)).toEqual({ job_id: "j", job_description: "d", force: true });
  });
});

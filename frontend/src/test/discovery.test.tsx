import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderAt, spiedAdapter } from "./render";
import { createDemoAdapter } from "../demo/adapter";
import { liveAdapter } from "../api/live";
import { safeExternalUrl } from "../components/ui";

afterEach(() => vi.unstubAllGlobals());

const card = (title: string, n = 0) => screen.getAllByRole("heading", { name: title, level: 3 })[n].closest("article")!;
const countLine = () => screen.getByText(/jobs (discovered|shown)/).closest("p")!;

describe("Job discovery cards", () => {
  it("shows a rich card per job with distinct dates, salary, links, and preference vs experience labels", async () => {
    const adapter = spiedAdapter();
    renderAt("/", adapter);
    await userEvent.click(screen.getByRole("link", { name: "Job discovery" }));
    await screen.findByRole("heading", { name: "Backend Developer (Python)", level: 3 });
    const c = card("Backend Developer (Python)");
    expect(within(c).getByText("Matches preferences")).toBeInTheDocument();
    expect(within(c).getByText("Needs review")).toBeInTheDocument();
    expect(within(c).getByText("Jersey City, NJ · Hybrid")).toBeInTheDocument();
    expect(within(c).getByText("Full-time", { selector: ".badge-employment" })).toBeInTheDocument();
    expect(within(c).getByText("3+ years of Python backend development", { selector: "p.job-experience *, p.job-experience" })).toBeInTheDocument();
    expect(within(c).getByText("125,000–150,000 USD per year")).toBeInTheDocument();
    expect(within(c).getByText(/^Posted /)).toBeInTheDocument();
    expect(within(c).getByText(/^First discovered /)).toBeInTheDocument();
    expect(within(c).getByText(/^Last seen /)).toBeInTheDocument();
    expect(within(c).getByText('Role: Backend ("backend")')).toBeInTheDocument();
    expect(within(within(c).getByRole("list", { name: "Technologies mentioned" })).getByText("FastAPI")).toBeInTheDocument();
    expect(within(c).getByText(/Experience match \(AI, optional\): Not evaluated/)).toBeInTheDocument();
    // Three different destinations.
    const posting = within(c).getByRole("link", { name: /^Open employer job posting/ });
    expect(posting).toHaveAttribute("href", "https://example.com/discovery/kestrel/backend-python");
    expect(posting).toHaveAttribute("target", "_blank");
    expect(posting).toHaveAttribute("rel", "noopener noreferrer");
    expect(within(c).getByRole("link", { name: /^Company website/ })).toHaveAttribute("href", "https://kestreldata.example");
    // The provider-supplied application form, separate from the posting page; the source is the employer's board.
    expect(within(c).getByRole("link", { name: /^Open employer application/ })).toHaveAttribute("href", "https://example.com/discovery/kestrel/backend-python/apply");
    expect(within(c).getByText(/Source: Lever \(employer job board\)/)).toBeInTheDocument();
    expect(screen.getByLabelText("Jobs by source")).toHaveTextContent(/Greenhouse \d+ · Himalayas \d+ · Lever \d+/);
    expect(within(c).getByRole("link", { name: /Kestrel Data Co\. company profile/ })).toHaveAttribute("href", expect.stringMatching(/^\/companies\?company=/));
    expect(adapter.startDiscoveryRun).not.toHaveBeenCalled();
    expect(adapter.requestMatch).not.toHaveBeenCalled(); // no AI on page load
  });

  it("handles missing data without inventing it, and labels aggregator links and unverified companies", async () => {
    renderAt("/discovery?review_status=all&show_duplicates=1", spiedAdapter());
    await screen.findByRole("heading", { name: "Sales Engineer", level: 3 });
    const sales = card("Sales Engineer");
    expect(within(sales).getByText("Posting date not provided")).toBeInTheDocument();
    const salt = card("Production Support Engineer");
    expect(within(salt).getByRole("link", { name: /Open posting \(via Himalayas\)/ })).toBeInTheDocument();
    expect(within(salt).getByText("Website unavailable")).toBeInTheDocument(); // unresolved identity: no website
    expect(within(salt).queryByText("Removed from saved")).toBeNull(); // only shown after an unsave
    expect(within(salt).getByText("Passed")).toBeInTheDocument();
  });

  it("expands details with overview, conservative requirements, and experience match", async () => {
    const adapter = spiedAdapter();
    renderAt("/discovery", adapter);
    await screen.findByRole("heading", { name: "Backend Developer (Python)", level: 3 });
    await userEvent.click(within(card("Backend Developer (Python)")).getByRole("button", { name: /^Details/ }));
    const overview = await screen.findByRole("region", { name: "Overview" });
    expect(within(overview).getByText(/FastAPI services and PostgreSQL/)).toBeInTheDocument();
    expect(within(overview).getByText("Full-time")).toBeInTheDocument();
    const reqs = screen.getByRole("region", { name: "Requirements" });
    // Listed under requirements and again under "experience mentioned".
    expect(within(reqs).getAllByText("3+ years of Python backend development", { selector: "li" })).toHaveLength(2);
    expect(within(reqs).getByText("Docker and CI/CD")).toBeInTheDocument();
    const exp = screen.getByRole("region", { name: "Experience match" });
    expect(within(exp).getByText("Not evaluated")).toBeInTheDocument();
    expect(adapter.getDiscoveredJob).toHaveBeenCalledTimes(1);
    expect(adapter.requestMatch).not.toHaveBeenCalled();
    await userEvent.click(within(card("Backend Developer (Python)")).getByRole("button", { name: /^Hide details/ }));
    expect(screen.queryByRole("region", { name: "Overview" })).not.toBeInTheDocument();
  });

  it("does not render markup from descriptions", async () => {
    const adapter = spiedAdapter();
    const [job] = await adapter.listDiscoveredJobs({}, 1, 0);
    adapter.getDiscoveredJob = vi.fn(async () => ({
      ...job, description: '<img src=x onerror="window.__pwned=1"><script>window.__pwned=1</script>Hello',
      review_history: [], requirements: { sections_found: false, required: [], preferred: [], experience: [], technologies: [] },
    }));
    renderAt("/discovery", adapter);
    await screen.findByRole("heading", { name: job.title, level: 3 });
    await userEvent.click(within(card(job.title)).getByRole("button", { name: /^Details/ }));
    const overview = await screen.findByRole("region", { name: "Overview" });
    expect(within(overview).getByText(/<script>window.__pwned=1<\/script>Hello/)).toBeInTheDocument();
    expect(overview.querySelector("script, img")).toBeNull();
    expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined();
    expect(screen.getByText("The posting doesn't separate required and preferred qualifications. Read the full description above.")).toBeInTheDocument();
  });
});

describe("Job discovery counts, filters, and sorting", () => {
  it("shows totals from all jobs and updates them after review actions", async () => {
    const adapter = spiedAdapter();
    renderAt("/discovery", adapter);
    await waitFor(() => expect(countLine()).toHaveTextContent("6 of 8 jobs shown"));
    expect(countLine()).toHaveTextContent("Showing 1–6 of 6 matching jobs");
    await userEvent.click(within(card("Sales Engineer")).getByRole("button", { name: /^Pass/ }));
    await waitFor(() => expect(countLine()).toHaveTextContent("5 of 8 jobs shown"));
    await userEvent.click(within(card("Backend Developer (Python)")).getByRole("button", { name: /^Save/ }));
    await waitFor(() => expect(countLine()).toHaveTextContent("4 of 8 jobs shown"));
    expect(adapter.getDiscoverySummary).toHaveBeenLastCalledWith(expect.objectContaining({ review_status: "new", hide_duplicates: true }));
  });

  it("filters, sorts, keeps filters in the URL, and resets", async () => {
    const adapter = spiedAdapter();
    renderAt("/discovery", adapter);
    await screen.findByRole("heading", { name: "Backend Developer (Python)", level: 3 });
    await userEvent.selectOptions(screen.getByLabelText("Work arrangement"), "hybrid");
    await waitFor(() => expect(screen.queryByRole("heading", { name: "Sales Engineer", level: 3 })).not.toBeInTheDocument());
    expect(window.location.search).toContain("workplace=hybrid");
    expect(countLine()).toHaveTextContent("1 of 8 jobs shown");
    await userEvent.selectOptions(screen.getByLabelText("Work arrangement"), "");
    await userEvent.selectOptions(screen.getByLabelText("Sort"), "title");
    await waitFor(() => expect(adapter.listDiscoveredJobs).toHaveBeenLastCalledWith(expect.objectContaining({ sort: "title" }), 21, 0));
    const titles = screen.getAllByRole("heading", { level: 3 }).map((h) => h.textContent);
    expect(titles).toEqual([...titles].sort((a, b) => a!.toLowerCase().localeCompare(b!.toLowerCase())));
    await userEvent.type(screen.getByLabelText("Min annual salary (USD)"), "160000");
    // Unknown salaries stay; only comparable salaries below the minimum are hidden.
    await waitFor(() => expect(screen.queryByRole("heading", { name: "Backend Developer (Python)", level: 3 })).not.toBeInTheDocument());
    expect(screen.getByRole("heading", { name: "Senior Platform Engineer", level: 3 })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("checkbox", { name: "Show possible duplicates" }));
    await waitFor(() => expect(countLine()).toHaveTextContent("(including possible duplicates)"));
    await userEvent.click(screen.getByRole("button", { name: "Reset filters" }));
    await waitFor(() => expect(countLine()).toHaveTextContent("6 of 8 jobs shown"));
    expect(countLine()).not.toHaveTextContent("including possible duplicates");
    expect(window.location.search).toBe("");
  });

  it("shows a reset action when nothing matches", async () => {
    renderAt("/discovery?q=zzz-nothing", spiedAdapter());
    expect(await screen.findByText("No discovered jobs match these filters.")).toBeInTheDocument();
    await waitFor(() => expect(countLine()).toHaveTextContent("0 of 8 jobs shown"));
    await userEvent.click(within(screen.getByText("No discovered jobs match these filters.").parentElement!).getByRole("button", { name: "Reset filters" }));
    expect(await screen.findByRole("heading", { name: "Backend Developer (Python)", level: 3 })).toBeInTheDocument();
  });

  it("filters the inbox to one employer from the URL", async () => {
    const adapter = spiedAdapter();
    const [kestrel] = await adapter.listCompanies({ q: "Kestrel" }, 1, 0);
    renderAt(`/discovery?company=${kestrel.id}&review_status=all`, adapter);
    expect(await screen.findByText("Kestrel Data Co.", { selector: "strong" })).toBeInTheDocument();
    await screen.findByRole("heading", { name: "Backend Developer (Python)", level: 3 });
    expect(screen.queryByRole("heading", { name: "Full Stack Engineer", level: 3 })).not.toBeInTheDocument();
    expect(adapter.listDiscoveredJobs).toHaveBeenLastCalledWith(expect.objectContaining({ company_id: kestrel.id }), 21, 0);
  });
});

describe("Discovery review actions and Saved Jobs", () => {
  it("saves (never applying), shows the saved state, then unsaves from Saved Jobs and saves again without duplicates", async () => {
    const adapter = spiedAdapter();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    renderAt("/discovery?review_status=all", adapter);
    await screen.findByRole("heading", { name: "Backend Developer (Python)", level: 3 });
    await userEvent.click(within(card("Backend Developer (Python)")).getByRole("button", { name: /^Save/ }));
    expect(await within(card("Backend Developer (Python)")).findByRole("link", { name: "Saved · View in Saved Jobs" })).toHaveAttribute("href", "/jobs");
    expect(adapter.createApplication).not.toHaveBeenCalled();

    await userEvent.click(within(card("Backend Developer (Python)")).getByRole("link", { name: "Saved · View in Saved Jobs" }));
    const row = (await screen.findByText("Backend Developer (Python)")).closest("tr")!;
    await userEvent.click(within(row).getByRole("button", { name: /^Remove from saved/ }));
    expect(await screen.findByText(/Removed “Backend Developer \(Python\)” from Saved Jobs/)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("cell", { name: /Backend Developer \(Python\)/ })).not.toBeInTheDocument());

    await userEvent.click(within(screen.getByRole("navigation", { name: "Main" })).getByRole("link", { name: "Job discovery" }));
    const c = await waitFor(() => card("Backend Developer (Python)"));
    expect(within(c).getByText("Removed from saved")).toBeInTheDocument();
    await userEvent.click(within(c).getByRole("button", { name: /^Save again/ }));
    // Saved again: it leaves the "Needs review" view and is back in Saved Jobs.
    expect(await screen.findByText(/Saved “Backend Developer \(Python\)” to your saved jobs/)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("heading", { name: "Backend Developer (Python)", level: 3 })).not.toBeInTheDocument());
    const saved = (await adapter.listJobs(200, 0)).filter((j) => j.title === "Backend Developer (Python)");
    expect(saved).toHaveLength(1);
    expect(adapter.createApplication).not.toHaveBeenCalled();
  });

  it("passes, defers, and restores", async () => {
    const adapter = spiedAdapter();
    renderAt("/discovery", adapter);
    await screen.findByRole("heading", { name: "Sales Engineer", level: 3 });
    await userEvent.click(within(card("Sales Engineer")).getByRole("button", { name: /^Pass/ }));
    await userEvent.click(within(card("Data Engineer II")).getByRole("button", { name: /^Review later/ }));
    await waitFor(() => expect(screen.queryByRole("heading", { name: "Sales Engineer", level: 3 })).not.toBeInTheDocument());
    await userEvent.selectOptions(screen.getByLabelText("Review"), "skipped");
    await userEvent.click(within(await waitFor(() => card("Sales Engineer"))).getByRole("button", { name: /^Restore/ }));
    expect(adapter.reviewDiscoveredJob).toHaveBeenLastCalledWith(expect.any(String), "new");
    await userEvent.selectOptions(screen.getByLabelText("Review"), "later");
    expect(await screen.findByRole("heading", { name: "Data Engineer II", level: 3 })).toBeInTheDocument();
  });

  it("protects jobs with a recorded application from being unsaved", async () => {
    renderAt("/jobs", spiedAdapter());
    const applied = (await screen.findByText("Data Engineer")).closest("tr")!;
    expect(within(applied).queryByRole("button", { name: /Remove from saved/ })).not.toBeInTheDocument();
    expect(within(applied).getByText("Has an application")).toBeInTheDocument();
  });

  it("runs discovery on request and shows run errors", async () => {
    const adapter = spiedAdapter();
    renderAt("/discovery", adapter);
    await screen.findByRole("heading", { name: "Backend Developer (Python)", level: 3 });
    await userEvent.click(screen.getByRole("button", { name: "Run discovery" }));
    const summary = await screen.findByRole("region", { name: "Discovery run status" });
    await waitFor(() => expect(summary).toHaveTextContent("3 new"));
    expect(summary).toHaveTextContent("partially completed");
    expect(summary).toHaveTextContent("Error — Northwind Cartography (Greenhouse): HTTP 503 (simulated)");
    expect(summary).toHaveTextContent("Skipped — JSearch: Not configured in the demo");
    await waitFor(() => expect(countLine()).toHaveTextContent("9 of 11 jobs shown"));
    const failing = spiedAdapter({ startDiscoveryRun: vi.fn(async () => { throw new Error("No discovery sources are available"); }) });
    renderAt("/discovery", failing);
    await userEvent.click((await screen.findAllByRole("button", { name: "Run discovery" })).at(-1)!);
    expect(await screen.findByText("No discovery sources are available")).toBeInTheDocument();
  });

  it("shows an error with retry", async () => {
    let fail = true;
    const adapter = spiedAdapter();
    const real = adapter.listDiscoveredJobs;
    adapter.listDiscoveredJobs = vi.fn((q, l, o) => (fail ? Promise.reject(new Error("Cannot reach the CareerOps API.")) : real(q, l, o)));
    renderAt("/discovery", adapter);
    expect(await screen.findByRole("alert")).toHaveTextContent("Cannot reach the CareerOps API.");
    fail = false;
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("heading", { name: "Backend Developer (Python)", level: 3 })).toBeInTheDocument();
  });

  it("saves role families and geographies", async () => {
    const adapter = spiedAdapter();
    renderAt("/discovery", adapter);
    const form = await screen.findByRole("form", { name: "Search preferences" });
    await waitFor(() => expect(within(form).getByRole("checkbox", { name: "SDET / QA automation" })).toBeChecked());
    await userEvent.click(within(form).getByRole("checkbox", { name: "SDET / QA automation" }));
    await userEvent.click(within(form).getByRole("checkbox", { name: "US-eligible remote" }));
    await userEvent.click(within(form).getByRole("button", { name: "Save preferences" }));
    expect(adapter.saveDiscoveryFilters).toHaveBeenCalledWith(expect.objectContaining({
      role_families: ["backend", "software", "fullstack", "platform", "devops", "support"], geographies: ["nj", "nyc"],
    }));
  });
});

describe("safe external links", () => {
  it("only allows http(s)", () => {
    expect(safeExternalUrl("https://example.com/a")).toBe("https://example.com/a");
    expect(safeExternalUrl("javascript:alert(1)")).toBeNull();
    expect(safeExternalUrl("data:text/html,x")).toBeNull();
    expect(safeExternalUrl("not a url")).toBeNull();
    expect(safeExternalUrl(null)).toBeNull();
  });
});

describe("demo discovery isolation", () => {
  it("never makes a network request, including runs, verification, details, and unsave", async () => {
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    const demo = createDemoAdapter();
    const c = await demo.createCompany({ name: "Real-looking Co", domain: "realco.example" });
    await expect(demo.verifyCompanyBoard(c.id, "https://acme.example/careers")).rejects.toThrow("Not a supported ATS link");
    expect((await demo.verifyCompanyBoard(c.id, "https://jobs.lever.co/unknownboard")).verification_status).toBe("failed");
    await demo.startDiscoveryRun();
    const [job] = await demo.listDiscoveredJobs({}, 1, 0);
    await demo.getDiscoveredJob(job.id);
    await demo.getDiscoverySummary({ review_status: "new" });
    const saved = await demo.saveDiscoveredJob(job.id);
    await demo.unsaveJob(saved.saved_job_id!);
    await demo.getDiscoveryCoverage();
    expect((await demo.discoverCompany(c.id)).discovery_evidence.at(-1)?.detail).toMatch(/no website was contacted/);
    expect(fetch).not.toHaveBeenCalled();
  });

  it("repeated runs don't duplicate, unsave/resave reuses the same job, and applications are protected", async () => {
    const demo = createDemoAdapter();
    await demo.startDiscoveryRun();
    expect((await demo.startDiscoveryRun()).jobs_inserted).toBe(0);
    const all = await demo.listDiscoveredJobs({}, 200, 0);
    expect(new Set(all.map((j) => `${j.provider}:${j.source_posting_id}`)).size).toBe(all.length);
    const jobsBefore = (await demo.listJobs(200, 0)).length;
    const appsBefore = (await demo.listApplications({}, 200, 0)).length;
    const first = await demo.saveDiscoveredJob(all[0].id);
    expect((await demo.saveDiscoveredJob(all[0].id)).saved_job_id).toBe(first.saved_job_id);
    await expect(demo.reviewDiscoveredJob(all[0].id, "skipped")).rejects.toThrow("Saved Jobs");
    await demo.unsaveJob(first.saved_job_id!);
    await demo.unsaveJob(first.saved_job_id!); // idempotent
    expect((await demo.listJobs(200, 0)).length).toBe(jobsBefore);
    await demo.startDiscoveryRun(); // rediscovery never resaves
    const after = (await demo.listDiscoveredJobs({}, 200, 0)).find((j) => j.id === all[0].id)!;
    expect(after.review_status).toBe("new");
    expect(after.unsaved_at).not.toBeNull();
    const again = await demo.saveDiscoveredJob(all[0].id);
    expect(again.saved_job_id).toBe(first.saved_job_id);
    expect((await demo.listJobs(200, 0)).length).toBe(jobsBefore + 1);
    expect((await demo.listApplications({}, 200, 0)).length).toBe(appsBefore);
    const history = (await demo.getDiscoveredJob(all[0].id)).review_history.map((h) => h.action);
    expect(history).toEqual(["saved", "unsaved", "saved"]);
    const [app] = await demo.listApplications({}, 1, 0);
    await expect(demo.unsaveJob(app.job_id)).rejects.toThrow("recorded application");
    expect((await demo.listApplications({}, 200, 0)).find((a) => a.id === app.id)).toEqual(app);
  });

  it("uses only fictional employers and example postings, and persists in localStorage", async () => {
    const demo = createDemoAdapter();
    const jobs = await demo.listDiscoveredJobs({}, 200, 0);
    for (const j of jobs) expect(new URL(j.posting_url!).hostname).toBe("example.com");
    for (const c of await demo.listCompanies({}, 200, 0)) if (c.domain) expect(c.domain).toMatch(/\.example$/);
    await demo.reviewDiscoveredJob(jobs[0].id, "skipped");
    const reloaded = await createDemoAdapter().listDiscoveredJobs({ review_status: "skipped" }, 200, 0);
    expect(reloaded.map((j) => j.id)).toContain(jobs[0].id);
  });
});

describe("live discovery adapter", () => {
  it("builds query strings and endpoints", async () => {
    const fetch = vi.fn(async () => new Response("[]", { status: 200 }));
    vi.stubGlobal("fetch", fetch);
    await liveAdapter.listDiscoveredJobs({ review_status: "new", filter_result: "", provider: "himalayas", q: "", hide_duplicates: true, min_salary: 120000, sort: "posted" }, 21, 0);
    expect(fetch).toHaveBeenLastCalledWith("/api/discovery/jobs?review_status=new&provider=himalayas&hide_duplicates=true&min_salary=120000&sort=posted&limit=21&offset=0", expect.anything());
    await liveAdapter.getDiscoverySummary({ review_status: "new", hide_duplicates: false });
    expect(fetch).toHaveBeenLastCalledWith("/api/discovery/jobs/summary?review_status=new", expect.anything());
    await liveAdapter.getDiscoveredJob("abc");
    expect(fetch).toHaveBeenLastCalledWith("/api/discovery/jobs/abc", expect.anything());
    await liveAdapter.unsaveJob("j1");
    expect(fetch).toHaveBeenLastCalledWith("/api/jobs/j1/unsave", expect.objectContaining({ method: "POST" }));
    await liveAdapter.listCompanies({ q: "", ats_status: "", origin: "", sort: "name" }, 26, 0);
    expect(fetch).toHaveBeenLastCalledWith("/api/companies?sort=name&limit=26&offset=0", expect.anything());
    await liveAdapter.discoverCompany("c1").catch(() => undefined);
    expect(fetch).toHaveBeenLastCalledWith("/api/companies/c1/discover", expect.objectContaining({ method: "POST" }));
  });
});

describe("responsive layout", () => {
  it("stacks card headers, actions, and detail facts on narrow screens", async () => {
    const css = (await import("../styles.css?raw")).default;
    const mobile = css.slice(css.indexOf("@media (max-width: 760px)"));
    for (const rule of [".job-card-head { flex-direction: column;", ".job-actions { flex-direction: column; }", ".facts { grid-template-columns: 1fr; }"]) {
      expect(mobile).toContain(rule);
    }
  });
});


describe("required experience and employment type", () => {
  it("shows stated experience or 'Not specified', separately from the AI experience match", async () => {
    renderAt("/discovery", spiedAdapter());
    await screen.findByRole("heading", { name: "Senior Platform Engineer", level: 3 });
    const stated = within(card("Senior Platform Engineer")).getByText(/Experience required:/).closest("p")!;
    expect(stated).toHaveTextContent("Experience required: 5+ years building platform or infrastructure tooling");
    // "Senior" in a title never becomes a years requirement.
    const quill = within(card("Full Stack Engineer")).getByText(/Experience required:/).closest("p")!;
    expect(quill).toHaveTextContent("Experience required: Not specified");
    expect(within(card("Full Stack Engineer")).getByText(/Experience match \(AI, optional\): Not evaluated/)).toBeInTheDocument();
    expect(within(card("Data Engineer II")).getByText("Employment type not stated", { selector: ".badge-employment" })).toBeInTheDocument();
  });

  it("filters by several employment types server-side, keeps the selection in the URL through actions, and resets", async () => {
    const adapter = spiedAdapter();
    renderAt("/discovery", adapter);
    await screen.findByRole("heading", { name: "Backend Developer (Python)", level: 3 });
    const types = screen.getByRole("group", { name: "Employment type" });
    await userEvent.click(within(types).getByRole("checkbox", { name: "Contract" }));
    await userEvent.click(within(types).getByRole("checkbox", { name: "Contract-to-hire" }));
    await waitFor(() => expect(countLine()).toHaveTextContent("1 of 8 jobs shown"));
    expect(screen.getAllByRole("heading", { level: 3 }).map((h) => h.textContent)).toEqual(["SDET, Payments"]);
    expect(new URLSearchParams(window.location.search).get("employment_type")).toBe("Contract,Contract-to-hire");
    expect(adapter.getDiscoverySummary).toHaveBeenLastCalledWith(expect.objectContaining({ employment_type: ["Contract", "Contract-to-hire"] }));
    // A review action keeps the filter.
    await userEvent.click(within(card("SDET, Payments")).getByRole("button", { name: /^Review later/ }));
    await waitFor(() => expect(countLine()).toHaveTextContent("0 of 8 jobs shown"));
    expect(within(screen.getByRole("group", { name: "Employment type" })).getByRole("checkbox", { name: "Contract-to-hire" })).toBeChecked();
    // "Not stated" includes jobs without a type; leaving it out hides them.
    await userEvent.click(within(screen.getByRole("group", { name: "Employment type" })).getByRole("checkbox", { name: "Not stated" }));
    expect(await screen.findByRole("heading", { name: "Data Engineer II", level: 3 })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Sales Engineer", level: 3 })).not.toBeInTheDocument(); // Full-time, not selected
    await userEvent.click(screen.getByRole("button", { name: "Reset filters" }));
    await waitFor(() => expect(countLine()).toHaveTextContent("5 of 8 jobs shown"));
    expect(within(screen.getByRole("group", { name: "Employment type" })).getByRole("checkbox", { name: "Contract" })).not.toBeChecked();
  });

  it("paginates with the employment filter applied (demo adapter, no network)", async () => {
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    const demo = createDemoAdapter();
    const filter = { employment_type: ["Full-time" as const, "unknown" as const] };
    const all = await demo.listDiscoveredJobs(filter, 200, 0);
    const summary = await demo.getDiscoverySummary(filter);
    expect(summary.matching).toBe(all.length);
    expect(all.every((j) => j.employment_type === "Full-time" || j.employment_type === null)).toBe(true);
    const page2 = await demo.listDiscoveredJobs(filter, 3, 3);
    expect(page2.map((j) => j.id)).toEqual(all.slice(3, 6).map((j) => j.id));
    expect((await demo.listDiscoveredJobs({ employment_type: ["Contract-to-hire"] }, 200, 0)).map((j) => j.title)).toEqual(["SDET, Payments"]);
    expect(fetch).not.toHaveBeenCalled();
  });

  it("sends repeated employment_type parameters to the API", async () => {
    const fetch = vi.fn(async () => new Response("[]", { status: 200 }));
    vi.stubGlobal("fetch", fetch);
    await liveAdapter.listDiscoveredJobs({ employment_type: ["Contract", "unknown"] }, 21, 0);
    expect(fetch).toHaveBeenLastCalledWith("/api/discovery/jobs?employment_type=Contract&employment_type=unknown&limit=21&offset=0", expect.anything());
  });
});

describe("employer-source discovery", () => {
  it("shows an empty inbox after a reset: zero count, no cards, no pager, and how to populate it", async () => {
    const adapter = spiedAdapter({
      listDiscoveredJobs: async () => [],
      getDiscoverySummary: async () => ({ total: 0, matching: 0, by_result: { match: 0, unknown: 0, rejected: 0 }, by_source: {}, archived: 405, non_vacancies: 22, closed: 0 }),
    });
    renderAt("/discovery", adapter);
    expect(await screen.findByText("0 jobs discovered")).toBeInTheDocument();
    expect(screen.queryAllByRole("article")).toHaveLength(0);
    expect(screen.getByText(/No jobs discovered yet/)).toBeInTheDocument();
    expect(screen.getByLabelText("Jobs by source")).toHaveTextContent("405 earlier postings archived");
    expect(screen.getByLabelText("Jobs by source")).toHaveTextContent("22 non-vacancy listings hidden");
    expect(screen.queryByRole("button", { name: "Next" })).not.toBeInTheDocument();
  });

  it("links a Greenhouse job only to its employer posting (no application form is claimed)", async () => {
    renderAt("/discovery", spiedAdapter());
    await screen.findAllByRole("heading", { level: 3 });
    const gh = screen.getAllByText(/Source: Greenhouse \(employer job board\)/)[0].closest("article")!;
    expect(within(gh).getByRole("link", { name: /^Open employer job posting/ })).toBeInTheDocument();
    expect(within(gh).queryByRole("link", { name: /Open employer application/ })).not.toBeInTheDocument();
  });
});

describe("background sync progress", () => {
  it("polls a running run, updates progress and counts without a reload, and marks partial completion", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const adapter = spiedAdapter();
    const [base] = await adapter.listDiscoveryRuns(1, 0);
    const running = { ...base, id: "r1", status: "running" as const, trigger: "manual" as const, tasks_total: 3, tasks_done: 0,
      tasks_failed: 0, tasks_remaining: 3, jobs_inserted: 0, sources: [], warnings: [] };
    const halfway = { ...running, tasks_done: 1, tasks_remaining: 2, jobs_inserted: 4 };
    const finished = { ...running, status: "partial" as const, tasks_done: 2, tasks_failed: 1, tasks_remaining: 0, jobs_inserted: 4,
      sources_checked: 3, sources_failed: 1, sources: [{ kind: "board" as const, provider: "greenhouse" as const, company: "Acme",
        status: "failed" as const, retrieved: 0, inserted: 0, updated: 0, duplicates: 0, duration_ms: 1, error: "HTTP 503" }] };
    adapter.startDiscoveryRun = vi.fn(async () => running);
    const steps = [halfway, finished];
    adapter.getDiscoveryRun = vi.fn(async () => steps.shift() ?? finished);
    renderAt("/discovery", adapter);
    await screen.findByRole("heading", { name: "Backend Developer (Python)", level: 3 });
    await userEvent.click(screen.getByRole("button", { name: "Run discovery" }));
    const status = await screen.findByRole("region", { name: "Discovery run status" });
    expect(status).toHaveTextContent("3 sources queued · 0 completed · 3 remaining");
    expect(screen.getByRole("button", { name: "Discovery running…" })).toBeDisabled();
    const summaryCalls = (adapter.getDiscoverySummary as ReturnType<typeof vi.fn>).mock.calls.length;
    await vi.advanceTimersByTimeAsync(2100);
    await waitFor(() => expect(status).toHaveTextContent("1 completed · 2 remaining"));
    expect((adapter.getDiscoverySummary as ReturnType<typeof vi.fn>).mock.calls.length).toBeGreaterThan(summaryCalls);  // counts refresh
    await vi.advanceTimersByTimeAsync(2100);
    await waitFor(() => expect(status).toHaveTextContent("partially completed"));
    expect(status).toHaveTextContent("3 completed · 0 remaining · 1 failed");
    expect(status).toHaveTextContent("Error — Acme (Greenhouse): HTTP 503");
    expect(screen.getByRole("button", { name: "Run discovery" })).toBeEnabled();
    vi.useRealTimers();
  });
});

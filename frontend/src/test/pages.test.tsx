import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderAt, spiedAdapter } from "./render";
import { today } from "../dates";

const statusCount = (label: string) =>
  within(screen.getByRole("region", { name: "By status" })).getByText(label).closest("li")!.querySelector(".meter-value")!.textContent;
const statValue = (label: string) => screen.getByText(label, { selector: ".stat-label" }).closest(".stat")!.querySelector(".stat-value")!.textContent;

describe("Dashboard", () => {
  it("shows totals, status breakdown, follow-ups, and recent applications from adapter data", async () => {
    renderAt("/", spiedAdapter());
    await screen.findByText("Saved jobs", { selector: ".stat-label" });
    expect(statValue("Saved jobs")).toBe("10");
    expect(statValue("Applications submitted")).toBe("7");
    expect(screen.getByText(/3 not yet applied/)).toBeInTheDocument();
    expect(screen.getByText("5 active · 2 closed")).toBeInTheDocument();
    expect(statusCount("Interviewing")).toBe("2");
    expect(statusCount("Offer")).toBe("1");
    expect(statusCount("Withdrawn")).toBe("1");
    const upcoming = screen.getByRole("region", { name: "Upcoming follow-ups" });
    // Soonest follow-up first (offer at +1 day).
    expect(within(upcoming).getAllByRole("listitem")[0]).toHaveTextContent("Tidewater Robotics");
    const recent = screen.getByRole("region", { name: "Recent applications" });
    expect(within(recent).getAllByRole("listitem")).toHaveLength(5);
  });

  it("shows empty states with zero counts when there is no data", async () => {
    renderAt("/", spiedAdapter({ listJobs: async () => [], listApplications: async () => [] }));
    expect(await screen.findByText("No upcoming follow-ups.")).toBeInTheDocument();
    expect(statValue("Saved jobs")).toBe("0");
    expect(statValue("Applications submitted")).toBe("0");
    expect(statusCount("Applied")).toBe("0");
    // Empty state offers the next step instead of a dead end.
    expect(screen.getByRole("link", { name: "Add your first job" })).toHaveAttribute("href", "/jobs?add=1");
  });

  it("Add job action opens the job form on the Saved jobs page", async () => {
    renderAt("/", spiedAdapter());
    expect(await screen.findByRole("link", { name: "View applications" })).toHaveAttribute("href", "/applications");
    await userEvent.click(screen.getByRole("link", { name: "Add job" }));
    expect(await screen.findByRole("form", { name: "Add a job opportunity" })).toBeInTheDocument();
  });

  it("shows an error with retry when loading fails", async () => {
    let fail = true;
    const adapter = spiedAdapter();
    const real = adapter.listJobs;
    adapter.listJobs = vi.fn((l, o) => (fail ? Promise.reject(new Error("Cannot reach the CareerOps API.")) : real(l, o)));
    renderAt("/", adapter);
    expect(await screen.findByRole("alert")).toHaveTextContent("Cannot reach the CareerOps API.");
    fail = false;
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(statValue("Saved jobs")).toBe("10"));
  });
});

describe("Saved jobs", () => {
  it("creates a job with trimmed values and shows it unapplied", async () => {
    const adapter = spiedAdapter();
    renderAt("/jobs", adapter);
    await userEvent.click(await screen.findByRole("button", { name: "Add job" }));
    await userEvent.type(screen.getByLabelText("Company *"), "  Acme Test Corp ");
    await userEvent.type(screen.getByLabelText("Title *"), "QA Engineer");
    await userEvent.type(screen.getByLabelText("Posting URL"), "https://example.com/acme");
    await userEvent.click(screen.getByRole("button", { name: "Save job" }));
    expect(adapter.createJob).toHaveBeenCalledWith({
      company: "Acme Test Corp", title: "QA Engineer", location: null,
      posting_url: "https://example.com/acme", description: null, notes: null,
    });
    const row = (await screen.findByText("Acme Test Corp")).closest("tr")!;
    expect(within(row).getByRole("link", { name: /record application/i })).toBeInTheDocument();
    expect(within(row).queryByText("Applied")).not.toBeInTheDocument();
  });

  it("validates required fields before calling the API and surfaces server errors", async () => {
    const adapter = spiedAdapter({ createJob: () => Promise.reject(new Error("posting_url: Input should be a valid URL")) });
    renderAt("/jobs", adapter);
    await userEvent.click(await screen.findByRole("button", { name: "Add job" }));
    await userEvent.click(screen.getByRole("button", { name: "Save job" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Company and title are required.");
    expect(adapter.createJob).not.toHaveBeenCalled();
    await userEvent.type(screen.getByLabelText("Company *"), "X");
    await userEvent.type(screen.getByLabelText("Title *"), "Y");
    await userEvent.click(screen.getByRole("button", { name: "Save job" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("posting_url: Input should be a valid URL");
  });

  it("empty filter results offer a way to clear the filters", async () => {
    renderAt("/jobs", spiedAdapter());
    await screen.findByText("Lumen Harbor Labs");
    await userEvent.type(screen.getByRole("searchbox"), "no such company");
    await userEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(screen.getByRole("searchbox")).toHaveValue("");
    expect(screen.getByText("Lumen Harbor Labs")).toBeInTheDocument();
  });

  it("filters by search text and applied state", async () => {
    renderAt("/jobs", spiedAdapter());
    await screen.findByText("Lumen Harbor Labs");
    await userEvent.type(screen.getByRole("searchbox"), "kestrel");
    expect(screen.getAllByRole("row")).toHaveLength(2); // header + 1
    await userEvent.clear(screen.getByRole("searchbox"));
    await userEvent.selectOptions(screen.getByLabelText("Show"), "open");
    expect(screen.getAllByRole("row")).toHaveLength(4); // header + 3 jobs without applications
  });
});

describe("Applications", () => {
  it("requires explicit submission confirmation before recording an application", async () => {
    const adapter = spiedAdapter();
    renderAt("/jobs", adapter);
    await userEvent.click(await screen.findByRole("link", { name: /record application for API Engineer at Pinecrest Fintech/i }));
    const form = await screen.findByRole("form", { name: "Record a submitted application" });
    await waitFor(() => expect(within(form).getByLabelText("Saved job *")).toHaveDisplayValue("Pinecrest Fintech — API Engineer"));
    const submit = within(form).getByRole("button", { name: "Record application" });
    expect(submit).toBeDisabled();
    expect(adapter.createApplication).not.toHaveBeenCalled();

    await userEvent.click(within(form).getByRole("checkbox", { name: /I confirm I actually submitted/ }));
    await userEvent.click(submit);
    expect(adapter.createApplication).toHaveBeenCalledWith(expect.objectContaining({
      submission_confirmed: true, status: "applied", follow_up_date: null, notes: null,
    }));
    expect(await screen.findByText("Pinecrest Fintech")).toBeInTheDocument();
  });

  it("only offers saved jobs that have no application yet", async () => {
    renderAt("/applications?record=", spiedAdapter());
    const select = await screen.findByLabelText("Saved job *");
    expect(within(select).getAllByRole("option")).toHaveLength(4); // placeholder + 3 open jobs
  });

  it("ignores a preselected job that already has an application", async () => {
    const adapter = spiedAdapter();
    const [existing] = await adapter.listApplications({}, 1, 0);
    renderAt(`/applications?record=${existing.job_id}`, adapter);
    const select = await screen.findByLabelText("Saved job *");
    expect(select).toHaveDisplayValue("Select a saved job…");
    await userEvent.click(screen.getByRole("checkbox", { name: /I confirm/ }));
    await userEvent.click(screen.getByRole("button", { name: "Record application" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Choose the saved job you applied to.");
    expect(adapter.createApplication).not.toHaveBeenCalled();
  });

  it("rejects a follow-up date before the application date", async () => {
    const adapter = spiedAdapter();
    renderAt("/applications", adapter);
    await userEvent.click(await screen.findByRole("button", { name: /Edit Backend Engineer/ }));
    const date = screen.getByLabelText("Follow-up date");
    await userEvent.clear(date);
    await userEvent.type(date, "2000-01-01");
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Follow-up date cannot be before the application date.");
    expect(adapter.updateApplication).not.toHaveBeenCalled();
  });

  it("updates an application's status", async () => {
    const adapter = spiedAdapter();
    renderAt("/applications", adapter);
    await userEvent.click(await screen.findByRole("button", { name: /Edit Data Engineer at Cedarline Health/ }));
    const edit = screen.getByRole("form", { name: /Edit application/ });
    await userEvent.selectOptions(within(edit).getByLabelText("Status"), "interviewing");
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }));
    expect(adapter.updateApplication).toHaveBeenCalledWith(expect.any(String), expect.objectContaining({ status: "interviewing" }));
    const row = (await screen.findByText("Cedarline Health")).closest("tr")!;
    expect(within(row).getByText("Interviewing")).toBeInTheDocument();
  });

  it("filters by status, company, and date and paginates", async () => {
    const adapter = spiedAdapter();
    renderAt("/applications", adapter);
    await screen.findByText("Lumen Harbor Labs");
    const filters = screen.getByRole("form", { name: "Filter applications" });
    await userEvent.selectOptions(within(filters).getByLabelText("Status"), "interviewing");
    await userEvent.type(within(filters).getByLabelText("Company"), "saltmarsh");
    await userEvent.type(within(filters).getByLabelText("Applied from"), "2000-01-01");
    await userEvent.click(within(filters).getByRole("button", { name: "Apply filters" }));
    expect(adapter.listApplications).toHaveBeenLastCalledWith(
      { status: "interviewing", company: "saltmarsh", applied_from: "2000-01-01", applied_to: "" }, 21, 0,
    );
    await waitFor(() => expect(screen.getAllByRole("row")).toHaveLength(2));
    expect(screen.getByText("Saltmarsh Energy")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
  });

  it("deletes only after explicit confirmation", async () => {
    const adapter = spiedAdapter();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    renderAt("/applications", adapter);
    const del = await screen.findByRole("button", { name: /Delete Data Engineer at Cedarline Health/ });
    await userEvent.click(del);
    expect(confirm).toHaveBeenCalled();
    expect(adapter.deleteApplication).not.toHaveBeenCalled();
    confirm.mockReturnValue(true);
    await userEvent.click(del);
    expect(adapter.deleteApplication).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(screen.queryByText("Cedarline Health")).not.toBeInTheDocument());
    confirm.mockRestore();
  });

  it("returns to the previous page, keeping filters, when a delete empties the current page", async () => {
    const adapter = spiedAdapter();
    // Seed has 2 "applied" rows; 19 more (applied today) make 21, so page 2 holds only the oldest: Cedarline.
    for (let i = 0; i < 19; i++) {
      const job = await adapter.createJob({ company: `Filler ${i}`, title: "Dev" });
      await adapter.createApplication({
        job_id: job.id, submission_confirmed: true, status: "applied", applied_date: today(), follow_up_date: null, notes: null,
      });
    }
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    renderAt("/applications", adapter);
    await screen.findByText("Filler 0");
    await userEvent.selectOptions(screen.getByLabelText("Status"), "applied");
    await userEvent.click(screen.getByRole("button", { name: "Apply filters" }));
    await userEvent.click(await screen.findByRole("button", { name: "Next" }));
    await userEvent.click(await screen.findByRole("button", { name: /Delete Data Engineer at Cedarline Health/ }));

    await waitFor(() => expect(screen.getByText("Page 1")).toBeInTheDocument());
    await waitFor(() => expect(screen.getAllByRole("row")).toHaveLength(21)); // header + 20
    expect(adapter.listApplications).toHaveBeenLastCalledWith(expect.objectContaining({ status: "applied" }), 21, 0);
    expect(screen.getByLabelText("Status")).toHaveValue("applied");
    confirm.mockRestore();
  });

  it("stays on page 1 when deleting its last row", async () => {
    const adapter = spiedAdapter();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    renderAt("/applications", adapter);
    await screen.findByText("Lumen Harbor Labs");
    await userEvent.selectOptions(screen.getByLabelText("Status"), "offer");
    await userEvent.click(screen.getByRole("button", { name: "Apply filters" }));
    await userEvent.click(await screen.findByRole("button", { name: /Delete Software Engineer, Platform at Tidewater Robotics/ }));
    expect(await screen.findByText("No applications match these filters.")).toBeInTheDocument();
    expect(screen.getByText("Page 1")).toBeInTheDocument();
    for (const [, , offset] of vi.mocked(adapter.listApplications).mock.calls) expect(offset).toBeGreaterThanOrEqual(0);
    confirm.mockRestore();
  });

  it("downloads the Excel export using the active filters", async () => {
    const blob = new Blob(["x"]);
    const adapter = spiedAdapter({ exportApplications: async () => ({ blob, filename: "applications_20261120.xlsx" }) });
    const createUrl = vi.fn(() => "blob:test");
    Object.assign(URL, { createObjectURL: createUrl, revokeObjectURL: vi.fn() });
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    renderAt("/applications", adapter);
    await screen.findByText("Lumen Harbor Labs");
    await userEvent.selectOptions(screen.getByLabelText("Status"), "offer");
    await userEvent.click(screen.getByRole("button", { name: "Apply filters" }));
    await userEvent.click(screen.getByRole("button", { name: "Download Excel" }));
    expect(adapter.exportApplications).toHaveBeenCalledWith(expect.objectContaining({ status: "offer" }));
    expect(createUrl).toHaveBeenCalledWith(blob);
    expect(click).toHaveBeenCalled();
    click.mockRestore();
  });

  it("shows export errors", async () => {
    renderAt("/applications", spiedAdapter({ exportApplications: () => Promise.reject(new Error("Request failed (HTTP 500)")) }));
    await userEvent.click(await screen.findByRole("button", { name: "Download Excel" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Export failed: Request failed (HTTP 500)");
  });
});

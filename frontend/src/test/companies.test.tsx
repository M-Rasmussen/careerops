import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderAt, spiedAdapter } from "./render";

const companyRow = (name: string) => screen.getByText(name, { selector: "strong" }).closest("tr")!;

describe("Company directory", () => {
  it("is in the sidebar and lists employers by name with status, ATS, and job counts (no ranking)", async () => {
    const adapter = spiedAdapter();
    renderAt("/", adapter);
    await userEvent.click(screen.getByRole("link", { name: "Company directory" }));
    expect(await screen.findByRole("heading", { name: "Company directory" })).toBeInTheDocument();
    const names = (await screen.findAllByText(/./, { selector: "td[data-label=Company] > strong" })).map((n) => n.textContent);
    expect(names).toEqual([...names].sort((a, b) => a!.toLowerCase().localeCompare(b!.toLowerCase())));
    expect(screen.queryByText(/^P[123]$/)).toBeNull();
    expect(screen.queryByLabelText("Priority")).toBeNull();
    const lumen = companyRow("Lumen Harbor Labs");
    expect(within(lumen).getByText("Watched")).toBeInTheDocument();
    expect(within(lumen).getByText("Verified")).toBeInTheDocument();
    expect(within(lumen).getByRole("link", { name: /jobs at Lumen Harbor Labs/ })).toHaveAttribute("href", expect.stringMatching(/^\/discovery\?company=/));
    expect(within(companyRow("Orbital Fern")).getByText("Not found")).toBeInTheDocument();
    expect(within(companyRow("Saltmarsh Robotics")).getByText("Unresolved (name only)")).toBeInTheDocument();
    expect(within(companyRow("Quillfeather Health")).getByText("Discovered")).toBeInTheDocument();
  });

  it("searches, filters by ATS status and origin, and sorts", async () => {
    const adapter = spiedAdapter();
    renderAt("/companies", adapter);
    await screen.findByText("Lumen Harbor Labs", { selector: "strong" });
    await userEvent.selectOptions(screen.getByLabelText("Status"), "discovered");
    await waitFor(() => expect(screen.queryByText("Lumen Harbor Labs", { selector: "strong" })).not.toBeInTheDocument());
    expect(screen.getByText("Quillfeather Health", { selector: "strong" })).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("Status"), "");
    await userEvent.selectOptions(screen.getByLabelText("ATS board"), "none");
    await waitFor(() => expect(screen.queryByText("Kestrel Data Co.", { selector: "strong" })).not.toBeInTheDocument());
    await userEvent.selectOptions(screen.getByLabelText("ATS board"), "");
    await userEvent.type(screen.getByLabelText("Search"), "quill");
    await waitFor(() => expect(screen.getAllByText(/./, { selector: "td[data-label=Company] > strong" })).toHaveLength(1));
    await userEvent.clear(screen.getByLabelText("Search"));
    await userEvent.selectOptions(screen.getByLabelText("Sort"), "jobs");
    expect(adapter.listCompanies).toHaveBeenLastCalledWith(expect.objectContaining({ sort: "jobs" }), 26, 0);
  });

  it("toggles the watchlist and adds a company", async () => {
    const adapter = spiedAdapter();
    renderAt("/companies", adapter);
    await screen.findByText("Orbital Fern", { selector: "strong" });
    await userEvent.click(within(companyRow("Orbital Fern")).getByRole("button", { name: /^Watch/ }));
    expect(adapter.updateCompany).toHaveBeenLastCalledWith(expect.any(String), { watchlist: true });
    await waitFor(() => expect(within(companyRow("Orbital Fern")).getByText("Watched")).toBeInTheDocument());

    await userEvent.click(screen.getByRole("button", { name: "Add company" }));
    const form = screen.getByRole("form", { name: "Add an employer" });
    await userEvent.type(within(form).getByLabelText("Name *"), "New Employer");
    await userEvent.type(within(form).getByLabelText("Domain"), "https://www.NewEmployer.example/");
    await userEvent.click(within(form).getByRole("button", { name: "Add company" }));
    expect(adapter.createCompany).toHaveBeenCalledWith(expect.objectContaining({ name: "New Employer", domain: "https://www.NewEmployer.example/" }));
    expect(await screen.findByText("New Employer", { selector: "strong" })).toBeInTheDocument();
  });

  it("shows board details, verifies a pasted ATS URL, rejects other URLs, and toggles monitoring", async () => {
    const adapter = spiedAdapter();
    renderAt("/companies", adapter);
    await screen.findByText("Brightpath Logistics", { selector: "strong" });
    await userEvent.click(within(companyRow("Brightpath Logistics")).getByRole("button", { name: /^Details/ }));
    const details = screen.getByRole("region", { name: "Details for Brightpath Logistics" });
    expect(within(details).getByText("No verified board yet.")).toBeInTheDocument();
    const url = within(details).getByLabelText("Greenhouse, Lever, or Ashby URL");
    await userEvent.type(url, "https://brightpath.example/careers");
    await userEvent.click(within(details).getByRole("button", { name: "Verify board" }));
    expect(await within(details).findByRole("alert")).toHaveTextContent("Not a supported ATS link");
    await userEvent.clear(url);
    await userEvent.type(url, "https://jobs.lever.co/brightpath/abc-123");
    await userEvent.click(within(details).getByRole("button", { name: "Verify board" }));
    expect(adapter.verifyCompanyBoard).toHaveBeenLastCalledWith(expect.any(String), "https://jobs.lever.co/brightpath/abc-123");
    const refreshed = await screen.findByRole("region", { name: "Details for Brightpath Logistics" });
    expect(await within(refreshed).findByText("Lever · brightpath")).toBeInTheDocument();
    expect(within(refreshed).getByRole("link", { name: /jobs\.lever\.co\/brightpath/ })).toHaveAttribute("href", "https://jobs.lever.co/brightpath");
    await userEvent.click(within(refreshed).getByRole("checkbox", { name: /Monitor/ }));
    expect(adapter.updateJobSource).toHaveBeenLastCalledWith(expect.any(String), { enabled: false });
  });

  it("shows career-source status and retries discovery with a corrected careers URL", async () => {
    const adapter = spiedAdapter();
    renderAt("/companies", adapter);
    await screen.findByText("Lumen Harbor Labs", { selector: "strong" });
    expect(within(companyRow("Lumen Harbor Labs")).getByText("Careers: Verified")).toBeInTheDocument();
    expect(within(companyRow("Orbital Fern")).getByText("Careers: Not checked")).toBeInTheDocument();
    await userEvent.click(within(companyRow("Orbital Fern")).getByRole("button", { name: /^Details/ }));
    const details = screen.getByRole("region", { name: "Details for Orbital Fern" });
    await userEvent.type(within(details).getByLabelText(/Official careers page URL/), "https://orbitalfern.example/careers");
    await userEvent.click(within(details).getByRole("button", { name: "Retry discovery" }));
    expect(adapter.updateCompany).toHaveBeenCalledWith(expect.any(String), { careers_url: "https://orbitalfern.example/careers" });
    expect(adapter.discoverCompany).toHaveBeenCalledTimes(1);
    const refreshed = await screen.findByRole("region", { name: "Details for Orbital Fern" });
    expect(await within(refreshed).findByText(/Lookup problem:/)).toBeInTheDocument();
    expect(within(refreshed).getByText("https://orbitalfern.example/careers", { selector: "a" })).toBeInTheDocument();
  });

  it("offers a domain-change candidate for review and applies it only on click", async () => {
    const adapter = spiedAdapter();
    const [fern] = await adapter.listCompanies({ q: "Orbital" }, 1, 0);
    adapter.getCompany = vi.fn(async () => ({
      ...fern, discovery_status: "needs_review" as const, discovery_error: "Website: Left the official domain (orbitalfern-new.example); not followed",
      discovery_evidence: [{ step: "domain_candidate", detail: "Redirects to another domain", url: "https://orbitalfern.example/", at: "2026-11-24T00:00:00Z", candidate: "orbitalfern-new.example" }],
    }));
    renderAt(`/companies?company=${fern.id}`, adapter);
    const details = await screen.findByRole("region", { name: `Details for ${fern.name}` });
    expect(adapter.updateCompany).not.toHaveBeenCalled();
    await userEvent.click(within(details).getByRole("button", { name: "Use orbitalfern-new.example as official domain" }));
    expect(adapter.updateCompany).toHaveBeenCalledWith(fern.id, { domain: "orbitalfern-new.example" });
    expect(adapter.discoverCompany).toHaveBeenCalledWith(fern.id);
  });

  it("runs a deep scan for one company only on request", async () => {
    const adapter = spiedAdapter();
    renderAt("/companies", adapter);
    await screen.findByText("Kestrel Data Co.", { selector: "strong" });
    expect(adapter.discoverCompany).not.toHaveBeenCalled();
    await userEvent.click(within(companyRow("Kestrel Data Co.")).getByRole("button", { name: /^Details/ }));
    await userEvent.click(within(screen.getByRole("region", { name: "Details for Kestrel Data Co." })).getByRole("button", { name: "Deep scan" }));
    expect(adapter.discoverCompany).toHaveBeenCalledTimes(1);
    expect(adapter.discoverCompany).toHaveBeenCalledWith(expect.any(String), true);
  });

  it("opens one employer from a discovery link", async () => {
    const adapter = spiedAdapter();
    const [kestrel] = await adapter.listCompanies({ q: "Kestrel" }, 1, 0);
    renderAt(`/companies?company=${kestrel.id}`, adapter);
    expect(await screen.findByRole("region", { name: "Details for Kestrel Data Co." })).toBeInTheDocument();
    expect(screen.getAllByText(/./, { selector: "td[data-label=Company] > strong" })).toHaveLength(1);
    await userEvent.click(screen.getByRole("button", { name: "Show all companies" }));
    expect(await screen.findByText("Lumen Harbor Labs", { selector: "strong" })).toBeInTheDocument();
  });

  it("shows an error with retry", async () => {
    let fail = true;
    const adapter = spiedAdapter();
    const real = adapter.listCompanies;
    adapter.listCompanies = vi.fn((q, l, o) => (fail ? Promise.reject(new Error("Cannot reach the CareerOps API.")) : real(q, l, o)));
    renderAt("/companies", adapter);
    expect(await screen.findByRole("alert")).toHaveTextContent("Cannot reach the CareerOps API.");
    fail = false;
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("Lumen Harbor Labs", { selector: "strong" })).toBeInTheDocument();
  });
});

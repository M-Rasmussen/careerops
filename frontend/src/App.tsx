import { BrowserRouter, Route, Routes } from "react-router";
import { DataContext } from "./api/context";
import { Layout } from "./components/Layout";
import { ApplicationsPage } from "./pages/ApplicationsPage";
import { CompaniesPage } from "./pages/CompaniesPage";
import { DashboardPage } from "./pages/DashboardPage";
import { DiscoveryPage } from "./pages/DiscoveryPage";
import { JobsPage } from "./pages/JobsPage";
import { MatchingPage } from "./pages/MatchingPage";
import type { DataAdapter } from "./types";

export function App({ adapter }: { adapter: DataAdapter }) {
  return (
    <DataContext.Provider value={adapter}>
      <BrowserRouter>
        <Routes>
          <Route element={<Layout />}>
            <Route index element={<DashboardPage />} />
            <Route path="discovery" element={<DiscoveryPage />} />
            <Route path="companies" element={<CompaniesPage />} />
            <Route path="jobs" element={<JobsPage />} />
            <Route path="applications" element={<ApplicationsPage />} />
            <Route path="matching" element={<MatchingPage />} />
            <Route path="*" element={<h1>Page not found</h1>} />
          </Route>
        </Routes>
      </BrowserRouter>
    </DataContext.Provider>
  );
}

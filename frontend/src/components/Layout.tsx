import { NavLink, Outlet } from "react-router";
import { useData } from "../api/context";

export function Layout() {
  const data = useData();

  function resetDemo() {
    if (!window.confirm("Reset the demo? All your changes will be replaced with the original sample data.")) return;
    data.reset?.();
    window.location.reload();
  }

  return (
    <div className="shell">
      <a className="skip-link" href="#main">Skip to content</a>
      <aside className="sidebar">
        <div className="brand">CareerOps</div>
        <nav aria-label="Main">
          <NavLink to="/" end>Dashboard</NavLink>
          <NavLink to="/jobs">Saved jobs</NavLink>
          <NavLink to="/applications">Applications</NavLink>
        </nav>
        {data.mode === "demo" && (
          <button type="button" className="btn btn-small sidebar-reset" onClick={resetDemo}>Reset demo</button>
        )}
      </aside>
      <div className="content">
        {data.mode === "demo" && (
          <div className="demo-banner" role="note">
            <strong>Demo mode</strong> — fictional sample data stored only in your browser. No real backend, database, or personal data.
          </div>
        )}
        <main id="main" tabIndex={-1}>
          <Outlet />
        </main>
      </div>
    </div>
  );
}

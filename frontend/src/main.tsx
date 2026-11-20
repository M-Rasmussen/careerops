import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { liveAdapter } from "./api/live";
import { createDemoAdapter } from "./demo/adapter";
import "./styles.css";

// Fixed at build time: the demo build (`npm run build:demo`) drops the live adapter as dead code.
const adapter = import.meta.env.VITE_DEMO_MODE === "true" ? createDemoAdapter() : liveAdapter;

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App adapter={adapter} />
  </StrictMode>,
);

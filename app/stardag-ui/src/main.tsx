import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./index.css";
import App from "./App.tsx";
import { initAuthConfig } from "./auth/config";

// Resolve auth config (runtime fetch with build-time fallback) before
// rendering: the auth layer needs it synchronously from then on.
initAuthConfig().finally(() => {
  createRoot(document.getElementById("root")!).render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
});

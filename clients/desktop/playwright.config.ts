import os from "node:os";
import path from "node:path";
import { defineConfig, devices } from "@playwright/test";

// End-to-end: the real UI in a real browser against a real backend. The component
// tests (vitest + jsdom) cover behaviour; these cover what jsdom cannot see —
// layout, real fetch/CORS, and the screens wired to the actual API.

export const API_PORT = 8123;

// A fresh SQLite file per run, so nothing leaks between runs or into a dev database.
const db = path.join(os.tmpdir(), `osprey-e2e-${Date.now()}.db`).replace(/\\/g, "/");
const python =
  process.env.OSPREY_E2E_PYTHON ??
  (process.platform === "win32" ? "../../backend/.venv/Scripts/python.exe" : "../../backend/.venv/bin/python");

export default defineConfig({
  testDir: "e2e",
  timeout: 60_000,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: { baseURL: "http://localhost:1420", trace: "retain-on-failure" },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: [
    {
      command: `"${python}" -m uvicorn osprey.main:app --host 127.0.0.1 --port ${API_PORT} --app-dir ../../backend`,
      url: `http://127.0.0.1:${API_PORT}/health`,
      reuseExistingServer: false,
      timeout: 120_000,
      env: {
        OSPREY_DATABASE_URL: `sqlite+aiosqlite:///${db}`,
        OSPREY_CREATE_SCHEMA_ON_START: "true",
        // A throwaway signing key for this run only (the dev default is short).
        OSPREY_SECRET_KEY: `e2e-${Date.now()}-signing-key-for-this-run-only`,
      },
    },
    {
      command: "npx vite --port 1420 --strictPort",
      url: "http://localhost:1420",
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
    },
  ],
});

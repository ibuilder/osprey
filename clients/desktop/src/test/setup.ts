import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

// The Tauri bridge only exists inside the desktop shell; stub it so components can
// be rendered in jsdom.
vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn().mockResolvedValue(undefined) }));

// jsdom has no WebSocket implementation worth exercising here — the hotlist opens
// one on mount purely for live updates, which these tests drive directly instead.
class StubWebSocket {
  onmessage: ((ev: { data: string }) => void) | null = null;
  close() {}
}
vi.stubGlobal("WebSocket", StubWebSocket);

afterEach(cleanup);

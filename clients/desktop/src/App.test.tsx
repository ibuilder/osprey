import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { invoke } from "@tauri-apps/api/core";
import { describe, expect, it, vi } from "vitest";

import { HotlistView, ItemModal, Login } from "./App";
import type { Api, Hotlist, ItemDetail } from "./api";

// A hotlist shaped like the backend's snapshot payload, spanning all three buckets
// so the filters have something to bite on.
const HOTLIST: Hotlist = {
  project_id: "p1",
  generated_at: "2026-07-23T00:00:00+00:00",
  item_count: 3,
  total_exposure: 279000,
  buckets: {
    act_today: { count: 1, exposure: 180000 },
    this_week: { count: 1, exposure: 45000 },
    watch: { count: 1, exposure: 0 },
  },
  items: [
    {
      item_id: "i1",
      what: "NOTICE OF DELAY — differing site conditions",
      category: "contractual_notice",
      bucket: "act_today",
      bucket_label: "Act today",
      bucket_emoji: "🔴",
      why: "Act today: contractual notice deadline",
      owner: "PM",
      due: "2026-07-29",
      dollar_exposure: 180000,
      recommended_action: "Respond in writing before the notice lapses.",
      notice_deadline: true,
      score: 83,
      sources: [{ source_type: "outlook", title: "Notice", url: "https://x/1" }],
    },
    {
      item_id: "i2",
      what: "PCO-088 — slab thickening at loading dock",
      category: "change_order",
      bucket: "this_week",
      bucket_label: "This week",
      bucket_emoji: "🟠",
      why: "This week: $45,000 exposure",
      owner: null,
      due: null,
      dollar_exposure: 45000,
      recommended_action: "Price the change and issue the change order.",
      notice_deadline: false,
      score: 64,
      sources: [],
    },
    {
      item_id: "i3",
      what: "Two-week look-ahead — steel delivery slipping",
      category: "schedule",
      bucket: "watch",
      bucket_label: "Watch",
      bucket_emoji: "🟡",
      why: "Watch: may affect critical path",
      owner: null,
      due: null,
      dollar_exposure: null,
      recommended_action: "Assess critical-path impact.",
      notice_deadline: false,
      score: 30,
      sources: [],
    },
  ],
};

const ITEM_DETAIL: ItemDetail = {
  id: "i1",
  title: "NOTICE OF DELAY — differing site conditions",
  category: "contractual_notice",
  summary: "Formal notice of delay",
  status: "open",
  owner: "PM",
  score: 83,
  bucket: "act_today",
  explanation: "Act today: contractual notice deadline (highest weight)",
  factors: {
    urgency: 0.95,
    impact: 0.9,
    confidence: 0.8,
    weights: { urgency: 0.4, impact: 0.5, confidence: 0.1 },
    dollar_exposure: 180000,
    deadline: "2026-07-29",
    notice_deadline: true,
    recommended_action: "Respond in writing before the notice lapses.",
    citations: [{ signal_id: "s1", quote_span: "a written response is required within 7 days" }],
  },
  signals: [
    {
      id: "s1",
      source_type: "outlook",
      source_kind: "email",
      title: "NOTICE OF DELAY",
      url: "https://outlook/1",
      occurred_at: "2026-07-22T08:00:00Z",
    },
  ],
};

function stubApi(overrides: Partial<Record<keyof Api, unknown>> = {}) {
  return {
    hotlist: vi.fn().mockResolvedValue(HOTLIST),
    refresh: vi.fn().mockResolvedValue(HOTLIST),
    item: vi.fn().mockResolvedValue(ITEM_DETAIL),
    act: vi.fn().mockResolvedValue({}),
    openHotlistSocket: vi.fn().mockReturnValue({ close: vi.fn() }),
    exportUrl: vi.fn().mockReturnValue("http://x/export"),
    ...overrides,
  } as unknown as Api;
}

describe("HotlistView", () => {
  it("renders the bucket counts and every item", async () => {
    const { container } = render(<HotlistView api={stubApi()} projectId="p1" />);

    expect(await screen.findByText(/NOTICE OF DELAY/)).toBeInTheDocument();
    expect(screen.getByText(/PCO-088/)).toBeInTheDocument();
    expect(screen.getByText(/steel delivery slipping/)).toBeInTheDocument();
    // Header summarises the snapshot. The text is interpolated, so it spans
    // several text nodes — assert on the rendered output as a whole.
    expect(container.textContent).toContain("3 items");
    expect(container.textContent).toContain("279,000");
    // A contractual notice is called out explicitly — the domain rule that matters.
    expect(screen.getByText("NOTICE")).toBeInTheDocument();
  });

  it("filters to one bucket when its tile is clicked, and restores on a second click", async () => {
    const user = userEvent.setup();
    const { container } = render(<HotlistView api={stubApi()} projectId="p1" />);
    await screen.findByText(/NOTICE OF DELAY/);

    await user.click(screen.getByText("act today"));

    await waitFor(() => expect(screen.queryByText(/PCO-088/)).not.toBeInTheDocument());
    expect(screen.getByText(/NOTICE OF DELAY/)).toBeInTheDocument();
    expect(container.textContent).toContain("1 shown");

    await user.click(screen.getByText("act today"));
    await waitFor(() => expect(screen.getByText(/PCO-088/)).toBeInTheDocument());
  });

  it("narrows the list as you type in the search box", async () => {
    const user = userEvent.setup();
    render(<HotlistView api={stubApi()} projectId="p1" />);
    await screen.findByText(/NOTICE OF DELAY/);

    await user.type(screen.getByPlaceholderText(/Search the hotlist/), "slab");

    await waitFor(() => expect(screen.queryByText(/NOTICE OF DELAY/)).not.toBeInTheDocument());
    expect(screen.getByText(/PCO-088/)).toBeInTheDocument();
  });

  it("clears filters with the Clear button", async () => {
    const user = userEvent.setup();
    render(<HotlistView api={stubApi()} projectId="p1" />);
    await screen.findByText(/NOTICE OF DELAY/);

    await user.type(screen.getByPlaceholderText(/Search the hotlist/), "zzz-no-match");
    expect(await screen.findByText(/No items match your filters/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Clear" }));
    await waitFor(() => expect(screen.getByText(/NOTICE OF DELAY/)).toBeInTheDocument());
  });

  it("shows the onboarding empty state when nothing is on the hotlist", async () => {
    const empty = { ...HOTLIST, item_count: 0, items: [] };
    render(<HotlistView api={stubApi({ hotlist: vi.fn().mockResolvedValue(empty) })} projectId="p1" />);

    expect(await screen.findByText(/Nothing on the hotlist yet/)).toBeInTheDocument();
  });

  it("opens the detail modal when a row is clicked", async () => {
    const user = userEvent.setup();
    const api = stubApi();
    render(<HotlistView api={api} projectId="p1" />);

    await user.click(await screen.findByText(/NOTICE OF DELAY/));

    expect(await screen.findByText(/Why it ranked here/i)).toBeInTheDocument();
    expect(api.item).toHaveBeenCalledWith("i1");
  });
});

describe("ItemModal", () => {
  it("shows the factor breakdown, the citation and the sources", async () => {
    const { container } = render(<ItemModal api={stubApi()} itemId="i1" onClose={vi.fn()} onAct={vi.fn()} />);

    expect(await screen.findByText(/Why it ranked here/i)).toBeInTheDocument();
    // Explainability is the product promise: factors and a cited quote must render.
    expect(screen.getByText("Urgency")).toBeInTheDocument();
    expect(screen.getByText("Impact")).toBeInTheDocument();
    expect(screen.getByText("Confidence")).toBeInTheDocument();
    expect(screen.getByText(/0\.95/)).toBeInTheDocument();
    expect(screen.getByText(/written response is required within 7 days/)).toBeInTheDocument();
    expect(container.textContent).toContain("Sources (1)");
    expect(screen.getByText("NOTICE DEADLINE")).toBeInTheDocument();
  });

  it("reports the chosen action back to the caller", async () => {
    const user = userEvent.setup();
    const onAct = vi.fn();
    render(<ItemModal api={stubApi()} itemId="i1" onClose={vi.fn()} onAct={onAct} />);
    await screen.findByText(/Why it ranked here/i);

    await user.click(screen.getByRole("button", { name: "Mark done" }));

    expect(onAct).toHaveBeenCalledWith("i1", "done");
  });

  it("closes when the dismiss button is used", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    render(<ItemModal api={stubApi()} itemId="i1" onClose={onClose} onAct={vi.fn()} />);
    await screen.findByText(/Why it ranked here/i);

    await user.click(screen.getByRole("button", { name: "✕" }));

    expect(onClose).toHaveBeenCalled();
  });
});

describe("Login", () => {
  it("toggles between signing in and creating an account", async () => {
    const user = userEvent.setup();
    render(<Login onLogin={vi.fn()} />);

    expect(screen.getByRole("button", { name: "Sign in" })).toBeInTheDocument();
    expect(screen.queryByPlaceholderText("Organization name")).not.toBeInTheDocument();

    await user.click(screen.getByText("Create an account"));

    expect(screen.getByRole("button", { name: "Create account" })).toBeInTheDocument();
    expect(screen.getByPlaceholderText("Organization name")).toBeInTheDocument();
  });

  it("surfaces a failed sign-in instead of failing silently", async () => {
    const user = userEvent.setup();
    const { Api } = await import("./api");
    vi.spyOn(Api, "login").mockRejectedValueOnce(new Error("login failed: 401"));

    render(<Login onLogin={vi.fn()} />);
    await user.type(screen.getByPlaceholderText("Email"), "a@b.com");
    await user.type(screen.getByPlaceholderText("Password"), "wrong-password");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByText(/login failed: 401/)).toBeInTheDocument();
  });
});

describe("Login — bundled backend handshake", () => {
  it("adopts the sidecar URL and hides the Backend URL field when ready", async () => {
    vi.mocked(invoke).mockResolvedValueOnce({ status: "ready", url: "http://127.0.0.1:51234" });

    render(<Login onLogin={vi.fn()} />);

    expect(await screen.findByText(/Running your own private copy/)).toBeInTheDocument();
    expect(screen.queryByPlaceholderText("Backend URL")).not.toBeInTheDocument();
  });

  it("reveals the Backend URL field at once when no sidecar is bundled", async () => {
    // The shell says "unavailable" immediately, so the user must not be made to sit
    // through the start-up timeout before they can type a URL.
    vi.mocked(invoke).mockResolvedValueOnce({ status: "unavailable", url: null });

    render(<Login onLogin={vi.fn()} />);

    expect(await screen.findByPlaceholderText("Backend URL")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign in" })).toBeEnabled();
  });

  it("shows progress while the backend is still starting", async () => {
    vi.mocked(invoke).mockResolvedValue({ status: "starting", url: null });
    try {
      render(<Login onLogin={vi.fn()} />);

      expect(await screen.findByText(/Starting your private copy/)).toBeInTheDocument();
      // Submitting against a placeholder URL would just fail confusingly.
      expect(screen.getByRole("button", { name: "Starting…" })).toBeDisabled();
    } finally {
      vi.mocked(invoke).mockReset();
      vi.mocked(invoke).mockResolvedValue(undefined);
    }
  });
});

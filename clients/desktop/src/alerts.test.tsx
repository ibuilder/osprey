import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { invoke } from "@tauri-apps/api/core";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { DesktopSection } from "./Admin";
import { HotlistView } from "./App";
import { criticalAlerts, loadAlerted, MAX_INDIVIDUAL_ALERTS, saveAlerted } from "./alerts";
import type { Api, Hotlist, HotlistItem } from "./api";

function item(id: string, bucket: string, extra: Partial<HotlistItem> = {}): HotlistItem {
  return {
    item_id: id,
    what: `Item ${id}`,
    category: "general",
    bucket,
    bucket_label: bucket,
    bucket_emoji: "",
    why: `why ${id}`,
    owner: null,
    due: null,
    dollar_exposure: null,
    recommended_action: "",
    notice_deadline: false,
    score: 50,
    sources: [],
    ...extra,
  };
}

function hotlist(items: HotlistItem[]): Hotlist {
  return {
    project_id: "p1",
    generated_at: "2026-09-14T00:00:00Z",
    item_count: items.length,
    total_exposure: 0,
    buckets: {},
    items,
  };
}

beforeEach(() => {
  localStorage.clear();
  vi.mocked(invoke).mockReset();
  vi.mocked(invoke).mockResolvedValue(undefined);
});

describe("criticalAlerts", () => {
  it("records what is already there the first time, without alerting", () => {
    const { alerts, alerted } = criticalAlerts(hotlist([item("a", "act_today")]), null);

    expect(alerts).toEqual([]);
    expect([...alerted]).toEqual(["a"]);
  });

  it("alerts once on a newly critical item, and never again for it", () => {
    const first = criticalAlerts(hotlist([item("a", "act_today", { due: "2026-09-20" })]), new Set());
    expect(first.alerts).toHaveLength(1);
    expect(first.alerts[0].title).toBe("Act today: Item a");
    expect(first.alerts[0].body).toContain("Due 2026-09-20");

    const again = criticalAlerts(hotlist([item("a", "act_today")]), first.alerted);
    expect(again.alerts).toEqual([]);
  });

  it("ignores items that are not act-today", () => {
    const { alerts, alerted } = criticalAlerts(
      hotlist([item("w", "this_week"), item("x", "watch")]),
      new Set(),
    );
    expect(alerts).toEqual([]);
    expect(alerted.size).toBe(0);
  });

  it("leads with notice deadlines, which can waive a claim", () => {
    const { alerts } = criticalAlerts(
      hotlist([item("n", "act_today", { notice_deadline: true, what: "Notice of delay" })]),
      new Set(),
    );
    expect(alerts[0].title).toBe("Notice deadline: Notice of delay");
  });

  it("rolls a burst into a summary instead of a wall of notifications", () => {
    const burst = Array.from({ length: MAX_INDIVIDUAL_ALERTS + 2 }, (_, i) => item(`b${i}`, "act_today"));
    const { alerts } = criticalAlerts(hotlist(burst), new Set());

    expect(alerts).toHaveLength(MAX_INDIVIDUAL_ALERTS + 1);
    expect(alerts[alerts.length - 1].title).toBe("2 more items need you today");
  });

  it("remembers alerted items across restarts", () => {
    saveAlerted("p1", new Set(["a", "b"]));
    expect(loadAlerted("p1")).toEqual(new Set(["a", "b"]));
    expect(loadAlerted("never-seen")).toBeNull();
  });
});

describe("HotlistView alerts", () => {
  it("raises a notification when the live hotlist gains a critical item", async () => {
    let push: ((h: Hotlist) => void) | undefined;
    const api = {
      hotlist: vi.fn().mockResolvedValue(hotlist([item("old", "act_today")])),
      refresh: vi.fn(),
      item: vi.fn(),
      act: vi.fn(),
      exportUrl: vi.fn(),
      openHotlistSocket: vi.fn((_: string, onUpdate: (h: Hotlist) => void) => {
        push = onUpdate;
        return { close: vi.fn() };
      }),
    } as unknown as Api;

    render(<HotlistView api={api} projectId="p1" />);
    await screen.findByText("Item old");
    // The first load only records what exists.
    expect(invoke).not.toHaveBeenCalledWith("notify_critical", expect.anything());

    push?.(hotlist([item("old", "act_today"), item("new", "act_today", { what: "Slab pour on hold" })]));

    await waitFor(() =>
      expect(invoke).toHaveBeenCalledWith("notify_critical", {
        title: "Act today: Slab pour on hold",
        body: "why new",
      }),
    );
    expect(vi.mocked(invoke).mock.calls.filter(([cmd]) => cmd === "notify_critical")).toHaveLength(1);
  });
});

describe("DesktopSection", () => {
  it("toggles start-at-login and shows what the OS reports", async () => {
    const user = userEvent.setup();
    vi.mocked(invoke).mockImplementation(async (cmd: string) => {
      if (cmd === "autostart_enabled") return false;
      if (cmd === "set_autostart") return true;
      return undefined;
    });

    render(<DesktopSection />);
    const box = await screen.findByRole("checkbox", { name: /Start Osprey in the tray/ });
    expect(box).not.toBeChecked();

    await user.click(box);

    expect(invoke).toHaveBeenCalledWith("set_autostart", { enabled: true });
    await waitFor(() => expect(box).toBeChecked());
  });

  it("is hidden outside the desktop shell", async () => {
    vi.mocked(invoke).mockRejectedValue(new Error("not in Tauri"));

    const { container } = render(<DesktopSection />);

    await waitFor(() => expect(invoke).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });
});

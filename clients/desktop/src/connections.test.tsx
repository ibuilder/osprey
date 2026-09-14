import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { invoke } from "@tauri-apps/api/core";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ConnectionsView } from "./App";
import type { Api } from "./api";

const REASON =
  "Lets Osprey register and renew ACC issue webhooks, so new and updated issues arrive within seconds.";

function stubApi() {
  return {
    sources: vi.fn().mockResolvedValue([
      { source_type: "outlook", auth: "oauth", configured: true, scopes: ["Mail.Read"], optional_scopes: {} },
      {
        source_type: "acc",
        auth: "oauth",
        configured: true,
        scopes: ["data:read"],
        optional_scopes: { "data:write": REASON },
      },
    ]),
    connections: vi.fn().mockResolvedValue([]),
  } as unknown as Api;
}

beforeEach(() => {
  vi.mocked(invoke).mockReset();
  vi.mocked(invoke).mockResolvedValue(undefined);
});

describe("ConnectionsView optional scopes", () => {
  it("shows an optional scope with its reason, unticked", async () => {
    render(<ConnectionsView api={stubApi()} projectId="p1" />);

    const box = await screen.findByRole("checkbox", { name: /Also grant data:write/ });
    expect(box).not.toBeChecked();
    expect(screen.getByText(new RegExp(REASON.slice(0, 40)))).toBeInTheDocument();
    // Sources with nothing optional offer nothing to tick.
    expect(screen.getAllByRole("checkbox")).toHaveLength(1);
  });

  it("connects read-only unless the admin opts in", async () => {
    const user = userEvent.setup();
    render(<ConnectionsView api={stubApi()} projectId="p1" />);
    await screen.findByRole("checkbox", { name: /Also grant data:write/ });

    const [, accConnect] = screen.getAllByRole("button", { name: "Connect" });
    await user.click(accConnect);

    await waitFor(() =>
      expect(invoke).toHaveBeenCalledWith("oauth_connect", {
        sourceType: "acc",
        projectId: "p1",
        optionalScopes: [],
      }),
    );
  });

  it("passes the opted-in scope through to the connect flow", async () => {
    const user = userEvent.setup();
    render(<ConnectionsView api={stubApi()} projectId="p1" />);

    await user.click(await screen.findByRole("checkbox", { name: /Also grant data:write/ }));
    const [, accConnect] = screen.getAllByRole("button", { name: "Connect" });
    await user.click(accConnect);

    await waitFor(() =>
      expect(invoke).toHaveBeenCalledWith("oauth_connect", {
        sourceType: "acc",
        projectId: "p1",
        optionalScopes: ["data:write"],
      }),
    );
  });
});

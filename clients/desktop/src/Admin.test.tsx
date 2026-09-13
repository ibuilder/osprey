import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import {
  describeAgent,
  InvitesSection,
  MembersSection,
  PasswordSection,
  RetentionSection,
  ScimSection,
  SessionsSection,
  when,
} from "./Admin";
import { tabsFor } from "./App";
import { canGrant, type Api, type Member } from "./api";

const MEMBERS: Member[] = [
  {
    user_id: "u-owner",
    email: "owner@example.com",
    full_name: "Olive Owner",
    role: "owner",
    is_active: true,
    scim_managed: false,
    last_login_at: "2026-09-01T12:00:00Z",
  },
  {
    user_id: "u-pm",
    email: "pm@example.com",
    full_name: "",
    role: "pm",
    is_active: true,
    scim_managed: false,
    last_login_at: null,
  },
  {
    user_id: "u-scim",
    email: "idp@example.com",
    full_name: "",
    role: "viewer",
    is_active: true,
    scim_managed: true,
    last_login_at: null,
  },
];

function stubApi(overrides: Partial<Record<keyof Api, unknown>> = {}) {
  return {
    members: vi.fn().mockResolvedValue(MEMBERS),
    setMemberRole: vi.fn().mockResolvedValue({}),
    deactivateMember: vi.fn().mockResolvedValue(undefined),
    reactivateMember: vi.fn().mockResolvedValue(undefined),
    removeMember: vi.fn().mockResolvedValue(undefined),
    invites: vi.fn().mockResolvedValue([]),
    createInvite: vi.fn(),
    revokeInvite: vi.fn().mockResolvedValue(undefined),
    scimTokens: vi.fn().mockResolvedValue([]),
    createScimToken: vi.fn(),
    revokeScimToken: vi.fn().mockResolvedValue(undefined),
    retention: vi.fn().mockResolvedValue({
      signal_days: null,
      item_days: 30,
      effective_signal_days: 365,
      effective_item_days: 30,
    }),
    setRetention: vi.fn().mockResolvedValue({}),
    retentionPreview: vi.fn().mockResolvedValue({
      signals: 4,
      items: 2,
      scores: 2,
      snapshots: 0,
      cutoff_signal: null,
      cutoff_item: null,
    }),
    runRetention: vi.fn().mockResolvedValue({ signals: 4, items: 2, scores: 2 }),
    sessions: vi.fn().mockResolvedValue([]),
    revokeSession: vi.fn().mockResolvedValue(undefined),
    logoutAll: vi.fn().mockResolvedValue(undefined),
    changePassword: vi.fn().mockResolvedValue(undefined),
    ...overrides,
  } as unknown as Api;
}

describe("role rules", () => {
  it("never lets a role grant above itself", () => {
    expect(canGrant("owner", "owner")).toBe(true);
    expect(canGrant("admin", "admin")).toBe(true);
    expect(canGrant("admin", "owner")).toBe(false);
    expect(canGrant("pm", "admin")).toBe(false);
    expect(canGrant("nonsense", "viewer")).toBe(false);
  });

  it("shows the admin tab only to admins and owners, and account to everyone", () => {
    expect(tabsFor("owner")).toContain("admin");
    expect(tabsFor("admin")).toContain("admin");
    expect(tabsFor("pm")).not.toContain("admin");
    expect(tabsFor("viewer")).not.toContain("admin");
    expect(tabsFor("viewer")).toContain("account");
  });
});

describe("display helpers", () => {
  it("reads an offset-less server timestamp as UTC, not local time", () => {
    // SQLite returns naive timestamps. Found by running the screens against a
    // real backend: expiries showed hours off on a machine not set to UTC.
    expect(when("2026-09-20T17:33:54")).toBe(when("2026-09-20T17:33:54Z"));
    expect(when("2026-09-20T17:33:54+00:00")).toBe(when("2026-09-20T17:33:54Z"));
    expect(when(null)).toBe("never");
  });

  it("names the browser and OS instead of printing the user agent", () => {
    expect(
      describeAgent(
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0 Safari/537.36",
      ),
    ).toBe("Chrome on Windows");
    expect(
      describeAgent("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 Version/18.0 Safari/605.1.15"),
    ).toBe("Safari on macOS");
    expect(describeAgent("Mozilla/5.0 (Windows NT 10.0) Chrome/152.0 Safari/537.36 Edg/152.0")).toBe(
      "Edge on Windows",
    );
    expect(describeAgent("python-httpx/0.28")).toBe("python-httpx");
    expect(describeAgent("")).toBe("Unknown device");
  });
});

describe("MembersSection", () => {
  it("lists members and changes a role", async () => {
    const user = userEvent.setup();
    const api = stubApi();
    render(<MembersSection api={api} role="owner" userId="u-owner" />);

    const select = await screen.findByLabelText("Role for pm@example.com");
    await user.selectOptions(select, "admin");

    expect(api.setMemberRole).toHaveBeenCalledWith("u-pm", "admin");
  });

  it("locks identity-provider users, whose role belongs to the IdP", async () => {
    render(<MembersSection api={stubApi()} role="owner" userId="u-owner" />);

    expect(await screen.findByLabelText("Role for idp@example.com")).toBeDisabled();
    expect(screen.getByText("SCIM")).toBeInTheDocument();
  });

  it("does not let an admin touch an owner, or offer owner as a choice", async () => {
    render(<MembersSection api={stubApi()} role="admin" userId="u-admin" />);

    expect(await screen.findByLabelText("Role for owner@example.com")).toBeDisabled();
    const pmSelect = screen.getByLabelText("Role for pm@example.com");
    const options = within(pmSelect).getAllByRole("option").map((o) => o.textContent);
    expect(options).not.toContain("owner");
  });

  it("asks for a second click before removing someone", async () => {
    const user = userEvent.setup();
    const api = stubApi();
    render(<MembersSection api={api} role="owner" userId="u-owner" />);
    await screen.findByLabelText("Role for pm@example.com");

    // The owner's own row offers no Remove, so the first is the pm's.
    await user.click(screen.getAllByRole("button", { name: "Remove" })[0]);
    expect(api.removeMember).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Remove pm@example.com" }));
    expect(api.removeMember).toHaveBeenCalledWith("u-pm");
  });

  it("shows the server's reason when it refuses", async () => {
    const user = userEvent.setup();
    const api = stubApi({
      setMemberRole: vi
        .fn()
        .mockRejectedValue(new Error("this is the organization's last active owner; promote someone else first")),
    });
    render(<MembersSection api={api} role="owner" userId="u-owner" />);

    await user.selectOptions(await screen.findByLabelText("Role for owner@example.com"), "pm");

    expect(await screen.findByText(/last active owner/)).toBeInTheDocument();
  });
});

describe("InvitesSection", () => {
  it("shows the invite code once, after creating it", async () => {
    const user = userEvent.setup();
    const api = stubApi({
      createInvite: vi.fn().mockResolvedValue({
        id: "inv1",
        email: "new@example.com",
        role: "pm",
        invited_by: "owner@example.com",
        expires_at: "2026-09-20T00:00:00Z",
        accepted: false,
        token: "secret-invite-code",
      }),
    });
    render(<InvitesSection api={api} role="owner" />);

    await user.type(screen.getByPlaceholderText("name@company.com"), "new@example.com");
    await user.selectOptions(screen.getByLabelText("Role for the invite"), "pm");
    await user.click(screen.getByRole("button", { name: "Create invite" }));

    expect(api.createInvite).toHaveBeenCalledWith("new@example.com", "pm");
    expect(await screen.findByText("secret-invite-code")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Done" }));
    expect(screen.queryByText("secret-invite-code")).not.toBeInTheDocument();
  });

  it("renders nothing for a role that cannot invite", () => {
    const api = stubApi();
    const { container } = render(<InvitesSection api={api} role="pm" />);
    expect(container).toBeEmptyDOMElement();
    expect(api.invites).not.toHaveBeenCalled();
  });
});

describe("ScimSection", () => {
  it("never offers an owner ceiling", async () => {
    render(<ScimSection api={stubApi()} />);
    const options = within(screen.getByLabelText("Highest role this token may assign"))
      .getAllByRole("option")
      .map((o) => o.textContent);
    expect(options).toEqual(["up to admin", "up to pm", "up to viewer"]);
  });

  it("creates a token and reveals it once", async () => {
    const user = userEvent.setup();
    const api = stubApi({
      createScimToken: vi.fn().mockResolvedValue({
        id: "t1",
        name: "Okta",
        max_role: "pm",
        created_at: "",
        last_used_at: null,
        revoked: false,
        token: "osp_scim_abc",
      }),
    });
    render(<ScimSection api={api} />);

    await user.type(screen.getByPlaceholderText(/Label/), "Okta");
    await user.click(screen.getByRole("button", { name: "Create token" }));

    expect(api.createScimToken).toHaveBeenCalledWith("Okta", "pm");
    expect(await screen.findByText("osp_scim_abc")).toBeInTheDocument();
  });
});

describe("RetentionSection", () => {
  it("shows what is in effect and what a run would delete", async () => {
    const { container } = render(<RetentionSection api={stubApi()} role="owner" />);

    await waitFor(() => expect(container.textContent).toContain("signals 365 days"));
    expect(container.textContent).toContain("delete 4 signal(s), 2 item(s)");
  });

  it("saves blank as inherit and a number as days", async () => {
    const user = userEvent.setup();
    const api = stubApi();
    render(<RetentionSection api={api} role="owner" />);
    const items = await screen.findByDisplayValue("30");

    await user.clear(items);
    await user.type(items, "90");
    await user.click(screen.getByRole("button", { name: "Save policy" }));

    expect(api.setRetention).toHaveBeenCalledWith(null, 90);
  });

  it("refuses a value the server would reject, before sending it", async () => {
    const user = userEvent.setup();
    render(<RetentionSection api={stubApi()} role="owner" />);
    const items = await screen.findByDisplayValue("30");

    await user.clear(items);
    await user.type(items, "-5");

    expect(screen.getByText(/whole number from 0 to 3650/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save policy" })).toBeDisabled();
  });

  it("confirms before purging", async () => {
    const user = userEvent.setup();
    const api = stubApi();
    render(<RetentionSection api={api} role="owner" />);

    await user.click(await screen.findByRole("button", { name: "Run now" }));
    expect(api.runRetention).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Delete 8 record(s) now" }));

    expect(api.runRetention).toHaveBeenCalled();
    expect(await screen.findByText(/removed 8 record/)).toBeInTheDocument();
  });

  it("is read-only for an admin", async () => {
    render(<RetentionSection api={stubApi()} role="admin" />);

    expect(await screen.findByDisplayValue("30")).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Save policy" })).not.toBeInTheDocument();
    expect(screen.getByText(/Only an owner/)).toBeInTheDocument();
  });
});

describe("Account", () => {
  it("revokes one session", async () => {
    const user = userEvent.setup();
    const api = stubApi({
      sessions: vi.fn().mockResolvedValue([
        {
          id: "s1",
          created_at: "",
          expires_at: "",
          // The desktop shell on Windows is WebView2, which identifies as Edge.
          user_agent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/152.0 Safari/537.36 Edg/152.0",
          ip: "10.0.0.2",
        },
      ]),
    });
    render(<SessionsSection api={api} onSignedOut={vi.fn()} />);

    await screen.findByText("Edge on Windows");
    await user.click(screen.getByRole("button", { name: "Sign out" }));

    expect(api.revokeSession).toHaveBeenCalledWith("s1");
  });

  it("signs out locally after signing out everywhere", async () => {
    const user = userEvent.setup();
    const api = stubApi();
    const onSignedOut = vi.fn();
    render(<SessionsSection api={api} onSignedOut={onSignedOut} />);

    await user.click(screen.getByRole("button", { name: "Sign out everywhere" }));
    await user.click(screen.getByRole("button", { name: "Sign out of every device" }));

    await waitFor(() => expect(onSignedOut).toHaveBeenCalled());
    expect(api.logoutAll).toHaveBeenCalled();
  });

  it("changes the password and returns to sign-in, since every session is revoked", async () => {
    const user = userEvent.setup();
    const api = stubApi();
    const onSignedOut = vi.fn();
    render(<PasswordSection api={api} onSignedOut={onSignedOut} />);

    await user.type(screen.getByPlaceholderText("Current password"), "Old-Passw0rd!");
    await user.type(screen.getByPlaceholderText("New password"), "N3w-Passw0rd!long");
    await user.type(screen.getByPlaceholderText("Confirm new password"), "N3w-Passw0rd!long");
    await user.click(screen.getByRole("button", { name: "Change password" }));

    expect(api.changePassword).toHaveBeenCalledWith("Old-Passw0rd!", "N3w-Passw0rd!long");
    await waitFor(() => expect(onSignedOut).toHaveBeenCalled());
  });

  it("keeps the user signed in and explains when the change is refused", async () => {
    const user = userEvent.setup();
    const onSignedOut = vi.fn();
    const api = stubApi({
      changePassword: vi.fn().mockRejectedValue(new Error("current password is incorrect")),
    });
    render(<PasswordSection api={api} onSignedOut={onSignedOut} />);

    await user.type(screen.getByPlaceholderText("Current password"), "wrong");
    await user.type(screen.getByPlaceholderText("New password"), "N3w-Passw0rd!long");
    await user.type(screen.getByPlaceholderText("Confirm new password"), "N3w-Passw0rd!long");
    await user.click(screen.getByRole("button", { name: "Change password" }));

    expect(await screen.findByText("current password is incorrect")).toBeInTheDocument();
    expect(onSignedOut).not.toHaveBeenCalled();
  });

  it("blocks a mismatched confirmation", async () => {
    const user = userEvent.setup();
    render(<PasswordSection api={stubApi()} onSignedOut={vi.fn()} />);

    await user.type(screen.getByPlaceholderText("Current password"), "x");
    await user.type(screen.getByPlaceholderText("New password"), "one");
    await user.type(screen.getByPlaceholderText("Confirm new password"), "two");

    expect(screen.getByText(/don't match/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Change password" })).toBeDisabled();
  });
});

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { HealthSection } from "./Admin";
import type { Api, ConnectionHealth } from "./api";

const CONNECTIONS: ConnectionHealth[] = [
  {
    id: "c1",
    source_type: "outlook",
    account_ref: "pm@gc.com",
    status: "active",
    last_sync: "2026-09-14T08:00:00Z",
    last_error: null,
    project_id: "p1",
  },
  {
    id: "c2",
    source_type: "procore",
    account_ref: "company-42",
    status: "error",
    last_sync: null,
    last_error: "401 Unauthorized: token expired",
    project_id: "p1",
  },
];

function stubApi(overrides: Partial<Record<keyof Api, unknown>> = {}) {
  return {
    connectionsHealth: vi.fn().mockResolvedValue(CONNECTIONS),
    auditVerify: vi.fn().mockResolvedValue({ org_id: "o1", audit_chain_intact: true }),
    tenantIsolation: vi.fn().mockResolvedValue({
      enabled: true,
      enforced: true,
      detail: "row-level security is enforced for this connection",
    }),
    orgStats: vi.fn().mockResolvedValue({
      projects: 2,
      connections: 2,
      ai_connections: 0,
      scripts: 1,
      items: 14,
      signals: 90,
    }),
    ...overrides,
  } as unknown as Api;
}

describe("HealthSection", () => {
  it("puts broken connections first, with their error", async () => {
    render(<HealthSection api={stubApi()} />);

    expect(await screen.findByText("401 Unauthorized: token expired")).toBeInTheDocument();
    const rows = screen.getAllByRole("row");
    expect(within(rows[0]).getByText("procore")).toBeInTheDocument(); // error sorts first
    expect(within(rows[0]).getByText("never synced")).toBeInTheDocument();
    expect(within(rows[1]).getByText("outlook")).toBeInTheDocument();
    expect(screen.getByText("1 need attention")).toBeInTheDocument();
  });

  it("reports an intact audit log, enforced isolation and org counts", async () => {
    const { container } = render(<HealthSection api={stubApi()} />);

    expect(await screen.findByText("Audit log chain is intact.")).toBeInTheDocument();
    expect(await screen.findByText(/Tenant isolation is enforced/)).toBeInTheDocument();
    await waitFor(() => expect(container.textContent).toContain("90 signal(s)"));
  });

  it("says plainly when the audit chain is broken", async () => {
    render(
      <HealthSection
        api={stubApi({
          auditVerify: vi.fn().mockResolvedValue({ org_id: "o1", audit_chain_intact: false }),
        })}
      />,
    );

    expect(await screen.findByText(/Audit log chain is broken/)).toBeInTheDocument();
  });

  it("warns when isolation is configured but bypassed", async () => {
    render(
      <HealthSection
        api={stubApi({
          tenantIsolation: vi.fn().mockResolvedValue({
            enabled: true,
            enforced: false,
            detail: "the database role can bypass RLS",
          }),
        })}
      />,
    );

    expect(await screen.findByText(/is not enforced: the database role can bypass RLS/)).toBeInTheDocument();
  });

  it("keeps the other checks visible when one endpoint fails", async () => {
    render(
      <HealthSection
        api={stubApi({ auditVerify: vi.fn().mockRejectedValue(new Error("403 Forbidden")) })}
      />,
    );

    expect(await screen.findByText("Audit log check failed: 403 Forbidden")).toBeInTheDocument();
    expect(await screen.findByText("401 Unauthorized: token expired")).toBeInTheDocument();
    expect(screen.getByText(/Tenant isolation is enforced/)).toBeInTheDocument();
  });

  it("checks again on demand", async () => {
    const user = userEvent.setup();
    const api = stubApi();
    render(<HealthSection api={api} />);
    await screen.findByText("Audit log chain is intact.");

    await user.click(screen.getByRole("button", { name: "Check again" }));

    await waitFor(() => expect(api.connectionsHealth).toHaveBeenCalledTimes(2));
    expect(api.auditVerify).toHaveBeenCalledTimes(2);
  });
});

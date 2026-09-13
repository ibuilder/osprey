import { afterEach, describe, expect, it, vi } from "vitest";

import { Api, describeError, type Session } from "./api";

const BASE = "http://localhost:8000";

function session(overrides: Partial<Session> = {}): Session {
  return {
    baseUrl: BASE,
    token: "access-1",
    refreshToken: "refresh-1",
    role: "owner",
    orgId: "o1",
    userId: "u1",
    ...overrides,
  };
}

function json(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", ...headers },
  });
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("describeError", () => {
  it("shows the server's explanation, not just the status code", async () => {
    const message = await describeError(
      json({ detail: "password must be at least 12 characters" }, 422),
      "Could not create the account",
    );
    expect(message).toBe("password must be at least 12 characters");
  });

  it("flattens a validation error into something a person can act on", async () => {
    const message = await describeError(
      json({ detail: [{ loc: ["body", "email"], msg: "not a valid email address" }] }, 422),
      "failed",
    );
    expect(message).toBe("email: not a valid email address");
  });

  it("tells the user how long to wait when rate limited", async () => {
    const message = await describeError(
      new Response("", { status: 429, headers: { "Retry-After": "900" } }),
      "failed",
    );
    expect(message).toContain("900 seconds");
  });

  it("falls back to the status when the body is not JSON", async () => {
    const message = await describeError(new Response("<html>502</html>", { status: 502 }), "failed");
    expect(message).toBe("failed: 502");
  });
});

describe("Api — session refresh", () => {
  it("keeps a session alive across an expired access token", async () => {
    const onSession = vi.fn();
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(json({ detail: "invalid or expired token" }, 401)) // first try
      .mockResolvedValueOnce(json({ access_token: "access-2", refresh_token: "refresh-2", role: "owner" })) // refresh
      .mockResolvedValueOnce(json([{ id: "p1", name: "Tower B" }])); // retry

    const api = new Api(session(), onSession);
    await expect(api.projects()).resolves.toEqual([{ id: "p1", name: "Tower B" }]);

    expect(fetchMock.mock.calls[1][0]).toBe(`${BASE}/auth/refresh`);
    // The rotated token must be handed back, or the next refresh replays the old
    // one -- which the server treats as theft and kills the whole family.
    expect(onSession).toHaveBeenCalledWith(
      expect.objectContaining({ token: "access-2", refreshToken: "refresh-2" }),
    );
  });

  it("ends the session when the refresh token is no longer valid", async () => {
    const onSession = vi.fn();
    vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(json({ detail: "invalid or expired token" }, 401))
      .mockResolvedValueOnce(json({ detail: "refresh token already used" }, 401));

    const api = new Api(session(), onSession);
    await expect(api.projects()).rejects.toThrow();
    expect(onSession).toHaveBeenCalledWith(null);
  });

  it("shares one exchange between concurrent 401s", async () => {
    // Two requests racing must not each POST the same single-use refresh token.
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url.endsWith("/auth/refresh")) {
        return json({ access_token: "access-2", refresh_token: "refresh-2", role: "owner" });
      }
      return fetchMock.mock.calls.length <= 2 ? json({ detail: "expired" }, 401) : json([]);
    });

    const api = new Api(session(), vi.fn());
    await Promise.all([api.projects(), api.projects()]);

    const refreshCalls = fetchMock.mock.calls.filter((c) => String(c[0]).endsWith("/auth/refresh"));
    expect(refreshCalls).toHaveLength(1);
  });

  it("does not attempt a refresh when there is no refresh token", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(json({ detail: "missing bearer token" }, 401));

    const api = new Api(session({ refreshToken: null }), vi.fn());
    await expect(api.projects()).rejects.toThrow("missing bearer token");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe("Api — sign in and out", () => {
  it("carries the refresh token out of a successful sign-in", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      json({
        access_token: "a",
        refresh_token: "r",
        role: "owner",
        org_id: "o1",
        user_id: "u1",
      }),
    );
    const s = await Api.login(BASE, "a@b.com", "Sup3rSecret!pass");
    expect(s.refreshToken).toBe("r");
  });

  it("reports why a sign-in was refused", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(json({ detail: "invalid credentials" }, 401));
    await expect(Api.login(BASE, "a@b.com", "nope")).rejects.toThrow("invalid credentials");
  });

  it("revokes the session server-side on sign out", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(null, { status: 204 }));
    await new Api(session(), vi.fn()).logout();
    expect(fetchMock).toHaveBeenCalledWith(
      `${BASE}/auth/logout`,
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("signs out locally even when the server is unreachable", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new Error("network down"));
    await expect(new Api(session(), vi.fn()).logout()).resolves.toBeUndefined();
  });

  it("reports SSO as unavailable rather than throwing when the server has none", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(new Response("", { status: 404 }));
    await expect(Api.ssoConfig(BASE)).resolves.toEqual({ enabled: false, issuer: "" });
  });
});

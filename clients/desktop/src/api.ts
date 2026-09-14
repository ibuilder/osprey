// Typed Osprey API client used by the desktop UI.

export const DEFAULT_BASE = "http://localhost:8000";

export interface Session {
  baseUrl: string;
  token: string;
  /** Opaque, single-use. Exchanged for a new pair when the access token expires. */
  refreshToken: string | null;
  role: string;
  orgId: string;
  userId: string;
}

/** The server's error body: {detail} is a string, or a list for validation errors. */
interface ErrorBody {
  detail?: string | { loc?: (string | number)[]; msg?: string }[];
  request_id?: string;
}

/**
 * Turn a failed response into a message worth showing.
 *
 * The API explains *why* it refused -- which password rule was broken, that an
 * account is locked, how long to wait -- and surfacing only the status code
 * ("register failed: 422") throws that away and leaves the user guessing.
 */
export async function describeError(res: Response, fallback: string): Promise<string> {
  let body: ErrorBody | null = null;
  try {
    body = (await res.json()) as ErrorBody;
  } catch {
    // Not JSON (a proxy error page, say); fall through to the status text.
  }
  const detail = body?.detail;
  if (typeof detail === "string" && detail) return detail;
  if (Array.isArray(detail) && detail.length) {
    return detail
      .map((e) => {
        const field = (e.loc ?? []).filter((p) => p !== "body").join(".");
        return field ? `${field}: ${e.msg ?? "is invalid"}` : (e.msg ?? "is invalid");
      })
      .join("; ");
  }
  if (res.status === 429) {
    const retry = res.headers.get("Retry-After");
    return retry
      ? `Too many attempts. Try again in ${retry} seconds.`
      : "Too many attempts. Try again shortly.";
  }
  return `${fallback}: ${res.status}`;
}

export interface HotlistItem {
  item_id: string;
  what: string;
  category: string;
  bucket: string;
  bucket_label: string;
  bucket_emoji: string;
  why: string;
  owner: string | null;
  due: string | null;
  dollar_exposure: number | null;
  recommended_action: string;
  notice_deadline: boolean;
  score: number;
  sources: { source_type: string; title: string; url: string | null }[];
}

export interface Hotlist {
  project_id: string;
  generated_at: string;
  item_count: number;
  total_exposure: number;
  buckets: Record<string, { count: number; exposure: number }>;
  items: HotlistItem[];
}

export interface ItemDetail {
  id: string;
  title: string;
  category: string;
  summary: string;
  status: string;
  owner: string | null;
  score: number | null;
  bucket: string | null;
  explanation: string;
  factors: Record<string, any>;
  signals: { id: string; source_type: string; source_kind: string; title: string; url: string | null; occurred_at: string | null }[];
}

// ---- Org administration ---------------------------------------------------- //

export type Role = "owner" | "admin" | "pm" | "viewer";

/** Highest first, the order a role picker should list them. */
export const ROLES: Role[] = ["owner", "admin", "pm", "viewer"];

const ROLE_RANK: Record<string, number> = { viewer: 0, pm: 1, admin: 2, owner: 3 };

/**
 * Whether `actor` may grant (or take away) `target`. Mirrors the server's rule --
 * nobody grants above their own role -- so the UI does not offer choices the
 * server will refuse. The server still enforces it; this is only presentation.
 */
export function canGrant(actor: string, target: string): boolean {
  return (ROLE_RANK[actor] ?? -1) >= (ROLE_RANK[target] ?? Infinity);
}

export interface Member {
  user_id: string;
  email: string;
  full_name: string;
  role: Role;
  is_active: boolean;
  /** Provisioned by the identity provider: role and status are changed there. */
  scim_managed: boolean;
  last_login_at: string | null;
}

export interface Invite {
  id: string;
  email: string;
  role: Role;
  invited_by: string;
  expires_at: string;
  accepted: boolean;
  /** Present only in the creation response. */
  token: string | null;
}

export interface ScimToken {
  id: string;
  name: string;
  max_role: Role;
  created_at: string;
  last_used_at: string | null;
  revoked: boolean;
  /** Present only in the creation response. */
  token: string | null;
}

export interface Retention {
  /** null inherits the deployment default; 0 keeps forever. */
  signal_days: number | null;
  item_days: number | null;
  effective_signal_days: number;
  effective_item_days: number;
}

export interface PurgePreview {
  signals: number;
  items: number;
  scores: number;
  snapshots: number;
  cutoff_signal: string | null;
  cutoff_item: string | null;
}

export interface DeletionStatus {
  org_id: string;
  requested_at: string | null;
  /** true once the org is gone; false while a queued erasure drains. */
  completed: boolean;
  deleted_rows?: Record<string, number>;
}

export interface ActiveSession {
  id: string;
  created_at: string;
  expires_at: string;
  user_agent: string;
  ip: string;
}

export interface ConnectionHealth {
  id: string;
  source_type: string;
  account_ref: string;
  /** pending | active | degraded | error | revoked */
  status: string;
  last_sync: string | null;
  last_error: string | null;
  project_id: string;
}

export interface OrgStats {
  projects: number;
  connections: number;
  ai_connections: number;
  scripts: number;
  items: number;
  signals: number;
}

export interface TenantIsolation {
  enabled: boolean;
  enforced: boolean;
  detail: string;
}

function sessionFrom(baseUrl: string, d: any): Session {
  return {
    baseUrl,
    token: d.access_token,
    refreshToken: d.refresh_token ?? null,
    role: d.role,
    orgId: d.org_id,
    userId: d.user_id,
  };
}

export class Api {
  /** Called when the session changes (token refreshed) or dies (must sign in). */
  constructor(
    private session: Session,
    private onSessionChange?: (s: Session | null) => void,
  ) {}

  /** In-flight refresh, so N concurrent 401s trigger one exchange, not N. */
  private refreshing: Promise<boolean> | null = null;

  private async send(path: string, init: RequestInit): Promise<Response> {
    return fetch(`${this.session.baseUrl}${path}`, {
      ...init,
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${this.session.token}`,
        ...(init.headers || {}),
      },
    });
  }

  /**
   * Exchange the refresh token for a new pair.
   *
   * Refresh tokens are single-use and rotate: presenting one twice is treated by
   * the server as theft and kills the whole session family. So concurrent callers
   * must share one exchange rather than each sending the same token.
   */
  private async refreshSession(): Promise<boolean> {
    if (!this.session.refreshToken) return false;
    if (!this.refreshing) {
      this.refreshing = (async () => {
        const res = await fetch(`${this.session.baseUrl}/auth/refresh`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ refresh_token: this.session.refreshToken }),
        });
        if (!res.ok) {
          this.onSessionChange?.(null); // expired, revoked, or replayed
          return false;
        }
        const d = await res.json();
        this.session = {
          ...this.session,
          token: d.access_token,
          refreshToken: d.refresh_token ?? null,
          role: d.role,
        };
        this.onSessionChange?.(this.session);
        return true;
      })().finally(() => {
        this.refreshing = null;
      });
    }
    return this.refreshing;
  }

  private async req<T>(path: string, init: RequestInit = {}): Promise<T> {
    let res = await this.send(path, init);
    // Access tokens are short-lived by design, and the server also invalidates
    // them immediately on a role change or a sign-out-everywhere. One retry
    // behind a successful refresh keeps that invisible to the user.
    if (res.status === 401 && (await this.refreshSession())) {
      res = await this.send(path, init);
    }
    if (!res.ok) throw new Error(await describeError(res, "request failed"));
    const ct = res.headers.get("content-type") || "";
    return (ct.includes("json") ? await res.json() : (await res.blob())) as T;
  }

  static async login(baseUrl: string, email: string, password: string): Promise<Session> {
    const res = await fetch(`${baseUrl}/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    });
    if (!res.ok) throw new Error(await describeError(res, "Sign-in failed"));
    return sessionFrom(baseUrl, await res.json());
  }

  /** Ends this session on the server; the local token alone is not enough. */
  async logout(): Promise<void> {
    if (!this.session.refreshToken) return;
    await fetch(`${this.session.baseUrl}/auth/logout`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: this.session.refreshToken }),
    }).catch(() => {
      // Signing out locally must succeed even if the server is unreachable.
    });
  }

  /** Whether this server offers SSO, so the sign-in screen can show the button. */
  static async ssoConfig(baseUrl: string): Promise<{ enabled: boolean; issuer: string }> {
    try {
      const res = await fetch(`${baseUrl}/auth/sso/config`);
      if (!res.ok) return { enabled: false, issuer: "" };
      return await res.json();
    } catch {
      return { enabled: false, issuer: "" };
    }
  }

  /** Redeem an invite code: creates the account (or attaches an existing one) and signs in. */
  static async acceptInvite(baseUrl: string, token: string, password: string, fullName: string): Promise<Session> {
    const res = await fetch(`${baseUrl}/invites/accept`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token, password, full_name: fullName }),
    });
    if (!res.ok) throw new Error(await describeError(res, "Could not accept the invite"));
    return sessionFrom(baseUrl, await res.json());
  }

  static async register(baseUrl: string, email: string, password: string, orgName: string): Promise<Session> {
    const res = await fetch(`${baseUrl}/auth/register`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password, org_name: orgName }),
    });
    if (!res.ok) throw new Error(await describeError(res, "Could not create the account"));
    return sessionFrom(baseUrl, await res.json());
  }

  projects = () => this.req<{ id: string; name: string }[]>("/projects");
  createProject = (name: string) =>
    this.req<{ id: string }>("/projects", { method: "POST", body: JSON.stringify({ name }) });

  sources = () =>
    this.req<
      {
        source_type: string;
        auth: string;
        configured: boolean;
        /** Scope -> why it would be requested; granted only if the admin opts in. */
        optional_scopes?: Record<string, string>;
      }[]
    >("/connections/sources");
  connections = (projectId: string) => this.req<any[]>(`/connections?project_id=${projectId}`);

  hotlist = (projectId: string, refresh = false) =>
    this.req<Hotlist>(`/projects/${projectId}/hotlist${refresh ? "?refresh=true" : ""}`);
  item = (itemId: string) => this.req<ItemDetail>(`/items/${itemId}`);
  refresh = (projectId: string) =>
    this.req<Hotlist>(`/projects/${projectId}/hotlist/refresh`, { method: "POST" });
  act = (itemId: string, type: string) =>
    this.req(`/items/${itemId}/actions`, { method: "POST", body: JSON.stringify({ type }) });

  aiConnections = () => this.req<any[]>("/ai/connections");
  createAiConnection = (body: object) =>
    this.req("/ai/connections", { method: "POST", body: JSON.stringify(body) });
  sift = (projectId: string, instruction: string, aiConnectionId?: string) =>
    this.req<{ findings: any[]; scanned_signals: number }>(`/ai/projects/${projectId}/sift`, {
      method: "POST",
      body: JSON.stringify({ instruction, ai_connection_id: aiConnectionId ?? null }),
    });

  scripts = (projectId: string) => this.req<any[]>(`/projects/${projectId}/scripts`);
  createScript = (projectId: string, name: string, source: string) =>
    this.req(`/projects/${projectId}/scripts`, {
      method: "POST",
      body: JSON.stringify({ name, source_code: source }),
    });
  runScript = (scriptId: string) => this.req<any>(`/scripts/${scriptId}/run`, { method: "POST" });

  // Members and invites (admin+; listing members is open to every role).
  members = () => this.req<Member[]>("/orgs/current/members");
  setMemberRole = (userId: string, role: Role) =>
    this.req<Member>(`/orgs/current/members/${encodeURIComponent(userId)}/role`, {
      method: "PUT",
      body: JSON.stringify({ role }),
    });
  deactivateMember = (userId: string) =>
    this.req(`/orgs/current/members/${encodeURIComponent(userId)}/deactivate`, { method: "POST" });
  reactivateMember = (userId: string) =>
    this.req(`/orgs/current/members/${encodeURIComponent(userId)}/reactivate`, { method: "POST" });
  removeMember = (userId: string) =>
    this.req(`/orgs/current/members/${encodeURIComponent(userId)}`, { method: "DELETE" });

  invites = () => this.req<Invite[]>("/orgs/current/invites");
  createInvite = (email: string, role: Role, expiresDays = 7) =>
    this.req<Invite>("/orgs/current/invites", {
      method: "POST",
      body: JSON.stringify({ email, role, expires_days: expiresDays }),
    });
  revokeInvite = (inviteId: string) =>
    this.req(`/orgs/current/invites/${encodeURIComponent(inviteId)}`, { method: "DELETE" });

  // SCIM provisioning tokens (owner only).
  scimTokens = () => this.req<ScimToken[]>("/orgs/current/scim-tokens");
  createScimToken = (name: string, maxRole: Role) =>
    this.req<ScimToken>("/orgs/current/scim-tokens", {
      method: "POST",
      body: JSON.stringify({ name, max_role: maxRole }),
    });
  revokeScimToken = (tokenId: string) =>
    this.req(`/orgs/current/scim-tokens/${encodeURIComponent(tokenId)}`, { method: "DELETE" });

  // Retention (view: admin; change and run: owner).
  retention = () => this.req<Retention>("/orgs/current/retention");
  setRetention = (signalDays: number | null, itemDays: number | null) =>
    this.req<Retention>("/orgs/current/retention", {
      method: "PUT",
      body: JSON.stringify({ signal_days: signalDays, item_days: itemDays }),
    });
  retentionPreview = () => this.req<PurgePreview>("/orgs/current/retention/preview");
  runRetention = () =>
    this.req<Record<string, number>>("/orgs/current/retention/run", { method: "POST" });
  orgSettings = () => this.req<{ org_id: string; org_name: string }>("/orgs/current/settings");
  /** Everything the tenant holds, as JSON (connector tokens excluded server-side). */
  exportOrg = () => this.req<unknown>("/orgs/current/export");
  /** Irreversible. The server refuses unless `confirmOrgName` matches exactly. */
  deleteOrg = (confirmOrgName: string) =>
    this.req<DeletionStatus>("/orgs/current/delete", {
      method: "POST",
      body: JSON.stringify({ confirm_org_name: confirmOrgName }),
    });
  deletionStatus = () => this.req<DeletionStatus>("/orgs/current/deletion-status");

  // The caller's own sessions.
  sessions = () => this.req<ActiveSession[]>("/auth/sessions");
  revokeSession = (sessionId: string) =>
    this.req(`/auth/sessions/${encodeURIComponent(sessionId)}`, { method: "DELETE" });
  logoutAll = () => this.req("/auth/logout-all", { method: "POST" });
  changePassword = (currentPassword: string, newPassword: string) =>
    this.req("/auth/password", {
      method: "POST",
      body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
    });

  // Admin health console (admin+).
  connectionsHealth = () => this.req<ConnectionHealth[]>("/admin/connections/health");
  auditVerify = () => this.req<{ org_id: string; audit_chain_intact: boolean }>("/admin/audit/verify");
  orgStats = () => this.req<OrgStats>("/admin/stats");
  tenantIsolation = () => this.req<TenantIsolation>("/admin/security/tenant-isolation");

  exportUrl = (projectId: string, fmt: "xlsx" | "pdf") =>
    `${this.session.baseUrl}/projects/${projectId}/hotlist/export?format=${fmt}`;

  // Live hotlist over WebSocket (token in query — WS can't set Authorization).
  openHotlistSocket(projectId: string, onUpdate: (h: Hotlist) => void): WebSocket {
    const wsBase = this.session.baseUrl.replace(/^http/, "ws");
    const ws = new WebSocket(`${wsBase}/ws/projects/${projectId}/hotlist?token=${this.session.token}`);
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.type === "hotlist") onUpdate(msg.payload as Hotlist);
    };
    return ws;
  }
}

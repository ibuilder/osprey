// Typed Osprey API client used by the desktop UI.

export const DEFAULT_BASE = "http://localhost:8000";

export interface Session {
  baseUrl: string;
  token: string;
  role: string;
  orgId: string;
  userId: string;
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

export class Api {
  constructor(private session: Session) {}

  private async req<T>(path: string, init: RequestInit = {}): Promise<T> {
    const res = await fetch(`${this.session.baseUrl}${path}`, {
      ...init,
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${this.session.token}`,
        ...(init.headers || {}),
      },
    });
    if (!res.ok) throw new Error(`${res.status}: ${await res.text()}`);
    const ct = res.headers.get("content-type") || "";
    return (ct.includes("json") ? await res.json() : (await res.blob())) as T;
  }

  static async login(baseUrl: string, email: string, password: string): Promise<Session> {
    const res = await fetch(`${baseUrl}/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    });
    if (!res.ok) throw new Error(`login failed: ${res.status}`);
    const d = await res.json();
    return { baseUrl, token: d.access_token, role: d.role, orgId: d.org_id, userId: d.user_id };
  }

  static async register(baseUrl: string, email: string, password: string, orgName: string): Promise<Session> {
    const res = await fetch(`${baseUrl}/auth/register`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password, org_name: orgName }),
    });
    if (!res.ok) throw new Error(`register failed: ${res.status}`);
    const d = await res.json();
    return { baseUrl, token: d.access_token, role: d.role, orgId: d.org_id, userId: d.user_id };
  }

  projects = () => this.req<{ id: string; name: string }[]>("/projects");
  createProject = (name: string) =>
    this.req<{ id: string }>("/projects", { method: "POST", body: JSON.stringify({ name }) });

  sources = () => this.req<{ source_type: string; auth: string; configured: boolean }[]>("/connections/sources");
  connections = (projectId: string) => this.req<any[]>(`/connections?project_id=${projectId}`);

  hotlist = (projectId: string, refresh = false) =>
    this.req<Hotlist>(`/projects/${projectId}/hotlist${refresh ? "?refresh=true" : ""}`);
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

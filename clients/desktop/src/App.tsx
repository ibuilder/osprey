import { invoke } from "@tauri-apps/api/core";
import { useEffect, useRef, useState } from "react";
import { Api, DEFAULT_BASE, Hotlist, Session } from "./api";

type Tab = "hotlist" | "connections" | "ai" | "scripts";

export default function App() {
  const [session, setSession] = useState<Session | null>(null);
  if (!session) return <Login onLogin={setSession} />;
  return <Main session={session} onLogout={() => setSession(null)} />;
}

function Login({ onLogin }: { onLogin: (s: Session) => void }) {
  const [baseUrl, setBaseUrl] = useState(DEFAULT_BASE);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [orgName, setOrgName] = useState("");
  const [mode, setMode] = useState<"login" | "register">("login");
  const [err, setErr] = useState("");

  async function submit() {
    setErr("");
    try {
      const s =
        mode === "login"
          ? await Api.login(baseUrl, email, password)
          : await Api.register(baseUrl, email, password, orgName || "My Org");
      // Hand the backend session to the Rust shell so the OAuth loopback can call it.
      await invoke("set_session", { baseUrl: s.baseUrl, token: s.token }).catch(() => {});
      onLogin(s);
    } catch (e) {
      setErr(String(e));
    }
  }

  return (
    <div className="login">
      <div className="card">
        <h2 style={{ marginTop: 0 }}>Osprey</h2>
        <div className="muted">The foreman that never sleeps.</div>
        <input placeholder="Backend URL" value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} />
        <input placeholder="Email" value={email} onChange={(e) => setEmail(e.target.value)} />
        <input type="password" placeholder="Password" value={password} onChange={(e) => setPassword(e.target.value)} />
        {mode === "register" && (
          <input placeholder="Organization name" value={orgName} onChange={(e) => setOrgName(e.target.value)} />
        )}
        {err && <div className="notice">{err}</div>}
        <button className="primary" style={{ width: "100%" }} onClick={submit}>
          {mode === "login" ? "Sign in" : "Create account"}
        </button>
        <div className="muted" style={{ textAlign: "center" }}>
          <a onClick={() => setMode(mode === "login" ? "register" : "login")}>
            {mode === "login" ? "Create an account" : "Have an account? Sign in"}
          </a>
        </div>
      </div>
    </div>
  );
}

function Main({ session, onLogout }: { session: Session; onLogout: () => void }) {
  const api = new Api(session);
  const [tab, setTab] = useState<Tab>("hotlist");
  const [projects, setProjects] = useState<{ id: string; name: string }[]>([]);
  const [projectId, setProjectId] = useState<string>("");

  useEffect(() => {
    api.projects().then((p) => {
      setProjects(p);
      if (p[0]) setProjectId(p[0].id);
    });
  }, []);

  async function newProject() {
    const name = prompt("Project name");
    if (!name) return;
    const { id } = await api.createProject(name);
    const p = await api.projects();
    setProjects(p);
    setProjectId(id);
  }

  return (
    <div className="app">
      <div className="topbar">
        <span className="brand">🜲 Osprey</span>
        <select value={projectId} onChange={(e) => setProjectId(e.target.value)}>
          {projects.map((p) => (
            <option key={p.id} value={p.id}>{p.name}</option>
          ))}
        </select>
        <button onClick={newProject}>+ Project</button>
        <div className="tabs">
          {(["hotlist", "connections", "ai", "scripts"] as Tab[]).map((t) => (
            <button key={t} className={tab === t ? "active" : ""} onClick={() => setTab(t)}>{t}</button>
          ))}
          <button onClick={onLogout}>Sign out</button>
        </div>
      </div>
      <div className="body">
        {!projectId && <div className="muted">Create a project to begin.</div>}
        {projectId && tab === "hotlist" && <HotlistView api={api} projectId={projectId} />}
        {projectId && tab === "connections" && <ConnectionsView api={api} projectId={projectId} />}
        {projectId && tab === "ai" && <AiView api={api} projectId={projectId} />}
        {projectId && tab === "scripts" && <ScriptsView api={api} projectId={projectId} />}
      </div>
    </div>
  );
}

const BUCKETS = ["act_today", "this_week", "watch"] as const;

function HotlistView({ api, projectId }: { api: Api; projectId: string }) {
  const [hot, setHot] = useState<Hotlist | null>(null);
  const [bucket, setBucket] = useState<string>("");
  const [category, setCategory] = useState<string>("");
  const [query, setQuery] = useState("");
  const [openId, setOpenId] = useState<string | null>(null);
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    api.hotlist(projectId, false).then(setHot).catch(() => {});
    const ws = api.openHotlistSocket(projectId, setHot);
    wsRef.current = ws;
    return () => ws.close();
  }, [projectId]);

  async function download(fmt: "xlsx" | "pdf") {
    const res = await fetch(api.exportUrl(projectId, fmt), {
      headers: { Authorization: `Bearer ${(api as any).session.token}` },
    });
    const blob = await res.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `osprey-hotlist.${fmt}`;
    a.click();
  }

  async function act(itemId: string, type: string) {
    await api.act(itemId, type);
    setOpenId(null);
    setHot(await api.refresh(projectId));
  }

  if (!hot) return <div className="muted">Loading…</div>;
  if (hot.item_count === 0) {
    return (
      <div className="empty">
        <div className="empty-mark">🜲</div>
        <h3>Nothing on the hotlist yet</h3>
        <p className="muted">Connect a source (Connections tab) or forward an email, then hit Refresh.
          Osprey will start surfacing what needs you.</p>
        <button className="primary" onClick={() => api.refresh(projectId).then(setHot)}>Refresh</button>
      </div>
    );
  }

  const categories = Array.from(new Set(hot.items.map((i) => i.category))).sort();
  const items = hot.items.filter((it) => {
    if (bucket && it.bucket !== bucket) return false;
    if (category && it.category !== category) return false;
    if (query && !`${it.what} ${it.why} ${it.recommended_action}`.toLowerCase().includes(query.toLowerCase())) return false;
    return true;
  });

  return (
    <div>
      <div className="row" style={{ marginBottom: 12 }}>
        <div className="muted">
          {hot.item_count} items · ${Math.round(hot.total_exposure).toLocaleString()} exposure · <span style={{ color: "var(--prio-green)" }}>live</span>
        </div>
        <div className="spacer" />
        <button onClick={() => api.refresh(projectId).then(setHot)}>Refresh</button>
        <button onClick={() => download("xlsx")}>Excel</button>
        <button onClick={() => download("pdf")}>PDF</button>
      </div>
      <div className="buckets">
        {BUCKETS.map((b) => (
          <div
            key={b}
            className={`bucket ${b} ${bucket && bucket !== b ? "dim" : ""}`}
            onClick={() => setBucket(bucket === b ? "" : b)}
            title="Click to filter"
          >
            <div className="n">{hot.buckets[b]?.count ?? 0}</div>
            <div>{b.replace("_", " ")}</div>
          </div>
        ))}
      </div>

      <div className="row filterbar">
        <input placeholder="Search the hotlist…" value={query} onChange={(e) => setQuery(e.target.value)} style={{ maxWidth: 280 }} />
        <select value={category} onChange={(e) => setCategory(e.target.value)} style={{ width: "auto" }}>
          <option value="">All categories</option>
          {categories.map((c) => <option key={c} value={c}>{c}</option>)}
        </select>
        {(bucket || category || query) && <button onClick={() => { setBucket(""); setCategory(""); setQuery(""); }}>Clear</button>}
        <div className="spacer" />
        <span className="muted">{items.length} shown</span>
      </div>

      {items.map((it) => (
        <div key={it.item_id} className={`card item ${it.bucket} clickable`} onClick={() => setOpenId(it.item_id)}>
          <div className="row">
            <span className="what">{it.what}</span>
            {it.notice_deadline && <span className="pill notice">NOTICE</span>}
            <div className="spacer" />
            <span className="score">{Math.round(it.score)}</span>
          </div>
          <div className="why">{it.why}</div>
          <div className="row muted">
            <span className="pill">{it.category}</span>
            {it.due && <span>due {it.due}</span>}
            {it.dollar_exposure ? <span>${Math.round(it.dollar_exposure).toLocaleString()}</span> : null}
            <div className="spacer" />
            <span className="muted" style={{ fontSize: 12 }}>click for detail →</span>
          </div>
        </div>
      ))}
      {items.length === 0 && <div className="muted">No items match your filters.</div>}

      {openId && <ItemModal api={api} itemId={openId} onClose={() => setOpenId(null)} onAct={act} />}
    </div>
  );
}

function Bar({ label, value }: { label: string; value: number }) {
  return (
    <div className="bar-row">
      <span className="bar-label">{label}</span>
      <div className="bar-track"><div className="bar-fill" style={{ width: `${Math.round(value * 100)}%` }} /></div>
      <span className="bar-val mono">{value.toFixed(2)}</span>
    </div>
  );
}

function ItemModal({ api, itemId, onClose, onAct }: { api: Api; itemId: string; onClose: () => void; onAct: (id: string, type: string) => void }) {
  const [item, setItem] = useState<import("./api").ItemDetail | null>(null);
  useEffect(() => { api.item(itemId).then(setItem); }, [itemId]);

  const f = item?.factors ?? {};
  const citations: { signal_id: string; quote_span: string }[] = f.citations ?? [];

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        {!item ? <div className="muted">Loading…</div> : (
          <>
            <div className="row">
              <h3 style={{ margin: 0 }}>{item.title}</h3>
              <div className="spacer" />
              <button onClick={onClose}>✕</button>
            </div>
            <div className="row muted" style={{ margin: "6px 0 14px" }}>
              <span className="pill">{item.category}</span>
              {item.bucket && <span className={`pill p-${item.bucket}`}>{item.bucket.replace("_", " ")}</span>}
              {item.score != null && <span className="score">score {Math.round(item.score)}</span>}
              {f.notice_deadline && <span className="pill notice">NOTICE DEADLINE</span>}
            </div>

            <div className="why" style={{ fontSize: 14 }}>{item.explanation}</div>
            {f.recommended_action && <div className="reco">→ {f.recommended_action}</div>}

            <h4>Why it ranked here</h4>
            <Bar label="Urgency" value={f.urgency ?? 0} />
            <Bar label="Impact" value={f.impact ?? 0} />
            <Bar label="Confidence" value={f.confidence ?? 0} />
            <div className="muted" style={{ fontSize: 12, marginTop: 6 }}>
              weights: urgency {f.weights?.urgency} · impact {f.weights?.impact} · confidence {f.weights?.confidence}
              {f.dollar_exposure ? ` · exposure $${Math.round(f.dollar_exposure).toLocaleString()}` : ""}
              {f.deadline ? ` · deadline ${String(f.deadline).slice(0, 10)}` : ""}
            </div>

            {citations.length > 0 && (
              <>
                <h4>Cited from the source</h4>
                {citations.map((c, i) => <blockquote key={i} className="cite">“{c.quote_span}”</blockquote>)}
              </>
            )}

            <h4>Sources ({item.signals.length})</h4>
            {item.signals.map((s) => (
              <div key={s.id} className="row srcrow">
                <span className="pill">{s.source_type}</span>
                <span style={{ flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{s.title}</span>
                {s.url && <a href={s.url} target="_blank">open ↗</a>}
              </div>
            ))}

            <div className="row" style={{ marginTop: 16 }}>
              <div className="spacer" />
              <button onClick={() => onAct(item.id, "snooze")}>Snooze</button>
              <button onClick={() => onAct(item.id, "escalate")}>Escalate</button>
              <button className="primary" onClick={() => onAct(item.id, "done")}>Mark done</button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function ConnectionsView({ api, projectId }: { api: Api; projectId: string }) {
  const [sources, setSources] = useState<any[]>([]);
  const [conns, setConns] = useState<any[]>([]);
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState("");

  const reload = () => api.connections(projectId).then(setConns);
  useEffect(() => {
    api.sources().then(setSources);
    reload();
  }, [projectId]);

  async function connect(sourceType: string) {
    setErr("");
    setBusy(sourceType);
    try {
      // Rust shell opens the system browser + loopback; backend seals the tokens.
      await invoke("oauth_connect", { sourceType, projectId });
      await reload();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy("");
    }
  }

  return (
    <div>
      <div className="card">
        <b>Connect a source</b>
        <div className="muted">You authorize each source in your own browser. Tokens are sealed on the server — never stored in this app.</div>
        <div className="grid2" style={{ marginTop: 10 }}>
          {sources.filter((s) => s.auth === "oauth").map((s) => (
            <div key={s.source_type} className="row">
              <span>{s.source_type}</span>
              <div className="spacer" />
              <button
                className="primary"
                disabled={!s.configured || busy === s.source_type}
                onClick={() => connect(s.source_type)}
              >
                {busy === s.source_type ? "Connecting…" : s.configured ? "Connect" : "Not configured"}
              </button>
            </div>
          ))}
        </div>
        {err && <div className="notice">{err}</div>}
      </div>
      <div className="card">
        <b>Connected</b>
        {conns.length === 0 && <div className="muted">No sources connected yet.</div>}
        {conns.map((c) => (
          <div key={c.id} className="row" style={{ padding: "6px 0" }}>
            <span className="pill">{c.source_type}</span>
            <span className="muted">{c.account_ref}</span>
            <div className="spacer" />
            <span className="muted">{c.status}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function AiView({ api, projectId }: { api: Api; projectId: string }) {
  const [conns, setConns] = useState<any[]>([]);
  const [instruction, setInstruction] = useState("");
  const [aiId, setAiId] = useState<string>("");
  const [result, setResult] = useState<any>(null);
  const [provider, setProvider] = useState("claude");
  const [key, setKey] = useState("");

  const reload = () => api.aiConnections().then(setConns);
  useEffect(() => { reload(); }, []);

  return (
    <div>
      <div className="card">
        <b>Connect your AI</b>
        <div className="muted">Bring your own Claude / OpenAI / Ollama. The key is sealed at rest and used only to sift your data.</div>
        <div className="row" style={{ marginTop: 10 }}>
          <select value={provider} onChange={(e) => setProvider(e.target.value)} style={{ width: 140 }}>
            <option value="claude">Claude</option>
            <option value="openai">OpenAI</option>
            <option value="ollama">Ollama (local)</option>
          </select>
          <input placeholder="API key" value={key} onChange={(e) => setKey(e.target.value)} />
          <button className="primary" onClick={() => api.createAiConnection({ provider, api_key: key }).then(() => { setKey(""); reload(); })}>Save</button>
        </div>
        {conns.length > 0 && <div className="muted" style={{ marginTop: 8 }}>{conns.length} AI connection(s) saved.</div>}
      </div>
      <div className="card">
        <b>Sift the project into the hotlist</b>
        <div className="muted">Ask in plain English. Matching signals become scored, cited hotlist items.</div>
        <textarea
          placeholder="e.g. flag anything about liquidated damages or notice deadlines"
          value={instruction}
          onChange={(e) => setInstruction(e.target.value)}
          style={{ marginTop: 8 }}
        />
        <div className="row" style={{ marginTop: 8 }}>
          <select value={aiId} onChange={(e) => setAiId(e.target.value)} style={{ width: 220 }}>
            <option value="">Default provider</option>
            {conns.map((c) => <option key={c.id} value={c.id}>{c.label || c.provider}</option>)}
          </select>
          <div className="spacer" />
          <button className="primary" onClick={() => api.sift(projectId, instruction, aiId || undefined).then(setResult)}>Run sift</button>
        </div>
        {result && (
          <div className="muted" style={{ marginTop: 10 }}>
            Scanned {result.scanned_signals} signals · {result.findings.length} finding(s) pushed to the hotlist.
          </div>
        )}
      </div>
    </div>
  );
}

function ScriptsView({ api, projectId }: { api: Api; projectId: string }) {
  const [scripts, setScripts] = useState<any[]>([]);
  const [name, setName] = useState("");
  const [code, setCode] = useState(
    'osprey.emit_signal("Example", "A signal from a background script", amount=1000)\n'
  );
  const reload = () => api.scripts(projectId).then(setScripts);
  useEffect(() => { reload(); }, [projectId]);

  return (
    <div>
      <div className="card">
        <b>New background script</b>
        <div className="muted">Runs sandboxed on the server; call <span className="mono">osprey.emit_signal(...)</span> to push into the hotlist.</div>
        <input placeholder="Script name" value={name} onChange={(e) => setName(e.target.value)} style={{ margin: "8px 0" }} />
        <textarea className="code" value={code} onChange={(e) => setCode(e.target.value)} />
        <div className="row" style={{ marginTop: 8 }}>
          <div className="spacer" />
          <button className="primary" onClick={() => api.createScript(projectId, name, code).then(() => { setName(""); reload(); })}>Save script</button>
        </div>
      </div>
      {scripts.map((s) => (
        <div key={s.id} className="card row">
          <b>{s.name}</b>
          <span className="pill">{s.status}</span>
          {s.last_result?.emitted != null && <span className="muted">emitted {s.last_result.emitted}</span>}
          <div className="spacer" />
          <button onClick={() => api.runScript(s.id).then(reload)}>Run now</button>
        </div>
      ))}
    </div>
  );
}

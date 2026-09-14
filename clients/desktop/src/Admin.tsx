// Org administration and account screens.
//
// Everything here was previously reachable only with curl. The server enforces
// every rule (role ceilings, last-owner protection, SCIM ownership); the UI
// mirrors those rules only so it does not offer buttons that are certain to fail,
// and it always shows the server's own reason when something is refused.

import { invoke } from "@tauri-apps/api/core";
import { useEffect, useState } from "react";
import {
  ActiveSession,
  Api,
  canGrant,
  Invite,
  Member,
  PurgePreview,
  Retention,
  Role,
  ROLES,
  ScimToken,
} from "./api";

/** "Chrome on Windows" from a full user-agent string; the raw one is noise. */
export function describeAgent(ua: string): string {
  if (!ua) return "Unknown device";
  const os = /Windows/.test(ua)
    ? "Windows"
    : /iPhone|iPad/.test(ua)
      ? "iOS"
      : /Mac OS X|Macintosh/.test(ua)
        ? "macOS"
        : /Android/.test(ua)
          ? "Android"
          : /Linux/.test(ua)
            ? "Linux"
            : "";
  // Order matters: Edge and Opera also say Chrome, and Chrome also says Safari.
  const client = /Edg\//.test(ua)
    ? "Edge"
    : /OPR\//.test(ua)
      ? "Opera"
      : /Firefox\//.test(ua)
        ? "Firefox"
        : /Chrome\//.test(ua)
          ? "Chrome"
          : /Safari\//.test(ua)
            ? "Safari"
            : ua.split(/[\s/]/)[0];
  return os ? `${client} on ${os}` : client;
}

function errText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

export function when(iso: string | null | undefined): string {
  if (!iso) return "never";
  // The server stores UTC. SQLite hands timestamps back without an offset, and
  // Date() would read those as local time -- hours off for anyone not on UTC.
  const zoned = /[zZ]|[+-]\d\d:?\d\d$/.test(iso) || !iso.includes("T") ? iso : `${iso}Z`;
  const d = new Date(zoned);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

/**
 * A destructive button that asks for a second click instead of a native dialog.
 * `window.confirm` is unreliable inside some webviews and invisible to tests.
 */
function ConfirmButton({
  label,
  confirmLabel,
  onConfirm,
  disabled,
}: {
  label: string;
  confirmLabel: string;
  onConfirm: () => void | Promise<void>;
  disabled?: boolean;
}) {
  const [armed, setArmed] = useState(false);
  if (!armed) {
    return (
      <button disabled={disabled} onClick={() => setArmed(true)}>
        {label}
      </button>
    );
  }
  return (
    <span className="row" style={{ gap: 6 }}>
      <button
        className="danger"
        onClick={async () => {
          setArmed(false);
          await onConfirm();
        }}
      >
        {confirmLabel}
      </button>
      <button onClick={() => setArmed(false)}>Cancel</button>
    </span>
  );
}

/** A secret the server will never show again. */
function OneTimeSecret({ title, secret, onDismiss }: { title: string; secret: string; onDismiss: () => void }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="secret" role="alert">
      <b>{title}</b>
      <div className="muted">Copy it now. Osprey stores only a hash and cannot show it again.</div>
      <div className="row" style={{ marginTop: 8 }}>
        <code className="mono secret-value">{secret}</code>
        <button
          onClick={() =>
            navigator.clipboard
              ?.writeText(secret)
              .then(() => setCopied(true))
              .catch(() => {})
          }
        >
          {copied ? "Copied" : "Copy"}
        </button>
        <button onClick={onDismiss}>Done</button>
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Admin tab
// --------------------------------------------------------------------------- //
export function AdminView({ api, role, userId }: { api: Api; role: string; userId: string }) {
  const isOwner = role === "owner";
  return (
    <div>
      <MembersSection api={api} role={role} userId={userId} />
      <InvitesSection api={api} role={role} />
      {isOwner && <ScimSection api={api} />}
      <RetentionSection api={api} role={role} />
    </div>
  );
}

export function MembersSection({ api, role, userId }: { api: Api; role: string; userId: string }) {
  const [members, setMembers] = useState<Member[] | null>(null);
  const [err, setErr] = useState("");

  const reload = () =>
    api
      .members()
      .then(setMembers)
      .catch((e) => setErr(errText(e)));
  useEffect(() => {
    reload();
  }, []);

  async function run(fn: () => Promise<unknown>) {
    setErr("");
    try {
      await fn();
    } catch (e) {
      setErr(errText(e));
    }
    await reload();
  }

  return (
    <div className="card">
      <b>Members</b>
      <div className="muted">
        Changing a role or deactivating someone signs them out everywhere immediately.
      </div>
      {err && <div className="notice">{err}</div>}
      {!members && !err && <div className="muted">Loading…</div>}
      <table className="admin-table">
        <tbody>
          {members?.map((m) => {
            const self = m.user_id === userId;
            // Nobody may change a peer they could not have appointed.
            const manageable = !m.scim_managed && canGrant(role, m.role) && canGrant(role, "admin");
            return (
              <tr key={m.user_id} className={m.is_active ? "" : "inactive"}>
                <td>
                  <div>{m.full_name || m.email}</div>
                  {m.full_name && <div className="muted">{m.email}</div>}
                </td>
                <td>
                  <select
                    aria-label={`Role for ${m.email}`}
                    value={m.role}
                    disabled={!manageable}
                    onChange={(e) => run(() => api.setMemberRole(m.user_id, e.target.value as Role))}
                  >
                    {ROLES.filter((r) => r === m.role || canGrant(role, r)).map((r) => (
                      <option key={r} value={r}>
                        {r}
                      </option>
                    ))}
                  </select>
                </td>
                <td className="muted">
                  {m.scim_managed && <span className="pill">SCIM</span>}{" "}
                  {!m.is_active && <span className="pill">deactivated</span>}{" "}
                  {self && <span className="pill">you</span>}
                  <div>last sign-in {when(m.last_login_at)}</div>
                </td>
                <td className="actions">
                  {manageable && !self && m.is_active && (
                    <ConfirmButton
                      label="Deactivate"
                      confirmLabel={`Deactivate ${m.email}`}
                      onConfirm={() => run(() => api.deactivateMember(m.user_id))}
                    />
                  )}
                  {manageable && !m.is_active && (
                    <button onClick={() => run(() => api.reactivateMember(m.user_id))}>Reactivate</button>
                  )}
                  {manageable && !self && (
                    <ConfirmButton
                      label="Remove"
                      confirmLabel={`Remove ${m.email}`}
                      onConfirm={() => run(() => api.removeMember(m.user_id))}
                    />
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export function InvitesSection({ api, role }: { api: Api; role: string }) {
  const [invites, setInvites] = useState<Invite[]>([]);
  const [email, setEmail] = useState("");
  const [inviteRole, setInviteRole] = useState<Role>("viewer");
  const [created, setCreated] = useState<Invite | null>(null);
  const [err, setErr] = useState("");
  const canInvite = canGrant(role, "admin");

  const reload = () =>
    api
      .invites()
      .then(setInvites)
      .catch((e) => setErr(errText(e)));
  useEffect(() => {
    if (canInvite) reload();
  }, []);

  if (!canInvite) return null;

  async function create() {
    setErr("");
    try {
      const invite = await api.createInvite(email.trim(), inviteRole);
      setCreated(invite);
      setEmail("");
      await reload();
    } catch (e) {
      setErr(errText(e));
    }
  }

  const pending = invites.filter((i) => !i.accepted);

  return (
    <div className="card">
      <b>Invite someone</b>
      <div className="muted">
        Osprey sends no email. Create the invite, then send the code through your own channel; it
        works once and expires in 7 days.
      </div>
      <div className="row" style={{ marginTop: 10 }}>
        <input placeholder="name@company.com" value={email} onChange={(e) => setEmail(e.target.value)} />
        <select
          aria-label="Role for the invite"
          value={inviteRole}
          onChange={(e) => setInviteRole(e.target.value as Role)}
          style={{ width: 130 }}
        >
          {ROLES.filter((r) => canGrant(role, r)).map((r) => (
            <option key={r} value={r}>
              {r}
            </option>
          ))}
        </select>
        <button className="primary" disabled={!email.trim()} onClick={create}>
          Create invite
        </button>
      </div>
      {err && <div className="notice">{err}</div>}
      {created?.token && (
        <OneTimeSecret
          title={`Invite code for ${created.email}`}
          secret={created.token}
          onDismiss={() => setCreated(null)}
        />
      )}
      {pending.length > 0 && (
        <table className="admin-table">
          <tbody>
            {pending.map((i) => (
              <tr key={i.id}>
                <td>{i.email}</td>
                <td>
                  <span className="pill">{i.role}</span>
                </td>
                <td className="muted">expires {when(i.expires_at)}</td>
                <td className="actions">
                  <ConfirmButton
                    label="Revoke"
                    confirmLabel="Revoke invite"
                    onConfirm={async () => {
                      try {
                        await api.revokeInvite(i.id);
                      } catch (e) {
                        setErr(errText(e));
                      }
                      await reload();
                    }}
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

export function ScimSection({ api }: { api: Api }) {
  const [tokens, setTokens] = useState<ScimToken[]>([]);
  const [name, setName] = useState("");
  // Owner is not offered: the server refuses a token that could mint owners.
  const [maxRole, setMaxRole] = useState<Role>("pm");
  const [created, setCreated] = useState<ScimToken | null>(null);
  const [err, setErr] = useState("");

  const reload = () =>
    api
      .scimTokens()
      .then(setTokens)
      .catch((e) => setErr(errText(e)));
  useEffect(() => {
    reload();
  }, []);

  async function create() {
    setErr("");
    try {
      setCreated(await api.createScimToken(name.trim(), maxRole));
      setName("");
      await reload();
    } catch (e) {
      setErr(errText(e));
    }
  }

  return (
    <div className="card">
      <b>SCIM provisioning</b>
      <div className="muted">
        A bearer token for your identity provider's SCIM connector. It can create users up to the
        role ceiling you choose, never owners.
      </div>
      <div className="row" style={{ marginTop: 10 }}>
        <input placeholder="Label, e.g. Okta" value={name} onChange={(e) => setName(e.target.value)} />
        <select
          aria-label="Highest role this token may assign"
          value={maxRole}
          onChange={(e) => setMaxRole(e.target.value as Role)}
          style={{ width: 130 }}
        >
          {ROLES.filter((r) => r !== "owner").map((r) => (
            <option key={r} value={r}>
              up to {r}
            </option>
          ))}
        </select>
        <button className="primary" onClick={create}>
          Create token
        </button>
      </div>
      {err && <div className="notice">{err}</div>}
      {created?.token && (
        <OneTimeSecret title="SCIM token" secret={created.token} onDismiss={() => setCreated(null)} />
      )}
      {tokens.length > 0 && (
        <table className="admin-table">
          <tbody>
            {tokens.map((t) => (
              <tr key={t.id} className={t.revoked ? "inactive" : ""}>
                <td>{t.name || "(unnamed)"}</td>
                <td>
                  <span className="pill">up to {t.max_role}</span>
                </td>
                <td className="muted">
                  {t.revoked ? "revoked" : `last used ${when(t.last_used_at)}`}
                </td>
                <td className="actions">
                  {!t.revoked && (
                    <ConfirmButton
                      label="Revoke"
                      confirmLabel="Revoke token"
                      onConfirm={async () => {
                        try {
                          await api.revokeScimToken(t.id);
                        } catch (e) {
                          setErr(errText(e));
                        }
                        await reload();
                      }}
                    />
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

/** "" in a field means "inherit the deployment default"; 0 means keep forever. */
function parseDays(value: string): number | null {
  const trimmed = value.trim();
  return trimmed === "" ? null : Number(trimmed);
}

function describeWindow(days: number): string {
  return days === 0 ? "kept forever" : `${days} days`;
}

export function RetentionSection({ api, role }: { api: Api; role: string }) {
  const [policy, setPolicy] = useState<Retention | null>(null);
  const [preview, setPreview] = useState<PurgePreview | null>(null);
  const [signalDays, setSignalDays] = useState("");
  const [itemDays, setItemDays] = useState("");
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");
  const isOwner = role === "owner";
  const canView = canGrant(role, "admin");

  async function reload() {
    try {
      const p = await api.retention();
      setPolicy(p);
      setSignalDays(p.signal_days == null ? "" : String(p.signal_days));
      setItemDays(p.item_days == null ? "" : String(p.item_days));
      setPreview(await api.retentionPreview());
    } catch (e) {
      setErr(errText(e));
    }
  }
  useEffect(() => {
    if (canView) reload();
  }, []);

  if (!canView) return null;

  const invalid = [signalDays, itemDays].some((v) => {
    const n = parseDays(v);
    return n !== null && (!Number.isInteger(n) || n < 0 || n > 3650);
  });

  async function save() {
    setErr("");
    setMsg("");
    try {
      await api.setRetention(parseDays(signalDays), parseDays(itemDays));
      setMsg("Retention policy saved.");
      await reload();
    } catch (e) {
      setErr(errText(e));
    }
  }

  async function purge() {
    setErr("");
    setMsg("");
    try {
      const removed = await api.runRetention();
      const total = Object.values(removed).reduce((a, b) => a + b, 0);
      setMsg(`Retention run removed ${total} record(s).`);
      await reload();
    } catch (e) {
      setErr(errText(e));
    }
  }

  const due = preview ? preview.signals + preview.items + preview.scores + preview.snapshots : 0;

  return (
    <div className="card">
      <b>Data retention</b>
      <div className="muted">
        Days to keep source signals and closed hotlist items. Leave a field blank to use the server
        default; 0 keeps data forever.
      </div>
      {policy && (
        <div className="muted" style={{ marginTop: 6 }}>
          In effect: signals {describeWindow(policy.effective_signal_days)}, items{" "}
          {describeWindow(policy.effective_item_days)}.
        </div>
      )}
      <div className="grid2" style={{ marginTop: 10 }}>
        <label className="muted">
          Signals (days)
          <input
            inputMode="numeric"
            value={signalDays}
            disabled={!isOwner}
            placeholder="server default"
            onChange={(e) => setSignalDays(e.target.value)}
          />
        </label>
        <label className="muted">
          Items (days)
          <input
            inputMode="numeric"
            value={itemDays}
            disabled={!isOwner}
            placeholder="server default"
            onChange={(e) => setItemDays(e.target.value)}
          />
        </label>
      </div>
      {invalid && <div className="notice">Days must be a whole number from 0 to 3650.</div>}
      {preview && (
        <div className="muted" style={{ marginTop: 8 }}>
          The next run would delete {preview.signals} signal(s), {preview.items} item(s),{" "}
          {preview.scores} score(s) and {preview.snapshots} snapshot(s).
        </div>
      )}
      {err && <div className="notice">{err}</div>}
      {msg && <div className="muted">{msg}</div>}
      {isOwner ? (
        <div className="row" style={{ marginTop: 10 }}>
          <div className="spacer" />
          <ConfirmButton
            label="Run now"
            confirmLabel={`Delete ${due} record(s) now`}
            disabled={due === 0}
            onConfirm={purge}
          />
          <button className="primary" disabled={invalid} onClick={save}>
            Save policy
          </button>
        </div>
      ) : (
        <div className="muted" style={{ marginTop: 8 }}>
          Only an owner can change retention.
        </div>
      )}
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Account tab (every role)
// --------------------------------------------------------------------------- //
export function AccountView({ api, onSignedOut }: { api: Api; onSignedOut: () => void }) {
  return (
    <div>
      <DesktopSection />
      <SessionsSection api={api} onSignedOut={onSignedOut} />
      <PasswordSection api={api} onSignedOut={onSignedOut} />
    </div>
  );
}

/** Start-at-login. Only rendered inside the desktop shell, which owns the setting. */
export function DesktopSection() {
  const [available, setAvailable] = useState(false);
  const [enabled, setEnabled] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  useEffect(() => {
    invoke<boolean>("autostart_enabled")
      .then((on) => {
        setEnabled(Boolean(on));
        setAvailable(true);
      })
      .catch(() => setAvailable(false)); // a browser tab: no such setting
  }, []);

  if (!available) return null;

  async function toggle(next: boolean) {
    setErr("");
    setBusy(true);
    try {
      // Show what the OS reports afterwards, not what was asked for.
      setEnabled(Boolean(await invoke<boolean>("set_autostart", { enabled: next })));
    } catch (e) {
      setErr(errText(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card">
      <b>On this computer</b>
      <label className="row" style={{ marginTop: 10, gap: 8 }}>
        <input
          type="checkbox"
          style={{ width: "auto" }}
          checked={enabled}
          disabled={busy}
          onChange={(e) => toggle(e.target.checked)}
        />
        <span>Start Osprey in the tray when I sign in</span>
      </label>
      <div className="muted" style={{ marginTop: 6 }}>
        Osprey alerts you when an item needs action today. Closing the window keeps it running in
        the tray so those alerts still arrive; choose Quit from the tray menu to stop it.
      </div>
      {err && <div className="notice">{err}</div>}
    </div>
  );
}

export function SessionsSection({ api, onSignedOut }: { api: Api; onSignedOut: () => void }) {
  const [rows, setRows] = useState<ActiveSession[] | null>(null);
  const [err, setErr] = useState("");

  const reload = () =>
    api
      .sessions()
      .then(setRows)
      .catch((e) => setErr(errText(e)));
  useEffect(() => {
    reload();
  }, []);

  return (
    <div className="card">
      <b>Where you're signed in</b>
      <div className="muted">
        Sign out a device you don't recognise. Signing out everywhere includes this device.
      </div>
      {err && <div className="notice">{err}</div>}
      {rows && rows.length === 0 && <div className="muted">No active sessions.</div>}
      <table className="admin-table">
        <tbody>
          {rows?.map((s) => (
            <tr key={s.id}>
              <td>
                <div title={s.user_agent}>{describeAgent(s.user_agent)}</div>
                <div className="muted">{s.ip || "unknown address"}</div>
              </td>
              <td className="muted">since {when(s.created_at)}</td>
              <td className="actions">
                <button
                  onClick={async () => {
                    setErr("");
                    try {
                      await api.revokeSession(s.id);
                    } catch (e) {
                      setErr(errText(e));
                    }
                    await reload();
                  }}
                >
                  Sign out
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="row" style={{ marginTop: 10 }}>
        <div className="spacer" />
        <ConfirmButton
          label="Sign out everywhere"
          confirmLabel="Sign out of every device"
          onConfirm={async () => {
            setErr("");
            try {
              await api.logoutAll();
              onSignedOut();
            } catch (e) {
              setErr(errText(e));
            }
          }}
        />
      </div>
    </div>
  );
}

export function PasswordSection({ api, onSignedOut }: { api: Api; onSignedOut: () => void }) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [err, setErr] = useState("");
  const mismatch = confirm !== "" && next !== confirm;

  async function submit() {
    setErr("");
    try {
      await api.changePassword(current, next);
      // The server revokes every session, this one included, so a stale stolen
      // token cannot outlive the change. Return to sign-in rather than fail later.
      onSignedOut();
    } catch (e) {
      setErr(errText(e));
    }
  }

  return (
    <div className="card">
      <b>Change password</b>
      <div className="muted">
        You'll be signed out of every device, including this one. Accounts that use single sign-on
        change their password with the identity provider.
      </div>
      <div className="grid2" style={{ marginTop: 10 }}>
        <input
          type="password"
          placeholder="Current password"
          value={current}
          onChange={(e) => setCurrent(e.target.value)}
        />
        <div />
        <input type="password" placeholder="New password" value={next} onChange={(e) => setNext(e.target.value)} />
        <input
          type="password"
          placeholder="Confirm new password"
          value={confirm}
          onChange={(e) => setConfirm(e.target.value)}
        />
      </div>
      {mismatch && <div className="notice">The new passwords don't match.</div>}
      {err && <div className="notice">{err}</div>}
      <div className="row" style={{ marginTop: 10 }}>
        <div className="spacer" />
        <button className="primary" disabled={!current || !next || next !== confirm} onClick={submit}>
          Change password
        </button>
      </div>
    </div>
  );
}

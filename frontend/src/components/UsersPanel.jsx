import { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";
import { cx, fmtDate } from "../fmt.js";

/**
 * Admin user management: list accounts, create one, reset passwords,
 * flip roles, disable/enable. Everything goes through /api/users — the
 * server enforces admin rights, so a non-admin landing here by URL just
 * sees its 403 surfaced as an error banner.
 *
 * `me` is the signed-in account ({username, role}) or null in single-user
 * mode, where the local operator is the admin: creating the first account
 * bootstraps multi-user mode (the server mints the session; the panel then
 * calls onIdentityChange() so the shell picks up the new identity).
 */
export default function UsersPanel({ me, multiUser, onIdentityChange, onBack }) {
  const [users, setUsers] = useState(null); // null = loading
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState(null);

  // create form
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState("user");

  // per-row "new password" inputs, keyed by username
  const [newPw, setNewPw] = useState({});

  const refresh = useCallback(() => {
    api.listUsers()
      .then((r) => setUsers(r.users || []))
      .catch((e) => setError(String(e.message || e)));
  }, []);
  useEffect(() => { refresh(); }, [refresh]);

  const run = (label, fn) =>
    fn().then(() => {
      setError(null);
      setNotice(label);
      refresh();
    }, (e) => {
      setNotice(null);
      setError(String(e.message || e));
    });

  const create = (e) => {
    e.preventDefault();
    run(`Created ${username.trim()} (${role})`, () =>
      api.createUser(username.trim(), password, role)
    ).then(() => {
      setUsername("");
      setPassword("");
      // bootstrap: the first account flips the server into multi-user mode
      // and carries us over as the new admin — re-probe the identity
      if (!multiUser && onIdentityChange) onIdentityChange();
    });
  };

  const resetPw = (u) => {
    const pw = (newPw[u.username] || "").trim();
    if (!pw) return;
    run(`Password reset for ${u.username} — their sessions were revoked`, () =>
      api.resetUserPassword(u.username, pw)
    ).then(() => setNewPw((p) => ({ ...p, [u.username]: "" })));
  };

  const toggleRole = (u) =>
    run(`${u.username} is now ${u.role === "admin" ? "user" : "admin"}`, () =>
      api.setUserRole(u.username, u.role === "admin" ? "user" : "admin"));

  const toggleDisabled = (u) =>
    run(u.disabled ? `Enabled ${u.username}` : `Disabled ${u.username}`, () =>
      u.disabled ? api.enableUser(u.username) : api.disableUser(u.username));

  return (
    <div className="flow users-view">
      <div className="users-head">
        <h2>
          👤 Users{" "}
          <span className="muted">
            ({users == null ? "…" : users.length})
          </span>
        </h2>
        <button className="btn ghost sm" onClick={onBack}>← Back</button>
      </div>

      {!multiUser && (
        <p className="muted">
          No accounts yet — the server runs in single-user mode (one shared
          environment). Creating the first admin switches it to multi-user
          mode: private scripts, per-user job history, this page for
          management. You&apos;ll stay signed in as that admin.
        </p>
      )}

      <form className="card" onSubmit={create}>
        <div className="users-form-row">
          <input
            type="text"
            placeholder="username"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            title="Letters, digits, '-' or '_' — it becomes their private scripts directory name"
            required
          />
          <input
            type="password"
            placeholder="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
          />
          <select
            value={!multiUser ? "admin" : role}
            onChange={(e) => setRole(e.target.value)}
            title={!multiUser
              ? "The first account must be an admin"
              : "Regular users run scripts; admins also manage users"}
          >
            <option value="user">user</option>
            <option value="admin">admin</option>
          </select>
          <button className="btn primary" type="submit"
                  disabled={!username.trim() || !password}>Create</button>
        </div>
      </form>

      {error && <div className="error-banner" role="alert">{error}</div>}
      {notice && <div className="ok-banner" role="status">{notice}</div>}

      <section className="card">
        <table className="users-table">
          <thead>
            <tr>
              <th>User</th><th>Role</th><th>Created</th><th>Status</th>
              <th className="row-actions"></th>
            </tr>
          </thead>
          <tbody>
            {users && users.map((u) => (
              <tr key={u.username} className={cx(u.disabled && "paused-row")}>
                <td>
                  <strong>{u.username}</strong>
                  {me && u.username === me.username && (
                    <span className="muted small"> (you)</span>
                  )}
                </td>
                <td><span className="badge">{u.role}</span></td>
                <td className="muted">{u.created_at ? fmtDate(u.created_at) : ""}</td>
                <td>
                  {u.disabled
                    ? <span className="badge failed">disabled</span>
                    : <span className="badge completed">active</span>}
                </td>
                <td className="row-actions">
                  <input
                    type="password"
                    placeholder="new password"
                    value={newPw[u.username] || ""}
                    onChange={(e) => setNewPw((p) => ({ ...p, [u.username]: e.target.value }))}
                    onKeyDown={(e) => e.key === "Enter" && resetPw(u)}
                    title="Set a new password and revoke their sessions"
                  />
                  <button className="btn sm" onClick={() => resetPw(u)}
                          disabled={!(newPw[u.username] || "").trim()}>Reset pw</button>
                  <button className="btn ghost sm" onClick={() => toggleRole(u)}
                          title={u.role === "admin" ? "Demote to a regular user" : "Promote to admin"}>
                    {u.role === "admin" ? "↓ user" : "↑ admin"}
                  </button>
                  <button className="btn ghost sm" onClick={() => toggleDisabled(u)}
                          title={u.disabled ? "Restore access" : "Revoke access and kill their sessions"}>
                    {u.disabled ? "Enable" : "Disable"}
                  </button>
                </td>
              </tr>
            ))}
            {users && users.length === 0 && (
              <tr><td colSpan={5} className="muted">No accounts.</td></tr>
            )}
          </tbody>
        </table>
      </section>
    </div>
  );
}

import { useState } from "react";
import { api } from "../api.js";

export default function LoginView({ multiUser = false, onSuccess }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.login(username, password);
      setPassword("");
      onSuccess();
    } catch (err) {
      setError(String(err.message || err));
    } finally {
      setBusy(false);
    }
  };

  const canSubmit = multiUser ? username.trim() && password : password;

  return (
    <div className="login-wrap">
      <form className="card login-card" onSubmit={submit}>
        <div className="brand login-brand">
          <span className="brand-mark">▤</span> ScriptEngine
        </div>
        <p className="muted">
          {multiUser ? "Sign in with your account." : "This server requires a password (SCRIPTENGINE_TOKEN)."}
        </p>
        {multiUser && (
          <input
            type="text"
            placeholder="Username"
            autoComplete="username"
            autoFocus
            value={username}
            onChange={(e) => setUsername(e.target.value)}
          />
        )}
        <input
          type="password"
            placeholder="Password"
            autoComplete="current-password"
            autoFocus={!multiUser}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        {error && <div className="login-error">{error}</div>}
        <button className="btn primary" type="submit" disabled={busy || !canSubmit}>
          {busy ? "Checking…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}

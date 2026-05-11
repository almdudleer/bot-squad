import { useState } from "react";
import { useNavigate, Link } from "react-router-dom";
import { api } from "../api";

export function Login() {
  const navigate = useNavigate();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await api.login(username, password);
      navigate("/");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mc-login-page">
      <div className="mc-login-box">
        <div className="mc-login-wordmark">BOT-SQUAD</div>
        <div className="mc-login-subtitle">mission control — sign in to continue</div>

        <form onSubmit={submit}>
          <div className="mb-3">
            <label className="form-label">Username</label>
            <input
              type="text"
              className="form-control"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoFocus
              required
              autoComplete="username"
            />
          </div>
          <div className="mb-3">
            <label className="form-label">Password</label>
            <input
              type="password"
              className="form-control"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
              autoComplete="current-password"
            />
          </div>
          {error && (
            <div className="alert alert-danger mb-3" style={{ fontSize: "0.8rem" }}>
              {error}
            </div>
          )}
          <button
            className="btn btn-primary w-100"
            disabled={busy}
            style={{ marginTop: "0.25rem" }}
          >
            {busy ? "Signing in…" : "Sign in"}
          </button>
        </form>

        <div style={{ marginTop: "1.25rem", textAlign: "center" }}>
          <Link
            to="/help"
            style={{ fontSize: "0.75rem", color: "var(--mc-text-dim)" }}
          >
            Usage manual
          </Link>
        </div>
      </div>
    </div>
  );
}

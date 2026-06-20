import { useEffect, useState } from "react";
import { api, UserRow } from "../api";
import { Modal } from "../components/Modal";

interface NewUserState {
  username: string;
  password: string;
  linux_user: string;
  is_admin: boolean;
}

interface ResetState {
  username: string;
  password: string;
}

interface EditLinuxState {
  username: string;
  linux_user: string;
}

// T-0323: the whole /users router is Depends(require_admin), so a non-admin
// who direct-navs here gets a 403. We key off the status code (not the body
// text) so a BE message change doesn't silently break the gate. Mirrors
// mothership/Users.tsx's isAccessDeniedError helper. call() wraps non-OK
// responses as ``API error <status>: <body>``.
function isAccessDeniedError(err: unknown): boolean {
  if (err instanceof Error) {
    return /^API error 403\b/.test(err.message);
  }
  return false;
}

export function Users() {
  const [users, setUsers] = useState<UserRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [denied, setDenied] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  const [creating, setCreating] = useState<NewUserState | null>(null);
  const [createError, setCreateError] = useState<string | null>(null);
  const [creatingSaving, setCreatingSaving] = useState(false);

  const [resetting, setResetting] = useState<ResetState | null>(null);
  const [resetError, setResetError] = useState<string | null>(null);
  const [resetSaving, setResetSaving] = useState(false);

  const [editingLinux, setEditingLinux] = useState<EditLinuxState | null>(null);
  const [editLinuxError, setEditLinuxError] = useState<string | null>(null);
  const [editLinuxSaving, setEditLinuxSaving] = useState(false);

  function reload() {
    setError(null);
    api
      .listUsers()
      .then(setUsers)
      .catch((e) => {
        // T-0323: render a friendly denied state instead of dumping the raw
        // "API error 403" string when a non-admin lands here directly.
        if (isAccessDeniedError(e)) {
          setDenied(true);
          return;
        }
        setError(String(e));
      });
  }

  useEffect(() => {
    reload();
  }, []);

  async function submitCreate() {
    if (!creating) return;
    if (!creating.username.trim() || !creating.password) {
      setCreateError("username and password required");
      return;
    }
    setCreatingSaving(true);
    setCreateError(null);
    try {
      await api.createUser(
        creating.username.trim(),
        creating.password,
        creating.linux_user.trim() || undefined,
        creating.is_admin,
      );
      setCreating(null);
      setNotice(`User ${creating.username} created.`);
      reload();
    } catch (e) {
      setCreateError(String(e));
    } finally {
      setCreatingSaving(false);
    }
  }

  async function submitReset() {
    if (!resetting) return;
    if (!resetting.password) {
      setResetError("password required");
      return;
    }
    setResetSaving(true);
    setResetError(null);
    try {
      await api.resetUserPassword(resetting.username, resetting.password);
      setResetting(null);
      setNotice(`Password reset for ${resetting.username}.`);
    } catch (e) {
      setResetError(String(e));
    } finally {
      setResetSaving(false);
    }
  }

  async function submitEditLinux() {
    if (!editingLinux) return;
    if (!editingLinux.linux_user.trim()) {
      setEditLinuxError("linux_user required");
      return;
    }
    setEditLinuxSaving(true);
    setEditLinuxError(null);
    try {
      await api.patchUser(editingLinux.username, { linux_user: editingLinux.linux_user.trim() });
      setEditingLinux(null);
      setNotice(`Updated linux_user for ${editingLinux.username}.`);
      reload();
    } catch (e) {
      setEditLinuxError(String(e));
    } finally {
      setEditLinuxSaving(false);
    }
  }

  async function toggleAdmin(u: UserRow) {
    const want = !u.is_admin;
    const msg = want
      ? `Grant admin to ${u.username}?`
      : `Remove admin from ${u.username}?`;
    if (!window.confirm(msg)) return;
    try {
      await api.patchUser(u.username, { is_admin: want });
      setNotice(`${u.username} is now ${want ? "admin" : "not admin"}.`);
      reload();
    } catch (e) {
      setError(String(e));
    }
  }

  async function deleteUser(u: UserRow) {
    if (!window.confirm(`Delete user ${u.username}? This cannot be undone.`)) return;
    try {
      await api.deleteUser(u.username);
      setNotice(`Deleted ${u.username}.`);
      reload();
    } catch (e) {
      setError(String(e));
    }
  }

  if (denied) {
    return (
      <div className="container py-4" style={{ maxWidth: "860px" }}>
        <div className="mc-empty" data-testid="users-denied">
          <div className="mc-empty-icon">◇</div>
          <div>You don't have permission to view users.</div>
        </div>
      </div>
    );
  }

  return (
    <div className="container py-4" style={{ maxWidth: "860px" }}>
      <div className="d-flex justify-content-between align-items-center mb-3">
        <h2 style={{ fontSize: "1rem", fontWeight: 600, margin: 0 }}>Users</h2>
        <button
          type="button"
          className="btn btn-outline-primary btn-sm"
          style={{ fontSize: "0.72rem" }}
          onClick={() =>
            setCreating({
              username: "",
              password: "",
              linux_user: "",
              is_admin: false,
            })
          }
        >
          + New user
        </button>
      </div>

      {error && <div className="alert alert-danger">{error}</div>}
      {notice && <div className="alert alert-success py-2">{notice}</div>}

      {users === null && !error && <div className="mc-loading">Loading</div>}

      {users && (
        <table className="table table-sm align-middle">
          <thead>
            <tr style={{ fontSize: "0.72rem", textTransform: "uppercase", color: "var(--mc-text-dim)" }}>
              <th>Username</th>
              <th>Linux user</th>
              <th>Admin</th>
              <th style={{ width: "1%" }}>Actions</th>
            </tr>
          </thead>
          <tbody>
            {users.map((u) => (
              <tr key={u.username}>
                <td style={{ fontFamily: "var(--mc-mono)" }}>{u.username}</td>
                <td style={{ fontFamily: "var(--mc-mono)" }}>{u.linux_user}</td>
                <td>{u.is_admin ? "yes" : "no"}</td>
                <td>
                  <div className="d-flex gap-1">
                    <button
                      type="button"
                      className="btn btn-outline-secondary btn-sm"
                      style={{ fontSize: "0.7rem" }}
                      onClick={() => setResetting({ username: u.username, password: "" })}
                    >
                      Reset password
                    </button>
                    <button
                      type="button"
                      className="btn btn-outline-secondary btn-sm"
                      style={{ fontSize: "0.7rem" }}
                      onClick={() => toggleAdmin(u)}
                    >
                      {u.is_admin ? "Remove admin" : "Make admin"}
                    </button>
                    <button
                      type="button"
                      className="btn btn-outline-secondary btn-sm"
                      style={{ fontSize: "0.7rem" }}
                      onClick={() => setEditingLinux({ username: u.username, linux_user: u.linux_user })}
                    >
                      Edit linux user
                    </button>
                    <button
                      type="button"
                      className="btn btn-outline-danger btn-sm"
                      style={{ fontSize: "0.7rem" }}
                      onClick={() => deleteUser(u)}
                    >
                      Delete
                    </button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <Modal
        open={creating !== null}
        title="New user"
        onClose={() => setCreating(null)}
        footer={
          <>
            <button type="button" className="btn btn-secondary" onClick={() => setCreating(null)}>
              Cancel
            </button>
            <button
              type="button"
              className="btn btn-primary"
              onClick={submitCreate}
              disabled={creatingSaving}
            >
              {creatingSaving ? "Creating…" : "Create"}
            </button>
          </>
        }
      >
        {createError && <div className="alert alert-danger">{createError}</div>}
        <div className="mb-3">
          <label className="form-label">Username *</label>
          <input
            className="form-control"
            value={creating?.username ?? ""}
            onChange={(e) => creating && setCreating({ ...creating, username: e.target.value })}
            autoFocus
          />
        </div>
        <div className="mb-3">
          <label className="form-label">Password *</label>
          <input
            type="password"
            className="form-control"
            value={creating?.password ?? ""}
            onChange={(e) => creating && setCreating({ ...creating, password: e.target.value })}
          />
        </div>
        <div className="mb-3">
          <label className="form-label">Linux user</label>
          <input
            className="form-control"
            value={creating?.linux_user ?? ""}
            onChange={(e) => creating && setCreating({ ...creating, linux_user: e.target.value })}
            placeholder="defaults to username"
          />
        </div>
        <div className="form-check mb-3">
          <input
            id="new-user-admin"
            type="checkbox"
            className="form-check-input"
            checked={creating?.is_admin ?? false}
            onChange={(e) => creating && setCreating({ ...creating, is_admin: e.target.checked })}
          />
          <label className="form-check-label" htmlFor="new-user-admin">
            Admin
          </label>
        </div>
      </Modal>

      <Modal
        open={resetting !== null}
        title={resetting ? `Reset password — ${resetting.username}` : ""}
        onClose={() => setResetting(null)}
        footer={
          <>
            <button type="button" className="btn btn-secondary" onClick={() => setResetting(null)}>
              Cancel
            </button>
            <button
              type="button"
              className="btn btn-primary"
              onClick={submitReset}
              disabled={resetSaving}
            >
              {resetSaving ? "Saving…" : "Save"}
            </button>
          </>
        }
      >
        {resetError && <div className="alert alert-danger">{resetError}</div>}
        <div className="mb-3">
          <label className="form-label">New password *</label>
          <input
            type="password"
            className="form-control"
            value={resetting?.password ?? ""}
            onChange={(e) => resetting && setResetting({ ...resetting, password: e.target.value })}
            autoFocus
          />
        </div>
      </Modal>

      <Modal
        open={editingLinux !== null}
        title={editingLinux ? `Edit linux user — ${editingLinux.username}` : ""}
        onClose={() => setEditingLinux(null)}
        footer={
          <>
            <button type="button" className="btn btn-secondary" onClick={() => setEditingLinux(null)}>
              Cancel
            </button>
            <button
              type="button"
              className="btn btn-primary"
              onClick={submitEditLinux}
              disabled={editLinuxSaving}
            >
              {editLinuxSaving ? "Saving…" : "Save"}
            </button>
          </>
        }
      >
        {editLinuxError && <div className="alert alert-danger">{editLinuxError}</div>}
        <div className="mb-3">
          <label className="form-label">Linux user *</label>
          <input
            className="form-control"
            value={editingLinux?.linux_user ?? ""}
            onChange={(e) =>
              editingLinux && setEditingLinux({ ...editingLinux, linux_user: e.target.value })
            }
            autoFocus
          />
        </div>
      </Modal>
    </div>
  );
}

/**
 * T-0061 — worker controls placeholder.
 *
 * Real start/stop/restart wiring waits on T-0067 (per-user systemd-user
 * worker enablement). The systemd-user template + ``attach_hooks.py``
 * landed with Bundle D, but the FE controls that drive ``systemctl
 * --user start/stop`` from the browser aren't in scope for Bundle C —
 * they need an authenticated worker action that runs as the right
 * linux_user, which is a follow-up.
 *
 * The page exists today so the ATTACHMENT section nav item routes
 * somewhere meaningful instead of falling through to the SPA catch-all
 * (which redirects to /).
 */

export function AttachmentWorker() {
  return (
    <div className="container py-4" style={{ maxWidth: "640px" }}>
      <h2 style={{ fontSize: "1rem", fontWeight: 600, marginBottom: "0.5rem" }}>
        Worker controls
      </h2>
      <p
        style={{
          fontSize: "0.85rem",
          color: "var(--mc-text-dim)",
          marginBottom: "1rem",
        }}
      >
        Start, stop, and view logs for your per-user worker on this server.
      </p>
      <div className="alert alert-info" style={{ fontSize: "0.85rem" }}>
        Coming with T-0067 enablement. The per-user worker template + attach
        hook landed in Bundle D — the browser-side controls that drive
        <code> systemctl --user </code>
        need a worker action that runs as the right linux_user, tracked
        separately.
      </div>
    </div>
  );
}

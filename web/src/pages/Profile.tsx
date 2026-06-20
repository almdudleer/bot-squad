/**
 * /me — global cross-server profile (T-0059).
 *
 * T-0061 moved the Telegram-chat-id binding row OUT of this page and INTO
 * the per-attachment surface at /attachment/tg-binding. T-0170 deleted the
 * sidebar "Attachment" section entirely (the stakeholder found the name
 * meaningless) and re-homed its personal-scope children HERE: this page is
 * the user's "my settings on this server" hub. The links below reach the
 * per-user-per-server surfaces (TG binding, my sessions); the global
 * account identity (username + linux_user + admin flag) stays
 * inline. Future GlobalUser fields from T-0066 (display_name, email,
 * timezone) plug in here when their editing UI lands.
 */
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, MeProfile } from "../api";

// Only surfaces that are actually built belong here — /me must not advertise
// a dead page as a peer of the working settings. (T-0342 removed the
// not-yet-built "Worker controls" teaser; re-add it when T-0067 wires the
// per-user start/stop/logs controls.)
const PER_SERVER_LINKS: {
  to: string;
  label: string;
  blurb: string;
}[] = [
  {
    to: "/attachment/tg-binding",
    label: "Telegram binding",
    blurb: "Personal chat ID for task notifications + test pings.",
  },
  {
    to: "/attachment/sessions",
    label: "My sessions",
    blurb: "Your own agent sessions on this server.",
  },
];

export function Profile() {
  const [profile, setProfile] = useState<MeProfile | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .getMyProfile()
      .then(setProfile)
      .catch((e) => setError(String(e)));
  }, []);

  return (
    <div className="container py-4" style={{ maxWidth: "640px" }}>
      <h2 style={{ fontSize: "1rem", fontWeight: 600, marginBottom: "0.5rem" }}>
        My profile
      </h2>
      <p
        style={{
          fontSize: "0.75rem",
          color: "var(--mc-text-dim)",
          marginBottom: "1rem",
        }}
      >
        Your global account identity applies across all bot-squad servers
        you&apos;re attached to. Per-server settings (Telegram chat ID, your
        sessions) are linked below.
      </p>

      {error && <div className="alert alert-danger">{error}</div>}
      {profile === null && !error && <div className="mc-loading">Loading</div>}

      {profile && (
        <section className="mb-4">
          <div style={{ fontSize: "0.8rem", color: "var(--mc-text-dim)" }}>
            {profile.username}
            {profile.linux_user !== profile.username
              ? ` (${profile.linux_user})`
              : ""}
            {profile.is_admin ? " — admin" : ""}
          </div>
        </section>
      )}

      {/* T-0170: per-user-per-server controls, re-homed from the deleted
          sidebar "Attachment" section. */}
      <section>
        <h3
          style={{
            fontSize: "0.72rem",
            fontWeight: 600,
            textTransform: "uppercase",
            letterSpacing: "0.1em",
            color: "var(--mc-text-faint)",
            marginBottom: "0.6rem",
          }}
        >
          Per-server settings
        </h3>
        <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: "0.5rem" }}>
          {PER_SERVER_LINKS.map((l) => {
            const boxStyle = {
              display: "block",
              padding: "0.6rem 0.8rem",
              border: "1px solid var(--mc-border)",
              borderRadius: 3,
              textDecoration: "none",
              background: "var(--mc-surface)",
            } as const;
            return (
              <li key={l.to}>
                <Link to={l.to} style={boxStyle}>
                  <div style={{ fontSize: "0.85rem", color: "var(--mc-text)" }}>
                    {l.label} →
                  </div>
                  <div
                    style={{ fontSize: "0.72rem", color: "var(--mc-text-dim)" }}
                  >
                    {l.blurb}
                  </div>
                </Link>
              </li>
            );
          })}
        </ul>
      </section>
    </div>
  );
}

/**
 * /me — global cross-server profile (T-0059).
 *
 * T-0061 moved the Telegram-chat-id binding row OUT of this page and INTO
 * the per-attachment surface at /attachment/tg-binding, since TG bindings
 * are per-user-per-server (Attachment store, T-0066). What stays here is
 * the global account identity: username + linux_user + admin flag. Future
 * GlobalUser fields from T-0066 (display_name, email, timezone) plug in
 * here when their editing UI lands.
 */
import { useEffect, useState } from "react";
import { api, MeProfile } from "../api";

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
        These settings apply across all bot-squad servers you&apos;re attached
        to. Per-server bindings (Telegram chat ID, worker controls) live
        under the ATTACHMENT section.
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
    </div>
  );
}

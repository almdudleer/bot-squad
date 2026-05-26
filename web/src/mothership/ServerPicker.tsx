/**
 * T-0060 — server picker dropdown for the SERVER section header. Mothership
 * build only. Lives in `mothership/` so the bundle tree-shakes on detach
 * builds (mirror of mothership/routes.tsx posture, gated in Shell.tsx via
 * `import.meta.env.VITE_MOTHERSHIP === "1"`).
 *
 * Reads /api/m/servers, renders a `▾ <picked>` trigger, persists the
 * selection to localStorage under `SERVER_PICKER_STORAGE_KEY` so the user
 * lands on their preferred server next session.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { mothershipApi, type AttachedServer } from "./api";
import {
  resolveInitialPickedServer,
  SERVER_PICKER_STORAGE_KEY,
} from "../components/sidebarHelpers";

export type ServerPickerProps = {
  /** Server id of "where the user currently is" (derived from URL/route
   *  in Shell). Used to seed the picker when no stored selection wins. */
  currentServerId: string | null;
  /** Called with the resolved id whenever it changes — Shell uses this to
   *  re-scope SERVER subitems + ATTACHMENT contents. */
  onChange: (serverId: string | null) => void;
};

export function ServerPicker({ currentServerId, onChange }: ServerPickerProps) {
  const [servers, setServers] = useState<AttachedServer[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [picked, setPicked] = useState<string | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);

  const close = useCallback(() => setOpen(false), []);

  // Initial fetch + initial pick.
  useEffect(() => {
    let cancelled = false;
    mothershipApi
      .listServers()
      .then((rows) => {
        if (cancelled) return;
        setServers(rows);
        const stored = (() => {
          try {
            return localStorage.getItem(SERVER_PICKER_STORAGE_KEY);
          } catch {
            return null;
          }
        })();
        const resolved = resolveInitialPickedServer(
          stored,
          currentServerId,
          rows.map((s) => ({ id: s.id, isSelf: s.is_self })),
        );
        setPicked(resolved);
        onChange(resolved);
      })
      .catch((e) => {
        if (!cancelled) setError(String(e));
      });
    return () => {
      cancelled = true;
    };
    // currentServerId is a route hint — only seeds the first pick; user
    // edits via the dropdown after that. onChange is stable in the caller.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Dismiss on outside click / Escape.
  useEffect(() => {
    if (!open) return;
    function onDocClick(e: MouseEvent) {
      if (!rootRef.current) return;
      if (!rootRef.current.contains(e.target as Node)) close();
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") close();
    }
    document.addEventListener("mousedown", onDocClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDocClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [open, close]);

  function pick(id: string) {
    setPicked(id);
    try {
      localStorage.setItem(SERVER_PICKER_STORAGE_KEY, id);
    } catch {
      /* ignore quota / disabled */
    }
    close();
    onChange(id);
  }

  const pickedServer = servers?.find((s) => s.id === picked) ?? null;
  const label =
    error !== null
      ? "?"
      : servers === null
        ? "…"
        : pickedServer
          ? pickedServer.display_name
          : "no server";

  return (
    <div className="mc-srv-picker" ref={rootRef}>
      <button
        type="button"
        className="mc-srv-picker-trigger"
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        ▾ <span className="mc-srv-picker-label">{label}</span>
      </button>
      {open && servers && (
        <div className="mc-srv-picker-panel" role="listbox">
          {servers.length === 0 ? (
            <div className="mc-srv-picker-empty">No servers attached.</div>
          ) : (
            <ul className="mc-srv-picker-list">
              {servers.map((s) => (
                <li key={s.id}>
                  <button
                    type="button"
                    className={
                      "mc-srv-picker-row" +
                      (s.id === picked ? " mc-srv-picker-row-current" : "")
                    }
                    onClick={() => pick(s.id)}
                  >
                    {s.display_name}
                    {s.is_self && (
                      <span
                        className="mc-badge mc-badge-info"
                        style={{ marginLeft: "0.5rem" }}
                      >
                        this server
                      </span>
                    )}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

export default ServerPicker;

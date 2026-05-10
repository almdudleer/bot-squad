/**
 * Loads Telegram's Login Widget script and exposes a function to render
 * the widget into a given container. The widget posts the auth payload
 * to our /api/auth/tg endpoint via the onTelegramAuth callback.
 */
declare global {
  interface Window {
    onTelegramAuth?: (user: Record<string, unknown>) => void;
  }
}

export function loadTelegramWidget(
  botUsername: string,
  containerId: string,
  onAuth: (user: Record<string, unknown>) => void
): void {
  window.onTelegramAuth = onAuth;
  const script = document.createElement("script");
  script.async = true;
  script.src = "https://telegram.org/js/telegram-widget.js?22";
  script.setAttribute("data-telegram-login", botUsername);
  script.setAttribute("data-size", "large");
  script.setAttribute("data-onauth", "onTelegramAuth(user)");
  script.setAttribute("data-request-access", "write");
  document.getElementById(containerId)!.appendChild(script);
}

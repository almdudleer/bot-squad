import { useEffect, useRef } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import { loadTelegramWidget } from "../auth";

const BOT_USERNAME = "watchbot";   // matches BotFather username for token 8036906248

export function Login() {
  const navigate = useNavigate();
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!ref.current) return;
    if (ref.current.querySelector("script")) return;
    ref.current.id = "tg-login";
    loadTelegramWidget(BOT_USERNAME, "tg-login", async (user) => {
      try {
        await api.loginTg(user);
        navigate("/");
      } catch (e) {
        alert(`Login failed: ${e instanceof Error ? e.message : e}`);
      }
    });
  }, [navigate]);

  return (
    <div className="container py-5">
      <div className="row justify-content-center">
        <div className="col-md-6">
          <h1 className="mb-3">bot-squad</h1>
          <p className="text-muted">Sign in with Telegram to continue.</p>
          <div ref={ref} />
        </div>
      </div>
    </div>
  );
}

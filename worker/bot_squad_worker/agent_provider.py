"""Agent CLI providers used by the session manager.

The session registry deliberately keeps its existing ``claude_uuid`` field for
backward compatibility; it is an agent-session UUID when ``provider=codex``.
New code should use the neutral helpers in this module rather than spelling a
CLI command or transcript location directly.
"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path


CLAUDE = "claude"
CODEX = "codex"
DEFAULT_PROVIDER = CLAUDE
PROVIDERS = frozenset({CLAUDE, CODEX})
CODEX_MODEL_ALIASES = {
    "sol": "gpt-5.6-sol",
    "terra": "gpt-5.6-terra",
    "luna": "gpt-5.6-luna",
}
CODEX_MODELS = frozenset(CODEX_MODEL_ALIASES.values())

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class AgentProvider:
    name: str
    executable: str
    composer_markers: tuple[str, ...]
    # T-0904: does this CLI fire bot-squad's ``SessionStart`` hook
    # (``scripts/hooks/session_start.sh``, wired in ``.claude/settings.json``)?
    # That hook is where a spawned session is TOLD it is a bot-squad session —
    # the bsq CLI, its SID, and the peer bus's "check mail" -> ``bsq inbox
    # check`` mapping. Claude Code fires it; Codex has no hooks section in
    # ``~/.codex/config.toml`` and no equivalent, so nothing calls it and a
    # codex session boots with ZERO bot-squad orientation. False here means the
    # orientation has to ride the one channel that reaches every provider — the
    # composer-delivered prompt (see ``boot_orientation``).
    #
    # THE DEFAULT IS FALSE ON PURPOSE. Firing bot-squad's hook is a property of
    # exactly one CLI today and every provider added later is, by definition,
    # not that one — so a provider whose author forgets this field gets the
    # orientation (at worst a preamble a hooked CLI would have duplicated)
    # rather than silence, which is the whole of T-0904. ``ClaudeProvider``
    # therefore states ``True`` explicitly instead of inheriting it.
    runs_session_start_hook: bool = False

    def command_matches(self, command: str) -> bool:
        return command == self.executable

    def process_matches(self, argv0: str) -> bool:
        return argv0 == self.executable or argv0.endswith(f"/{self.executable}")

    def launch_command(
        self,
        *,
        resume_id: str | None = None,
        model: str = "",
        display_name: str = "",
        initial_prompt: str | None = None,
        effort: str = "",
    ) -> str:
        raise NotImplementedError

    def discover_session_id(self, cwd: str, user_home: str) -> str | None:
        raise NotImplementedError

    def transcript_path(
        self, cwd: str, session_id: str, user_home: str
    ) -> Path | None:
        raise NotImplementedError

    def session_ids(self, cwd: str, user_home: str) -> set[str]:
        found = self.discover_session_id(cwd, user_home)
        return {found} if found else set()


class ClaudeProvider(AgentProvider):
    def __init__(self) -> None:
        super().__init__(CLAUDE, "claude", ("❯",),
                         runs_session_start_hook=True)

    def command_matches(self, command: str) -> bool:
        # Claude's version-managed binary can be reported by tmux as 2.1.220.
        return super().command_matches(command) or bool(
            re.match(r"^\d+\.\d+\.\d+$", command or "")
        )

    def launch_command(
        self,
        *,
        resume_id: str | None = None,
        model: str = "",
        display_name: str = "",
        initial_prompt: str | None = None,
        effort: str = "",
    ) -> str:
        del initial_prompt  # delivered after the Claude composer is ready
        cmd = "claude --dangerously-skip-permissions"
        if resume_id:
            cmd += f" --resume {shlex.quote(resume_id)}"
        if model:
            cmd += f" --model {shlex.quote(model)}"
        # T-0871: reasoning effort, stated by bot-squad rather than inherited.
        # Absent, `claude` applies its own per-model `default_effort` — a
        # vendor-owned table (v2.1.227 ships claude-opus-4-7 -> xhigh), so a
        # model swap or a CLI update can move the whole fleet up a tier with
        # no change on our side and no signal. The value reaching here has
        # already passed fleet_model.resolve_effort (validated + clamped).
        if effort:
            cmd += f" --effort {shlex.quote(effort)}"
        if display_name:
            cmd += f" --name {shlex.quote(display_name)}"
        return cmd

    def discover_session_id(self, cwd: str, user_home: str) -> str | None:
        directory = Path(user_home) / ".claude" / "projects" / cwd.replace("/", "-")
        try:
            files = list(directory.glob("*.jsonl"))
            return max(files, key=lambda p: p.stat().st_mtime).stem if files else None
        except OSError:
            return None

    def transcript_path(
        self, cwd: str, session_id: str, user_home: str
    ) -> Path | None:
        path = (
            Path(user_home)
            / ".claude"
            / "projects"
            / cwd.replace("/", "-")
            / f"{session_id}.jsonl"
        )
        return path if path.exists() else None

    def session_ids(self, cwd: str, user_home: str) -> set[str]:
        directory = Path(user_home) / ".claude" / "projects" / cwd.replace("/", "-")
        try:
            return {path.stem for path in directory.glob("*.jsonl")}
        except OSError:
            return set()


class CodexProvider(AgentProvider):
    def __init__(self) -> None:
        # Current Codex TUI uses ›; accept Claude's rune as a harmless fallback
        # for mixed-version installs.
        super().__init__(CODEX, "codex", ("›", "❯"))

    def launch_command(
        self,
        *,
        resume_id: str | None = None,
        model: str = "",
        display_name: str = "",
        initial_prompt: str | None = None,
        effort: str = "",
    ) -> str:
        del display_name  # Codex resume is identified by UUID, not --name.
        del initial_prompt  # delivered through the length-safe tmux composer path
        del effort  # T-0871: Codex has no reasoning-effort flag.
        options = " --dangerously-bypass-approvals-and-sandbox"
        if model:
            options += f" --model {shlex.quote(model)}"
        if resume_id:
            cmd = f"codex resume{options} {shlex.quote(resume_id)}"
        else:
            cmd = f"codex{options}"
        return cmd

    @staticmethod
    def _session_files(user_home: str):
        return (Path(user_home) / ".codex" / "sessions").glob(
            "*/*/*/rollout-*.jsonl"
        )

    @staticmethod
    def _meta(path: Path) -> tuple[str | None, str | None]:
        import json

        try:
            first = path.open(encoding="utf-8").readline()
            row = json.loads(first)
            payload = row.get("payload") or {}
            return payload.get("session_id") or payload.get("id"), payload.get("cwd")
        except (OSError, ValueError, TypeError):
            return None, None

    def discover_session_id(self, cwd: str, user_home: str) -> str | None:
        candidates: list[tuple[float, str]] = []
        try:
            for path in self._session_files(user_home):
                session_id, session_cwd = self._meta(path)
                if (
                    session_id
                    and session_cwd
                    and Path(session_cwd).resolve() == Path(cwd).resolve()
                ):
                    candidates.append((path.stat().st_mtime, str(session_id)))
        except OSError:
            return None
        return max(candidates)[1] if candidates else None

    def transcript_path(
        self, cwd: str, session_id: str, user_home: str
    ) -> Path | None:
        del cwd
        try:
            for path in self._session_files(user_home):
                found, _ = self._meta(path)
                if found == session_id:
                    return path
        except OSError:
            return None
        return None

    def session_ids(self, cwd: str, user_home: str) -> set[str]:
        result: set[str] = set()
        try:
            for path in self._session_files(user_home):
                session_id, session_cwd = self._meta(path)
                if (
                    session_id
                    and session_cwd
                    and Path(session_cwd).resolve() == Path(cwd).resolve()
                ):
                    result.add(str(session_id))
        except OSError:
            return set()
        return result


_REGISTRY: dict[str, AgentProvider] = {
    CLAUDE: ClaudeProvider(),
    CODEX: CodexProvider(),
}


def get(name: str | None) -> AgentProvider:
    return _REGISTRY.get((name or "").strip().lower(), _REGISTRY[DEFAULT_PROVIDER])


def provider_for_model(
    model: str | None,
    default_provider: str = DEFAULT_PROVIDER,
    explicit_provider: str | None = None,
) -> str:
    """Resolve provider separately from its model.

    Without an explicit provider, a model cannot silently cross away from the
    configured project default. Codex's pseudo-model and Codex-specific model
    names remain convenient explicit opt-ins for backward compatibility.
    """
    requested = (explicit_provider or "").strip().lower()
    if requested:
        if requested not in PROVIDERS:
            raise ValueError(f"provider not allowed: {explicit_provider!r}")
        return requested
    value = (model or "").strip().lower()
    if value == CODEX or value in CODEX_MODEL_ALIASES or value in CODEX_MODELS:
        return CODEX
    return default_provider if default_provider in PROVIDERS else DEFAULT_PROVIDER


def is_uuid(value: str) -> bool:
    return bool(_UUID_RE.match(value or ""))

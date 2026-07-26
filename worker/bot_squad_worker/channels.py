"""Outbound channel abstraction (T-0490, M5/F5.3).

ONE interface — :class:`Channel` — hides the concrete messenger (Telegram today,
MAX stub-ready, mail later) behind a single ``send`` call. A registry/factory
(:func:`get_channel`) picks the impl per project/user config, so **adding a
channel needs no change to callers**: register a new ``Channel`` subclass under a
name and selection/config does the rest.

The same abstraction carries *technical deliveries* (deploy notifications) — see
``jobs.deploy_monitor_one``, which routes its start/finish pings through here
rather than calling ``tg.py`` directly.

Design reconciliation (voice-04): the channel is *selected* per project/user (the
factory args ``project``/``user``); the *address* travels on the send call
(``chat_id`` + optional ``topic_id``) because that is what the underlying
transports already speak. So ``get_channel(cfg, project=slug).send(text,
chat_id=..., ...)`` reads as "for this project's channel, send this to this
address" without the caller knowing which messenger answers.

TG/MAX impls delegate to the existing ``actions._get_tg_client`` /
``actions._get_max_client`` singletons — NOT a second client — so the lazy
client construction, the per-install config (token/proxy/quiet-hours/debounce),
and existing test monkeypatch points remain the single source of truth.

RECEIVE SEAM: :meth:`Channel.receive` is an interface method ONLY. It is
deliberately NOT wired into the inbound poller (``tg_listener``); the inbound
lane (T-0489/T-0492) owns that integration. It is here so a future channel
declares its inbound contract in one place; calling it today raises
``NotImplementedError``.
"""
from __future__ import annotations

import abc
import logging
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from bot_squad_worker.config import Config

log = logging.getLogger(__name__)

# Default transport when config/selection names none. TG is the project-bound
# deploy channel (a project carries ``tg_chat``), so it is the safe default for
# technical deliveries.
DEFAULT_CHANNEL = "tg"


class Channel(abc.ABC):
    """A single outbound (and, via the seam, inbound) messenger interface.

    Subclasses set :attr:`name` and implement :meth:`send`. They MAY override
    :meth:`receive` once the inbound lane wires it; until then it raises.
    """

    #: short registry/identity name, e.g. ``"tg"`` / ``"max"`` / ``"mail"``.
    name: str = ""

    @abc.abstractmethod
    def send(
        self,
        text: str,
        *,
        chat_id: str = "",
        sid: str = "",
        user: str = "",
        urgent: bool = False,
        topic_id: int | None = None,
        **extra: Any,
    ) -> bool:
        """Deliver ``text`` to ``chat_id`` on this channel.

        Returns True if sent, False if suppressed (empty token, debounce, quiet
        hours). Raises on transport/API errors — callers that must not block on a
        messenger outage wrap this (see ``jobs.deploy_monitor_one``).

        ``topic_id`` is the TG forum-thread id; channels that have no notion of
        threads ignore it. ``**extra`` lets a transport accept its own optional
        addressing (e.g. MAX ``recipient_kind``) without widening the interface.
        """
        raise NotImplementedError

    def receive(self, *args: Any, **kwargs: Any) -> Any:
        """Inbound seam — NOT wired here (see module docstring).

        The inbound poller lane (tg_listener; T-0489/T-0492) owns wiring this.
        Declared on the interface so each channel has one place to express its
        inbound contract; calling it today is a programming error.
        """
        raise NotImplementedError(
            f"{type(self).__name__}.receive() is an unwired seam — the inbound "
            "lane (tg_listener / T-0489) owns recv integration."
        )


class TgChannel(Channel):
    """Telegram channel — wraps the existing ``tg.TgClient.send``.

    Delegates via ``actions._get_tg_client`` (the lazy singleton) so there is
    exactly one TgClient per worker and existing monkeypatch points keep working.
    """

    name = "tg"

    def __init__(self, cfg: "Config") -> None:
        self._cfg = cfg

    def _client(self) -> Any:
        # Fetched per-send (not cached) so a monkeypatch installed after the
        # channel is built still takes effect, matching singleton semantics.
        from bot_squad_worker.actions import _get_tg_client

        return _get_tg_client(self._cfg)

    def send(
        self,
        text: str,
        *,
        chat_id: str = "",
        sid: str = "",
        user: str = "",
        urgent: bool = False,
        topic_id: int | None = None,
        **extra: Any,
    ) -> bool:
        # T-0513: forward TG-specific optional knobs only when a caller passes
        # them, so the default send shape (and the existing test fakes' fixed
        # signatures) stay unchanged. ``reply_markup`` carries a TG keyboard;
        # ``debounce=False`` lets interactive command replies (tg_listener) echo
        # every time instead of being collapsed by the 60s same-payload cooldown.
        opt: dict[str, Any] = {}
        if "reply_markup" in extra:
            opt["reply_markup"] = extra["reply_markup"]
        if "debounce" in extra:
            opt["debounce"] = extra["debounce"]
        # T-0719: the RAW routing SID behind the display `sid` label, forwarded
        # so the send can pin message_id -> session for reply routing. Same
        # opt-in shape as above: absent unless a caller asks for it, so fakes
        # with fixed signatures keep working.
        if "route_sid" in extra:
            opt["route_sid"] = extra["route_sid"]
        # T-0725: thread this send as a TG reply to the inbound message it
        # answers (the voice-note transcript arrived detached from its note).
        # Same opt-in shape again — omitted unless the caller asks, so fakes
        # with fixed signatures keep working. Transports without a reply
        # notion (MAX) simply never receive it.
        if "reply_to_message_id" in extra:
            opt["reply_to_message_id"] = extra["reply_to_message_id"]
        return self._client().send(
            chat_id=chat_id,
            text=text,
            sid=sid,
            user=user,
            urgent=urgent,
            topic_id=topic_id,
            **opt,
        )


class MaxChannel(Channel):
    """MAX (max.ru) channel — wraps ``max.MaxClient.send``.

    Stub-ready per DoD: the transport exists (``max.py``) but MAX is not the
    project-bound deploy channel today (projects carry ``tg_chat``, not a MAX
    id), so it is registered and fully functional but selected only when config
    asks for it. MAX has no forum threads → ``topic_id`` is ignored; a
    ``recipient_kind`` may ride in ``**extra`` (else the configured default).
    """

    name = "max"

    def __init__(self, cfg: "Config") -> None:
        self._cfg = cfg

    def _client(self) -> Any:
        from bot_squad_worker.actions import _get_max_client

        return _get_max_client(self._cfg)

    def send(
        self,
        text: str,
        *,
        chat_id: str = "",
        sid: str = "",
        user: str = "",
        urgent: bool = False,
        topic_id: int | None = None,  # noqa: ARG002 — MAX has no threads
        **extra: Any,
    ) -> bool:
        recipient_kind = extra.get("recipient_kind") or getattr(
            self._cfg, "max_recipient_kind", "chat_id"
        )
        return self._client().send(
            chat_id=chat_id,
            text=text,
            sid=sid,
            user=user,
            urgent=urgent,
            recipient_kind=recipient_kind,
        )


# ---------------------------------------------------------------------------
# Registry / factory — adding a channel = register() it; no caller change.
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, Callable[["Config"], Channel]] = {}


def register(name: str, factory: Callable[["Config"], Channel]) -> None:
    """Register ``factory`` (cfg -> Channel) under ``name`` (last write wins)."""
    _REGISTRY[name] = factory


def available() -> list[str]:
    """Sorted list of registered channel names."""
    return sorted(_REGISTRY)


def _resolve_name(
    cfg: "Config", *, project: str | None = None, user: str | None = None
) -> str:
    """Pick a channel name from config for a project/user.

    Today: an optional ``cfg.default_channel`` override, else ``DEFAULT_CHANNEL``
    (TG). The ``project``/``user`` args are the extension point for per-project or
    per-user channel preferences without changing this signature or any caller.
    """
    return getattr(cfg, "default_channel", "") or DEFAULT_CHANNEL


def get_channel(
    cfg: "Config",
    *,
    name: str | None = None,
    project: str | None = None,
    user: str | None = None,
) -> Channel:
    """Return a Channel impl. Explicit ``name`` wins; else config-driven.

    Raises ``ValueError`` for an unknown name (so a typo fails loud, not silent).
    """
    chosen = name or _resolve_name(cfg, project=project, user=user)
    try:
        factory = _REGISTRY[chosen]
    except KeyError:
        raise ValueError(
            f"unknown channel {chosen!r}; registered: {available()}"
        ) from None
    return factory(cfg)


# Built-in channels. A new channel (e.g. a MailChannel) is added with one
# register() call here — callers via get_channel() need no change.
register("tg", TgChannel)
register("max", MaxChannel)

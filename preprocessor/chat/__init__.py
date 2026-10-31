"""The chat frontend layer: one interface in front of Slack, Zulip or an
in-memory fake. The platform code lives in the shared botcore package
(botcore/botcore/chat); this package is the EA's side of it: config
loading, secrets, and the writer for downloaded files. Its submodules
(chat.base, chat.slack_frontend, ...) are the botcore modules themselves.

`load_frontend()` builds the frontend named by `chat.frontend` in
ea-config.json ("slack" by default; "web" is the bot's own web chat,
docs/webchat.md). The platform modules are imported only when chosen, so
a Slack deployment never imports the Zulip client and the reverse.
`prompt_strings_for_config()` gives the platform's prompt wording without
constructing a client, for code that assembles prompts outside the bot.
"""
from __future__ import annotations

import importlib
import logging
import os
from collections.abc import Mapping
from pathlib import Path

try:
    from .. import card_text  # noqa: F401  (chat.card_text: the card builders' inert())
    from .. import ea_config
    from .. import owner_trust
    from .. import resident_io as _rio
except ImportError:
    import card_text  # noqa: F401
    import ea_config
    import owner_trust
    import resident_io as _rio

from . import files as _files

# Downloads land in the resident's uploads dir: write them with the
# resident's access, as the frontends did before they moved to botcore.
# This package loads as `chat` and as `preprocessor.chat`, each with its own
# resident_io module object, and botcore keeps one hook: look the writer up
# at call time so every caller gets the same, patchable resident_io.
import sys as _sys


def _rio_now():
    return (_sys.modules.get("resident_io")
            or _sys.modules.get("preprocessor.resident_io") or _rio)


_files.use(lambda *a, **k: _rio_now().make_dir(*a, **k),
           lambda *a, **k: _rio_now().replace_bytes(*a, **k))

from .base import (
    AlreadyArchived,
    ChatError,
    ChannelNotPrivate,
    ChatFrontend,
    HandlerRegistry,
    Limits,
    NotArchived,
    NotPermitted,
    NotSupported,
    RoomNameTaken,
)
from .cards import (
    CODE_RE,
    REACTION_ROLES,
    CardAction,
    CardField,
    CardRegistry,
    CardSelect,
    ConfirmationCard,
    cancel_action,
    parse_code,
    primary_action,
    resolve_code,
    resolve_reaction,
)
# chat.text is reached as an attribute of the package (chat_notify,
# chat_read), so the package loads it.
from . import text  # noqa: F401
from .types import (
    CODE_ALPHABET,
    THREAD_KEY_RE,
    ActionEvent,
    ChannelInfo,
    FileRef,
    InboundMessage,
    MessageRef,
    RoomRef,
    Status,
    ViewEvent,
)

__all__ = [
    "AlreadyArchived", "ChannelNotPrivate", "ChatError", "ChatFrontend", "HandlerRegistry", "Limits",
    "NotArchived", "NotPermitted", "NotSupported", "RoomNameTaken",
    "CODE_RE", "REACTION_ROLES", "CardAction", "CardField", "CardRegistry",
    "CardSelect", "ConfirmationCard", "cancel_action", "parse_code",
    "primary_action", "resolve_code", "resolve_reaction",
    "CODE_ALPHABET", "THREAD_KEY_RE", "ActionEvent", "ChannelInfo", "FileRef",
    "InboundMessage", "MessageRef", "RoomRef", "Status", "ViewEvent",
    "load_frontend", "load_web_frontend", "prompt_strings_for_config", "private_dir",
]

FRONTENDS = ("slack", "zulip", "memory", "web")


def private_dir() -> Path:
    """The owner-only directory for state the LLM user must not reach
    (EA_PRIVATE_DIR, default `.ea-private` in the owner's home). Not created here."""
    return owner_trust.private_dir()


def _config_path(ea_dir: str | Path | None) -> Path | None:
    return Path(ea_dir) / "config" / "ea-config.json" if ea_dir else None


def _platform_module(name: str):
    """Import chat.<name>_frontend, or raise ChatError when it is absent."""
    modname = f"{__name__}.{name}_frontend"
    try:
        return importlib.import_module(modname)
    except ModuleNotFoundError as e:
        if e.name == modname:
            raise ChatError(f"frontend '{name}' is not installed yet") from e
        raise


# Front ends that can shadow the configured one (chat.shadow): the bot
# runs the configured one wrapped in a ShadowFrontend that mirrors it.
SHADOWS = ("web",)


def load_frontend(*, for_cli: bool = False, ea_dir: str | Path | None = None,
                  env: Mapping[str, str] | None = None) -> ChatFrontend:
    """Build the configured frontend. for_cli builds a sender-only client
    for command-line tools (no event loop). Raises ChatError for an
    unknown or uninstalled frontend.

    Secrets come by the owner_trust rule: `env` (default os.environ), then
    the owner-private secrets.env, then the checked legacy .env.secrets of
    `ea_dir` (default ~/.ea). A missing one raises KeyError, as a hard
    lookup in `env` did.

    A `shadow` in the chat block wraps the frontend in a ShadowFrontend
    that mirrors it into that one. A command-line sender gets the frontend
    alone: the web chat's store and event stream belong to the bot. A
    shadow that fails to build is logged and the frontend runs alone."""
    cfg = ea_config.get_chat(_config_path(ea_dir))
    shadow = str(cfg.get("shadow") or "")
    if shadow and shadow not in SHADOWS:
        raise ChatError(f"unknown chat shadow {shadow!r}; expected one of {', '.join(SHADOWS)}")
    primary = _load_primary(cfg, for_cli=for_cli, ea_dir=ea_dir, env=env)
    if not shadow or for_cli or shadow == cfg.get("frontend"):
        return primary
    from .shadow_frontend import ShadowFrontend
    try:
        web = _build_web(_with_web_store(cfg, ea_dir))
    except Exception:
        logging.getLogger("chat").exception(
            "the %s shadow did not build; %s runs alone", shadow, cfg.get("frontend"))
        return primary
    return ShadowFrontend(primary, web)


def load_web_frontend(*, ea_dir: str | Path | None = None) -> ChatFrontend | None:
    """The web chat front end the chat block runs (as `frontend` or as
    `shadow`), built and not started, for a process outside the bot that
    only notifies through it (alert.py); None when the block runs no web
    front end."""
    cfg = ea_config.get_chat(_config_path(ea_dir))
    if "web" not in (cfg.get("frontend"), cfg.get("shadow")):
        return None
    return _build_web(_with_web_store(cfg, ea_dir))


def _web_class():
    """botcore's WebFrontend, imported on first use, so an install that
    runs without the web chat never loads it."""
    from . import _botcore_path  # noqa: F401
    try:
        from botcore.chat import web_frontend
    except ModuleNotFoundError as e:
        if e.name == "botcore.chat.web_frontend":
            raise ChatError("frontend 'web' is not installed yet") from e
        raise
    return web_frontend.WebFrontend


def _with_web_store(cfg: dict, ea_dir: str | Path | None) -> dict:
    """The chat block with `web.store_path` filled by `_web_store_path`."""
    web = dict(cfg["web"]) if isinstance(cfg.get("web"), dict) else {}
    web["store_path"] = str(_web_store_path(web, ea_dir))
    return {**cfg, "web": web}


def _build_web(cfg: dict) -> ChatFrontend:
    """The web chat front end for the chat block `cfg`, built from its
    `web` section."""
    return _web_class().from_config(cfg.get("web") or {})


def _load_primary(cfg: dict, *, for_cli: bool, ea_dir: str | Path | None,
                  env: Mapping[str, str] | None) -> ChatFrontend:
    """The frontend `cfg["frontend"]` names (see load_frontend)."""
    env = os.environ if env is None else env
    legacy = Path(ea_dir) / owner_trust.LEGACY_SECRETS_FILE if ea_dir else None
    name = cfg.get("frontend")
    if name == "slack":
        mod = _platform_module("slack")
        env = _with_secrets(env, ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"), legacy)
        if ea_dir is None and owner_trust.is_owner_process():
            env = _trusted_slack_owner(env, cfg, for_cli)
        return mod.SlackFrontend.from_env(env, for_cli=for_cli)
    if name == "zulip":
        mod = _platform_module("zulip")
        api_key = owner_trust.owner_secret("ZULIP_API_KEY", env=env, legacy=legacy)
        if not api_key:
            raise KeyError("ZULIP_API_KEY")
        return mod.ZulipFrontend(cfg["zulip"], api_key=api_key,
                                 private_dir=private_dir())
    if name == "memory":
        from .memory_frontend import MemoryFrontend
        return MemoryFrontend.from_config(cfg.get("memory", {}))
    if name == "web":
        mod = _platform_module("web")
        web = cfg.get("web") or {}
        return mod.WebFrontend.from_config(web, store_path=_web_store_path(web, ea_dir))
    raise ChatError(f"unknown chat frontend {name!r}; expected one of {', '.join(FRONTENDS)}")


WEB_STORE_FILE = "webchat.sqlite"


def _web_store_path(web: Mapping, ea_dir: str | Path | None) -> Path:
    """The web chat's store: the block's `store_path`, else
    `<private_dir>/webchat.sqlite` for an owner process, else
    `<ea_dir>/state/webchat.sqlite`."""
    if web.get("store_path"):
        return Path(str(web["store_path"]))
    if ea_dir is None and owner_trust.is_owner_process():
        return private_dir() / WEB_STORE_FILE
    return Path(ea_dir or owner_trust.ea_dir()) / "state" / WEB_STORE_FILE


def _trusted_slack_owner(env: Mapping[str, str], cfg: dict, for_cli: bool) -> Mapping[str, str]:
    """`env` with EA_SLACK_OWNER_ID taken from the owner's chat.json
    (`slack.owner_user_id`), never from ~/.ea/.env, which ea-bot can
    rewrite: the owner id decides whose click approves a card. The bot
    refuses to start without it; a CLI sender, which approves nothing, only
    drops the untrusted copy."""
    slack = cfg.get("slack") if isinstance(cfg.get("slack"), dict) else {}
    owner = str(slack.get("owner_user_id") or "").strip()
    if not owner and not for_cli:
        raise owner_trust.OwnerTrustError(
            f"{private_dir() / owner_trust.CHAT_FILE} has no slack.owner_user_id. The bot "
            f"takes the Slack owner id only from it, never from ~/.ea/.env in ea-bot "
            f"space. Add it as the owner: python3 preprocessor/owner_trust.py seed --chat "
            f"--chat-owner-id <your Slack member id>")
    return {**env, "EA_SLACK_OWNER_ID": owner}


def _with_secrets(env: Mapping[str, str], names, legacy) -> Mapping[str, str]:
    """`env` with each secret in `names` it lacks filled in by the
    owner_trust rule; `env` itself when it has them all."""
    missing = [n for n in names if not env.get(n)]
    if not missing:
        return env
    out = dict(env)
    for n in missing:
        val = owner_trust.owner_secret(n, env=env, legacy=legacy)
        if val:
            out[n] = val
    return out


def prompt_strings_for_config(ea_dir: str | Path | None = None) -> dict[str, str]:
    """The configured platform's prompt wording, with no client built.
    The memory frontend uses the Slack wording."""
    cfg = ea_config.get_chat(_config_path(ea_dir))
    name = cfg.get("frontend")
    if name == "zulip":
        return _platform_module("zulip").ZulipFrontend.prompt_strings_for(
            str((cfg.get("zulip") or {}).get("dm_stream") or ""))
    if name == "slack":
        return dict(_platform_module("slack").SlackFrontend.PROMPT_STRINGS)
    if name == "memory":
        try:
            return dict(_platform_module("slack").SlackFrontend.PROMPT_STRINGS)
        except ChatError:
            from .memory_frontend import SLACK_LIKE_PROMPT_STRINGS
            return dict(SLACK_LIKE_PROMPT_STRINGS)
    if name == "web":
        return dict(_platform_module("web").WebFrontend.PROMPT_STRINGS)
    raise ChatError(f"unknown chat frontend {name!r}; expected one of {', '.join(FRONTENDS)}")

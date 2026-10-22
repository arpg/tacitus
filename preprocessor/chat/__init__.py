"""The chat frontend layer: one interface in front of Slack, Zulip or an
in-memory fake.

`load_frontend()` builds the frontend named by `chat.frontend` in
ea-config.json ("slack" by default). The platform modules are imported
only when chosen, so a Slack deployment never imports the Zulip client
and the reverse. `prompt_strings_for_config()` gives the platform's
prompt wording without constructing a client, for code that assembles
prompts outside the bot.
"""
from __future__ import annotations

import importlib
import os
from collections.abc import Mapping
from pathlib import Path

try:
    from .. import card_text  # noqa: F401  (chat.card_text: the card builders' inert())
    from .. import ea_config
    from .. import owner_trust
except ImportError:
    import card_text  # noqa: F401
    import ea_config
    import owner_trust

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
    "load_frontend", "prompt_strings_for_config", "private_dir",
]

FRONTENDS = ("slack", "zulip", "memory")


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


def load_frontend(*, for_cli: bool = False, ea_dir: str | Path | None = None,
                  env: Mapping[str, str] | None = None) -> ChatFrontend:
    """Build the configured frontend. for_cli builds a sender-only client
    for command-line tools (no event loop). Raises ChatError for an
    unknown or uninstalled frontend.

    Secrets come by the owner_trust rule: `env` (default os.environ), then
    the owner-private secrets.env, then the checked legacy .env.secrets of
    `ea_dir` (default ~/.ea). A missing one raises KeyError, as a hard
    lookup in `env` did."""
    cfg = ea_config.get_chat(_config_path(ea_dir))
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
    raise ChatError(f"unknown chat frontend {name!r}; expected one of {', '.join(FRONTENDS)}")


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
    raise ChatError(f"unknown chat frontend {name!r}; expected one of {', '.join(FRONTENDS)}")

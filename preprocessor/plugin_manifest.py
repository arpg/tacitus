"""The plugin manifest: read, check and normalize a plugin's plugin.json.

A plugin is one directory with a `plugin.json` at its root (docs/plugins.md).
Two manifest versions exist:

- **v1** (no `manifest_version`): the original format. `enabled`,
  `schedule`, `extra_schedules`, `model`, `timeout_seconds`, `output`,
  `scopes` and a `slack` (or `chat`) block at the top level. The plugin's
  own `plugin.json` decides everything, as before.
- **v2** (`"manifest_version": 2`): every capability sits under
  `capabilities`, and the owner's lock file (plugin_registry) decides
  whether the plugin is enabled and which capabilities the owner accepted.

`normalize()` turns either version into one dict shape, so the rest of the
code reads one format. `capability_summary()` is the part of a manifest the
owner consents to; its digest changes whenever a capability does, which is
how an update asks for consent again.

This module reads files and does nothing else: no lock, no installs, no
owner checks. plugin_registry and scripts/ea-plugin.py build on it.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from pathlib import Path

MANIFEST_FILE = "plugin.json"
MANIFEST_VERSION = 2

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
# The one shape of a tool grant: python3 on a file or glob inside the
# plugin's directory, optionally with any arguments.
TOOL_GRANT_RE = re.compile(r"^Bash\(python3 \{plugin_dir\}/[A-Za-z0-9_./*-]+( \*)?\)$")
PENDING_RE = re.compile(r"^pending-[a-z0-9][a-z0-9-]{0,62}\.json$")
SECRET_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
VERB_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
HOST_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9-]{1,63}\.)*[a-z0-9-]{1,63}$")
PORTS_RE = re.compile(r"^\d{1,5}(-\d{1,5})?$")
CRON_RE = re.compile(r"^(@(daily|hourly|weekly|monthly|yearly|annually|reboot)|(\S+\s+){4}\S+)$")
FIELD_RE = re.compile(r"\{([^{}]*)\}")

ZONES = ("agent", "owner")
LLM_MODES = ("claude", "remote-first", "remote", "none")
CARD_USERS = ("resident", "owner")
VERB_CLASSES = ("safe", "gated")
AUTH_KINDS = ("graph",)
# Placeholders a command may carry. {token} only in a card's approve
# command; the item reaches a card executor as JSON on stdin.
COMMAND_FIELDS = {"plugin_dir", "python", "token"}
DEFAULT_STATE_DIRS = ("state",)
# The topic tags mail_tagger gives (mail_tagger.TOPICS); a mail trigger
# may name them.
MAIL_TOPICS = ("lor", "recruiting", "hiring", "purchasing", "funding", "manuscripts")
MAIL_TRIGGER_CAP = 10


class ManifestError(ValueError):
    """A manifest that cannot be read or does not pass the checks."""


# ── reading ───────────────────────────────────────────────────────────

def read(plugin_dir) -> dict:
    """The raw manifest of the plugin in `plugin_dir`. ManifestError when
    it is missing, unreadable or not a JSON object."""
    path = Path(plugin_dir) / MANIFEST_FILE
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        raise ManifestError(f"{path}: no {MANIFEST_FILE}") from None
    except (OSError, ValueError) as e:
        raise ManifestError(f"{path}: unreadable: {e}") from None
    if not isinstance(raw, dict):
        raise ManifestError(f"{path}: not a JSON object")
    return raw


def version(raw: dict) -> int:
    """2 for a manifest that says so, else 1."""
    try:
        return 2 if int(raw.get("manifest_version") or 1) == 2 else 1
    except (TypeError, ValueError):
        return 1


# ── small checks ──────────────────────────────────────────────────────

def _strs(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return []


def _template_fields(text: str) -> set[str]:
    return {m.group(1) for m in FIELD_RE.finditer(text)}


def _check_command(cmd, where: str, errors: list, allow_token: bool = False) -> list[str]:
    if not isinstance(cmd, list) or not cmd or not all(isinstance(c, str) and c for c in cmd):
        errors.append(f"{where}: command must be a non-empty list of strings")
        return []
    allowed = COMMAND_FIELDS if allow_token else COMMAND_FIELDS - {"token"}
    for part in cmd:
        bad = _template_fields(part) - allowed
        if bad:
            errors.append(f"{where}: unknown placeholder(s) {sorted(bad)} in {part!r}")
    if not any("{plugin_dir}" in part for part in cmd):
        errors.append(f"{where}: the command must run code inside {{plugin_dir}}")
    return list(cmd)


def _check_cron(expr, where: str, errors: list) -> str:
    expr = str(expr or "").strip()
    if not CRON_RE.match(expr):
        errors.append(f"{where}: bad cron expression {expr!r}")
    return expr


def _check_relpath(rel, where: str, errors: list) -> str:
    rel = str(rel or "").strip()
    p = Path(rel)
    if not rel or p.is_absolute() or ".." in p.parts:
        errors.append(f"{where}: {rel!r} must be a path inside the plugin")
    return rel


def _int(value, default: int, lo: int = 1, hi: int = 86400) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


# ── v1 ────────────────────────────────────────────────────────────────

def _normalize_v1(raw: dict, dir_name: str, errors: list) -> dict:
    name = str(raw.get("name") or dir_name)
    schedules = []
    sched = str(raw.get("schedule") or "").strip()
    if sched:
        schedules.append({"label": "default", "cron": sched, "args": [],
                          "llm": "claude", "model": str(raw.get("model") or "sonnet"),
                          "timeout_seconds": _int(raw.get("timeout_seconds"), 1800),
                          "skill": "skill.md"})
        for ex in raw.get("extra_schedules") or []:
            if isinstance(ex, dict) and ex.get("cron"):
                schedules.append({"label": str(ex.get("label") or "extra"),
                                  "cron": str(ex["cron"]).strip(),
                                  "args": str(ex.get("args") or "").split(),
                                  "llm": "claude", "model": str(raw.get("model") or "sonnet"),
                                  "timeout_seconds": _int(raw.get("timeout_seconds"), 1800),
                                  "skill": "skill.md"})
    chat = raw.get("slack") or raw.get("chat")
    chat = _norm_chat(chat, errors, strict=False) if isinstance(chat, dict) else None
    caps = _empty_capabilities()
    caps["schedules"] = schedules
    caps["chat"] = chat
    if chat:
        caps["tools"] = ["Bash(python3 {plugin_dir}/*.py *)"]
    return {"manifest_version": 1, "name": name, "version": str(raw.get("version") or ""),
            "description": str(raw.get("description") or ""), "zone": "agent",
            "enabled": bool(raw.get("enabled")), "capabilities": caps,
            "state_dirs": list(DEFAULT_STATE_DIRS),
            "model": str(raw.get("model") or "sonnet"),
            "timeout_seconds": _int(raw.get("timeout_seconds"), 1800),
            "output": raw.get("output") if isinstance(raw.get("output"), dict) else {},
            "scopes": _strs(raw.get("scopes"))}


# ── v2 ────────────────────────────────────────────────────────────────

def _empty_capabilities() -> dict:
    return {"schedules": [], "chat": None, "skills": [], "tools": [], "denied_tools": [],
            "cards": [], "mail_rules": {"closure_confirm_senders": [],
                                        "closure_exclude_senders": [],
                                        "keep_from_anthropic_domains": []},
            "network": {"domains": [], "local": []}, "secrets": [],
            "owner_hooks": {"verbs": [], "auth_sources": [], "cron": []},
            "claude_plugin": "", "mail_triggers": []}


def _norm_chat(chat: dict, errors: list, strict: bool) -> dict | None:
    keywords = [k.lower() for k in _strs(chat.get("keywords"))]
    patterns = []
    for pat in _strs(chat.get("patterns")):
        try:
            re.compile(pat)
            patterns.append(pat)
        except re.error as e:
            if strict:
                errors.append(f"capabilities.chat: bad pattern {pat!r}: {e}")
    skill = str(chat.get("skill") or "skill.md")
    if strict:
        _check_relpath(skill, "capabilities.chat.skill", errors)
        if not keywords and not patterns:
            errors.append("capabilities.chat: needs keywords or patterns")
    return {"skill": skill, "keywords": keywords, "patterns": patterns}


def _norm_schedules(items, errors: list) -> list[dict]:
    out, labels = [], set()
    for i, s in enumerate(items if isinstance(items, list) else []):
        where = f"capabilities.schedules[{i}]"
        if not isinstance(s, dict):
            errors.append(f"{where}: must be an object")
            continue
        label = str(s.get("label") or ("default" if i == 0 else f"s{i}"))
        if not NAME_RE.match(label) or label in labels:
            errors.append(f"{where}: label {label!r} must be unique and match {NAME_RE.pattern}")
        labels.add(label)
        llm = str(s.get("llm") or "claude")
        if llm not in LLM_MODES:
            errors.append(f"{where}: llm must be one of {LLM_MODES}")
        args = s.get("args") or []
        if isinstance(args, str):
            args = args.split()
        out.append({"label": label, "cron": _check_cron(s.get("cron"), where, errors),
                    "args": [str(a) for a in args], "llm": llm,
                    "model": str(s.get("model") or "sonnet"),
                    "timeout_seconds": _int(s.get("timeout_seconds"), 1800),
                    "skill": _check_relpath(s.get("skill") or "skill.md", where, errors)})
    return out


def _norm_card(c, i: int, zone: str, errors: list) -> dict | None:
    where = f"capabilities.cards[{i}]"
    if not isinstance(c, dict):
        errors.append(f"{where}: must be an object")
        return None
    cid = str(c.get("id") or "")
    if not NAME_RE.match(cid):
        errors.append(f"{where}: id {cid!r} must match {NAME_RE.pattern}")
    pending = str(c.get("pending_file") or "")
    if not PENDING_RE.match(pending):
        errors.append(f"{where}: pending_file {pending!r} must match {PENDING_RE.pattern}")
    if not str(c.get("title") or "").strip():
        errors.append(f"{where}: needs a title")

    def executor(e, key: str, allow_token: bool):
        if e is None:
            return None
        if not isinstance(e, dict):
            errors.append(f"{where}.{key}: must be an object")
            return None
        user = str(e.get("user") or "resident")
        if user not in CARD_USERS:
            errors.append(f"{where}.{key}: user must be one of {CARD_USERS}")
        if user == "owner" and zone != "owner":
            errors.append(f"{where}.{key}: only an owner-zone plugin may run as the owner")
        return {"command": _check_command(e.get("command"), f"{where}.{key}", errors,
                                          allow_token=allow_token),
                "user": user, "timeout_seconds": _int(e.get("timeout_seconds"), 300)}

    approve = executor(c.get("approve"), "approve", True)
    if approve is None:
        errors.append(f"{where}: needs an approve executor")
    variants = []
    for j, v in enumerate(c.get("variants") or []):
        if not isinstance(v, dict) or not isinstance(v.get("when"), dict):
            errors.append(f"{where}.variants[{j}]: needs a `when` object")
            continue
        variant = {"when": dict(v["when"])}
        variant.update(_card_text(v))
        variants.append(variant)
    card = {"id": cid, "pending_file": pending,
            "variants": variants, "approve": approve,
            "cancel": executor(c.get("cancel"), "cancel", False)}
    card.update(_card_text(c))
    card.setdefault("approve_label", ":white_check_mark: Approve")
    card.setdefault("approve_word", "approve")
    card.setdefault("cancel_label", "Cancel")
    card.setdefault("cancel_word", "cancel")
    card.setdefault("lines", [])
    card.setdefault("required", [])
    card.setdefault("key_fields", [])
    return card


# Card keys a variant may override. Text keys are templates over the
# item's fields ({title}, {summary}); required and key_fields are lists.
_CARD_TEXT_KEYS = ("title", "lines", "approve_label", "approve_word", "cancel_label",
                   "cancel_word", "button_noun", "busy_status", "missing_text",
                   "required", "key_fields", "fallback")
_CARD_LIST_KEYS = ("lines", "required", "key_fields")


def _card_text(src: dict) -> dict:
    """The card text keys `src` sets, as strings or lists of strings."""
    return {k: (_strs(src[k]) if k in _CARD_LIST_KEYS else str(src[k]))
            for k in _CARD_TEXT_KEYS if k in src}


def _norm_mail_rules(m, errors: list) -> dict:
    m = m if isinstance(m, dict) else {}
    out = {}
    for key in ("closure_confirm_senders", "closure_exclude_senders"):
        vals = [v.strip().lower() for v in _strs(m.get(key)) if v.strip()]
        for v in vals:
            # A short substring matches far more senders than it means to.
            if len(v) < 6 or not ("@" in v or "." in v):
                errors.append(f"capabilities.mail_rules.{key}: {v!r} is too broad")
        out[key] = vals
    doms = [d.strip().lower().lstrip("@") for d in _strs(m.get("keep_from_anthropic_domains"))]
    for d in doms:
        if not HOST_RE.match(d):
            errors.append(f"capabilities.mail_rules.keep_from_anthropic_domains: bad domain {d!r}")
    out["keep_from_anthropic_domains"] = doms
    return out


def _norm_mail_triggers(items, errors: list) -> list[dict]:
    """Incoming mail the owner's tagger tick hands to the plugin as it
    arrives (plugin_mail.py). A trigger matches on the sender, the
    tagger's topic tag, the subject and whether the mail is bulk mail;
    the keys it names must all match. A trigger with no `from` must set
    `bulk`, so a topic alone never routes a colleague's mail."""
    if items is None:
        return []
    if not isinstance(items, list):
        errors.append("capabilities.mail_triggers must be a list")
        return []
    if len(items) > MAIL_TRIGGER_CAP:
        errors.append(f"capabilities.mail_triggers: at most {MAIL_TRIGGER_CAP} triggers")
    out, ids = [], set()
    for i, t in enumerate(items[:MAIL_TRIGGER_CAP]):
        where = f"capabilities.mail_triggers[{i}]"
        if not isinstance(t, dict):
            errors.append(f"{where} must be an object")
            continue
        tid = str(t.get("id") or "")
        if not NAME_RE.match(tid) or tid in ids:
            errors.append(f"{where}.id {tid!r} must be a unique slug")
        ids.add(tid)
        senders = [v.strip().lower() for v in _strs(t.get("from")) if v.strip()]
        for v in senders:
            dom = v[1:] if v.startswith("@") else v.rpartition("@")[2]
            if not ("@" in v and HOST_RE.match(dom) and "." in dom):
                errors.append(f"{where}.from: {v!r} must be an address or an @domain")
        topics = [v.strip().lower() for v in _strs(t.get("topics")) if v.strip()]
        for v in topics:
            if v not in MAIL_TOPICS:
                errors.append(f"{where}.topics: {v!r} is not one of {MAIL_TOPICS}")
        subject = [v.strip().lower() for v in _strs(t.get("subject")) if v.strip()]
        for v in subject:
            if len(v) < 4:
                errors.append(f"{where}.subject: {v!r} is too broad")
        exclude = [v.strip().lower() for v in _strs(t.get("exclude_from")) if v.strip()]
        bulk = bool(t.get("bulk"))
        if not senders and not topics:
            errors.append(f"{where}: needs `from` or `topics`")
        if not senders and not bulk:
            errors.append(f"{where}: a trigger with no `from` must set \"bulk\": true")
        out.append({"id": tid, "from": senders, "topics": topics, "subject": subject,
                    "exclude_from": exclude, "bulk": bulk})
    return out


def _norm_network(n, errors: list) -> dict:
    n = n if isinstance(n, dict) else {}
    domains = [d.strip().lower() for d in _strs(n.get("domains"))]
    for d in domains:
        if not HOST_RE.match(d.lstrip("*.")):
            errors.append(f"capabilities.network.domains: bad host {d!r}")
    local = []
    for i, e in enumerate(n.get("local") or []):
        where = f"capabilities.network.local[{i}]"
        if not isinstance(e, dict):
            errors.append(f"{where}: must be an object")
            continue
        daddr, proto, ports = str(e.get("daddr") or ""), str(e.get("proto") or "tcp"), str(e.get("ports") or "")
        try:
            ipaddress.ip_address(daddr)
        except ValueError:
            errors.append(f"{where}: daddr {daddr!r} is not an IP address")
        if proto not in ("tcp", "udp"):
            errors.append(f"{where}: proto must be tcp or udp")
        if not PORTS_RE.match(ports):
            errors.append(f"{where}: ports {ports!r} must be N or N-M")
        if not str(e.get("reason") or "").strip():
            errors.append(f"{where}: needs a reason")
        local.append({"daddr": daddr, "proto": proto, "ports": ports,
                      "reason": str(e.get("reason") or "")})
    return {"domains": domains, "local": local}


def _norm_owner_hooks(h, zone: str, errors: list) -> dict:
    h = h if isinstance(h, dict) else {}
    out = {"verbs": [], "auth_sources": [], "cron": []}
    if any(h.get(k) for k in out) and zone != "owner":
        errors.append("capabilities.owner_hooks: only an owner-zone plugin may declare them")
    seen = set()
    for i, v in enumerate(h.get("verbs") or []):
        where = f"capabilities.owner_hooks.verbs[{i}]"
        if not isinstance(v, dict):
            errors.append(f"{where}: must be an object")
            continue
        name = str(v.get("name") or "")
        if not VERB_RE.match(name) or name in seen:
            errors.append(f"{where}: name {name!r} must be unique and match {VERB_RE.pattern}")
        seen.add(name)
        cls = str(v.get("class") or "")
        if cls not in VERB_CLASSES:
            errors.append(f"{where}: class must be one of {VERB_CLASSES}")
        alias = v.get("alias")
        if alias is not None:
            if (not isinstance(alias, dict) or not VERB_RE.match(str(alias.get("verb") or ""))
                    or not re.match(r"^[a-z_]{1,40}$", str(alias.get("tool") or ""))):
                errors.append(f"{where}: alias must be {{\"tool\": ..., \"verb\": ...}}")
                alias = None
            else:
                alias = {"tool": str(alias["tool"]), "verb": str(alias["verb"])}
        out["verbs"].append({"name": name, "class": cls, "alias": alias,
                             "command": _check_command(v.get("command"), where, errors),
                             "timeout_seconds": _int(v.get("timeout_seconds"), 120)})
    for i, a in enumerate(h.get("auth_sources") or []):
        where = f"capabilities.owner_hooks.auth_sources[{i}]"
        if not isinstance(a, dict):
            errors.append(f"{where}: must be an object")
            continue
        kind = str(a.get("kind") or "")
        if kind not in AUTH_KINDS:
            errors.append(f"{where}: kind must be one of {AUTH_KINDS}")
        module = _check_relpath(a.get("module"), where, errors)
        if module and not module.endswith(".py"):
            errors.append(f"{where}: module must be a .py file")
        func = str(a.get("function") or "")
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$", func):
            errors.append(f"{where}: bad function name {func!r}")
        out["auth_sources"].append({"name": str(a.get("name") or func), "kind": kind,
                                    "module": module, "function": func})
    labels = set()
    for i, c in enumerate(h.get("cron") or []):
        where = f"capabilities.owner_hooks.cron[{i}]"
        if not isinstance(c, dict):
            errors.append(f"{where}: must be an object")
            continue
        label = str(c.get("label") or "")
        if not NAME_RE.match(label) or label in labels:
            errors.append(f"{where}: label {label!r} must be unique and match {NAME_RE.pattern}")
        labels.add(label)
        out["cron"].append({"label": label, "cron": _check_cron(c.get("cron"), where, errors),
                            "command": _check_command(c.get("command"), where, errors),
                            "timeout_seconds": _int(c.get("timeout_seconds"), 1800)})
    return out


def _normalize_v2(raw: dict, dir_name: str, errors: list) -> dict:
    name = str(raw.get("name") or "")
    if not NAME_RE.match(name):
        errors.append(f"name {name!r} must match {NAME_RE.pattern}")
    zone = str(raw.get("zone") or "agent")
    if zone not in ZONES:
        errors.append(f"zone must be one of {ZONES}")
    caps_raw = raw.get("capabilities")
    if caps_raw is not None and not isinstance(caps_raw, dict):
        errors.append("capabilities must be an object")
        caps_raw = {}
    caps_raw = caps_raw or {}
    caps = _empty_capabilities()
    caps["schedules"] = _norm_schedules(caps_raw.get("schedules"), errors)
    chat = caps_raw.get("chat") or caps_raw.get("slack")
    caps["chat"] = _norm_chat(chat, errors, strict=True) if isinstance(chat, dict) else None
    caps["skills"] = [_check_relpath(s, "capabilities.skills", errors)
                      for s in _strs(caps_raw.get("skills"))]
    tools = _strs(caps_raw.get("tools"))
    if caps["chat"] and not tools:
        tools = ["Bash(python3 {plugin_dir}/*.py *)"]
    for t in tools:
        # A grant reaches only the plugin's own files, never the host's:
        # python3 on a path under {plugin_dir}, with no ".." and no other
        # placeholder.
        if not TOOL_GRANT_RE.match(t) or ".." in t or _template_fields(t) - {"plugin_dir"}:
            errors.append(f"capabilities.tools: {t!r} must be Bash(python3 {{plugin_dir}}/<path> "
                          "[*]) with no '..'")
    caps["tools"] = tools
    caps["denied_tools"] = _strs(caps_raw.get("denied_tools"))
    cards, ids, files = [], set(), set()
    for i, c in enumerate(caps_raw.get("cards") or []):
        card = _norm_card(c, i, zone, errors)
        if card is None:
            continue
        if card["id"] in ids or card["pending_file"] in files:
            errors.append(f"capabilities.cards[{i}]: id and pending_file must be unique")
        ids.add(card["id"])
        files.add(card["pending_file"])
        cards.append(card)
    caps["cards"] = cards
    caps["mail_rules"] = _norm_mail_rules(caps_raw.get("mail_rules"), errors)
    caps["mail_triggers"] = _norm_mail_triggers(caps_raw.get("mail_triggers"), errors)
    caps["network"] = _norm_network(caps_raw.get("network"), errors)
    secrets = _strs(caps_raw.get("secrets"))
    for s in secrets:
        if not SECRET_RE.match(s):
            errors.append(f"capabilities.secrets: bad name {s!r}")
    caps["secrets"] = secrets
    caps["owner_hooks"] = _norm_owner_hooks(caps_raw.get("owner_hooks"), zone, errors)
    cp = str(caps_raw.get("claude_plugin") or "")
    caps["claude_plugin"] = _check_relpath(cp, "capabilities.claude_plugin", errors) if cp else ""
    state_dirs = _strs(raw.get("state_dirs")) or list(DEFAULT_STATE_DIRS)
    for s in state_dirs:
        _check_relpath(s, "state_dirs", errors)
    return {"manifest_version": 2, "name": name or dir_name,
            "version": str(raw.get("version") or ""),
            "description": str(raw.get("description") or ""), "zone": zone,
            "enabled": True, "capabilities": caps, "state_dirs": state_dirs,
            "model": str(raw.get("model") or "sonnet"),
            "timeout_seconds": _int(raw.get("timeout_seconds"), 1800),
            "output": raw.get("output") if isinstance(raw.get("output"), dict) else {},
            "scopes": _strs(raw.get("scopes")),
            "context": _norm_context(raw.get("context"), errors)}


# The read grants a scheduled run may ask for beyond its own directory
# (run-plugin.sh builds the allow list from them). Only the values that
# differ from "nothing" are kept, so a manifest without a context block
# summarises the same as before the block was part of the lock.
_CONTEXT_FLAGS = ("fetch_url", "google_calendar", "google_drive", "ea_briefing")
_GRAPH_VERB_RE = re.compile(r"^[a-z][a-z0-9-]{0,40}$")


def _norm_context(raw, errors: list) -> dict:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        errors.append("context must be an object")
        return {}
    out: dict = {}
    for key in ("ledger_read", "ledger_write"):
        v = raw.get(key)
        if v in (None, False):
            continue
        if v is True or v == "own":
            out[key] = v
        else:
            errors.append(f"context.{key} must be true, false or \"own\"")
    for key in _CONTEXT_FLAGS:
        if raw.get(key):
            out[key] = True
    verbs = _strs(raw.get("graph_actions"))
    bad = [v for v in verbs if not _GRAPH_VERB_RE.match(v)]
    if bad:
        errors.append(f"context.graph_actions: bad verb names {bad}")
    if verbs:
        out["graph_actions"] = sorted(set(verbs))
    return out


# ── public API ────────────────────────────────────────────────────────

def normalize(raw: dict, dir_name: str = "") -> tuple[dict, list[str]]:
    """(normalized manifest, errors). A v1 manifest is read leniently, the
    way the code always read it; a v2 manifest is checked strictly. An
    error list that is not empty means a v2 manifest must not be installed
    or honoured."""
    errors: list[str] = []
    if version(raw) == 2:
        norm = _normalize_v2(raw, dir_name, errors)
    else:
        norm = _normalize_v1(raw, dir_name, errors)
    return norm, errors


def load(plugin_dir) -> tuple[dict, list[str]]:
    """normalize(read(plugin_dir)). Also checks that the files a v2
    manifest names exist in the directory."""
    pdir = Path(plugin_dir)
    norm, errors = normalize(read(pdir), pdir.name)
    if norm["manifest_version"] == 2:
        caps = norm["capabilities"]
        rels = list(caps["skills"]) + [s["skill"] for s in caps["schedules"] if s["llm"] != "none"]
        if caps["chat"]:
            rels.append(caps["chat"]["skill"])
        rels += [a["module"] for a in caps["owner_hooks"]["auth_sources"] if a["module"]]
        for rel in rels:
            if rel and not (pdir / rel).is_file():
                errors.append(f"{rel}: named in the manifest but not in the plugin")
        cp = caps["claude_plugin"]
        if cp and not (pdir / cp / ".claude-plugin" / "plugin.json").is_file():
            errors.append(f"{cp}: claude_plugin names a directory without "
                          f".claude-plugin/plugin.json (a Claude Code plugin root)")
    return norm, errors


def capability_summary(norm: dict) -> dict:
    """What the owner consents to: everything that changes what the plugin
    can do or reach. Keywords, labels and card wording are in it too,
    since they decide when the plugin's text reaches a prompt or a card."""
    caps = norm["capabilities"]
    summary = {"zone": norm["zone"],
               "schedules": [{k: s[k] for k in ("label", "cron", "args", "llm", "model", "skill")}
                             for s in caps["schedules"]],
               "chat": caps["chat"],
               "skills": sorted(caps["skills"]),
               "tools": sorted(caps["tools"]),
               "denied_tools": sorted(caps["denied_tools"]),
               "cards": caps["cards"],
               "mail_rules": caps["mail_rules"],
               "network": caps["network"],
               "secrets": sorted(caps["secrets"]),
               "owner_hooks": caps["owner_hooks"],
               "claude_plugin": caps["claude_plugin"]}
    if norm.get("context"):
        summary["context"] = norm["context"]
    # Only when declared, so a lock entry from before mail triggers keeps
    # the digest the owner accepted.
    if caps.get("mail_triggers"):
        summary["mail_triggers"] = caps["mail_triggers"]
    return summary


def capability_digest(summary: dict) -> str:
    """sha256 of the summary in a canonical JSON form."""
    blob = json.dumps(summary, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def describe(summary: dict) -> list[str]:
    """The consent text: one line per capability, plain enough to read
    before typing --accept-capabilities."""
    lines = [f"zone: {summary['zone']} "
             + ("(its code runs as the owner, with the owner's tokens)"
                if summary["zone"] == "owner" else "(its code runs as the resident)")]
    for s in summary["schedules"]:
        lines.append(f"schedule {s['label']}: {s['cron']} llm={s['llm']} model={s['model']}"
                     + (f" args={' '.join(s['args'])}" if s["args"] else ""))
    if summary["chat"]:
        ch = summary["chat"]
        lines.append(f"chat trigger: skill {ch['skill']}, {len(ch['keywords'])} keyword(s), "
                     f"{len(ch['patterns'])} pattern(s)")
    for s in summary["skills"]:
        lines.append(f"skill: {s}")
    for t in summary["tools"]:
        lines.append(f"agent tool grant: {t}")
    for t in summary["denied_tools"]:
        lines.append(f"agent tool denial: {t}")
    ctx = summary.get("context") or {}
    for key in ("ledger_read", "ledger_write"):
        if ctx.get(key):
            lines.append(f"scheduled run reads the ledger ({key}: {ctx[key]})"
                         if key == "ledger_read" else
                         f"scheduled run writes the ledger ({key}: {ctx[key]})")
    for v in ctx.get("graph_actions") or []:
        lines.append(f"scheduled run may call graph_actions {v} (mediated)")
    for key in _CONTEXT_FLAGS:
        if ctx.get(key):
            lines.append(f"scheduled run may use {key}")
    for c in summary["cards"]:
        a = c.get("approve") or {}
        lines.append(f"confirmation card {c['id']}: watches {c['pending_file']}; on approve runs "
                     f"{' '.join(a.get('command') or [])} as the {a.get('user', 'resident')}")
        if c.get("cancel"):
            lines.append(f"  on cancel runs {' '.join(c['cancel']['command'])} "
                         f"as the {c['cancel']['user']}")
    mr = summary["mail_rules"]
    for s in mr["closure_confirm_senders"]:
        lines.append(f"mail rule: mail from *{s}* can close a ledger row")
    for s in mr["closure_exclude_senders"]:
        lines.append(f"mail rule: mail from *{s}* never closes a ledger row")
    for d in mr["keep_from_anthropic_domains"]:
        lines.append(f"mail rule: mail from {d} stays with the remote model")
    for t in summary.get("mail_triggers") or []:
        what = []
        if t["from"]:
            what.append("from " + ", ".join(t["from"]))
        if t["topics"]:
            what.append("tagged " + ", ".join(t["topics"]))
        if t["subject"]:
            what.append("subject has " + " or ".join(repr(x) for x in t["subject"]))
        if t["bulk"]:
            what.append("bulk mail only")
        if t["exclude_from"]:
            what.append("not from " + ", ".join(t["exclude_from"]))
        lines.append(f"mail trigger {t['id']}: new mail {'; '.join(what)} is copied, "
                     "with its text, to the plugin as it arrives")
    for d in summary["network"]["domains"]:
        lines.append(f"network: reaches {d}")
    for e in summary["network"]["local"]:
        lines.append(f"network: the resident may reach {e['proto']} {e['daddr']}:{e['ports']} "
                     f"({e['reason']})")
    for s in summary["secrets"]:
        lines.append(f"secret: {s}")
    oh = summary["owner_hooks"]
    for v in oh["verbs"]:
        alias = f" (also as {v['alias']['tool']} {v['alias']['verb']})" if v.get("alias") else ""
        lines.append(f"owner verb {v['name']} [{v['class'].upper()}]{alias}: "
                     f"{' '.join(v['command'])}")
    for a in oh["auth_sources"]:
        lines.append(f"owner auth source {a['name']} ({a['kind']}): {a['module']}:{a['function']}")
    for c in oh["cron"]:
        lines.append(f"owner cron {c['label']}: {c['cron']} {' '.join(c['command'])}")
    if summary["claude_plugin"]:
        lines.append(f"Claude Code plugin loaded into agent sessions (skills, agents, hooks, "
                     f"MCP servers): {summary['claude_plugin']}")
    return lines


def needs_explicit_consent(summary: dict) -> bool:
    """True when a trusted source is not enough: the plugin runs code as the
    owner. As in OpenClaw, trusting a source never grants owner tools."""
    oh = summary["owner_hooks"]
    owner_cards = any((c.get("approve") or {}).get("user") == "owner"
                      or (c.get("cancel") or {}).get("user") == "owner"
                      for c in summary["cards"])
    return (summary["zone"] == "owner" or owner_cards
            or bool(oh["verbs"] or oh["auth_sources"] or oh["cron"]))

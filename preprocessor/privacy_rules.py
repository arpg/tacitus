#!/usr/bin/env python3
"""Keep levels: how much of one message a hosted model may see. A hosted
model is any frontier model the EA runs on: Claude, Codex, or one added
later. Every check here keys on that class, never on a model's name
(model_routing.is_hosted). docs/remote-llm.md, "Keep from frontier models".

Four levels, from the most private:

  local    the cron lanes drop it and the mediator refuses a cron read of
           it; a chat read gets the local model's record, never the text;
           its files are never read or described
  facts    code-derived fields only (mail_privacy.FACT_FIELDS): sender,
           subject, dates, offered and accepted times, the kind of ask,
           the deadline, file names and flags; no text and no summary
  summary  the local model's record: the summary, the ask, the deadline.
           The EA tracks the thread, drafts through the writer and
           schedules; it never reads the text or the files
  full     ordinary mail

The level comes from matchers checked in this order:

  participant  anyone on the keep list (`keep_from_frontier` in
               remote-llm.json, owner-private): local. No rule in the
               preferences store raises it.
  person       a rule the user gave for one address, a name or a domain.
               A domain rule only keeps mail closer (local or facts); a
               rule that opens mail names one person.
  topic        mail from outside the institution that the admissions
               filter scores as applicant mail: local by default.
  engaged      the user has written in the thread, has written to the
               sender (the engaged-person store, the style profiles, recent
               sent mail), keeps the sender in the project store, or one of
               the admissions filter's own exemptions fires: topic mail
               rises to the engaged rule's level (summary, the ceiling the
               owner chose).
  attachments  the files on topic mail stay local at every level; a
               file is read only at full.

The rules live in the user's preferences store (preferences.py) under the
scope `privacy`, in plain sentences. The built-in rules are seeded there
on first use, so `preferences.py list --scope privacy` shows them and
`retire --id <id>` withdraws one (the keep-list rule follows the private
config and cannot be retired). A rule the user adds outranks them:

    preferences.py add --kind instruction --scope privacy \\
        --text "Treat Pat Doe as a normal correspondent." \\
        --quote "<the user's words>" --match person:pat@example.org --level full

The level is decided again at each read from the facts the extraction
stored (keep_facts on the record), the rules and the engagement now, so a
reply the user sends today lifts the applicant's earlier mail too.

    privacy_rules.py show [--json]     the rules in force, in plain words
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import remote_llm  # noqa: E402

LEVELS = ("local", "facts", "summary", "full")
RANK = {lvl: i for i, lvl in enumerate(LEVELS)}
# The words every surface uses for a level.
PLAIN = {"local": "Kept on your machine", "facts": "Facts only", "summary": "Summary",
         "full": "Normal mail"}
SCOPE = "privacy"
MATCHES = ("participant", "topic", "engaged", "attachments", "person", "name", "domain")
PERSON_MATCHES = ("person", "name", "domain")
OPEN_LEVELS = ("summary", "full")

# The built-in rules, seeded into the preferences store. Their text is
# what the user reads in `preferences.py list --scope privacy`.
DEFAULTS = (
    {"id": "privacy-keep-list", "privacy": {"match": "participant", "level": "local"},
     "text": "Mail with anyone on your keep list stays on your machine. The list is in your "
             "private settings; change it there."},
    {"id": "privacy-applicant-mail", "privacy": {"match": "topic", "level": "local"},
     "text": "Mail from prospective students stays on your machine."},
    {"id": "privacy-engaged-applicants", "privacy": {"match": "engaged", "level": "summary"},
     "text": "Once you have written to an applicant, the assistant may read a short summary of "
             "their mail, made on your machine. It never reads the mail itself."},
    {"id": "privacy-applicant-files", "privacy": {"match": "attachments", "level": "local"},
     "text": "Files that prospective students send stay on your machine."},
)
DEFAULT_IDS = tuple(d["id"] for d in DEFAULTS)
# A rule that follows the private config: the store shows it, and only
# the config changes it.
FIXED_IDS = ("privacy-keep-list",)

ENGAGED_FILE = "engaged.json"
ENGAGED_CAP = 5000
CTX_TTL_S = 120.0
PARTICIPANT_CAP = 20

_ADDR = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_DOMAIN = re.compile(r"(?<![\w@.])@?((?:[a-z0-9-]+\.)+[a-z]{2,})\b", re.I)


class RuleError(ValueError):
    """A privacy rule the code cannot apply, in plain words."""


# ── levels ────────────────────────────────────────────────────────────

def rank(level: str) -> int:
    return RANK.get(level, RANK["full"])


def stricter(a: str, b: str) -> str:
    return a if rank(a) <= rank(b) else b


def plain(level: str) -> str:
    return PLAIN.get(level, PLAIN["full"])


# Words that name a level, checked in this order ("keep everything" is
# local before "everything" is read as full).
_LEVEL_WORDS = (
    ("local", re.compile(r"\b(?:on (?:my|this|your) (?:own )?(?:machine|computer|server)|local(?:ly)?|"
                         r"keep (?:it|them|all|everything)|nothing at all|see nothing|never)\b", re.I)),
    ("facts", re.compile(r"\b(?:facts?|who (?:wrote|sent)|only who|when it came|not (?:the )?words|"
                         r"not what)\b", re.I)),
    ("summary", re.compile(r"\bsummar(?:y|ies|ise|ize)\b", re.I)),
    ("full", re.compile(r"\b(?:normal(?:ly)?|ordinary|like any other|full|everything|all of it)\b", re.I)),
)


def parse_level(text: str, answers: dict | None = None) -> str:
    """The level a user's words name, or ''. `answers` maps a numbered
    answer ("1", "2", "3") to its level, for a question that lists them."""
    t = " ".join(str(text or "").split())
    if not t:
        return ""
    if t.lower() in LEVELS:
        return t.lower()
    m = re.match(r"^\(?([1-9])\)?(?:[.):\s]|$)", t)
    if answers and m and m.group(1) in answers:
        return answers[m.group(1)]
    for level, rx in _LEVEL_WORDS:
        if rx.search(t):
            return level
    return ""


def name_key(name: str) -> str:
    """'Doe, Pat' and 'Pat Doe' are one person: sorted lowercase words of
    two or more letters, at least two of them (admissions_filter's key)."""
    words = sorted(w for w in re.findall(r"[a-z]{2,}", (name or "").lower()))
    return " ".join(words) if len(words) >= 2 else ""


def _domain_of(addr: str) -> str:
    return (addr or "").rsplit("@", 1)[-1].strip().lower()


def _in_domain(addr: str, dom: str) -> bool:
    d = _domain_of(addr)
    return bool(d and dom and (d == dom or d.endswith("." + dom)))


# ── rules ─────────────────────────────────────────────────────────────

def check(priv: dict) -> dict:
    """A privacy matcher made whole, or RuleError: a known match, a level,
    a value where the match needs one, and no domain-wide opening."""
    if not isinstance(priv, dict):
        raise RuleError("a privacy rule needs a match and a level")
    match = str(priv.get("match") or "").strip().lower()
    level = str(priv.get("level") or "").strip().lower()
    value = str(priv.get("value") or "").strip()
    if match not in MATCHES:
        raise RuleError(f"the match must be one of {', '.join(MATCHES)}")
    if level not in LEVELS:
        raise RuleError(f"the level must be one of {', '.join(LEVELS)}")
    out = {"match": match, "level": level}
    if match in PERSON_MATCHES:
        if match == "person":
            value = value.lower().lstrip("<").rstrip(">")
            if not _ADDR.fullmatch(value):
                raise RuleError("a person rule names one email address")
        elif match == "domain":
            value = value.lower().lstrip("@.").rstrip(".")
            if "@" in value or "." not in value:
                raise RuleError("a domain rule names a domain, such as example.org")
            if level in OPEN_LEVELS:
                raise RuleError("a rule that opens mail names one person; a whole domain can only "
                                "be kept closer (local or facts)")
        else:
            if not name_key(value):
                raise RuleError("a name rule needs a first and a last name; or give the address")
        out["value"] = value
    return out


def parse_rule(text: str) -> dict | None:
    """A matcher read from a plain sentence, or None: an address, a domain
    or a name ("treat <name> as ..."), and a level word. Shapes: "Treat
    <person> as a normal correspondent." and "Keep everything from
    <domain> on my machine." """
    t = " ".join(str(text or "").split())
    level = parse_level(t)
    if not level:
        return None
    m = _ADDR.search(t)
    if m:
        return {"match": "person", "value": m.group(0).lower(), "level": level}
    m = _DOMAIN.search(t)
    if m:
        return {"match": "domain", "value": m.group(1).lower(), "level": level}
    m = re.search(r"\btreat\s+(.+?)\s+as\b", t, re.I)
    if m and name_key(m.group(1)):
        return {"match": "name", "value": m.group(1).strip(), "level": level}
    return None


def rule_privacy(rule: dict) -> dict | None:
    """The checked matcher of one store rule, or None when the rule names
    none the code can apply."""
    try:
        if isinstance(rule.get("privacy"), dict):
            return check(rule["privacy"])
        parsed = parse_rule(rule.get("text") or "")
        return check(parsed) if parsed else None
    except RuleError:
        return None


def prefs_path() -> Path:
    """The preferences store as the EA_DIR of this call names it."""
    ea = Path(os.environ.get("EA_DIR", Path.home() / ".ea"))
    return ea / "config" / "preferences.json"


def _prefs():
    try:
        import preferences
    except ModuleNotFoundError:
        from preprocessor import preferences
    return preferences


def _is_privacy(rule: dict) -> bool:
    return SCOPE in [str(s).strip().lower() for s in rule.get("scopes") or []]


def effective(data: dict | None = None) -> list[dict]:
    """The rules in force, in the order they are checked: the user's own
    (newest first), then the built-in ones that are not retired. A
    built-in rule missing from the store applies as code holds it. Each
    row: {"id", "match", "level", "value"?, "text", "source", "builtin"}."""
    data = data if data is not None else _prefs().load(prefs_path())
    rules = [r for r in data.get("rules") or [] if isinstance(r, dict)]
    by_id = {r.get("id"): r for r in rules}
    own = []
    for i, r in enumerate(rules):
        if r.get("id") in DEFAULT_IDS or not _is_privacy(r) or r.get("status", "active") != "active":
            continue
        p = rule_privacy(r)
        if p:
            own.append((str(r.get("date") or ""), i, {**p, "id": r.get("id") or "", "text": r.get("text") or "",
                                                     "source": r.get("source") or "", "builtin": False}))
    own.sort(key=lambda x: (x[0], x[1]), reverse=True)
    out = [row for _d, _i, row in own]
    for d in DEFAULTS:
        row = by_id.get(d["id"])
        if row is not None and row.get("status", "active") != "active" and d["id"] not in FIXED_IDS:
            continue
        out.append({**d["privacy"], "id": d["id"], "text": (row or d).get("text") or d["text"],
                    "source": "default", "builtin": True})
    return out


def unreadable(data: dict | None = None) -> list[str]:
    """Ids of active privacy rules whose matcher the code cannot read."""
    data = data if data is not None else _prefs().load(prefs_path())
    return [r.get("id") or "?" for r in data.get("rules") or []
            if isinstance(r, dict) and _is_privacy(r) and r.get("status", "active") == "active"
            and r.get("id") not in DEFAULT_IDS and rule_privacy(r) is None]


def seed(data: dict) -> list[str]:
    """Add the built-in rules the store lacks (an edit of `update`);
    returns their ids. A retired one is not brought back."""
    have = {r.get("id") for r in data.get("rules") or [] if isinstance(r, dict)}
    added = []
    for d in DEFAULTS:
        if d["id"] in have:
            continue
        data.setdefault("rules", []).append({
            "id": d["id"], "kind": "instruction", "scopes": [SCOPE], "text": d["text"], "quote": "",
            "date": date.today().isoformat(), "source": "default", "status": "active",
            "privacy": dict(d["privacy"])})
        added.append(d["id"])
    return added


def ensure_seeded(path: Path | None = None) -> list[str] | None:
    """Seed the built-in rules into the store when any is missing; the ids
    added, or None when the store could not be written. Never raises: the
    rules apply from code either way."""
    prefs = _prefs()
    path = path or prefs_path()
    try:
        data = prefs.load(path)
        have = {r.get("id") for r in data.get("rules") or [] if isinstance(r, dict)}
        if all(i in have for i in DEFAULT_IDS):
            return []
        added = prefs.update(seed, path) or []
        reset()
        return added
    except Exception as e:
        print(f"privacy_rules: the built-in rules were not seeded: {str(e)[:120]}", file=sys.stderr)
        return None


def _first(rules: list[dict], match: str) -> dict | None:
    return next((r for r in rules if r.get("match") == match), None)


# ── the engaged-person store (owner-private) ──────────────────────────

def _store_path() -> Path:
    return remote_llm.store_dir() / ENGAGED_FILE


def load_engaged() -> dict[str, str]:
    """{address: when the user last wrote to it}. Empty outside an owner
    process."""
    if not remote_llm._owner():
        return {}
    try:
        import owner_trust
        raw = json.loads(owner_trust.read_owner_file(_store_path()))
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"privacy_rules: the engaged store is unreadable: {str(e)[:120]}", file=sys.stderr)
        return {}
    people = raw.get("people") if isinstance(raw, dict) else None
    return {str(k).lower(): str(v) for k, v in (people or {}).items() if isinstance(k, str) and "@" in k}


def note_engaged(sent: dict[str, str]) -> int:
    """Record {address: when} for mail the user sent; the number of
    addresses new to the store. Owner process; a no-op elsewhere."""
    if not sent or not remote_llm._owner():
        return 0
    import owner_trust
    people = load_engaged()
    new = 0
    for addr, at in sent.items():
        a = str(addr or "").strip().lower()
        if not _ADDR.fullmatch(a):
            continue
        if a not in people:
            new += 1
        if str(at or "") > people.get(a, ""):
            people[a] = str(at or "")
    if len(people) > ENGAGED_CAP:
        people = dict(sorted(people.items(), key=lambda kv: kv[1], reverse=True)[:ENGAGED_CAP])
    owner_trust.write_owner_file(_store_path(), (json.dumps({"version": 1, "people": people}) + "\n").encode(),
                                 mode=0o600)
    _CTX.update(at=0.0, ctx=None)
    return new


# ── the context of a decision ─────────────────────────────────────────

_CTX: dict = {"at": 0.0, "ctx": None}


def reset() -> None:
    """Drop the cached context (a test, or a write that changes it)."""
    _CTX.update(at=0.0, ctx=None)


def _user_addresses() -> set[str]:
    try:
        import mail_privacy
        return set(mail_privacy._user_addresses())
    except Exception:
        return set()


def _own_convs() -> set[str]:
    try:
        import mail_privacy
        return set((mail_privacy.load_state().get("own_sent") or {}).keys())
    except Exception:
        return set()


def _project_people() -> set[str]:
    try:
        import ledger
        import project_store
        con = ledger.guarded_connect(project_store.DB_PATH, read_only=True)
        try:
            return {str(r[0]).strip().lower() for r in con.execute("SELECT email FROM people") if r[0]}
        finally:
            con.close()
    except Exception:
        return set()


def _admissions_context(user: set[str], known: set[str], convs: set[str]) -> dict:
    """The admissions filter's own context (its exemptions): the full one
    in an owner process, a minimal one elsewhere."""
    base = {"user": set(user), "known_addrs": set(known), "known_names": set(), "sent_convs": set(convs),
            "vips": set(), "lists": None, "allowed_senders": set(), "allowed_convs": set()}
    if not remote_llm._owner():
        return base
    try:
        import admissions_filter as af
        lists = None
        try:
            import list_digest
            lists = list_digest.load_config().get("lists") or None
        except Exception:
            lists = None
        state = af.load_state()
        if Path(af.STYLE_DB).exists():
            full = af.build_context(sent=None, lists=lists, state=state)
        else:
            # No style profiles yet (a new install): the rest of the
            # filter's context, with no line about the missing store.
            full = {**base, "vips": af._vips(), "lists": lists,
                    "allowed_senders": {a.lower() for a in state.get("allowed_senders") or []},
                    "allowed_convs": set(state.get("allowed_conversations") or [])}
        full["user"] |= base["user"]
        full["known_addrs"] |= base["known_addrs"]
        full["sent_convs"] |= base["sent_convs"]
        return full
    except Exception as e:
        print(f"privacy_rules: the admissions context is partial: {str(e)[:120]}", file=sys.stderr)
        return base


def build_context(cfg: dict | None = None, *, data: dict | None = None, rules: list[dict] | None = None,
                  own_convs=None, engaged=None, people=None, user=None, af_ctx: dict | None = None) -> dict:
    """Everything a decision reads besides the message. Each part can be
    given (a test); the rest is read: the rules from the store, the
    user's threads from the tick state, the engaged store, the project
    store's people and the admissions filter's context."""
    cfg = cfg if cfg is not None else remote_llm.config()
    if rules is None:
        try:
            rules = effective(data)
        except Exception as e:
            print(f"privacy_rules: the store is unreadable, the built-in rules apply: {str(e)[:120]}",
                  file=sys.stderr)
            rules = effective({"rules": []})
    owner = remote_llm._owner()
    own_convs = set(own_convs) if own_convs is not None else (_own_convs() if owner else set())
    engaged = {str(a).lower() for a in (engaged if engaged is not None else (load_engaged() if owner else {}))}
    people = {str(a).lower() for a in (people if people is not None else (_project_people() if owner else set()))}
    user = set(user) if user is not None else _user_addresses()
    if af_ctx is None:
        af_ctx = _admissions_context(user, engaged | people, own_convs)
    return {"cfg": cfg, "keep": remote_llm.keep_list(cfg), "rules": rules, "own_convs": own_convs,
            "engaged": engaged, "people": people, "user": user, "af": af_ctx}


def context(*, refresh: bool = False) -> dict:
    """The decision context, cached for CTX_TTL_S per process (the
    mediator and the mail tick are long-running)."""
    now = time.monotonic()
    if not refresh and _CTX["ctx"] is not None and now - _CTX["at"] < CTX_TTL_S:
        return _CTX["ctx"]
    ctx = build_context()
    _CTX.update(at=now, ctx=ctx)
    return ctx


# ── facts and the decision ────────────────────────────────────────────

def _addr(rec) -> str:
    return (((rec or {}).get("emailAddress") or {}).get("address") or "").strip().lower()


def _name(rec) -> str:
    return (((rec or {}).get("emailAddress") or {}).get("name") or "").strip()


def participants(msg: dict) -> list[str]:
    out = []
    for a in [_addr(msg.get("from"))] + [_addr(r) for k in ("toRecipients", "ccRecipients")
                                         for r in msg.get(k) or []]:
        if a and a not in out:
            out.append(a)
    return out[:PARTICIPANT_CAP]


def _body_exemption(body: str) -> str:
    try:
        import admissions_filter as af
        text = af._norm(body or "")
        if af._ANSWERS_USER.search(text):
            return "answers something the user wrote"
        if af._REFERRAL.search(text):
            return "a third party introduces a student"
    except Exception:
        pass
    return ""


def message_facts(msg: dict, body: str, cfg: dict | None = None, *, own: bool = False,
                  ctx: dict | None = None) -> dict:
    """What a decision needs of one Graph-shaped message, from its headers
    and text, in code: the keep-list reason, whether it is applicant mail
    (and its score), the admissions filter's exemption that fires, the
    participants and the sender's name. Stored on the record as
    keep_facts; no text is kept."""
    cfg = cfg if cfg is not None else remote_llm.config()
    people = participants(msg)
    listed = ""
    for a in people:
        listed = remote_llm.keep_reason(a, cfg)
        if listed:
            break
    frm = _addr(msg.get("from"))
    facts = {"listed": listed, "topic": False, "score": 0, "exempt": "",
             "participants": people, "name": _name(msg.get("from"))[:80]}
    if own or not frm or remote_llm.keep_list(cfg).get("admissions", True) is False:
        return facts
    try:
        import admissions_filter as af
        if af.is_internal(frm):
            return facts
        score, _kinds = af.score_text(msg.get("subject") or "", body or "")
    except Exception:
        return facts
    facts["score"] = score
    if score >= af.MATCH_SCORE:
        facts["topic"] = True
        why = _body_exemption(body)
        if not why:
            try:
                ctx = ctx if ctx is not None else context()
                why = af.exclusion(msg, ctx["af"]) if ctx.get("af") else ""
            except Exception:
                why = ""
        facts["exempt"] = why
    return facts


def engaged_reason(facts: dict, *, sender: str, name: str = "", conv: str = "", own: bool = False,
                   ctx: dict) -> str:
    """Why the user is engaged with this sender or thread, or ''."""
    if own:
        return "the user's own message"
    if facts.get("exempt"):
        return str(facts["exempt"])
    s = (sender or "").strip().lower()
    if conv and conv in ctx.get("own_convs", ()):
        return "the user wrote in this thread"
    if s and s in ctx.get("engaged", ()):
        return "the user has written to them"
    if s and s in ctx.get("people", ()):
        return "a person in the user's projects"
    af_ctx = ctx.get("af")
    if s and af_ctx:
        try:
            import admissions_filter as af
            why = af.exclusion({"from": {"emailAddress": {"address": s, "name": name or ""}},
                                "conversationId": conv or ""}, af_ctx)
            if why and why not in ("no sender", "internal sender", "from the user"):
                return why
        except Exception:
            pass
    return ""


def _matches(rule: dict, *, sender: str, name: str, people: list[str]) -> bool:
    m, v = rule.get("match"), str(rule.get("value") or "")
    if m == "person":
        # A rule that opens mail follows its sender; one that keeps it
        # closer covers the person anywhere on the message.
        return sender == v if rule.get("level") in OPEN_LEVELS else v in people
    if m == "name":
        k = name_key(v)
        return bool(k) and k == name_key(name)
    if m == "domain":
        return any(_in_domain(a, v) for a in people)
    return False


def decide(facts: dict, *, sender: str = "", name: str = "", conv: str = "", own: bool = False,
           ctx: dict | None = None) -> dict:
    """{"level", "reason", "rule", "files_kept", "engaged"} for one
    message: `reason` says why it is kept ('' at full), `rule` the rule
    that set the level, `files_kept` whether its files stay on the
    machine (always below full, and for applicant mail at every level)."""
    ctx = ctx if ctx is not None else context()
    rules = ctx.get("rules") if ctx.get("rules") is not None else effective({"rules": []})
    sender = (sender or "").strip().lower()
    people = [str(a).lower() for a in facts.get("participants") or ([sender] if sender else [])]
    name = name or str(facts.get("name") or "")

    def out(level, reason, rule, *, files=False, engaged=""):
        return {"level": level, "reason": reason if level != "full" else "", "rule": rule,
                "files_kept": bool(files) or level != "full", "engaged": engaged}

    listed = str(facts.get("listed") or "")
    if not listed:
        for a in people:
            listed = remote_llm.keep_reason(a, ctx.get("cfg") or {})
            if listed:
                break
    if listed:
        return out("local", listed, "privacy-keep-list")
    topic = bool(facts.get("topic")) and ctx.get("keep", {}).get("admissions", True) is not False
    files = bool(topic and _first(rules, "attachments"))
    for r in rules:
        if r.get("match") in PERSON_MATCHES and _matches(r, sender=sender, name=name, people=people):
            return out(r["level"], "your rule", r.get("id") or "", files=files)
    if not topic:
        return out("full", "", "")
    trule = _first(rules, "topic")
    level, rule_id = (trule["level"], trule.get("id") or "") if trule else ("full", "")
    erule = _first(rules, "engaged")
    why = ""
    if erule and rank(erule["level"]) > rank(level):
        why = engaged_reason(facts, sender=sender, name=name, conv=conv, own=own, ctx=ctx)
        if why:
            level, rule_id = erule["level"], erule.get("id") or ""
    return out(level, "admissions", rule_id, files=files, engaged=why)


def for_message(msg: dict, body: str = "", cfg: dict | None = None, *, own: bool = False,
                ctx: dict | None = None) -> dict:
    """decide() for a Graph-shaped message read here (the tagger, the
    plugin triggers, the hosted admissions digest)."""
    ctx = ctx if ctx is not None else context()
    facts = message_facts(msg, body, cfg if cfg is not None else ctx.get("cfg"), own=own, ctx=ctx)
    return decide(facts, sender=_addr(msg.get("from")), name=_name(msg.get("from")),
                  conv=msg.get("conversationId") or "", own=own, ctx=ctx)


# ── what the rules say, in plain words ────────────────────────────────

def describe(ctx: dict | None = None) -> list[str]:
    """One line per rule in force, in order."""
    ctx = ctx if ctx is not None else context()
    keep = ctx.get("keep") or {}
    lines = []
    for r in ctx.get("rules") or []:
        if r["match"] == "participant":
            n_d, n_a = len(keep.get("domains") or []), len(keep.get("addresses") or [])
            lines.append(f"Keep list ({n_d} domain{'s' if n_d != 1 else ''}, {n_a} "
                         f"address{'es' if n_a != 1 else ''}): {plain(r['level'])}.")
        elif r["match"] == "topic":
            if keep.get("admissions", True) is False:
                lines.append("Applicant mail: the rule is off in your private settings.")
            else:
                lines.append(f"Applicant mail: {plain(r['level'])}.")
        elif r["match"] == "engaged":
            lines.append(f"Applicants you have written to: {plain(r['level'])}.")
        elif r["match"] == "attachments":
            lines.append(f"Files from applicants: {plain(r['level'])}.")
        else:
            lines.append(f"{r.get('value')}: {plain(r['level'])} (your rule {r.get('id')}).")
    return lines


def doctor_row(cfg: dict | None = None) -> tuple[str, str, str]:
    """(PASS | WARN, the rules in force in one line, the fix)."""
    cfg = cfg if cfg is not None else remote_llm.config()
    ctx = build_context(cfg)
    own = [r for r in ctx["rules"] if not r.get("builtin")]
    detail = " ".join(describe(ctx))
    if own:
        detail += f" {len(own)} rule{'s' if len(own) != 1 else ''} of yours."
    fixes = []
    if cfg.get("keep_legacy_key"):
        fixes.append(f"rename {remote_llm.LEGACY_KEEP_KEY} to {remote_llm.KEEP_KEY} in "
                     f"{remote_llm.CONFIG_FILE}")
    bad = unreadable()
    if bad:
        fixes.append(f"rules the code cannot read: {', '.join(bad[:5])} (add --match and --level, "
                     "or retire them)")
    return ("WARN" if fixes else "PASS"), detail, "; ".join(fixes)


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("show", help="the rules in force, in plain words")
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    ctx = build_context()
    if a.json:
        print(json.dumps({"rules": ctx["rules"], "lines": describe(ctx)}, indent=2))
    else:
        print("\n".join(describe(ctx)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

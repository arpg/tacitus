"""The kinds of work case (cases.py): one declaration per kind.

A WORK CASE is one piece of work someone asked the owner for that ends
with something delivered, not with a reply. What differs from one kind of
work to the next is data: the words the owner reads, the first decision
and its answers, what counts as delivery, how mail of the kind looks, the
default autonomy tiers, the reminder ladder and the guide the EA reads.
A kind is that data, declared here as a `Kind`; cases.py is the one
machine that runs every kind.

A core kind is one small file in this package that defines `KIND`. A
plugin declares a kind as data in its manifest
(`capabilities.case_kinds`, plugin_manifest.py); `from_manifest` turns
that into a `Kind` with the generic rule detector. A plugin kind never
replaces a core kind of the same name.

Pure: nothing here reads the store or the mail.
"""
from __future__ import annotations

import importlib
import re
from dataclasses import dataclass, field
from typing import Callable

CORE = ("letter", "review", "form", "report")

# The states every kind's parts share. `delivered` names the last one per
# kind ("submitted" for a letter); declined ends a part without it.
OPEN_STATES = ("asked", "accepted", "drafted", "ready")
METHODS = ("portal", "email", "upload", "unknown")


@dataclass(frozen=True)
class Decision:
    """The one question a new case brings, with its answers. A choice in
    `yes` accepts, one in `no` declines and closes; `then` names the next
    step a choice asks for (the work lane acts on it)."""
    question: str
    choices: tuple = ("Yes", "No")
    yes: tuple = ("Yes",)
    no: tuple = ("No",)
    then: dict = field(default_factory=dict)
    qkind: str = "decide"
    # The yes answers that carry the case's one date: the card offers each
    # with the date the records state ("Yes, by Tue Dec 1") and with a date
    # the owner picks; with no date stated, the yes asks for one.
    dated: tuple = ("Yes",)


@dataclass(frozen=True)
class Rules:
    """Detection for a kind as data, read by cases.rule_detect over one
    mail record. Patterns are regular expressions (case folded).

    request    the subject or the ask asks the owner for this work
    confirm    a service's notice that the work was received
    exclude    mail of the same words that asks for none of it
    senders    sender patterns ("@domain", or a substring with "@" or
               ".") whose mail is this kind's request
    confirm_senders  the same, for its confirmations
    automated_only   a request counts only from an automated sender
    fresh_only a request counts only in a fresh mail, not a reply or a
               forward (a thread about the work is not the request)
    states     the record states a request may have
    blocked_kinds    the record's confidential kinds that rule it out
    item       a pattern whose first group is the item's own id (a
               manuscript number, a form number): mail with the same id
               joins one case"""
    request: str = ""
    confirm: str = ""
    exclude: str = ""
    senders: tuple = ()
    confirm_senders: tuple = ()
    automated_only: bool = False
    fresh_only: bool = True
    states: tuple = ("todo", "needs-reply", "decide")
    blocked_kinds: tuple = ()
    item: str = ""

    def rx(self, name: str):
        pat = getattr(self, name)
        return _compiled(pat) if pat else None


_RX: dict = {}


def _compiled(pat: str):
    if pat not in _RX:
        _RX[pat] = re.compile(pat, re.I)
    return _RX[pat]


@dataclass(frozen=True)
class Kind:
    """One kind of work. Only `name` and `noun` are required; the rest
    has the defaults most kinds want."""
    name: str
    noun: str                                 # the line's head: "Review for <who>"
    part_noun: str = "part"
    part_nouns: str = "parts"
    delivered: str = "delivered"              # the last state, and its word
    subkinds: tuple = ()
    default_subkind: str = ""
    decision: Decision | None = None
    methods: tuple = METHODS
    material_kinds: tuple = ("document", "form", "instructions", "other")
    wanted_materials: tuple = ()
    send_delivers: tuple = ("email",)         # part methods a linked send delivers
    signals: tuple = ()                       # (delivered, declined, case done)
    counterpart: str = "org"                  # "person": joined by name; "org": by item
    ids: tuple = ()                           # id prefixes (case, part, question)
    no_parts_words: str = ""
    kept_row_words: str = ""
    response_mode: str = "submission"         # mail_completion's mode for its rows
    detect: Callable | None = None            # rec -> dict | None (else rule_detect)
    rules: Rules | None = None
    detect_default: bool = False              # detection at intake on by default
    autonomy: dict = field(default_factory=dict)
    ladder: tuple = (7, 3, 1, 0, -1)          # days before the date (negative: after)
    loud: tuple = (1, 0, -1)
    lead: int = 7                             # days before the case's date it comes back
    guide: str = ""                           # docs/ea/<guide>
    model_kinds: tuple = ()                   # the mail record's work.kind values
    trust_model: bool = False                 # the model's work field alone opens a case
    source: str = "core"

    # ── derived ──────────────────────────────────────────────────────
    @property
    def states(self) -> tuple:
        return OPEN_STATES + ("declined", self.delivered)

    @property
    def done_states(self) -> tuple:
        return ("declined", self.delivered)

    @property
    def signal_names(self) -> tuple:
        return self.signals or (f"{self.name}_delivered", f"{self.name}_declined",
                                f"{self.name}_done")

    @property
    def prefixes(self) -> tuple:
        if self.ids:
            return self.ids
        stem = re.sub(r"[^a-z0-9]", "", self.name.lower())[:12] or "work"
        return (f"case-{stem}", f"part-{stem}", f"q-{stem}")

    @property
    def question_kinds(self) -> tuple:
        return ((self.decision.qkind,) if self.decision else ()) + ("join", "part")

    def head(self, who: str) -> str:
        return f"{self.noun} for {who}"

    def kept_head(self) -> str:
        return f"{self.noun} (kept on your own model)"

    def moves(self) -> dict:
        """The state moves a part allows; every open state may also go to
        declined or straight to delivered (work done outside the EA)."""
        d = self.delivered
        return {"asked": ("accepted", "declined", "drafted", "ready", d),
                "accepted": ("drafted", "ready", "declined", d),
                "drafted": ("ready", "drafted", "declined", d),
                "ready": ("drafted", "declined", d),
                "declined": ("asked", "accepted"),
                d: ("accepted", "ready")}


# ── the registry ───────────────────────────────────────────────────────

def core_kinds() -> dict:
    """{name: Kind} for the kinds in this package, in CORE order."""
    out = {}
    for name in CORE:
        try:
            mod = importlib.import_module(f"{__name__}.{name}")
        except ImportError:
            continue
        k = getattr(mod, "KIND", None)
        if isinstance(k, Kind):
            out[k.name] = k
    return out


_SLUG = re.compile(r"^[a-z][a-z0-9_]{1,30}$")


def from_manifest(plugin: str, name: str, spec: dict) -> Kind | None:
    """A plugin's kind from its manifest's data (plugin_manifest has
    checked the shape). None for a name that is not a slug."""
    if not isinstance(spec, dict) or not _SLUG.match(str(name or "")):
        return None
    words = spec.get("words") if isinstance(spec.get("words"), dict) else {}
    dec = spec.get("decision") if isinstance(spec.get("decision"), dict) else None
    decision = None
    if dec and str(dec.get("question") or "").strip():
        choices = tuple(str(c) for c in dec.get("choices") or ("Yes", "No"))[:4]
        decision = Decision(question=str(dec["question"])[:120], choices=choices,
                            yes=tuple(c for c in choices if c in (dec.get("yes") or choices[:1])),
                            no=tuple(c for c in choices if c in (dec.get("no") or choices[-1:])))
    det = spec.get("detect") if isinstance(spec.get("detect"), dict) else {}
    rules = Rules(request=str(det.get("request") or ""), confirm=str(det.get("confirm") or ""),
                  exclude=str(det.get("exclude") or ""),
                  senders=tuple(str(s).lower() for s in det.get("senders") or ()),
                  confirm_senders=tuple(str(s).lower() for s in det.get("confirm_senders") or ()),
                  automated_only=bool(det.get("automated_only")))
    return Kind(name=name, noun=str(words.get("noun") or name.replace("_", " ").title())[:40],
                part_noun=str(words.get("part") or "part")[:40],
                part_nouns=str(words.get("parts") or (str(words.get("part") or "part") + "s"))[:40],
                delivered=str(words.get("delivered") or "delivered")[:20]
                if _SLUG.match(str(words.get("delivered") or "delivered")) else "delivered",
                decision=decision, rules=rules,
                lead=_lead(spec.get("lead_days")),
                detect_default=bool(spec.get("detect_default")),
                guide=str(spec.get("guide") or ""), source=f"plugin:{plugin}")


LEAD_MAX = 60


def _lead(value, default: int = 7) -> int:
    """A lead time in days (0 to LEAD_MAX), else `default`."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return n if 0 <= n <= LEAD_MAX else default


# ── helpers every kind's detection shares (pure) ───────────────────────

import unicodedata  # noqa: E402
from datetime import date  # noqa: E402
from urllib.parse import parse_qs, unquote, urlparse  # noqa: E402

_HONORIFIC = re.compile(r"^(?:dr|prof|professor|mr|mrs|ms|mx|miss)\.?\s+", re.I)


def norm_name(name: str) -> str:
    """A person's name for comparison: accents off, case folded, honorifics
    and punctuation gone, "Last, First" turned around."""
    text = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode()
    text = text.strip()
    if text.count(",") == 1 and "@" not in text:
        last, first = (p.strip() for p in text.split(","))
        if last and first:
            text = f"{first} {last}"
    text = _HONORIFIC.sub("", text)
    text = re.sub(r"[^A-Za-z\s'-]", " ", text).lower()
    return " ".join(text.replace("'", "").split())


def name_tokens(name: str) -> list[str]:
    return [t for t in re.split(r"[\s-]+", norm_name(name)) if t]


def name_match(a: str, b: str) -> str:
    """'same', 'maybe' or 'no' for two names of a person.

    same: the same words in any order, or the same first and last word with
    middle names left out ("Ana M. Ruiz" and "Ana Ruiz"), or a compound
    last name written with or without its hyphen.
    maybe: the same last name and a first name that is an initial or a
    prefix of the other ("A. Ruiz", "Alex Ruiz" and "Alexandra Ruiz"), or
    the same first name and only one of the last names of a compound one.
    no: anything else, and any name of one word."""
    ta, tb = name_tokens(a), name_tokens(b)
    if len(ta) < 2 or len(tb) < 2:
        return "no"
    if ta == tb or sorted(ta) == sorted(tb):
        return "same"
    na, nb = norm_name(a).replace("-", " ").split(), norm_name(b).replace("-", " ").split()
    if na == nb:
        return "same"
    fa, fb = ta[0], tb[0]
    la, lb = ta[-1], tb[-1]
    if fa == fb and la == lb:
        return "same"
    if la == lb and (fa[0] == fb[0]) and (fa.startswith(fb) or fb.startswith(fa) or
                                          len(fa) == 1 or len(fb) == 1):
        return "maybe"
    if fa == fb and (set(ta[1:]) & set(tb[1:])):
        return "maybe"
    return "no"


def day(value) -> str:
    """YYYY-MM-DD, or "" for anything that is not one."""
    v = str(value or "").strip()[:10]
    try:
        date.fromisoformat(v)
    except ValueError:
        return ""
    return v


def real_url(url: str) -> str:
    """A link with any mail scanner's wrapper taken off (a `url=` or `u=`
    query that holds the real address)."""
    u = str(url or "").strip()
    for _ in range(2):
        try:
            p = urlparse(u)
        except ValueError:
            return u
        q = parse_qs(p.query)
        inner = (q.get("url") or q.get("u") or q.get("target") or [""])[0]
        if inner.startswith("http"):
            u = unquote(inner)
            continue
        break
    return u


def sender_in(addr: str, pats) -> bool:
    a = (addr or "").lower()
    return any(p and (a == p or (p.startswith("@") and a.endswith(p)) or p in a) for p in pats)


# A reminder: the work is still owed.
REMINDER = re.compile(r"\b(?:reminder|remind\w*|still (?:need|waiting)|not yet (?:received|submitted|"
                      r"completed)|outstanding|overdue|past due|due (?:soon|in \d+ days?|tomorrow|"
                      r"today))\b", re.I)
# A sender that is a system, not a person.
AUTO_SENDER = re.compile(r"(?:^|[._+-])(?:no-?reply|do-?not-?reply|donotreply|notifications?|notify|"
                         r"mailer|system|portal|alerts?|automated|auto|admin|info|support|dse|"
                         r"esign\w*|sign|docusign|echosign|adobesign|forms?|surveys?|qualtrics|"
                         r"training|compliance|reports?|reporting|research\w*|grants?)(?:[._+-]|@)",
                         re.I)
REPLY_SUBJECT = re.compile(r"\s*(?:re|fw|fwd|aw|sv)\s*:", re.I)

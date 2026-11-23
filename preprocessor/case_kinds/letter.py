"""The letter kind: a letter someone asked the owner to write about a
person (a recommendation, a reference, a nomination, a letter of support,
a character or an evaluation letter), for a student, an employee, a
colleague, a tenant or a client. One case per person; its parts are the
DESTINATIONS (a program, an employer, an award, a portal), each with its
own date and way to submit. Only a submission finishes a destination.

This file is the kind's declaration (`KIND`, at the end) and its
detection: the common shapes of a request mail (a person's own ask, a
portal's request on the person's behalf, a reminder, a confirmation),
read from one mail record. No institution or vendor is named here; a
plugin adds sender patterns through
`capabilities.mail_rules.letter_request_senders` and
`letter_confirm_senders` (plugin_registry.mail_rules).
"""
from __future__ import annotations

import re

from . import Decision, Kind, day as _day, norm_name, real_url, sender_in
from urllib.parse import urlparse

KINDS = ("recommendation", "reference", "nomination", "support", "character", "evaluation", "other")
METHODS = ("portal", "email", "upload", "unknown")
MATERIAL_KINDS = ("cv", "statement", "list", "draft", "transcript", "sample", "other")
WRITE_CHOICES = ("Yes", "Yes, ask for materials", "No")


MATERIAL_WORDS = (("cv", r"\b(?:cv|resume|curriculum)\b"),
                  ("statement", r"\b(?:sop|statement|purpose|personal)\b"),
                  ("transcript", r"\btranscript"),
                  ("list", r"\b(?:programs?|schools?|list|deadlines?|positions?)\b"),
                  ("draft", r"\b(?:draft|template)\b"),
                  ("sample", r"\b(?:paper|manuscript|sample|thesis)\b"))


def material_kind(filename: str) -> str:
    """The kind of a file the person sent, from its name alone."""
    low = re.sub(r"[_.\-]+", " ", str(filename or "").lower())
    for kind, pat in MATERIAL_WORDS:
        if re.search(pat, low):
            return kind
    return "other"


LETTER_ROLES = ("none", "request", "reminder", "confirmation")

# A letter about a person, in the words of a request. Case matters only
# where a name follows (a capital letter after "for").
_NOUNS = r"(?:recommendation|reference|nomination|support|endorsement|character|evaluation)"
LETTER_RE_I = re.compile(
    r"\bletters?\s+of\s+(?:recommendation|reference|support|nomination|evaluation|endorsement)\b"
    r"|\b" + _NOUNS + r"\s+letters?\b"
    r"|\b(?:recommendation|reference|referee|evaluation)\s+(?:request|form|questionnaire|survey)s?\b"
    r"|\b(?:independent|online|confidential)\s+(?:recommendation|reference|evaluation)\b"
    r"|\brecommender\b|\breferee\s+(?:request|form|report)\b"
    r"|\b(?:submit|complete|upload|provide|write)\s+(?:a|an|the|your|my)?\s*(?:\w+\s+)?"
    r"(?:recommendation|reference)\b(?!\s+(?:on|about|to)\b)"
    r"|\b(?:listed|named|identified|selected|added)\s+you\s+as\s+(?:a|an|their|his|her)\s+"
    r"(?:reference|referee|recommender)\b", re.I)
LETTER_NAMED = re.compile(r"\b(?i:recommendations?|references?|nomination)\s+"
                          r"(?i:for|on behalf of)\s+[A-Z][a-z]")
LETTER_ABBR = re.compile(r"\b(?:LoR|LOR)s?\b")
_LNOUN = (r"(?:recommendation|reference|letter of (?:recommendation|reference|support|nomination|"
          r"evaluation)|(?:recommendation|reference|nomination|support|evaluation) letter|letter)")
_CONFIRM = re.compile(
    r"\b(?:thank(?:s| you)\s+(?:you\s+)?for\s+(?:submitting|uploading|providing|completing|sending)"
    r"(?:\s+(?:your|a|the))?\s+(?:\w+\s+)?" + _LNOUN +
    r"|" + _LNOUN + r"\s+(?:\w+\s+){0,3}(?:has|have)\s+been\s+(?:received|submitted|uploaded|completed)"
    r"|" + _LNOUN + r"s?\s+(?:was\s+|is\s+)?(?:submitted|received|uploaded)\b"
    r"|(?:" + _LNOUN + r"|evaluation)\s+(?:upload(?:ing)?|submission)\s+(?:confirm\w*|received|complete)"
    r"|confirm(?:s|ation)?\s+(?:of\s+)?(?:your\s+|the\s+)?(?:" + _LNOUN + r"|evaluation)\s+"
    r"(?:submission|upload|received)"
    r"|(?:we(?:'ve|’ve| have)\s+)?received\s+(?:the|your)\s+(?:\w+\s+)?" + _LNOUN +
    r"\s+(?:you\s+)?(?:submitted|uploaded|for))", re.I)
_REQUEST_VERB = re.compile(
    r"\b(?:request\w*|submit\w*|complete|upload\w*|write|writ\w+|provide|asks?\b|asked|"
    r"listed you|named you|identified you|selected you|as a (?:recommender|reference|referee)|"
    r"on behalf of|would you be (?:willing|able)|will you|could you|can you|willing to|"
    r"decide whether to (?:write|accept|serve)|confirm whether you (?:will|can|would) write)\b", re.I)
_REMINDER = re.compile(r"\b(?:reminder|remind\w*|still (?:need|waiting)|not yet (?:received|submitted)|"
                       r"outstanding|overdue|past due|due (?:soon|in \d+ days?|tomorrow|today))\b", re.I)
# Mail about letters that asks the owner for none: a call for
# nominations that names no person, a manuscript decision, a committee's
# own recommendation, advice asked for, letters about a program.
_NOT_A_REQUEST = re.compile(
    r"\b(?:call for nominations?|nominations? (?:are |is )?(?:due|requested|open|sought|invited)|"
    r"(?:solicit\w*|seeking|now accepting) nominations?|nominate (?:a|an|your|one|up to)\b|"
    r"manuscript|reviewer|associate editor|recommendation (?:to|of) (?:accept|reject|revise|"
    r"the editor)|editor'?s? recommendation|committee'?s?\s+(?:\w+\s+)?recommendation|"
    r"recommendations? (?:for|on|about) (?:commercial|robots?|tools?|software|vendors?|courses?|"
    r"books?|papers?|which|the (?:course|class|committee|department))|"
    r"letters? of (?:support|endorsement) for (?:the|our|a|an) (?:\w+\s+){0,3}"
    r"(?:program|proposal|major|minor|degree|center|grant|initiative)|"
    r"offer letter|initiat\w* (?:the |all |any )?(?:letters|references|reference checks)|"
    r"(?:letters|references)\b[^.]{0,40}\binitiat\w*|"
    r"request(?:ing)? (?:the )?(?:letters|references) from)", re.I)
_AUTO_SENDER = re.compile(r"(?:^|[._+-])(?:no-?reply|do-?not-?reply|donotreply|notifications?|"
                          r"mailer|admissions?|admit|apply|applications?|applicants?|recommend\w*|"
                          r"references?|referee|lor|support|system|portal|careers?|gradadm\w*|"
                          r"registration)(?:[._+-]|@)", re.I)
_LINK_WORDS = re.compile(r"(?:refer|recommend|reference|referee|lor\b|/lor|letter|evaluat|"
                         r"recommender|rec\b)", re.I)
_NAME_WORD = r"(?:[A-Z][a-z][A-Za-z'’]*(?:-[A-Z]?[a-z][A-Za-z'’]*)*|[A-Z]\.)"
_PERSON = (r"(" + _NAME_WORD + r"(?:\s+(?:" + _NAME_WORD + r"|[A-Z](?=\s)|de|da|van|von|del|della|"
           r"la|le|bin|al|ibn|dos|das)){1,4})")
_PERSON_RES = (
    re.compile(r"(?i:recommendation|reference|nomination|letter|evaluation|support|referee)s?\s+"
               r"(?i:(?:request|form|questionnaire|check|letter)\s+)?(?i:for|on behalf of|about)\s+"
               r"(?i:applicant\s+|candidate\s+|(?:your|my|our)\s+(?:former\s+|current\s+)?"
               r"(?:student|colleague|employee|tenant|client|report|intern)\s+)?" + _PERSON),
    re.compile(r"\b(?i:request|recommendation|reference|letter)\s+(?i:from)\s+" + _PERSON),
    re.compile(r"\b(?i:on behalf of)\s+" + _PERSON),
    re.compile(r"\b(?i:for)\s+(?i:applicant|candidate)\s+" + _PERSON),
    re.compile(_PERSON + r"\s+(?:has|have)\s+(?:listed|named|identified|selected|asked|requested|"
               r"invited|added)\s+you"),
    re.compile(r"\b(?i:applicant|candidate)[:\s]+" + _PERSON),
)
_DEST_RES = (
    re.compile(r"\bto\s+(?:the\s+)?([A-Z][\w&'’.,-]*(?:\s+(?:[A-Z][\w&'’.,-]*|of|for|"
               r"at|and|in|the|de)){0,9})\s+(?:via|through|using|by|before|at\b|on\b|portal|"
               r"online|with)"),
    re.compile(r"\bfor\s+(?:the\s+)?((?:University|College|Institute|School)[\w\s&'’.,-]{2,60}?)"
               r"(?:\s+(?:via|through|by|graduate|portal)|[,.;]|$)"),
    re.compile(r"\bfor\s+(?:the\s+)?([A-Z][\w&'’.-]*(?:\s+[A-Z][\w&'’.-]*){0,6})\s*$"),
)
_NOT_NAMES = {"the", "a", "an", "your", "our", "this", "that", "phd", "ms", "graduate", "admissions",
              "university", "college", "program", "school", "department", "recommendation",
              "letter", "reference", "fall", "spring", "summer", "winter", "dear", "professor",
              "dr", "prof", "you", "submit", "please", "request", "online", "no", "yes", "re",
              "fw", "fwd", "via", "for", "to", "and", "or", "of", "on", "in", "at", "by",
              "applicant", "candidate", "student", "reminder", "confirmation", "thank"}


def letterish(text: str) -> bool:
    """The words of a request for a letter about a person."""
    t = text or ""
    return bool(LETTER_RE_I.search(t) or LETTER_NAMED.search(t) or LETTER_ABBR.search(t))


_KIND_WORDS = (("nomination", r"\bnominat"), ("support", r"\b(?:letters? of support|support letters?)\b"),
               ("character", r"\bcharacter (?:letter|reference)\b"),
               ("reference", r"\b(?:reference|referee)\b"),
               ("evaluation", r"\bevaluation\b"),
               ("recommendation", r"\brecommend"))


def _portal_link(links) -> str:
    for raw in links or []:
        u = real_url(raw if isinstance(raw, str) else (raw or {}).get("url", ""))
        if not u.startswith("http"):
            continue
        p = urlparse(u)
        tail = f"{p.path}?{p.query}"
        if _LINK_WORDS.search(tail) and not re.search(r"help|faq|unsubscribe|privacy", tail, re.I):
            return u
    return ""


def _clean_person(name: str) -> str:
    """A person's name out of a matched phrase: cut at a possessive or at a
    word that is no part of a name; two to five words."""
    out = []
    for raw in str(name or "").split():
        if re.fullmatch(r"[A-Z]\.?", raw) and (out or raw.endswith(".")):
            out.append(raw.rstrip("."))  # an initial
            continue
        w = raw.strip(".,;:")
        poss = re.match(r"^(.+?)['’]s?$", w)
        if poss and len(w) > 2:
            w = poss.group(1)
        else:
            poss = None
        if not w or w.lower() in _NOT_NAMES or (w.isupper() and len(w) > 1):
            break
        out.append(w)
        if poss:
            break
    while out and len(out[-1]) <= 2 and out[-1].endswith(".") is False and out[-1].islower():
        out.pop()
    if len(out) < 2 or any(w.lower() in ("university", "college", "institute", "school") for w in out):
        return ""
    return " ".join(out)[:80]


def find_person(text: str) -> str:
    for rx in _PERSON_RES:
        for m in rx.finditer(text or ""):
            name = _clean_person(m.group(1))
            if name:
                return name
    return ""


def find_destination(*texts: str) -> str:
    for text in texts:
        for rx in _DEST_RES:
            m = rx.search(text or "")
            if m:
                dest = m.group(1).strip(" ,.-")
                dest = re.sub(r"\s+(?:via|through|by|graduate admissions)$", "", dest, flags=re.I)
                if len(dest) > 3 and dest.lower() not in _NOT_NAMES:
                    return dest[:120]
    return ""


def letter_kind(text: str) -> str:
    for kind, pat in _KIND_WORDS:
        if re.search(pat, text or "", re.I):
            return kind
    return "recommendation"


def _plugin_senders() -> tuple[list[str], list[str]]:
    """(request senders, confirmation senders) the enabled plugins add."""
    try:
        try:
            import plugin_registry
        except ModuleNotFoundError:  # imported as preprocessor.<mod>
            from preprocessor import plugin_registry
        rules = plugin_registry.mail_rules()
    except Exception:
        return [], []
    return (list(rules.get("letter_request_senders") or []),
            list(rules.get("letter_confirm_senders") or []))


_sender_in = sender_in


# The local model's sensitivity kinds under which a letter request about
# a person can sit: a request with no name in it needs one of these.
_PERSON_KINDS = ("letter", "personnel", "student-record", "admissions")


def _rule_detect(subject: str, ask: str, summary: str, sender: str, conf_kind: str,
                 automated: bool, req_pats, conf_pats) -> dict | None:
    """The rules for a record with no `letter` field. A request reads as one
    in its subject or its ask (the summary only names the person); a
    confirmation may say so anywhere."""
    asked = f"{subject} {ask}"
    every = f"{subject} {ask} {summary}"
    plugin_req, plugin_conf = _sender_in(sender, req_pats), _sender_in(sender, conf_pats)
    person = find_person(ask) or find_person(subject) or find_person(summary)
    fresh = not re.match(r"\s*(?:re|fw|fwd|aw|sv)\s*:", subject, re.I)
    if plugin_conf or (_CONFIRM.search(every) and (automated or fresh)):
        # Only a service's own notice counts: a person's "thanks for
        # sending it" in a thread is no proof of which place got it.
        role = "confirmation"
    elif _CONFIRM.search(every) and not automated and not _REQUEST_VERB.search(ask):
        return None
    elif plugin_req:
        role = "reminder" if _REMINDER.search(asked) else "request"
    elif not letterish(asked):
        return None
    elif conf_kind == "manuscript-review" or (_NOT_A_REQUEST.search(asked)
                                              and not find_person(asked)):
        return None
    elif not (_REQUEST_VERB.search(asked) or automated):
        return None
    elif not person and not automated and conf_kind and conf_kind not in _PERSON_KINDS:
        # A person's own ask names no one but comes as a letter matter;
        # letters talked about in other mail (a committee, a program) do not.
        return None
    else:
        role = "reminder" if _REMINDER.search(asked) else "request"
    return {"role": role, "source": "plugin" if plugin_req or plugin_conf else "rules",
            "kind": letter_kind(asked or every), "person": person,
            "destination": find_destination(ask, subject), "due": "", "method": "unknown",
            "destinations": []}


def detect(rec: dict, *, plugin_senders: tuple | None = None) -> dict | None:
    """What one mail record says about a letter: None, or {"role":
    request | reminder | confirmation, "kind", "person", "destination",
    "due", "method", "link", "attachments", "automated", "source": model |
    rules | plugin, "destinations": [{name, due}]}.

    `rec` is an extraction record (subject, from, own, result) or a
    record's fields with `subject` and `from` beside them. The local
    model's `letter` field decides when it is there; rules fill what it
    leaves out and decide for older records. The owner's own mail is
    never a request."""
    res = rec.get("result") if isinstance(rec.get("result"), dict) else rec
    if rec.get("own") or res.get("own"):
        return None
    subject = str(rec.get("subject") or res.get("subject") or "")
    sender = str(rec.get("from") or res.get("from") or "").lower()
    ask = str(res.get("ask") or "")
    summary = str(res.get("summary") or "")
    text = " ".join((subject, ask, summary))
    links = res.get("links") or []
    atts = [a.get("name") if isinstance(a, dict) else str(a) for a in res.get("attachments") or []]
    atts = [a for a in atts if a and not re.match(r"image\d+\.(?:png|jpe?g|gif)$", a, re.I)]
    model = res.get("letter") if isinstance(res.get("letter"), dict) else None
    req_pats, conf_pats = plugin_senders if plugin_senders is not None else _plugin_senders()
    link = _portal_link(links)
    automated = bool(_AUTO_SENDER.search(sender + "@")) or bool(link)
    out = None
    if model and model.get("role") in LETTER_ROLES[1:]:
        out = {"role": model["role"], "source": "model",
               "kind": model.get("kind") if model.get("kind") in KINDS else letter_kind(text),
               "person": _clean_person(model.get("person") or "") or find_person(text),
               "destination": str(model.get("destination") or "")[:120] or find_destination(ask, subject),
               "due": _day(model.get("due") or res.get("deadline") or ""),
               "method": model.get("method") if model.get("method") in METHODS else "unknown",
               "destinations": [{"name": str(d.get("name") or "")[:120], "due": _day(d.get("due"))}
                                for d in model.get("destinations") or []
                                if isinstance(d, dict) and d.get("name")][:12]}
    elif model and model.get("role") == "none" and not (_sender_in(sender, req_pats + conf_pats)):
        return None
    if out is None:
        out = _rule_detect(subject, ask, summary, sender, res.get("confidential_kind") or "",
                           automated, req_pats, conf_pats)
        if out is None:
            return None
        out["due"] = _day(res.get("deadline") or "")
    if out.get("destination") and out.get("person") and (
            norm_name(out["person"]) in norm_name(out["destination"])):
        out["destination"] = ""
    if out["role"] in ("request", "reminder"):
        if link:
            out["method"] = "portal"
        elif out["method"] == "unknown" and re.search(r"\b(?:portal|online|secure link|via the "
                                                      r"(?:provided )?link|upload)\b", text, re.I):
            out["method"] = "portal"
        elif out["method"] == "unknown" and re.search(r"\b(?:e-?mail (?:it|the letter|your letter)|"
                                                      r"send (?:it|the letter) to)\b", text, re.I):
            out["method"] = "email"
    out.update(link=link, attachments=atts[:10], automated=automated, sender=sender,
               subject=subject[:200])
    if not out["person"] and not automated and out["role"] in ("request", "reminder"):
        out["person_is_sender"] = True
    return out


KIND = Kind(
    name="letter", noun="Letter", part_noun="destination", part_nouns="destinations",
    delivered="submitted", subkinds=KINDS, default_subkind="recommendation",
    decision=Decision(question="Will you write it?", choices=WRITE_CHOICES,
                      yes=WRITE_CHOICES[:2], no=("No",),
                      then={WRITE_CHOICES[1]: "ask_materials"}, qkind="write"),
    material_kinds=MATERIAL_KINDS, wanted_materials=("cv", "statement"),
    send_delivers=("email",), signals=("letter_submitted", "letter_declined", "letter_case_done"),
    counterpart="person", ids=("lc", "ld", "lq"),
    no_parts_words="no places to send it yet.",
    kept_row_words="Letter request (kept on your own model)",
    response_mode="submission", detect=detect, detect_default=True,
    autonomy={"draft": "ask-first", "upload": "do-and-tell"}, guide="letters.md",
    model_kinds=("letter",))

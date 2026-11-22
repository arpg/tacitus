"""What completes an emailed request, independently of how it arrived.

New mail records name the response mode. Older records use the ask's
explicit work verbs; a recommendation subject alone never suppresses a
question about willingness, logistics, or an already submitted letter.
"""
from __future__ import annotations

import re

MODES = ("email", "submission", "document", "form", "other", "none", "unknown")
WORK_MODES = frozenset({"submission", "document", "form", "other"})
_DECISION = re.compile(r"\b(?:decide whether|confirm whether|confirm if|confirm your availability|"
                       r"confirm (?:you|he|the user) (?:will|can|would)|"
                       r"confirm or decline|willing to|able to)\b", re.I)
_SUBMIT = re.compile(r"\b(?:submit|upload|file|lodge)\b.{0,120}\b(?:letter|recommendation|"
                     r"reference|report|review|application|form|document|portal|secure link)\b", re.I)
_FORM = re.compile(r"\b(?:complete|sign|fill (?:in|out))\b.{0,80}\b(?:form|survey|questionnaire|"
                   r"application|evaluation)\b", re.I)
_DOCUMENT = re.compile(r"\b(?:write|draft|prepare|compose|revise)\b(?!\s+back\b)"
                       r"(?:(?!\b(?:emails?|repl(?:y|ies)|messages?)\b).){0,80}\b(?:letters?|"
                       r"recommendations?|reports?|manuscripts?|proposals?|documents?)\b", re.I)


def response_mode(item: dict) -> str:
    """email | submission | document | form | other | none | unknown.

    A declared submission remains outstanding even after an email. An
    explicit question about accepting the work still needs a decision.
    No keyword in a subject or a quoted earlier message decides this.
    """
    if item.get("closes_on") == "submission":
        return "submission"
    brief = item.get("thread_brief") or item.get("brief") or item.get("remote_record")
    brief = brief if isinstance(brief, dict) else {}
    mode = item.get("response_mode")
    if mode not in MODES or mode == "unknown":
        mode = brief.get("response_mode")
    if mode in MODES and mode != "unknown":
        return mode
    ask = str(item.get("ask") or brief.get("ask") or "")
    state = item.get("state") or brief.get("state")
    if state == "decide" or _DECISION.search(ask):
        return "unknown"
    if _SUBMIT.search(ask):
        return "submission"
    if _FORM.search(ask):
        return "form"
    if _DOCUMENT.search(ask):
        return "document"
    return "unknown"


def non_email_work(item: dict) -> bool:
    return response_mode(item) in WORK_MODES

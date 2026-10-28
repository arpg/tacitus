"""What a running turn is doing, in a sentence.

The runner feeds every tool call here as it starts and ends (Claude's
stream-json events, or Codex's item events); the heartbeat asks for a
summary every few minutes and posts it in place of "Still working":

  :hourglass_flowing_sand: 6 min in. So far: read the thread, found
  times, wrote the body. Now: creating the draft (40 s).

A tool call becomes a short phrase from the table below (the script
and verb for a Bash call, the tool name otherwise); consecutive repeats
collapse, and the last four phrases are shown.
"""
from __future__ import annotations

import re
import threading
import time

SHOWN = 4

# verb -> phrase, per preprocessor script; "_" is the script's default.
SCRIPTS: dict[str, dict[str, str]] = {
    "graph_actions": {"_": "working in Outlook", "search-mail": "searching mail", "list-mail": "listing mail",
                      "get-conversation": "reading the thread", "get-message": "reading the message",
                      "create-reply": "creating the draft", "create-draft": "creating the draft",
                      "update-draft": "revising the draft", "preflight-check": "checking the draft",
                      "attach-file": "attaching a file", "remove-attachment": "changing an attachment",
                      "get-event": "reading the calendar", "calendar-view": "reading the calendar",
                      "archive-message": "archiving mail", "archive-conversation": "archiving mail",
                      "scan-attachments": "reading the attachments", "get-attachments": "reading the attachments",
                      "snooze-message": "snoozing the thread", "set-categories": "tagging mail",
                      "delete-draft": "deleting the draft", "clear-hold": "clearing the hold"},
    "scheduling_engine": {"_": "checking the calendar", "propose-meeting": "finding times",
                          "reschedule-brief": "preparing the reschedule", "validate-draft": "checking the times"},
    "email_writer": {"_": "writing the body"},
    "style_profiles": {"_": "reading the style profile"},
    "ledger": {"_": "updating the ledger", "list": "reading the ledger", "get": "reading the ledger",
               "types": "reading the ledger", "close": "closing the ledger row", "propose": "proposing a close"},
    "project_store": {"_": "filing to the project", "pack": "reading the project", "match": "reading the project",
                      "get": "reading the project", "list": "reading the project", "journal": "writing the journal"},
    "fetch_url": {"_": "reading a web page"},
    "doc_text": {"_": "reading a document"},
    "doc_write": {"_": "writing the document", "xlsx": "writing the spreadsheet"},
    "poll_filler": {"_": "working the poll", "read-poll": "reading the poll", "fill-poll": "filling the poll"},
    "booking_link": {"_": "reading the booking page"},
    "gmail_client": {"_": "drafting in Gmail", "search": "reading Gmail", "get-message": "reading Gmail",
                     "get-thread": "reading Gmail", "list-drafts": "reading Gmail"},
    "google_calendar_client": {"_": "reading Google Calendar"},
    "overleaf_tools": {"_": "working in Overleaf"},
    "site_tools": {"_": "working on the site"},
    "doc_review": {"_": "opening the review"},
    "compile_letter": {"_": "compiling the letter"},
    "preferences": {"_": "recording the rule", "list": "reading the rules"},
    "claude_usage": {"_": "checking usage"},
    "session_index": {"_": "reading the conversation index"},
    "log_missed_closure": {"_": "logging the missed closure"},
    "scheduled_sends": {"_": "reading the scheduled sends"},
    "mc_client": {"_": "working in the manuscript system"},
}

# first word of a phrase -> its past form, for the "So far" list.
PAST = {"reading": "read", "writing": "wrote", "finding": "found", "checking": "checked",
        "creating": "created", "revising": "revised", "searching": "searched", "listing": "listed",
        "attaching": "attached", "changing": "changed", "archiving": "archived", "snoozing": "snoozed",
        "tagging": "tagged", "deleting": "deleted", "clearing": "cleared", "preparing": "prepared",
        "updating": "updated", "closing": "closed", "proposing": "proposed", "filing": "filed",
        "working": "worked", "drafting": "drafted", "building": "built", "opening": "opened",
        "compiling": "compiled", "recording": "recorded", "logging": "logged", "running": "ran",
        "using": "used", "looking": "looked"}

_SCRIPT = re.compile(r"preprocessor/([a-z_0-9]+)\.py(?:\s+(?:--?[a-z-]+\s+)*([a-z][a-z0-9-]*))?")
_SKIP = {"ToolSearch", "TodoWrite", "Skill", "AskUserQuestion", "ScheduleWakeup", "Monitor"}


def describe(tool: str, args: dict | None = None) -> str | None:
    """A present-participle phrase for one tool call, or None for a call
    not worth a word (a tool lookup, a to-do update)."""
    args = args if isinstance(args, dict) else {}
    tool = str(tool or "")
    if tool in _SKIP:
        return None
    if tool in ("Read", "Glob", "Grep", "LS"):
        return "reading files"
    if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        return "writing files"
    if tool.startswith(("WebFetch", "WebSearch")):
        return "reading the web"
    if tool in ("Agent", "Task"):
        return "writing the body" if "email-author" in str(args.get("subagent_type") or "") else "running a subagent"
    if tool.startswith("mcp__"):
        low = tool.lower()
        if "gmail" in low:
            return "reading Gmail"
        if "drive" in low:
            return "reading Drive"
        if "calendar" in low:
            return "reading the calendar"
        return "using a connector"
    if tool == "Bash":
        cmd = str(args.get("command") or "")
        m = _SCRIPT.search(cmd)
        if m:
            script, verb = m.group(1), m.group(2) or ""
            table = SCRIPTS.get(script)
            if table is None:
                return f"running {script}"
            return table.get(verb) or table["_"]
        head = cmd.strip().split(" ", 1)[0].rsplit("/", 1)[-1] if cmd.strip() else ""
        if head == "git":
            return "checking the repository"
        if head in ("grep", "rg", "find", "ls", "cat", "head", "tail", "sed", "awk", "wc", "jq", "diff", "stat"):
            return "reading files"
        if head in ("python3", "python"):
            return "running a script"
        return "working in the shell"
    return None


def past(phrase: str) -> str:
    first, _, rest = phrase.partition(" ")
    return (PAST.get(first) or first) + (" " + rest if rest else "")


class Progress:
    """The steps of one turn, fed by the runner, read by the heartbeat."""

    def __init__(self, clock=None):
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self.started = self._clock()
        self._done: list[str] = []
        self._current: tuple[str, float] | None = None

    def begin(self, tool: str, args: dict | None = None) -> None:
        phrase = describe(tool, args)
        if phrase is None:
            return
        with self._lock:
            self._current = (phrase, self._clock())

    def end(self, tool: str, args: dict | None = None, elapsed_s: float | None = None) -> None:
        phrase = describe(tool, args)
        if phrase is None:
            return
        with self._lock:
            if not self._done or self._done[-1] != phrase:
                self._done.append(phrase)
            # A result means the pending call is over, whichever it was.
            self._current = None

    def codex_event(self, ev: dict) -> None:
        """A Codex JSONL event: a command's start and end."""
        if not isinstance(ev, dict):
            return
        item = ev.get("item") or {}
        if item.get("type") != "command_execution":
            return
        args = {"command": str(item.get("command") or "")}
        if ev.get("type") == "item.started":
            self.begin("Bash", args)
        elif ev.get("type") == "item.completed":
            self.end("Bash", args)

    def minutes(self) -> int:
        return int(round((self._clock() - self.started) / 60))

    def summary(self) -> str | None:
        """The sentence for the heartbeat, or None before the first step."""
        with self._lock:
            done, current = list(self._done), self._current
        if not done and current is None:
            return None
        parts = [f":hourglass_flowing_sand: {self.minutes()} min in."]
        if done:
            shown = done[-SHOWN:]
            lead = "So far: " + ("…, " if len(done) > SHOWN else "")
            parts.append(lead + ", ".join(past(p) for p in shown) + ".")
        if current is not None:
            secs = int(self._clock() - current[1])
            parts.append(f"Now: {current[0]} ({secs} s).")
        return " ".join(parts)

#!/usr/bin/env python3
"""
User preference rules: the EA's personalization layer.

The judgment that makes the EA *this* user's assistant (what to surface,
what to skip, who they are eligible for, how high the bar sits) lives in
one user-owned file, `~/.ea/config/preferences.json`: scoped rules in the
user's own words, injected into every prompt whose skill declares a
matching scope. A correction is `preferences.py add`, which the resident
can run from a Slack message, never a skill edit.

Rule shape:
    {"id": "standing-baas-not-opportunities",
     "kind": "avoid",                         # instruction | prefer | avoid | fact
     "scopes": ["funding", "newsletters"],    # see SCOPES below; "*" = everywhere
     "text": "Standing or office-wide BAAs are not opportunities; skip them.",
     "quote": "That office BAA is like the others, just noise - not actually an opportunity.",
     "date": "2026-09-01", "source": "chat", "status": "active"}

Calibration rows record how a run landed and what the user said, so the
bar moves with feedback:
    {"date": "2026-09-01", "scope": "newsletters:robotics-worldwide",
     "note": "1 of 17 posted (Foggy Docks); the user confirmed that was the right one."}

Scope matching: a rule scoped `newsletters` applies to a request for
`newsletters` and to `newsletters:rio-bulletin`; a rule scoped
`newsletters:rio-bulletin` applies only to that list; `*` applies to every
request. Skills declare what they consume in an HTML comment within their
first lines: `<!-- scopes: briefing, mail-triage -->` (`none` for a skill
that gets its rules another way). Plugins declare `"scopes": [...]` in
plugin.json.

Lanes that inject: run-agent.sh and run-plugin.sh append `render` output
to the skill text; ea-chat-bot.py appends it to the skill blurb of a
dispatch. Everything reads the file; only `add`, `calibrate` and `retire`
write it, under a lock, in place, so both the owner and the ea-bot user
can write without the config directory being group-writable.

Usage:
    preferences.py render --skill skills/morning-briefing.md [--skill ...]
    preferences.py render --scope newsletters --scope funding
    preferences.py list [--scope S ...] [--all] [--json]
    preferences.py add --kind avoid --scope funding --text "..." \
        [--quote "..."] [--date YYYY-MM-DD] [--source chat|edit|migration] [--id ID]
    preferences.py calibrate --scope newsletters:rio-bulletin --note "..." [--date D]
    preferences.py retire --id ID [--note "..."]
    preferences.py show --id ID
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

try:
    import owner_trust as _owner_trust
    import resident_io as _resident_io
except ModuleNotFoundError:  # imported as preprocessor.<mod>
    from preprocessor import owner_trust as _owner_trust
    from preprocessor import resident_io as _resident_io

EA_DIR = Path(os.environ.get("EA_DIR", Path.home() / ".ea"))
PREFS_FILE = EA_DIR / "config" / "preferences.json"
# A write the sandbox refused (a Codex turn: the config folder is not among
# its writable roots) waits here; `apply-staged` replays it after the turn,
# outside the sandbox, when the words it quotes are the user's.
STAGED_FILE = EA_DIR / "state" / "preferences-staged.jsonl"
REFUSED_FILE = EA_DIR / "state" / "preferences-staged-refused.jsonl"
STAGED_VERBS = {"preferences": ("add", "calibrate", "retire"), "onboarding": ("answer", "skip", "stop"),
                "site_tools": ("pull", "commit", "revert"),
                "autonomy": ("promote",), "doc_review": ("open", "update", "reply", "resolve")}
_APPLYING = False

KINDS = ("instruction", "prefer", "avoid", "fact")
# Where a rule came from: the chat (any frontend), a hand edit, or the
# migration. "slack" is the old spelling of "chat" and is folded into it.
SOURCES = ("chat", "edit", "migration", "slack")
_SOURCE_ALIASES = {"slack": "chat"}


def source_label(source: str | None) -> str:
    """The source as a message names it."""
    s = str(source or "edit")
    return _SOURCE_ALIASES.get(s, s)
# The documented vocabulary. Free-form scopes work too; these are the ones
# skills and CLAUDE.md talk about, so a rule filed under one of them is
# guaranteed to reach a prompt.
SCOPES = {
    "*": "every prompt that injects rules",
    "briefing": "morning briefing, afternoon catchup, weekly audit",
    "mail-triage": "what to surface, suppress or close in the inbox",
    "funding": "grants, awards, solicitations, sponsors",
    "newsletters": "mailing-list digests and newsletters (list-digest); newsletters:<list> for one list",
    "scheduling": "meeting proposals and calendar judgment",
    "drafting": "email and document drafts in the user's voice",
    "manuscripts": "editorial and reviewing work",
    "admissions": "prospective-student inquiries (the weekly admissions digest)",
}
SCOPES_RE = re.compile(r"<!--\s*scopes:\s*(.*?)\s*-->", re.I)
HEADER_LINES = 15

_DOC = ("Your standing rules, in your words, injected into every prompt whose skill "
        "declares a matching scope (see preprocessor/preferences.py). kind: instruction "
        "(a quoted standing instruction), prefer (post / do), avoid (skip / never do), "
        "fact (about you: role, eligibility, interests). scopes: " +
        "; ".join(f"{k} = {v}" for k, v in SCOPES.items()) +
        ". Add with `preferences.py add`, or edit this file by hand. Retired rules stay "
        "for the record with status=retired. calibration: how past runs landed and what "
        "you said; this is how the bar gets set.")


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------
def _empty() -> dict:
    return {"version": 1, "rules": [], "calibration": []}


def _parse(text: str, where: str = "") -> dict:
    """Store text -> dict with defaults; `_doc`-style keys stripped. Empty or
    unreadable text is an empty store, never an error: prompts must still
    assemble when the user has no rules yet."""
    if not text.strip():
        return _empty()
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as e:
        print(f"preferences: could not parse {where or 'store'}: {e}", file=sys.stderr)
        return _empty()
    if not isinstance(raw, dict):
        print(f"preferences: {where or 'store'} is not a JSON object", file=sys.stderr)
        return _empty()
    data = {k: v for k, v in raw.items() if isinstance(k, str) and not k.startswith("_")}
    for k, v in _empty().items():
        data.setdefault(k, v)
    if not isinstance(data.get("rules"), list):
        data["rules"] = []
    data["rules"] = [r for r in data["rules"] if isinstance(r, dict)]
    return data


def load(path: Path | None = None) -> dict:
    """The store at `path`, else at PREFS_FILE as it is when called.

    Read with the resident's access in an owner process (resident_io): the
    store is resident space and what it holds is rendered into prompts the
    resident reads, so a link at its name must not hand the owner another
    file. A missing or unreadable store is an empty one (one stderr line for
    a failure)."""
    path = PREFS_FILE if path is None else path
    text = _resident_io.read_text(path)
    if text is None:
        return _empty()
    return _parse(text, str(path))


def _render_doc(data: dict) -> str:
    doc = {"_doc": _DOC}
    doc.update({k: data[k] for k in ("version", "rules", "calibration")})
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def _write_locked(fh, data: dict) -> None:
    fh.seek(0)
    fh.truncate()
    fh.write(_render_doc(data))
    fh.flush()
    os.fsync(fh.fileno())


def _update_as_resident(mutator, path: Path):
    """The owner's update: the resident child opens, locks, reads and
    rewrites the store (resident_io.update_text); only the mutation runs in
    this process, on the text the child hands over. Raises OSError when the
    store could not be updated, like the direct path does."""
    box: dict = {}

    def fn(text: str) -> str:
        data = _parse(text, str(path))
        box["result"] = mutator(data)
        return _render_doc(data)

    if not _resident_io.update_text(path, fn, mode=0o664):
        raise OSError(f"preferences: could not update {path}")
    return box.get("result")


def update(mutator, path: Path | None = None):
    """Read-modify-write under ONE exclusive lock, so two writers (the
    resident recording a correction while the dev shell adds a rule) cannot
    lose each other's change. `mutator(data)` edits in place; its return
    value is passed back. In place rather than tmp+rename: the config
    directory is owned by ea-bot and not group-writable, and both users
    write this file, which is left group-writable for the same reason.
    With no `path`, the store is PREFS_FILE as it is when called, so a
    test that repoints PREFS_FILE never writes the live store.

    An owner process (the dashboard's item feedback, a cron) does the whole
    update with the resident's access (`_update_as_resident`): the store is
    in resident space, where a link at its name would otherwise turn the
    owner's rewrite into a rewrite of another file."""
    path = PREFS_FILE if path is None else path
    if _resident_io.is_owner():
        return _update_as_resident(mutator, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            fh.seek(0)
            data = _parse(fh.read(), str(path))
            result = mutator(data)
            _write_locked(fh, data)
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
    try:
        os.chmod(path, 0o664)
    except OSError:
        pass
    return result


def save(data: dict, path: Path | None = None) -> None:
    """Replace the store wholesale (migrations, tests). Same lock and mode
    as `update`; callers that add one rule should use `update`."""
    update(lambda d: d.update(data), path)


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    words = s.split("-")
    return "-".join(words[:7])[:56].strip("-") or "rule"


def _unique_id(base: str, existing: set[str]) -> str:
    if base not in existing:
        return base
    n = 2
    while f"{base}-{n}" in existing:
        n += 1
    return f"{base}-{n}"


def _norm_scopes(scopes) -> list[str]:
    out = []
    for s in scopes or []:
        for part in str(s).split(","):
            p = part.strip().lower()
            if p and p not in out:
                out.append(p)
    return out


def add_rule(data: dict, *, kind: str, scopes, text: str, quote: str = "",
             date_str: str = "", source: str = "edit", rule_id: str = "") -> dict:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
    if source not in SOURCES:
        raise ValueError(f"source must be one of {SOURCES}, got {source!r}")
    source = source_label(source)
    scopes = _norm_scopes(scopes)
    text = " ".join((text or "").split())
    if not scopes:
        raise ValueError("at least one --scope is required")
    if not text:
        raise ValueError("--text is required")
    ids = {r.get("id") for r in data["rules"]}
    rid = _unique_id(rule_id.strip() or _slug(text), ids)
    rule = {
        "id": rid,
        "kind": kind,
        "scopes": scopes,
        "text": text,
        "quote": " ".join((quote or "").split()),
        "date": date_str or date.today().isoformat(),
        "source": source,
        "status": "active",
    }
    data["rules"].append(rule)
    return rule


def add_calibration(data: dict, *, scope: str, note: str, date_str: str = "") -> dict:
    scope = (scope or "").strip().lower()
    note = " ".join((note or "").split())
    if not scope or not note:
        raise ValueError("--scope and --note are required")
    row = {"date": date_str or date.today().isoformat(), "scope": scope, "note": note}
    data["calibration"].append(row)
    return row


def retire_rule(data: dict, rule_id: str, note: str = "") -> dict:
    for r in data["rules"]:
        if r.get("id") == rule_id:
            r["status"] = "retired"
            r["retired_on"] = date.today().isoformat()
            if note:
                r["retired_note"] = " ".join(note.split())
            return r
    raise KeyError(f"no rule with id {rule_id!r}")


# ---------------------------------------------------------------------------
# Selection and rendering
# ---------------------------------------------------------------------------
def scope_matches(rule_scope: str, wanted: str) -> bool:
    """Does a rule filed under `rule_scope` apply to a request for `wanted`?
    `*` covers everything; `a` covers `a` and `a:child`; `a:child` covers
    only itself."""
    rs = (rule_scope or "").strip().lower()
    w = (wanted or "").strip().lower()
    if not rs or not w:
        return False
    if rs == "*":
        return True
    return w == rs or w.startswith(rs + ":")


def _applies(rule_scopes, wanted: list[str]) -> bool:
    return any(scope_matches(rs, w) for rs in (rule_scopes or []) for w in wanted)


def select(scopes, data: dict | None = None) -> dict:
    """Active rules and calibration rows that apply to `scopes`, grouped by
    kind. The shape the list-digest JSON carries per entry and the shape
    `render` prints."""
    wanted = _norm_scopes(scopes)
    data = data if data is not None else load()
    out = {"scopes": wanted, "instructions": [], "prefer": [], "avoid": [],
           "facts": [], "calibration": []}
    if not wanted:
        return out
    bucket = {"instruction": "instructions", "prefer": "prefer",
              "avoid": "avoid", "fact": "facts"}
    rules = [r for r in data.get("rules", [])
             if r.get("status", "active") == "active" and _applies(r.get("scopes"), wanted)]
    rules.sort(key=lambda r: (r.get("date", ""), r.get("id", "")))
    for r in rules:
        row = {"id": r.get("id"), "text": r.get("text", ""), "date": r.get("date", ""),
               "scopes": r.get("scopes", [])}
        if r.get("quote"):
            row["quote"] = r["quote"]
        out[bucket.get(r.get("kind"), "facts")].append(row)
    cal = [c for c in data.get("calibration", [])
           if any(scope_matches(c.get("scope", ""), w) for w in wanted)]
    cal.sort(key=lambda c: c.get("date", ""))
    out["calibration"] = cal
    return out


def is_empty(selection: dict) -> bool:
    return not any(selection.get(k) for k in ("instructions", "prefer", "avoid", "facts", "calibration"))


def render(scopes, data: dict | None = None) -> str:
    """The prompt block. Empty string when nothing applies, so callers can
    append it unconditionally."""
    return render_selection(select(scopes, data))


def render_selection(sel: dict) -> str:
    """The prompt block for a selection already made (the `select` shape,
    such as the `rules` a list-digest entry carries). Empty string when
    the selection is empty."""
    if is_empty(sel):
        return ""
    lines = [f"## User rules (scopes: {', '.join(sel.get('scopes') or [])})",
             "These are the user's own standing rules. They outrank the skill text above "
             "and any default in this prompt. Apply them; do not re-derive them."]

    def _row(r: dict) -> str:
        s = f"- {r['text']}"
        if r.get("quote"):
            s += f' ({r.get("date", "")}: "{r["quote"]}")'
        return s

    if sel.get("instructions"):
        lines += ["", "Standing instructions (the user's words):"]
        for r in sel["instructions"]:
            tag = ", ".join(r.get("scopes", []))
            q = r.get("quote") or r["text"]
            lines.append(f'- [{r.get("date", "")}; {tag}] "{q}"')
    if sel.get("prefer"):
        lines += ["", "Prefer (post / do):"] + [_row(r) for r in sel["prefer"]]
    if sel.get("avoid"):
        lines += ["", "Avoid (skip / never do):"] + [_row(r) for r in sel["avoid"]]
    if sel.get("facts"):
        lines += ["", "Facts about the user:"] + [_row(r) for r in sel["facts"]]
    if sel.get("calibration"):
        lines += ["", "Calibration (how past runs landed):"]
        for c in sel["calibration"]:
            lines.append(f"- {c.get('date', '')} [{c.get('scope', '')}] {c.get('note', '')}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Skill headers
# ---------------------------------------------------------------------------
def scopes_from_skill(path: Path | str) -> list[str] | None:
    """Scopes declared in a skill file's `<!-- scopes: a, b -->` header, or
    None when the skill declares nothing. `none` declares an empty list."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            head = "".join(fh.readline() for _ in range(HEADER_LINES))
    except OSError:
        return None
    m = SCOPES_RE.search(head)
    if not m:
        return None
    raw = m.group(1).strip()
    if raw.lower() in ("", "none", "-"):
        return []
    return _norm_scopes([raw])


def scopes_for_skills(paths) -> list[str]:
    out: list[str] = []
    for p in paths:
        for s in scopes_from_skill(p) or []:
            if s not in out:
                out.append(s)
    return out


def render_for_skills(paths, data: dict | None = None) -> str:
    return render(scopes_for_skills(paths), data)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _cmd_render(args) -> int:
    scopes = list(args.scope or [])
    if args.skill:
        scopes += scopes_for_skills(args.skill)
    sys.stdout.write(render(scopes))
    return 0


def _cmd_list(args) -> int:
    data = load()
    rules = data["rules"]
    if args.scope:
        rules = [r for r in rules if _applies(r.get("scopes"), _norm_scopes(args.scope))]
    if not args.all:
        rules = [r for r in rules if r.get("status", "active") == "active"]
    if args.json:
        print(json.dumps({"rules": rules, "calibration": data["calibration"]}, indent=2))
        return 0
    if not rules:
        print("(no rules)")
    for r in rules:
        flag = "" if r.get("status", "active") == "active" else f" [{r['status']}]"
        print(f"{r['id']}{flag}  ({r['kind']}; {', '.join(r.get('scopes', []))}; {r.get('date', '')})")
        print(f"    {r['text']}")
        if r.get("quote"):
            print(f'    "{r["quote"]}"')
    if data["calibration"] and not args.scope:
        print(f"calibration rows: {len(data['calibration'])}")
    return 0


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sandbox_refused(e: BaseException) -> bool:
    """The error a sandboxed turn gets on a write outside its roots: EACCES
    (landlock), EPERM, or EROFS (a read-only bind)."""
    import errno as _errno
    return isinstance(e, OSError) and e.errno in (_errno.EACCES, _errno.EPERM, _errno.EROFS)


def stage(tool: str, argv) -> dict:
    """Record a write the sandbox refused, to replay after the turn: the
    tool (`preferences` or `onboarding`), its argv and the run id."""
    rec = {"ts": _now_iso(), "tool": tool, "argv": [str(a) for a in argv],
           "run_id": os.environ.get("EA_RUN_ID", "")}
    if not _resident_io.append_text(STAGED_FILE, json.dumps(rec, ensure_ascii=False) + "\n"):
        raise OSError(f"preferences: could not stage the write in {STAGED_FILE}")
    return rec


def _option(argv: list, name: str) -> str:
    """The value after `name` in argv, or `--name=value`, or ''."""
    for i, a in enumerate(argv):
        if a == name and i + 1 < len(argv):
            return str(argv[i + 1])
        if a.startswith(name + "="):
            return a[len(name) + 1:]
    return ""


def claimed_words(tool: str, argv: list) -> str | None:
    """The user's words a staged write claims (`add --quote`, `answer
    --text`), '' when such a write claims none, None when the write needs
    none (a calibration, a retirement, a skip)."""
    verb = argv[0] if argv else ""
    if (tool, verb) in (("preferences", "add"), ("autonomy", "promote")):
        return _option(argv, "--quote")
    if tool == "onboarding" and verb == "answer":
        return _option(argv, "--text")
    return None


def _said(words: str, user_text: str) -> bool:
    norm = lambda t: " ".join(str(t).split()).casefold()  # noqa: E731
    return bool(norm(words)) and norm(words) in norm(user_text)


OUTPUT_TEXT_CAP = 300
OUTPUT_JSON_CAP = 4000


def _recorded_output(text: str) -> str:
    """What an applied write's record keeps of the verb's stdout. A JSON
    answer stays whole (the bot reads the pull request's url out of it),
    up to OUTPUT_JSON_CAP; plain text is cut at OUTPUT_TEXT_CAP."""
    text = text.strip()
    try:
        json.loads(text)
    except ValueError:
        return text[:OUTPUT_TEXT_CAP]
    return text[:OUTPUT_JSON_CAP]


def apply_staged(user_text: str | None, run_preferences=None, run_onboarding=None) -> dict:
    """Replay the staged writes (STAGED_FILE, then emptied) outside the
    sandbox. A rule, an onboarding answer or a tier promotion is applied only when the words
    it quotes are in `user_text`, the message the user sent this turn: a
    rule lifted from a mail body quotes nothing the user said. Every other
    record (a calibration, a retirement) is applied as it is. A refused
    record goes to REFUSED_FILE with its reason. Returns {"applied": [...],
    "refused": [...]} with each record and, for a refusal, "why"."""
    global _APPLYING
    import io as _io
    from contextlib import redirect_stdout, redirect_stderr
    box: dict = {"lines": []}

    def take(text: str) -> str:   # the records, and the file emptied under the lock
        box["lines"] = [ln for ln in (text or "").splitlines() if ln.strip()]
        return ""
    if not _resident_io.update_text(STAGED_FILE, take):
        return {"applied": [], "refused": [], "error": f"could not read {STAGED_FILE}"}
    lines = box["lines"]
    if not lines:
        return {"applied": [], "refused": []}
    runners = {"preferences": run_preferences or main,
               "onboarding": run_onboarding or _onboarding_main,
               "autonomy": _autonomy_main, "doc_review": _doc_review_main,
               "site_tools": _site_tools_main}
    out = {"applied": [], "refused": []}

    def refuse(rec, why):
        rec = dict(rec, why=why)
        out["refused"].append(rec)
        _resident_io.append_text(REFUSED_FILE, json.dumps(rec, ensure_ascii=False) + "\n")

    _APPLYING = True
    stale_prs: set[str] = set()   # a doc_review update that failed: its replies would claim a change
    try:
        for ln in lines:
            try:
                rec = json.loads(ln)
                tool, argv = rec["tool"], list(rec["argv"])
            except (ValueError, KeyError, TypeError):
                refuse({"line": ln[:200]}, "not a staged write")
                continue
            if tool not in STAGED_VERBS or not argv or argv[0] not in STAGED_VERBS[tool]:
                refuse(rec, "not a verb that stages")
                continue
            words = claimed_words(tool, argv)
            if words is not None:
                if not words:
                    refuse(rec, "a rule needs the user's words (--quote)")
                    continue
                if user_text is None or not _said(words, user_text):
                    refuse(rec, "the quoted words are not in the user's message this turn")
                    continue
            pr = _option(argv, "--pr") if tool == "doc_review" else ""
            if pr in stale_prs and argv[0] in ("reply", "resolve"):
                refuse(rec, f"the update of PR #{pr} did not land, so this reply waits for the next review")
                continue
            buf, err = _io.StringIO(), _io.StringIO()
            try:
                with redirect_stdout(buf), redirect_stderr(err):
                    rc = runners[tool](argv)
            except Exception as e:  # the store refused: keep the record
                refuse(rec, f"{type(e).__name__}: {e}"[:300])
                if tool == "doc_review" and argv[0] == "update":
                    stale_prs.add(pr)
                continue
            if rc:
                refuse(rec, (err.getvalue().strip() or buf.getvalue().strip() or f"exit {rc}")[:300])
                if tool == "doc_review" and argv[0] == "update":
                    stale_prs.add(pr)
                continue
            applied = dict(rec, output=_recorded_output(buf.getvalue()))
            if tool == "site_tools":
                result = json.loads(buf.getvalue())
                applied["result"] = {k: result[k] for k in ("status", "site", "commit", "publish_staged") if k in result}
            out["applied"].append(applied)
    finally:
        _APPLYING = False
    return out


def _autonomy_main(argv) -> int:
    try:
        import autonomy_tiers
    except ModuleNotFoundError:
        from preprocessor import autonomy_tiers
    return autonomy_tiers.main(argv)


def _site_tools_main(argv) -> int:
    try:
        import site_tools
    except ModuleNotFoundError:
        from preprocessor import site_tools
    return site_tools.main(argv)


def _doc_review_main(argv) -> int:
    try:
        import doc_review
    except ModuleNotFoundError:
        from preprocessor import doc_review
    return doc_review.main(argv)


def _onboarding_main(argv) -> int:
    try:
        import onboarding
    except ModuleNotFoundError:
        from preprocessor import onboarding
    return onboarding.main(argv)


def _cmd_apply_staged(args) -> int:
    text = None
    if args.user_text_file:
        text = sys.stdin.read() if args.user_text_file == "-" else Path(args.user_text_file).read_text(encoding="utf-8")
    res = apply_staged(text)
    print(json.dumps(res, ensure_ascii=False))
    return 0


def _cmd_add(args) -> int:
    try:
        rule = update(lambda d: add_rule(
            d, kind=args.kind, scopes=args.scope, text=args.text,
            quote=args.quote or "", date_str=args.date or "",
            source=args.source, rule_id=args.id or ""))
    except ValueError as e:
        print(json.dumps({"error": str(e)}))
        return 2
    print(json.dumps({"added": rule, "file": str(PREFS_FILE)}, ensure_ascii=False))
    return 0


def _cmd_calibrate(args) -> int:
    try:
        row = update(lambda d: add_calibration(
            d, scope=args.scope, note=args.note, date_str=args.date or ""))
    except ValueError as e:
        print(json.dumps({"error": str(e)}))
        return 2
    print(json.dumps({"calibration": row, "file": str(PREFS_FILE)}, ensure_ascii=False))
    return 0


def _cmd_retire(args) -> int:
    try:
        rule = update(lambda d: retire_rule(d, args.id, args.note or ""))
    except KeyError as e:
        print(json.dumps({"error": str(e)}))
        return 2
    print(json.dumps({"retired": rule}, ensure_ascii=False))
    return 0


def _owner_state_dir() -> Path:
    """Where owner-only state lives: the owner-private directory in an
    owner process, the EA tree's state directory in any other (a test on a
    scratch tree)."""
    if _owner_trust.is_owner_process():
        return _owner_trust.private_dir()
    return EA_DIR / "state"


# The digest cursor is a detection control (it decides which rule changes the
# daily digest reports), so the owner keeps it where the resident cannot move
# it. LEGACY_DIGEST_CURSOR is where it lived before; it seeds a first run.
DIGEST_CURSOR = _owner_state_dir() / "preferences-digest-seen.json"
LEGACY_DIGEST_CURSOR = EA_DIR / "state" / "preferences-digest-seen.json"


def _in_private(path: Path) -> bool:
    root = os.path.realpath(_owner_trust.private_dir())
    parent = os.path.realpath(Path(path).parent)
    return parent == root or parent.startswith(root.rstrip(os.sep) + os.sep)


def _cursor_shape(value) -> dict | None:
    """A cursor snapshot ({"rules": {id: status}}), or None for anything else."""
    if not isinstance(value, dict) or not isinstance(value.get("rules"), dict):
        return None
    return {"rules": {k: v for k, v in value["rules"].items()
                      if isinstance(k, str) and isinstance(v, str)}}


def read_digest_cursor(path: Path | None = None) -> dict | None:
    """The last digest snapshot, or None when there is none yet. In the
    owner-private directory the cursor is read through owner_trust's checks.
    With no cursor there, the legacy one in resident space (read with the
    resident's access) seeds the first run, so the move reports the changes
    made since the last digest instead of absorbing them."""
    path = DIGEST_CURSOR if path is None else path
    text = None
    if _in_private(path):
        try:
            text = _owner_trust.read_owner_file(path).decode("utf-8")
        except FileNotFoundError:
            if path == DIGEST_CURSOR and LEGACY_DIGEST_CURSOR != path:
                text = _resident_io.read_text(LEGACY_DIGEST_CURSOR)
        except (OSError, _owner_trust.OwnerTrustError) as e:
            print(f"preferences: digest cursor refused: {e}", file=sys.stderr)
            return None
    else:
        text = _resident_io.read_text(path)
    if text is None:
        return None
    try:
        return _cursor_shape(json.loads(text))
    except ValueError:
        return None


def write_digest_cursor(seen: dict, path: Path | None = None) -> None:
    """Persist the digest snapshot: through owner_trust (mode 0600) in the
    owner-private directory, by atomic replace anywhere else."""
    path = DIGEST_CURSOR if path is None else path
    text = json.dumps(seen)
    if _in_private(path):
        _owner_trust.write_owner_file(path, text, mode=0o600)
        return
    try:
        import atomic_io as _atomic
    except ModuleNotFoundError:  # imported as preprocessor.<mod>
        from preprocessor import atomic_io as _atomic
    _atomic.write_text(path, text)


def digest(data: dict, seen: dict | None) -> tuple[dict, dict]:
    """Rules added or retired since the last digest.

    `seen` is the previous snapshot ({"rules": {id: status}}) or None when no
    cursor exists yet. A first run (seen is None) reports nothing and just
    seeds the snapshot, so the digest never dumps the whole store once. The
    new snapshot is returned for the caller to persist.
    """
    prev = (seen or {}).get("rules", {})
    now = {r.get("id"): r.get("status", "active") for r in data.get("rules", [])}
    added, retired = [], []
    if seen is not None:
        by_id = {r.get("id"): r for r in data.get("rules", [])}
        for rid, st in now.items():
            was = prev.get(rid)
            if was is None and st == "active":
                added.append(by_id[rid])
            elif st == "retired" and was != "retired":
                retired.append(by_id[rid])
    return {"added": added, "retired": retired}, {"rules": now}


DIGEST_MAX_LINES = 12


def _digest_lines(rules: list[dict], line) -> list[str]:
    """One bullet per distinct rule, a repeat count on duplicates, at most
    DIGEST_MAX_LINES bullets and then a count of the rest."""
    groups: dict[str, list] = {}
    for r in rules:
        text = line(r)
        groups.setdefault(text, []).append(r)
    out = [f"- {text}" + (f" (x{len(rs)})" if len(rs) > 1 else "")
           for text, rs in groups.items()]
    if len(out) > DIGEST_MAX_LINES:
        rest = len(out) - DIGEST_MAX_LINES
        out = out[:DIGEST_MAX_LINES] + [f"- {rest} more. Run `preferences.py list` to see all."]
    return out


def render_digest(report: dict) -> str:
    """The digest as an assistant-voice message (STE), or '' when nothing
    changed. Every rule shapes what the EA surfaces or does, so the user
    reviews new ones and can catch a rule he did not set."""
    added, retired = report.get("added") or [], report.get("retired") or []
    if not added and not retired:
        return ""
    lines = [":memo: *Preferences changed.* Review these. Reply to correct any "
             "you did not set."]
    if added:
        lines += ["", f"*Added ({len(added)})*"]
        lines += _digest_lines(added, lambda r: (
            f"*{r.get('kind')}* [{', '.join(r.get('scopes') or [])}] {r.get('text')} "
            f"_via {source_label(r.get('source'))}_"))
    if retired:
        lines += ["", f"*Retired ({len(retired)})*"]
        lines += _digest_lines(retired, lambda r: (
            f"[{', '.join(r.get('scopes') or [])}] {r.get('text')}"))
    return "\n".join(lines)


def _cmd_digest(args) -> int:
    data = load()
    seen = read_digest_cursor()
    report, new_seen = digest(data, seen)
    if args.json:
        print(json.dumps(report, ensure_ascii=False))
    else:
        print(render_digest(report))
    if not args.dry_run:
        write_digest_cursor(new_seen)
    return 0


def _cmd_show(args) -> int:
    for r in load()["rules"]:
        if r.get("id") == args.id:
            print(json.dumps(r, indent=2, ensure_ascii=False))
            return 0
    print(json.dumps({"error": f"no rule with id {args.id!r}"}))
    return 2


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="User preference rules (personalization layer)", allow_abbrev=False)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("render", help="prompt block for the given scopes and/or skill files", allow_abbrev=False)
    p.add_argument("--scope", action="append")
    p.add_argument("--skill", action="append", help="skill file whose <!-- scopes: --> header to read")
    p = sub.add_parser("list", help="rules on file", allow_abbrev=False)
    p.add_argument("--scope", action="append")
    p.add_argument("--all", action="store_true", help="include retired rules")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("add", help="record a rule", allow_abbrev=False)
    p.add_argument("--kind", required=True, choices=KINDS)
    p.add_argument("--scope", action="append", required=True)
    p.add_argument("--text", required=True, help="the rule, one or two sentences")
    p.add_argument("--quote", help="the user's exact words, when they said it")
    p.add_argument("--date")
    p.add_argument("--source", default="edit", choices=SOURCES)
    p.add_argument("--id")
    p = sub.add_parser("calibrate", help="record how a run landed / what the user said", allow_abbrev=False)
    p.add_argument("--scope", required=True)
    p.add_argument("--note", required=True)
    p.add_argument("--date")
    p = sub.add_parser("retire", help="retire a rule (kept for the record)", allow_abbrev=False)
    p.add_argument("--id", required=True)
    p.add_argument("--note")
    p = sub.add_parser("show", allow_abbrev=False)
    p.add_argument("--id", required=True)
    p = sub.add_parser("digest", help="rules added/retired since the last digest", allow_abbrev=False)
    p.add_argument("--json", action="store_true")
    p.add_argument("--dry-run", action="store_true",
                   help="do not advance the cursor (preview)")
    p = sub.add_parser("apply-staged", help="replay the writes a sandboxed turn staged", allow_abbrev=False)
    p.add_argument("--user-text-file", help="the user's message this turn ('-' reads stdin); "
                                            "a rule whose quote is not in it is refused")
    argv = list(sys.argv[1:] if argv is None else argv)
    args = ap.parse_args(argv)
    handler = {"render": _cmd_render, "list": _cmd_list, "add": _cmd_add,
               "calibrate": _cmd_calibrate, "retire": _cmd_retire,
               "show": _cmd_show, "digest": _cmd_digest,
               "apply-staged": _cmd_apply_staged}[args.cmd]
    try:
        return handler(args)
    except OSError as e:
        # The sandbox of a Codex turn holds no config folder: the write
        # waits for apply-staged, which the bot runs after the turn.
        if _APPLYING or args.cmd not in STAGED_VERBS["preferences"] or not sandbox_refused(e):
            raise
        stage("preferences", argv)
        print(json.dumps({"status": "staged", "file": str(STAGED_FILE),
                          "note": "applied after this turn when the quote is the user's words"}))
        return 0


if __name__ == "__main__":
    sys.exit(main())

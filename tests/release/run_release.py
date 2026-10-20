#!/usr/bin/env python3
"""Release tests: the LLM-facing suite, run against the models in effect.

The post-commit wall and the prompt-only evals are deterministic and free.
This suite is the expensive one: every case reaches a model, and the
model is whichever one owns that lane today, read from the remote-LLM
switches and the bot's Claude default. It runs on Saturday night
(tests/release/run_weekly.sh) and whenever a tag is cut
(scripts/cut-tag.sh), and it keeps a score per case per run so a quality
drop between two runs is visible, not only a pass that turned into a fail.

Lanes and who answers:

  briefing    tests/evals/fixtures/*.json through the full-mode eval
              provider: the Claude briefing skill, on the bot's default
              Claude model. Regression fixtures are regressions here;
              hard-success fixtures are quality cases.
  drafting    tests/release/cases/drafting/*.json: a brief and a
              recipient. With the `drafting` switch at `remote` the body
              comes from email_writer (the remote model); otherwise from
              Claude with the same instructions, style profile and voice
              guide (draft_ab.remote_messages). Deterministic checks on
              the body (no em-dash, no significance clause, required and
              forbidden phrases, length) and a rubric score from the
              pinned judge model.
  scheduling  tests/release/cases/scheduling/*.json: one typed request
              through the remote scheduling agent (sched_agent.run_agent,
              writes recorded, nothing executed) when the `scheduling`
              switch is `shadow` or `enforce`; skipped, and said so, when
              the switch is off, because the Claude scheduling lane has no
              canned runner yet. A case that carries a `pipeline` block
              (a canned thread and what the shape must produce) runs the
              scheduling pipeline instead (sched_pipeline.run, shadow):
              code checks the status, the picks, the check problems and
              the shape (counter-offer, invite, hold). Dates in such a
              case are written as `{{+7d}}` and land in the future on
              every run. The solver reads the live calendars when a
              Graph or Google token is readable; otherwise, or with
              --canned-calendar, it reads tests/release/fixtures/
              calendar-canned.json (EA_CANNED_CALENDAR, dated relative
              to today), so the lane runs on a machine with no token.
              The lane is ask-first, so the turn's
              deliverable is its proposal in chat; a draft is checked when
              there is one. Checks: a reply, concrete times when the case
              expects them, no decline, no tool talk, no weekday mismatch,
              every option a poll offered mentioned (`must_mention`),
              sender-only on a drafted poll reply.

The judge (JUDGE_MODEL) is pinned: it does not move when the model under
test moves, and the remote model never grades itself.

The briefing lane runs on the Claude harness or, with --harness codex,
on Codex (run_evals.run_eval's `harness`). The harness is in every
result and the summary, and a run compares only to a baseline on the
same harness.

Store (owner-private, <EA_PRIVATE_DIR>/release-tests/):
  runs/<run id>.json   every case with its output (capped), checks, score
  runs.jsonl           one summary line per run: lane means, verdict
  last-run.json        the fingerprint of the last run, for --skip-if-unchanged

Verdict: `red` when a regression case failed; `amber` when a lane's mean
score fell more than AMBER_DROP below the baseline run; `harness` when
half or more of the measured cases errored before reaching a model;
`green` otherwise. The baseline is the newest earlier run that was
measured (not harness), or --baseline <run id>.

Exit: 0 green, 1 red or amber, 2 harness, 3 skipped (unchanged, or no
case ran).

    python3 tests/release/run_release.py                   # every lane
    python3 tests/release/run_release.py --lane drafting --repeat 3
    python3 tests/release/run_release.py --tag v1.2.0 --json
    python3 tests/release/run_release.py --dry-run         # routing only
    python3 tests/release/run_release.py --skip-if-unchanged --alert
    python3 tests/release/run_release.py --canned-calendar --lane scheduling
    python3 tests/release/run_release.py --harness codex --lane briefing
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
CASES = Path(__file__).resolve().parent / "cases"
CANNED_CALENDAR = Path(__file__).resolve().parent / "fixtures" / "calendar-canned.json"
CANNED_CALENDAR_ENV = "EA_CANNED_CALENDAR"
sys.path.insert(0, str(REPO / "preprocessor"))
sys.path.insert(0, str(REPO / "tests" / "evals"))

LANES = ("briefing", "drafting", "scheduling")
HARNESSES = ("claude", "codex")
JUDGE_MODEL = "claude-sonnet-5-5"
DEFAULT_CLAUDE_MODEL = "claude-opus-5-5"
AMBER_DROP = 0.15
RUBRIC_PASS = 6            # rubric score out of 10 a drafting case needs
BRIEFING_TIMEOUT_S = 420
JUDGE_TIMEOUT_S = 300
PROMPT_PATHS = ["skills", "CLAUDE.md", "preprocessor", "ea-chat-bot.py", "run-agent.sh",
                "docs/ea", "tests/release", "tests/evals"]

EXIT_GREEN, EXIT_FAIL, EXIT_HARNESS, EXIT_SKIPPED = 0, 1, 2, 3


def store_dir() -> Path:
    base = Path(os.environ.get("EA_PRIVATE_DIR") or "~/.ea-private").expanduser()
    return base / "release-tests"


def _private_dir(path: Path) -> Path:
    """`path`, created, owner-only: the writer refuses an --out under a
    group-writable directory (owner_trust), and the store holds model
    output."""
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


# ── who owns each lane today ───────────────────────────────────────────

def claude_model() -> str:
    """The bot's default Claude model: EA_MODEL, else the opus tier's model."""
    env = os.environ.get("EA_MODEL", "").strip()
    try:
        import model_tiers
        model, _ = model_tiers.model_for("opus", default_model=env or DEFAULT_CLAUDE_MODEL)
        return model
    except Exception:
        return env or DEFAULT_CLAUDE_MODEL


def switches() -> dict:
    """Effective switch values, every one `off` outside an owner process."""
    try:
        import remote_llm
        sw = remote_llm.switches()
        cfg = remote_llm.config()
    except Exception:
        return {"_model": "", "_endpoint": ""}
    out = {k: (v.get("effective") if isinstance(v, dict) else v) for k, v in sw.items()}
    out["_model"] = cfg.get("model", "")
    out["_endpoint"] = cfg.get("endpoint", "")
    return out


def route(sw: dict, harness: str = "claude") -> dict:
    """{lane: {"owner": claude|codex|remote|skip, "why": ...}} from the
    switches; the briefing lane's owner is the harness it runs on."""
    # The remote writer owns drafting when its switch is on; otherwise the
    # harness under test writes the body (Claude, or Codex as the resident).
    drafting = "remote" if sw.get("drafting") == "remote" else harness
    sched = sw.get("scheduling")
    return {
        "briefing": {"owner": harness,
                     "why": ("the eval fixtures exercise the briefing skill on Codex"
                             if harness == "codex" else
                             "the eval fixtures exercise the Claude briefing skill")},
        "drafting": {"owner": drafting,
                     "why": (f"drafting switch is {sw.get('drafting') or 'off'}"
                             + (f"; the {harness} harness writes the body" if drafting != "remote" else ""))},
        "scheduling": ({"owner": "remote", "why": f"scheduling switch is {sched}"}
                       if sched in ("shadow", "enforce") else
                       {"owner": "skip",
                        "why": "scheduling switch is off and the Claude scheduling lane "
                               "has no canned runner"}),
    }


def fingerprint(sw: dict, model: str, harness: str = "claude") -> dict:
    """What a run depends on: the last commit touching prompt-affecting
    paths, the models, the switch values and the harness."""
    try:
        commit = subprocess.run(["git", "-C", str(REPO), "log", "-1", "--format=%H", "--"]
                                + PROMPT_PATHS, capture_output=True, text=True,
                                timeout=30).stdout.strip()
    except Exception:
        commit = ""
    values = {k: v for k, v in sw.items() if not k.startswith("_")}
    return {"commit": commit, "claude_model": model, "remote_model": sw.get("_model", ""),
            "switches": values, "harness": harness}


def unchanged(now: dict, last: dict | None) -> bool:
    """A run with no harness recorded ran on Claude."""
    if not last:
        return False
    same = all(now.get(k) == last.get(k) for k in ("commit", "claude_model", "remote_model", "switches"))
    return same and (now.get("harness") or "claude") == (last.get("harness") or "claude")


# ── the calendar the scheduling lane reads ─────────────────────────────

def calendar_tokens(ea_dir: Path | None = None) -> list[str]:
    """The names of the Graph and Google token files this process can
    read, under EA_DIR/auth. Empty on a machine with no calendar sign-in,
    and for a user the auth directory keeps out."""
    ea_dir = ea_dir or Path(os.environ.get("EA_DIR") or "~/.ea").expanduser()
    auth = ea_dir / "auth"
    names = ["msal_token_cache.json", "msal_tokens.json"]
    try:
        names += sorted(p.name for p in auth.glob("google_token_*.json"))
    except OSError:
        pass
    return [n for n in names if (auth / n).is_file() and os.access(auth / n, os.R_OK)]


def calendar_mode(flag: str | None, live: bool, tokens: list[str]) -> dict:
    """{"mode": canned|live, "path", "why"}: `flag` is --canned-calendar's
    value (None when not given), `live` --live-calendar. With neither, a
    machine with no readable token reads the fixture."""
    if flag is not None:
        return {"mode": "canned", "path": str(Path(flag).expanduser()), "why": "--canned-calendar"}
    if live:
        return {"mode": "live", "path": "", "why": "--live-calendar"}
    if tokens:
        return {"mode": "live", "path": "", "why": "calendar token readable: " + ", ".join(tokens)}
    return {"mode": "canned", "path": str(CANNED_CALENDAR), "why": "no Graph or Google token readable"}


# ── model calls ────────────────────────────────────────────────────────

def codex_text(prompt: str, model: str = "", timeout: float = JUDGE_TIMEOUT_S) -> dict:
    """One `codex exec` turn as the resident, read-only, in the resident's
    home: the drafting case on the Codex harness. The same shape as
    claude_text. Codex runs as the resident because its sandbox confines
    writes, not reads (preprocessor/codex_ghost.py)."""
    import codex_cli
    import codex_ghost
    res = codex_cli.run(prompt, cwd=codex_ghost.BOT_HOME, model=model, sandbox="read-only",
                        timeout=int(timeout), prefix=codex_ghost._bot_env(),
                        last_message_dir=codex_ghost.BOT_HOME / ".local" / "state" / "ea-lanes")
    if res.get("harness_error"):
        return {"text": "", "error": res["harness_error"], "cost_usd": None}
    return {"text": res.get("output") or "", "error": "", "cost_usd": None}


def claude_text(prompt: str, model: str, timeout: float = JUDGE_TIMEOUT_S) -> dict:
    """One tool-less `claude -p` turn as this user. {"text", "error",
    "cost_usd"}; `error` names a CLI or transport failure."""
    cmd = ["claude", "-p", prompt, "--model", model, "--output-format", "json",
           "--mcp-config", '{"mcpServers":{}}', "--strict-mcp-config",
           "--no-session-persistence", "--tools", "", "--permission-mode", "dontAsk"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           cwd=str(REPO))
    except subprocess.TimeoutExpired:
        return {"text": "", "error": f"claude timed out after {int(timeout)}s", "cost_usd": None}
    except OSError as e:
        return {"text": "", "error": f"claude could not start: {e}", "cost_usd": None}
    out = p.stdout or ""
    try:
        data = json.loads(out[out.find("{"):out.rfind("}") + 1])
    except ValueError:
        data = {}
    if not data or data.get("is_error") or p.returncode != 0:
        detail = (data.get("result") if data else "") or (p.stderr or out).strip()[:200]
        return {"text": "", "error": f"claude failed (rc={p.returncode}): {detail}",
                "cost_usd": data.get("total_cost_usd") if data else None}
    return {"text": str(data.get("result") or ""), "error": "",
            "cost_usd": data.get("total_cost_usd")}


def _json_in(text: str) -> dict:
    try:
        return json.loads(text[text.find("{"):text.rfind("}") + 1])
    except ValueError:
        return {}


# ── cases ──────────────────────────────────────────────────────────────

REQUIRED = {"drafting": ("id", "name", "category", "to", "brief"),
            "scheduling": ("id", "name", "category", "text")}

_DATE_TOKEN = re.compile(r"\{\{([+-]\d+)d(?::([^}]+))?\}\}")


def expand_dates(obj, today: dt.date | None = None):
    """`{{+7d}}` in any string of a case becomes the ISO date seven days
    from today, `{{+7d:%A}}` that day's name, so a canned thread stays in
    the future on every run."""
    today = today or dt.date.today()
    if isinstance(obj, str):
        return _DATE_TOKEN.sub(lambda m: (today + dt.timedelta(days=int(m.group(1)))).strftime(m.group(2) or "%Y-%m-%d"), obj)
    if isinstance(obj, list):
        return [expand_dates(x, today) for x in obj]
    if isinstance(obj, dict):
        return {k: expand_dates(v, today) for k, v in obj.items()}
    return obj


def user_first_name() -> str:
    """The install's own first name, from the profile the writer signs
    with. A case says `{{first_name}}` where the sign-off goes, so the
    brief never contradicts the profile the writer is given."""
    ea_dir = Path(os.environ.get("EA_DIR", str(Path.home() / ".ea")))
    try:
        name = str(json.loads((ea_dir / "config" / "user-profile.json").read_text()).get("name") or "")
    except (OSError, ValueError):
        name = ""
    return name.split()[0] if name.strip() else "there"


def expand_names(obj, first: str):
    if isinstance(obj, str):
        return obj.replace("{{first_name}}", first)
    if isinstance(obj, list):
        return [expand_names(x, first) for x in obj]
    if isinstance(obj, dict):
        return {k: expand_names(v, first) for k, v in obj.items()}
    return obj


def load_cases(lane: str) -> list[dict]:
    out = []
    first = user_first_name()
    for f in sorted((CASES / lane).glob("*.json")):
        case = json.loads(f.read_text())
        missing = [k for k in REQUIRED[lane] if not case.get(k)]
        if missing:
            raise ValueError(f"{f.name}: missing {', '.join(missing)}")
        if case["category"] not in ("regression", "quality"):
            raise ValueError(f"{f.name}: category must be regression or quality")
        if lane == "scheduling" and case.get("pipeline") is not None:
            if not isinstance(case["pipeline"].get("thread"), list) or not case["pipeline"]["thread"]:
                raise ValueError(f"{f.name}: a pipeline case needs pipeline.thread, a list of messages")
            case = expand_dates(case)
        case = expand_names(case, first)
        case["_path"] = str(f)
        out.append(case)
    return out


# ── drafting lane ──────────────────────────────────────────────────────

_EM_DASH = re.compile(r"—|–|(?<=\S) -- (?=\S)|(?<=\S)--(?=\S)")
_SIGNIFICANCE = re.compile(
    r"\b(which is (?:precisely |exactly )?why|that'?s a real \w+, not|is not a \w+, it'?s a|"
    r"worth doing|the reason this matters|this matters because|not just a|more than just)\b", re.I)
_LLM_ADVERBS = re.compile(
    r"\b(directly|exactly|precisely|genuinely|deliberately|squarely|notably|crucially|"
    r"importantly|truly|ultimately)\b", re.I)


def draft_checks(body: str, case: dict) -> list[dict]:
    """The deterministic checks on a drafted body, each {name, pass, reason}."""
    checks = []
    text = body or ""
    checks.append({"name": "non_empty", "pass": bool(text.strip()),
                   "reason": "" if text.strip() else "no body"})
    m = _EM_DASH.search(text)
    checks.append({"name": "no_em_dash", "pass": m is None,
                   "reason": f"dash at: {text[max(0, m.start() - 20):m.end() + 20]!r}" if m else ""})
    m = _SIGNIFICANCE.search(text)
    checks.append({"name": "no_significance_clause", "pass": m is None,
                   "reason": f"found {m.group(0)!r}" if m else ""})
    found = sorted({x.lower() for x in _LLM_ADVERBS.findall(text)})
    checks.append({"name": "no_llm_adverbs", "pass": not found,
                   "reason": ", ".join(found)})
    low = text.lower()
    for needle in case.get("must_include") or []:
        ok = needle.lower() in low or bool(re.search(needle, text, re.I))
        checks.append({"name": f"includes:{needle}", "pass": ok,
                       "reason": "" if ok else "not in body"})
    for needle in case.get("must_not_include") or []:
        hit = needle.lower() in low or bool(re.search(needle, text, re.I))
        checks.append({"name": f"excludes:{needle}", "pass": not hit,
                       "reason": "present" if hit else ""})
    cap = int(case.get("max_words") or 0)
    if cap:
        n = len(text.split())
        checks.append({"name": f"max_words:{cap}", "pass": n <= cap,
                       "reason": f"{n} words" if n > cap else ""})
    return checks


RUBRIC_PROMPT = """You grade one drafted email against the writing rules below. Answer with one JSON object and nothing else:
{{"score": <0-10>, "problems": ["<short phrase>", ...], "note": "<one line>"}}

Score 10: the body does what the brief asks, in the user's voice, and breaks no rule. Score 0: it fails the brief or reads as machine-written. A rule broken costs about 2 points each.

=== THE BRIEF THE WRITER WAS GIVEN ===
{brief}

=== CASE-SPECIFIC RUBRIC ===
{rubric}

=== WRITING RULES (docs/ea/drafting-voice.md) ===
{guide}

=== THE BODY TO GRADE ===
{body}
"""


def rubric_judge(body: str, case: dict, *, judge=None) -> dict:
    """{"score": 0-10 or None, "problems": [...], "note", "error"}."""
    guide = (REPO / "docs" / "ea" / "drafting-voice.md").read_text()
    guide = guide.split("## Pre-send review")[0]
    prompt = RUBRIC_PROMPT.format(brief=case["brief"], rubric=case.get("rubric") or "(none)",
                                  guide=guide, body=body)
    r = (judge or claude_text)(prompt, JUDGE_MODEL)
    if r.get("error"):
        return {"score": None, "problems": [], "note": "", "error": r["error"]}
    obj = _json_in(r.get("text") or "")
    try:
        score = max(0.0, min(10.0, float(obj.get("score"))))
    except (TypeError, ValueError):
        return {"score": None, "problems": [], "note": "",
                "error": f"judge gave no score: {(r.get('text') or '')[:120]!r}"}
    return {"score": score, "problems": [str(p)[:120] for p in obj.get("problems") or []][:8],
            "note": str(obj.get("note") or "")[:200], "error": ""}


def draft_body(case: dict, owner: str, model: str, *, writer=None, claude=None) -> dict:
    """{"body", "error", "model"} from whoever owns the drafting lane."""
    if owner == "remote":
        import email_writer
        import remote_llm
        _private_dir(store_dir())
        out = _private_dir(store_dir() / "tmp")
        path = out / f"{case['id']}-body.txt"
        if path.exists():
            path.unlink()
        answer, code = (writer or email_writer.write)(case["to"], case["brief"], str(path))
        if code != 0:
            return {"body": "", "error": f"writer fallback: {answer.get('reason', '')}"[:200],
                    "model": remote_llm.config().get("model", "")}
        return {"body": answer.get("body") or "", "error": "",
                "model": remote_llm.config().get("model", "")}
    import draft_ab
    msgs = draft_ab.remote_messages(case["brief"], case["to"])
    prompt = msgs[0]["content"] + "\n\n=== THE REQUEST ===\n" + msgs[1]["content"]
    if owner == "codex":
        r = (claude or codex_text)(prompt, model, JUDGE_TIMEOUT_S)
        model = model or "codex"
    else:
        r = (claude or claude_text)(prompt, model, JUDGE_TIMEOUT_S)
    if r.get("error"):
        return {"body": "", "error": r["error"], "model": model}
    body, _meta = draft_ab.finish_body(r["text"])
    return {"body": body, "error": "", "model": model}


def run_drafting_case(case: dict, owner: str, model: str, *, writer=None, claude=None,
                      judge=None) -> dict:
    t0 = time.monotonic()
    got = draft_body(case, owner, model, writer=writer, claude=claude)
    res = {"lane": "drafting", "id": case["id"], "name": case["name"],
           "category": case["category"], "bug_ref": case.get("bug_ref", ""),
           "owner": owner, "model": got["model"], "output": (got["body"] or "")[:4000]}
    if got["error"]:
        res.update(error=got["error"], pass_=False, score=0.0, checks=[],
                   duration_s=round(time.monotonic() - t0, 1))
        return _finish(res)
    checks = draft_checks(got["body"], case)
    det_ok = all(c["pass"] for c in checks)
    verdict = rubric_judge(got["body"], case, judge=judge)
    if verdict["error"]:
        res.update(error=f"judge: {verdict['error']}", pass_=False, score=0.0, checks=checks,
                   duration_s=round(time.monotonic() - t0, 1))
        return _finish(res)
    rubric = verdict["score"] / 10.0
    score = rubric if det_ok else min(rubric, 0.4)
    res.update(checks=checks, rubric=verdict["score"], problems=verdict["problems"],
               note=verdict["note"], score=round(score, 3),
               pass_=det_ok and verdict["score"] >= RUBRIC_PASS, error="",
               duration_s=round(time.monotonic() - t0, 1))
    return _finish(res)


# ── scheduling lane ────────────────────────────────────────────────────

REPAIRED_PROBLEMS = {"same-day", "too-many", "preferred-day-skipped", "preferred-flag-wrong", "unknown-id", "repaired"}


def run_pipeline_case(case: dict, *, pipeline=None) -> dict:
    """One typed request through the scheduling pipeline (sched_pipeline.run)
    over the case's canned thread, in shadow: the solver reads the live
    calendar, the model reads the intent and picks, nothing is written or
    recorded. The checks are code's own: the status, the picks, the check
    problems, and the shape (a counter-offer, an invite, a hold)."""
    import remote_llm
    import sched_pipeline
    t0 = time.monotonic()
    spec = case["pipeline"]
    expect = spec.get("expect") or {}
    res = {"lane": "scheduling", "id": case["id"], "name": case["name"], "category": case["category"],
           "bug_ref": case.get("bug_ref", ""), "owner": "remote", "arm": "pipeline",
           "model": remote_llm.config().get("model", "")}
    try:
        rec = (pipeline or sched_pipeline.run)(case["text"], messages=spec["thread"], record=False,
                                              run_id=f"release-{case['id']}")
    except Exception as e:
        res.update(error=f"pipeline failed: {type(e).__name__}: {str(e)[:160]}", pass_=False, score=0.0,
                   checks=[], output="", duration_s=round(time.monotonic() - t0, 1))
        return _finish(res)
    res["output"] = (str(rec.get("reply") or "") + ("\n\n--- body ---\n" + rec["body"] if rec.get("body") else "")
                     + ("\n\n--- event ---\n" + json.dumps(rec["event"]) if rec.get("event") else ""))[:4000]
    want_status = expect.get("status", "ok")
    checks = [{"name": f"status:{want_status}", "pass": rec.get("status") == want_status,
               "reason": f"status {rec.get('status')}" + (f": {rec.get('error')}" if rec.get("error") else "")}]
    if rec.get("status") in ("solver-failed", "intent-failed", "pick-failed") or rec.get("status") != want_status \
            and rec.get("error"):
        res.update(error=f"pipeline {rec.get('status')}: {str(rec.get('error'))[:160]}", pass_=False, score=0.0,
                   checks=checks, duration_s=round(time.monotonic() - t0, 1))
        return _finish(res)
    if "min_picks" in expect:
        n = len(rec.get("picked") or [])
        checks.append({"name": f"picks>={expect['min_picks']}", "pass": n >= int(expect["min_picks"]),
                       "reason": f"{n} picked"})
    if expect.get("clean", True) and rec.get("status") == "ok" and rec.get("kind") != "invite":
        # The model's pick errors code repairs (a same-day pair, a skipped
        # preferred day, an unknown id) leave the reply right; a clock in
        # the prose does not, and that is the problem a case fails on.
        probs = rec.get("problems") or []
        left = [x for x in probs if x not in REPAIRED_PROBLEMS]
        checks.append({"name": "no_unrepaired_problems", "pass": not left,
                       "reason": ", ".join(left) or (f"repaired: {', '.join(probs)}" if probs else "")})
    for key in ("counter", "kind", "wide", "widened_from"):
        if key in expect:
            got = rec.get(key)
            ok = bool(got) == bool(expect[key]) if isinstance(expect[key], bool) else got == expect[key]
            checks.append({"name": f"{key}:{expect[key]}", "pass": ok, "reason": f"got {got!r}"})
    ev = rec.get("event") or {}
    if "hold" in expect:
        checks.append({"name": f"hold:{expect['hold']}", "pass": bool(ev.get("hold")) == bool(expect["hold"]),
                       "reason": f"event {ev}" if ev else "no event"})
    for a in expect.get("attendees") or []:
        ok = a.lower() in str(ev.get("attendees") or "").lower()
        checks.append({"name": f"attendee:{a}", "pass": ok, "reason": "" if ok else f"attendees {ev.get('attendees')!r}"})
    if expect.get("subject"):
        ok = expect["subject"].lower() in str(ev.get("subject") or "").lower()
        checks.append({"name": f"subject:{expect['subject']}", "pass": ok, "reason": f"subject {ev.get('subject')!r}"})
    if expect.get("starts_at"):
        ok = str(ev.get("start") or "").startswith(expect["starts_at"])
        checks.append({"name": f"starts_at:{expect['starts_at']}", "pass": ok, "reason": f"start {ev.get('start')!r}"})
    passed = sum(1 for c in checks if c["pass"])
    res.update(checks=checks, score=round(passed / len(checks), 3), pass_=all(c["pass"] for c in checks),
               error="", duration_s=round(time.monotonic() - t0, 1), picked=rec.get("picked"),
               problems=rec.get("problems"))
    return _finish(res)


def run_scheduling_case(case: dict, *, agent=None, pipeline=None) -> dict:
    """One typed request through the remote scheduling lane: the pipeline
    over a canned thread when the case carries one, else the free-form
    agent (writes recorded and never executed; nothing is appended to the
    shadow store)."""
    if case.get("pipeline") is not None:
        return run_pipeline_case(case, pipeline=pipeline)
    import sched_agent
    t0 = time.monotonic()
    job = {"run_id": f"release-{case['id']}-{int(time.time())}", "ts": dt.datetime.now().isoformat(),
           "lane": "release-test", "thread_ts": "", "user_msg": case["text"],
           "task_block": f"The user posted this NEW message:\n\n> {case['text']}", "claude": {}}
    res = {"lane": "scheduling", "id": case["id"], "name": case["name"],
           "category": case["category"], "bug_ref": case.get("bug_ref", ""),
           "owner": "remote", "model": ""}
    try:
        import remote_llm
        res["model"] = remote_llm.config().get("model", "")
        turn, _results, _messages = (agent or sched_agent.run_agent)(job)
    except Exception as e:
        res.update(error=f"agent failed: {type(e).__name__}: {str(e)[:160]}", pass_=False,
                   score=0.0, checks=[], output="", duration_s=round(time.monotonic() - t0, 1))
        return _finish(res)
    reply = str(turn.get("reply") or "")
    writes = turn.get("writes") or []
    drafts = [str((w.get("args") or {}).get("body") or "") for w in writes
              if w.get("tool") in ("create-reply", "create-draft", "update-draft")]
    res["output"] = (reply + ("\n\n--- drafts ---\n" + "\n---\n".join(drafts) if drafts else ""))[:4000]
    res["writes"] = [w.get("tool") for w in writes]
    status = turn.get("status")
    if status not in ("ok", "budget", "timeout") or not reply.strip():
        res.update(error=f"agent status {status}, reply {'empty' if not reply.strip() else 'present'}"
                   + (f": {str(turn.get('error'))[:120]}" if turn.get("error") else ""),
                   pass_=False, score=0.0, checks=[], duration_s=round(time.monotonic() - t0, 1))
        return _finish(res)
    checks = scheduling_checks(reply, drafts, writes, case, sched_agent)
    passed = sum(1 for c in checks if c["pass"])
    res.update(checks=checks, score=round(passed / len(checks), 3) if checks else 0.0,
               pass_=all(c["pass"] for c in checks), error="",
               duration_s=round(time.monotonic() - t0, 1))
    return _finish(res)


def scheduling_checks(reply: str, drafts: list[str], writes: list[dict], case: dict,
                      sched_agent) -> list[dict]:
    expect = case.get("expect") or {}
    breaks = set(sched_agent.rule_breaks(reply, drafts=drafts))
    checks = [{"name": "replied", "pass": bool(reply.strip()), "reason": ""}]
    if expect.get("times", True):
        times, _mm = sched_agent.extract_times("\n".join([reply] + drafts))
        need = int(case.get("min_times") or 2)
        checks.append({"name": f"proposes_times>={need}", "pass": len(times) >= need,
                       "reason": f"{len(times)} concrete time(s) found"})
    if expect.get("no_decline", True):
        checks.append({"name": "no_decline", "pass": "declines" not in breaks,
                       "reason": "a draft declines on the user's behalf" if "declines" in breaks else ""})
    checks.append({"name": "no_tool_talk", "pass": "tool-talk" not in breaks,
                   "reason": "the reply talks about tools" if "tool-talk" in breaks else ""})
    checks.append({"name": "no_weekday_mismatch", "pass": "weekday-mismatch" not in breaks,
                   "reason": "a weekday does not match its date" if "weekday-mismatch" in breaks else ""})
    if expect.get("sender_only"):
        # Scheduling is ask-first: a turn may propose in chat and stage no
        # draft. The rule binds the draft when there is one.
        blob = json.dumps(writes)
        ok = not drafts or "sender_only" in blob or "sender-only" in blob
        checks.append({"name": "sender_only", "pass": ok,
                       "reason": ("no draft staged (ask-first)" if not drafts else
                                  "" if ok else "the poll reply goes to everyone")})
    for needle in case.get("must_mention") or []:
        hit = needle.lower() in (reply + " " + " ".join(drafts)).lower()
        checks.append({"name": f"mentions:{needle}", "pass": hit, "reason": "" if hit else "absent"})
    if expect.get("drafts_reply"):
        checks.append({"name": "drafts_reply", "pass": bool(drafts),
                       "reason": "" if drafts else "no reply draft was written"})
    for needle in case.get("must_not_include") or []:
        hit = needle.lower() in (reply + " " + " ".join(drafts)).lower()
        checks.append({"name": f"excludes:{needle}", "pass": not hit, "reason": "present" if hit else ""})
    return checks


# ── briefing lane ──────────────────────────────────────────────────────

def run_briefing_case(fixture: dict, model: str, timeout: int, *, run_eval=None,
                      harness: str = "claude") -> dict:
    """One eval fixture in full mode on `harness`. The keyword reaches
    run_eval only for another harness, so the Claude default keeps the
    signature a run_eval without it has."""
    import run_evals
    t0 = time.monotonic()
    kw = {"harness": harness} if harness != "claude" else {}
    r = (run_eval or run_evals.run_eval)(fixture, "full", model, timeout, **kw)
    res = {"lane": "briefing", "id": fixture["id"], "name": fixture["name"],
           "category": "regression" if fixture["category"] == "regression" else "quality",
           "bug_ref": fixture.get("bug_ref", ""), "owner": harness,
           "model": model or f"{harness}-default", "harness": harness,
           "output": (r.get("output_full") or r.get("output_preview") or "")[:4000],
           "checks": [{"name": a["assertion"], "pass": a["pass"], "reason": a.get("reason", "")}
                      for a in r.get("assertions") or []],
           "score": round(float(r.get("score") or 0.0), 3), "pass_": bool(r.get("pass")),
           "error": r.get("error") or "", "duration_s": round(time.monotonic() - t0, 1)}
    return _finish(res)


def _finish(res: dict) -> dict:
    res["pass"] = bool(res.pop("pass_", res.get("pass", False)))
    if res.get("error"):
        res["pass"] = False
    return res


# ── the run ────────────────────────────────────────────────────────────

def _median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def fold_repeats(runs: list[dict]) -> dict:
    """One result per case from its repeats: the median score, pass when
    every measured repeat passed, error only when every repeat errored."""
    measured = [r for r in runs if not r.get("error")]
    if not measured:
        out = dict(runs[0])
        out["repeats"] = len(runs)
        return out
    out = dict(measured[0])
    out["score"] = round(_median([r["score"] for r in measured]), 3)
    out["pass"] = all(r["pass"] for r in measured)
    out["repeats"] = len(runs)
    out["errors"] = len(runs) - len(measured)
    out["pass_rate"] = round(sum(1 for r in measured if r["pass"]) / len(measured), 2)
    return out


def summarize(results: list[dict], skipped: list[dict]) -> dict:
    lanes = {}
    for lane in LANES:
        rows = [r for r in results if r["lane"] == lane]
        measured = [r for r in rows if not r.get("error")]
        lanes[lane] = {
            "n": len(rows), "measured": len(measured),
            "passed": sum(1 for r in measured if r["pass"]),
            "failed": sum(1 for r in measured if not r["pass"]),
            "errors": len(rows) - len(measured),
            "skipped": sum(1 for s in skipped if s["lane"] == lane),
            "mean": round(sum(r["score"] for r in measured) / len(measured), 3) if measured else None,
            "regressions_failed": [r["id"] for r in measured
                                   if r["category"] == "regression" and not r["pass"]],
        }
    return lanes


def verdict_for(lanes: dict, baseline: dict | None) -> tuple[str, list[str]]:
    """(verdict, reasons)."""
    reasons = []
    total = sum(v["n"] for v in lanes.values())
    errors = sum(v["errors"] for v in lanes.values())
    if total and errors * 2 >= total:
        return "harness", [f"{errors} of {total} cases errored before reaching a model"]
    red = [f"{lane}: {', '.join(v['regressions_failed'])}"
           for lane, v in lanes.items() if v["regressions_failed"]]
    if red:
        reasons += ["regression case failed: " + r for r in red]
    base_lanes = (baseline or {}).get("lanes") or {}
    for lane, v in lanes.items():
        b = base_lanes.get(lane) or {}
        if v["mean"] is not None and b.get("mean") is not None and v["mean"] < b["mean"] - AMBER_DROP:
            reasons.append(f"{lane} mean fell {b['mean']:.2f} -> {v['mean']:.2f} "
                           f"(baseline {baseline.get('run_id')})")
    if red:
        return "red", reasons
    if reasons:
        return "amber", reasons
    return "green", reasons


def read_runs(store: Path) -> list[dict]:
    p = store / "runs.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def pick_baseline(runs: list[dict], *, run_id: str = "", explicit: str = "",
                  harness: str = "claude") -> dict | None:
    """The newest earlier measured run on the same harness (a run with no
    harness recorded ran on Claude), or the explicit one, which must be
    on this harness too: a codex run never compares to a claude baseline."""
    if explicit:
        for r in runs:
            if r.get("run_id") == explicit:
                if (r.get("harness") or "claude") != harness:
                    raise ValueError(f"run {explicit!r} ran on {r.get('harness') or 'claude'}, "
                                     f"not {harness}")
                return r
        raise ValueError(f"no run {explicit!r} in runs.jsonl")
    for r in reversed(runs):
        if r.get("run_id") != run_id and r.get("verdict") not in ("harness", "skipped") \
                and (r.get("harness") or "claude") == harness:
            return r
    return None


def _alert(text: str) -> None:
    try:
        import slack_notifier as chat
        chat.post_to_slack(chat.health_channel_key(), text)
    except Exception as e:
        print(f"[alert] chat notify failed: {e}", file=sys.stderr)


def report_text(summary: dict, results: list[dict], skipped: list[dict], verbose: bool) -> str:
    lines = [f"Release tests {summary['run_id']}: {summary['verdict'].upper()}"]
    for r in summary["reasons"]:
        lines.append(f"  ! {r}")
    lines.append(f"  commit {summary['fingerprint']['commit'][:8] or '?'}; claude {summary['claude_model']}"
                 f" on {summary.get('harness') or 'claude'};"
                 f" remote {summary['fingerprint']['remote_model'] or '(none)'}")
    cal = summary.get("calendar") or {}
    if cal:
        lines.append(f"  calendar {cal['mode']}" + (f" ({cal['path']})" if cal.get("path") else "")
                     + f": {cal['why']}")
    for lane, v in summary["lanes"].items():
        owner = summary["owners"][lane]
        mean = f"{v['mean']:.2f}" if v["mean"] is not None else "-"
        base = ((summary.get("baseline") or {}).get("lanes") or {}).get(lane) or {}
        bmean = f" (baseline {base['mean']:.2f})" if base.get("mean") is not None else ""
        lines.append(f"  {lane:<11} {owner['owner']:<7} mean {mean}{bmean}  pass {v['passed']}/{v['measured']}"
                     f"  errors {v['errors']}  skipped {v['skipped']}")
    for r in results:
        if verbose or not r["pass"]:
            tag = "ERR " if r.get("error") else ("ok  " if r["pass"] else "FAIL")
            lines.append(f"    {tag} [{r['lane']}/{r['category']}] {r['id']} score {r['score']:.2f}"
                         + (f"  {r['error']}" if r.get("error") else ""))
            for c in r.get("checks") or []:
                if verbose or not c["pass"]:
                    lines.append(f"         {'ok' if c['pass'] else 'x '} {c['name']}"
                                 + (f": {c['reason']}" if c.get("reason") else ""))
            if r.get("problems"):
                lines.append(f"         judge: {'; '.join(r['problems'])}")
    for s in skipped:
        lines.append(f"    skip [{s['lane']}] {s['id']}: {s['why']}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tag", default="", help="label for this run (default: git describe)")
    ap.add_argument("--lane", action="append", choices=LANES, default=[])
    ap.add_argument("--case", action="append", default=[], metavar="ID",
                    help="only these case or fixture ids (repeatable)")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--baseline", default="", metavar="RUN_ID")
    ap.add_argument("--claude-model", default="", help="override the Claude model under test")
    ap.add_argument("--harness", choices=HARNESSES, default="claude",
                    help="the harness the briefing lane runs on (default claude)")
    ap.add_argument("--harness-model", default="",
                    help="the model for a harness other than claude; empty keeps that "
                         "harness's own default (codex: the model in its config)")
    ap.add_argument("--canned-calendar", nargs="?", const=str(CANNED_CALENDAR), default=None,
                    metavar="PATH", help="the scheduling lane reads this calendar fixture instead of "
                    "the live calendars (default fixture: tests/release/fixtures/calendar-canned.json); "
                    "on by default when no Graph or Google token is readable")
    ap.add_argument("--live-calendar", action="store_true",
                    help="read the live calendars even with no token readable")
    ap.add_argument("--timeout", type=int, default=BRIEFING_TIMEOUT_S)
    ap.add_argument("--skip-if-unchanged", action="store_true",
                    help="exit 3 when code, models and switches match the last run")
    ap.add_argument("--alert", action="store_true", help="post a red, amber or harness verdict to chat")
    ap.add_argument("--dry-run", action="store_true", help="show routing and cases; call no model")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--verbose", "-v", action="store_true")
    a = ap.parse_args(argv)

    store = store_dir()
    sw = switches()
    model = a.claude_model or claude_model()
    owners = route(sw, a.harness)
    fp = fingerprint(sw, model, a.harness)
    lanes = a.lane or list(LANES)
    only = set(a.case) or None
    calendar = calendar_mode(a.canned_calendar, a.live_calendar, calendar_tokens())
    if calendar["mode"] == "canned":
        if not Path(calendar["path"]).is_file():
            print(f"no canned calendar at {calendar['path']}", file=sys.stderr)
            return EXIT_HARNESS
        # sched_agent._resident_cmd reads it when it builds a solver call,
        # so every scheduling case of this run reads the fixture.
        os.environ[CANNED_CALENDAR_ENV] = calendar["path"]

    if a.skip_if_unchanged:
        last_p = store / "last-run.json"
        last = json.loads(last_p.read_text()) if last_p.exists() else None
        if unchanged(fp, last):
            print(f"unchanged since {last.get('run_id')}: commit {fp['commit'][:8]}, "
                  f"claude {fp['claude_model']}, remote {fp['remote_model']}, switches "
                  f"{json.dumps(fp['switches'], sort_keys=True)}. Skipping.")
            return EXIT_SKIPPED

    tag = a.tag or subprocess.run(["git", "-C", str(REPO), "describe", "--tags", "--always"],
                                  capture_output=True, text=True).stdout.strip() or "untagged"
    run_id = f"{tag}-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}"

    plan = []
    skipped = []
    for lane in lanes:
        own = owners[lane]["owner"]
        if lane == "briefing":
            import run_evals
            for f in run_evals.load_fixtures(None, only):
                plan.append(("briefing", f["id"], f))
        else:
            for c in load_cases(lane):
                if only and c["id"] not in only:
                    continue
                if own == "skip":
                    skipped.append({"lane": lane, "id": c["id"], "why": owners[lane]["why"]})
                else:
                    plan.append((lane, c["id"], c))

    if a.dry_run:
        print(f"run {run_id}; claude {model} on {a.harness}; remote {fp['remote_model'] or '(none)'}")
        print(f"  calendar: {calendar['mode']}" + (f" ({calendar['path']})" if calendar["path"] else "")
              + f", {calendar['why']}")
        for lane in lanes:
            print(f"  {lane}: {owners[lane]['owner']} ({owners[lane]['why']})")
        for lane, cid, item in plan:
            print(f"    {lane}/{cid}" + (" (pipeline)" if isinstance(item, dict) and item.get("pipeline") else ""))
        for s in skipped:
            print(f"    skip {s['lane']}/{s['id']}: {s['why']}")
        return EXIT_GREEN

    if not plan:
        print("no case to run", file=sys.stderr)
        return EXIT_SKIPPED

    results = []
    for lane, cid, item in plan:
        reps = []
        for _ in range(max(1, a.repeat)):
            if lane == "briefing":
                # The Claude model name means nothing to another harness.
                lane_model = model if a.harness == "claude" else a.harness_model
                reps.append(run_briefing_case(item, lane_model, a.timeout, harness=a.harness))
            elif lane == "drafting":
                reps.append(run_drafting_case(item, owners[lane]["owner"], model))
            else:
                reps.append(run_scheduling_case(item))
                reps[-1]["calendar"] = calendar["mode"]
            # The harness answers the briefing lane and the Claude-owned
            # drafting cases; a remote-owned case names none.
            reps[-1].setdefault("harness", a.harness if lane == "briefing" else
                                "claude" if reps[-1].get("owner") == "claude" else "")
            if not a.json:
                r = reps[-1]
                print(f"  {'ERR ' if r.get('error') else ('ok  ' if r['pass'] else 'FAIL')} "
                      f"{lane}/{cid} score {r['score']:.2f} ({r['duration_s']}s)"
                      + (f"  {r['error']}" if r.get("error") else ""), flush=True)
        results.append(fold_repeats(reps))

    lanes_summary = summarize(results, skipped)
    baseline = pick_baseline(read_runs(store), run_id=run_id, explicit=a.baseline, harness=a.harness)
    verdict, reasons = verdict_for(lanes_summary, baseline)
    summary = {"run_id": run_id, "tag": tag, "run_at": dt.datetime.now().isoformat(timespec="seconds"),
               "fingerprint": fp, "claude_model": model, "judge_model": JUDGE_MODEL,
               "harness": a.harness, "calendar": calendar,
               "owners": owners, "lanes": lanes_summary, "verdict": verdict, "reasons": reasons,
               "baseline": {"run_id": baseline["run_id"], "lanes": baseline["lanes"]} if baseline else None,
               "repeat": a.repeat, "partial": bool(a.lane or only)}

    _private_dir(store)
    _private_dir(store / "runs")
    (store / "runs" / f"{run_id}.json").write_text(
        json.dumps({"summary": summary, "results": results, "skipped": skipped}, indent=1, default=str))
    with (store / "runs.jsonl").open("a") as fh:
        fh.write(json.dumps(summary, default=str) + "\n")
    if not summary["partial"] and verdict != "harness":
        (store / "last-run.json").write_text(json.dumps({**fp, "run_id": run_id}, indent=1))

    if a.json:
        print(json.dumps({"summary": summary, "results": results, "skipped": skipped},
                         indent=2, default=str))
    else:
        print()
        print(report_text(summary, results, skipped, a.verbose))
        print(f"  stored: {store / 'runs' / (run_id + '.json')}")

    if a.alert and verdict != "green":
        icon = {"red": ":red_circle:", "amber": ":large_orange_circle:",
                "harness": ":rotating_light:"}[verdict]
        _alert(f"{icon} *Release tests {run_id}: {verdict}*\n"
               + "\n".join(f"- {r}" for r in reasons)
               + f"\nLog: `{store / 'runs' / (run_id + '.json')}`")

    return {"green": EXIT_GREEN, "amber": EXIT_FAIL, "red": EXIT_FAIL,
            "harness": EXIT_HARNESS}[verdict]


if __name__ == "__main__":
    sys.exit(main())

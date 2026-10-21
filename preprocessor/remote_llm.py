#!/usr/bin/env python3
"""Remote LLM: the owner-side client and switchboard for the lanes that run
work on an outside OpenAI-compatible endpoint instead of Claude
(docs/llm-lanes.md; design history in docs/remote-llm.md).

Six switches (SWITCHES), each in the private config:
  * `mail`        off | shadow | enforce | sealed. shadow: per-message
                  extraction beside the live lanes (mail_privacy.py).
                  enforce: the same extraction, and the mail gate
                  (mail_gate.py) hands the Claude lanes only what it lets
                  through; a room or thread still reads a thread's text on
                  request. ghost: enforce, and for a dated week every
                  lane's read is also sealed on the side and recorded,
                  so the loss can be graded before the cutover
                  (ghost_week.py); past its window it reads enforce.
                  sealed: enforce, and every mail read verb, in every
                  lane, returns the remote model's record of the message
                  in place of its text (mail_privacy.seal_output).
  * `scheduling`  off | shadow | enforce: the scheduling agent
                  (sched_agent.py). shadow records beside Claude; enforce
                  answers the scheduling channel and the proactive
                  scheduler itself, with Claude as the fallback
  * `drafting`    off | ab | remote: who writes email bodies (draft_ab.py,
                  email_writer.py)
  * `digests`     off | remote: the list-digest and admissions-digest
                  lanes on the remote model (remote_digests.py)
  * `briefings`   off | remote: the morning briefing and the afternoon
                  update on the remote model (remote_briefing.py)
  * `audits`      off | remote: the weekly audit on the remote model
                  (remote_audit.py), with flagged items handed to Claude

`shadow` runs beside the live lanes and records what the remote model
would have done; it changes nothing the Claude lanes see or do. A shadow
switch reads `off` after `shadow_until`, and one with no end date reads
`off`, so a shadow run always stops by itself. `enforce`, `sealed`, `ab`,
`remote` take work from Claude and have no end date.

Only an owner process uses the endpoint. The key is an owner secret
(REMOTE_LLM_API_KEY in the private secrets.env) and the config sits in the private
dir, which ea-bot cannot write, so the resident can neither read the key
nor point the stream at another server. In any other process `config()`
returns every switch off.

Config, <private_dir>/remote-llm.json, written by `remote_llm.py init`:
    {"endpoint": "https://llm.example.edu/v1",
     "model": "your-model-name",
     "mail": "shadow", "scheduling": "shadow",
     "drafting": "remote", "digests": "remote",
     "shadow_until": "2026-10-26",
     "keep_from_anthropic": {"domains": ["mil", "contractor.example.com"], "addresses": [],
                             "admissions": true}}

The endpoint is any OpenAI-compatible chat-completions server reachable
over https (vLLM, llama.cpp, Ollama behind TLS, a hosted provider). The
smallest useful setup is the writer alone: `init` with the endpoint and
model, the key in secrets.env, then `set drafting remote`.

The remote endpoint receives every message; nothing gates it.
`keep_from_anthropic` names the mail that should, in time, reach only the
remote model and never Claude: a participant whose address or domain is
listed (a domain also covers its subdomains, so "mil" covers army.mil)
and, with `admissions` on, a message the admissions filter would move.
In shadow mode it only labels records, so the reduced briefing and the
reports can show what an enforce mode would keep from Claude.

Store: <private_dir>/remote-llm/ (0700), JSONL files (0600). calls.jsonl
holds one line per request with purpose, sizes, timings and errors; it
never holds prompt or completion text.

CLI (as the owner):
    remote_llm.py switches        each switch: the value set and the one in effect
    remote_llm.py set <switch> <value>
                                  write one switch (validated, 0600, other keys kept)
    remote_llm.py status          switches, model, store sizes, last calls
    remote_llm.py ping            one tiny request; prints latency and model
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import owner_trust as _owner_trust  # noqa: E402

CONFIG_FILE = "remote-llm.json"
STORE_DIRNAME = "remote-llm"
SECRET_NAME = "REMOTE_LLM_API_KEY"
PARTS = ("mail", "scheduling")


def api_key() -> str:
    """The endpoint key (SECRET_NAME); "" when it is not set. Owner
    secrets only, so "" in any non-owner process."""
    return _owner_trust.owner_secret(SECRET_NAME)
MODES = ("off", "shadow", "enforce", "ghost", "sealed")
GHOST_DAYS = 7

# A model the server has swapped out takes four to five minutes to answer
# its first request; a warm one answers in seconds. One limit covers both.
TIMEOUT_S = 330
# Requests in flight at once, across every process (the live tick, the
# scheduling agent, tuning runs). vLLM batches concurrent requests on the
# GPU, so a few at a time cost the server little; a caller beyond the
# limit waits for a slot.
MAX_PARALLEL = 4
SLOT_POLL_S = 0.15
# The chat bot's small jobs (`chat-*`) sit in front of a person: a thread
# title, a dash rewrite. Each has a fallback, so it waits this long for a
# slot and then gives up instead of queueing behind long mail calls.
CHAT_SLOT_WAIT_S = 2.0
BREAKER_THRESHOLD = 3
BREAKER_COOLDOWN_S = 900

_owner = _owner_trust.is_owner_process


class RemoteLLMError(RuntimeError):
    """The endpoint failed, refused, or answered with something unusable."""


class ModelOutputError(RemoteLLMError):
    """The server answered, but the model's output was empty. A fault of
    the prompt or the token budget, not of the server, so it never counts
    toward the circuit breaker."""


class NoSlotError(RemoteLLMError):
    """Every request slot stayed taken for the caller's whole wait. Says
    nothing about the server, so it never counts toward the breaker."""


# ── config ────────────────────────────────────────────────────────────

def _off() -> dict:
    return {"endpoint": "", "model": "", "shadow_until": "", "ghost": {},
            "mail": "off", "scheduling": "off", "drafting": "off", "digests": "off",
            "briefings": "off", "audits": "off",
            "qwen_share": 0.5,
            "ab": {"share": 0.0, "lanes": [], "from": "", "until": "", "arm": "remote"},
            "sched_backstop": True,
            "keep_from_anthropic": {"domains": [], "addresses": [], "admissions": True}}


def _clean_list(values) -> list[str]:
    out = []
    for v in values if isinstance(values, list) else []:
        if isinstance(v, str) and v.strip():
            out.append(v.strip().lower().lstrip("@.").rstrip("."))
    return out


def config() -> dict:
    """The validated config. All switches off outside an owner process,
    when the file is absent or malformed, or when it names no https
    endpoint and model."""
    cfg = _off()
    if not _owner():
        return cfg
    try:
        raw = json.loads(_owner_trust.read_owner_file(
            _owner_trust.private_dir() / CONFIG_FILE))
    except FileNotFoundError:
        return cfg
    except Exception as e:
        print(f"remote_llm: config unreadable, every switch off: {e}", file=sys.stderr)
        return cfg
    if not isinstance(raw, dict):
        return cfg
    endpoint = str(raw.get("endpoint") or "").strip().rstrip("/")
    model = str(raw.get("model") or "").strip()
    if not endpoint.startswith("https://") or not model:
        return cfg
    cfg["endpoint"], cfg["model"] = endpoint, model
    cfg["shadow_until"] = str(raw.get("shadow_until") or "").strip()
    if raw.get("max_parallel") is not None:
        cfg["max_parallel"] = raw.get("max_parallel")
    # Drafting (draft_ab.py): the `ab` experiment and its remote share, or
    # `remote`, the remote model as the writer; else off.
    cfg["drafting"] = raw.get("drafting") if raw.get("drafting") in ("ab", "remote") else "off"
    # The digest lanes (remote_digests.py): `remote` runs them on the
    # remote model; anything else leaves them on Claude.
    cfg["digests"] = "remote" if raw.get("digests") == "remote" else "off"
    # The briefing lanes (remote_briefing.py): `remote` writes the morning
    # briefing and the afternoon update on the remote model.
    cfg["briefings"] = "remote" if raw.get("briefings") == "remote" else "off"
    # The weekly audit (remote_audit.py): `remote` writes it on the remote
    # model and hands the flagged items to Claude.
    cfg["audits"] = "remote" if raw.get("audits") == "remote" else "off"
    # The Claude backstop on the scheduling agent's live writes
    # (sched_agent.backstop_review) is on unless the file says false.
    cfg["sched_backstop"] = raw.get("sched_backstop") is not False
    # The model A/B (model_arms.py): {"share", "lanes", "from", "until",
    # "arm"}. `arm` is which arm a drawn topic gets, `remote` (the on-prem
    # model in the Claude Code harness) or `pipeline` (sched_pipeline);
    # anything else reads as remote.
    ab = raw.get("ab") if isinstance(raw.get("ab"), dict) else {}
    try:
        share = min(1.0, max(0.0, float(ab.get("share") or 0.0)))
    except (TypeError, ValueError):
        share = 0.0
    cfg["ab"] = {"share": share, "lanes": _clean_list(ab.get("lanes")),
                 "from": str(ab.get("from") or "").strip(), "until": str(ab.get("until") or "").strip(),
                 "arm": "pipeline" if ab.get("arm") == "pipeline" else "remote"}
    try:
        cfg["qwen_share"] = min(1.0, max(0.0, float(raw.get("qwen_share", 0.5))))
    except (TypeError, ValueError):
        cfg["qwen_share"] = 0.5
    for part in PARTS:
        val = raw.get(part)
        cfg[part] = val if val in MODES else "off"
    # The ghost windows (ghost_week.py): {"mail": {"from", "until"},
    # "scheduling": {"from", "until"}}, dates as YYYY-MM-DD.
    ghost = raw.get("ghost") if isinstance(raw.get("ghost"), dict) else {}
    cfg["ghost"] = {}
    for part in PARTS:
        w = ghost.get(part) if isinstance(ghost.get(part), dict) else {}
        cfg["ghost"][part] = {"from": str(w.get("from") or "").strip(),
                              "until": str(w.get("until") or "").strip()}
    ns = raw.get("keep_from_anthropic")
    ns = ns if isinstance(ns, dict) else {}
    cfg["keep_from_anthropic"] = {
        "domains": _clean_list(ns.get("domains")),
        "addresses": _clean_list(ns.get("addresses")),
        "admissions": ns.get("admissions") is not False,
    }
    # Enabled plugins add domains (docs/plugins.md, mail rules); they never
    # remove one the owner listed.
    try:
        try:
            import plugin_registry as _plugins
        except ModuleNotFoundError:
            from preprocessor import plugin_registry as _plugins
        extra = _plugins.mail_rules().get("keep_from_anthropic_domains") or []
    except Exception:
        extra = []
    if extra:
        doms = cfg["keep_from_anthropic"]["domains"]
        cfg["keep_from_anthropic"]["domains"] = doms + [d for d in extra if d not in doms]
    return cfg


def max_parallel(cfg: dict | None = None) -> int:
    """Request slots: `max_parallel` in the config, 1 to 8, default 4."""
    cfg = cfg if cfg is not None else config()
    try:
        n = int(cfg.get("max_parallel") or MAX_PARALLEL)
    except (TypeError, ValueError):
        n = MAX_PARALLEL
    return min(8, max(1, n))


def _until(cfg: dict) -> date | None:
    try:
        return date.fromisoformat(cfg.get("shadow_until") or "")
    except ValueError:
        return None


def ghost_window(part: str, cfg: dict | None = None) -> tuple[date | None, date | None]:
    """(from, until) of the part's ghost week, or (None, None)."""
    cfg = cfg if cfg is not None else config()
    w = (cfg.get("ghost") or {}).get(part) or {}
    try:
        frm = date.fromisoformat(w.get("from") or "")
    except ValueError:
        frm = None
    try:
        until = date.fromisoformat(w.get("until") or "")
    except ValueError:
        until = None
    return frm, until


def mode(part: str, cfg: dict | None = None, today: date | None = None) -> str:
    """`sealed`, `ghost`, `enforce`, `shadow` or `off` for one part. The
    end date applies to shadow only; `sealed` and `ghost` are mail only,
    `enforce` is mail or scheduling. `ghost` reads `enforce` once its
    window has passed (the lanes behave as under enforce either way);
    `sealed` and `enforce` have no end date."""
    cfg = cfg if cfg is not None else config()
    val = cfg.get(part, "off")
    if val == "sealed":
        return "sealed" if part == "mail" else "off"
    if val == "ghost":
        if part != "mail":
            return "off"
        _frm, until = ghost_window("mail", cfg)
        return "ghost" if until is None or (today or date.today()) <= until else "enforce"
    if val == "enforce":
        return "enforce" if part in ("mail", "scheduling") else "off"
    if val != "shadow":
        return "off"
    until = _until(cfg)
    if until is None or (today or date.today()) > until:
        return "off"
    return "shadow"


def enforcing(part: str = "mail", cfg: dict | None = None) -> bool:
    """True when the part takes work from Claude now: `enforce`, or
    `ghost` or `sealed` for mail. The one check a lane makes before it
    gates."""
    return mode(part, cfg) in ("enforce", "ghost", "sealed")


def reading(part: str = "mail", cfg: dict | None = None) -> bool:
    """True when the remote model reads for this part at all: shadow,
    enforce or sealed."""
    return mode(part, cfg) != "off"


def keep_reason(addr: str, cfg: dict | None = None) -> str:
    """Why mail with `addr` on it belongs to the remote model alone (an
    enforce mode would keep it from Claude), or ''. Covers the address and
    domain lists; the admissions rule needs the text, see
    mail_privacy.keep_reason()."""
    cfg = cfg if cfg is not None else config()
    a = (addr or "").strip().lower()
    if not a:
        return ""
    ks = cfg.get("keep_from_anthropic") or {}
    if a in ks.get("addresses", []):
        return "listed address"
    domain = a.rsplit("@", 1)[-1]
    for d in ks.get("domains", []):
        if domain == d or domain.endswith("." + d):
            return f"listed domain {d}"
    return ""


# ── store ─────────────────────────────────────────────────────────────

def store_dir() -> Path:
    """<private_dir>/remote-llm, created 0700 on first use."""
    d = _owner_trust.private_dir() / STORE_DIRNAME
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    return d


def append(name: str, rec: dict) -> None:
    """One JSON line onto <store>/<name>, created 0600, never through a link."""
    path = store_dir() / name
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW
                 | getattr(os, "O_CLOEXEC", 0), 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(json.dumps(rec, default=str) + "\n")


def read(name: str) -> list[dict]:
    """Every parseable line of <store>/<name>; [] when absent."""
    path = store_dir() / name
    out = []
    try:
        with open(path, errors="replace") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
    except FileNotFoundError:
        pass
    return out


def purge(names: list[str] | None = None) -> list[str]:
    """Delete the named store files (default: all of them). Returns what
    was removed."""
    d = store_dir()
    removed = []
    for p in sorted(d.rglob("*"), reverse=True):
        rel = str(p.relative_to(d))
        if names is not None and rel not in names and p.name not in names:
            continue
        try:
            if p.is_dir() and not p.is_symlink():
                p.rmdir()
            else:
                p.unlink()
            removed.append(rel)
        except OSError:
            pass
    return removed


# ── the client ────────────────────────────────────────────────────────

def _breaker(purpose: str = ""):
    """The breaker for this kind of call. Tuning runs have their own, so a
    failing experiment never blocks the live tick or the scheduling agent."""
    # The tool gateway's turns have their own too: a harness that
    # overruns the context must not close the mail gate's breaker. So do
    # the chat bot's small jobs (`chat-*`: thread titles, the dash
    # rewrite, context export), whose short timeouts expire while the
    # server loads a cold model.
    name = ("remote-llm-tune" if purpose.startswith("mail-tune")
            else "remote-llm-gateway" if purpose == "tool-gateway"
            else "remote-llm-chat" if purpose.startswith("chat-") else "remote-llm")
    try:
        import retry_policy
        return retry_policy.CircuitBreaker(name, threshold=BREAKER_THRESHOLD,
                                           cooldown=BREAKER_COOLDOWN_S)
    except Exception:
        return None


def _log_call(rec: dict) -> None:
    try:
        append("calls.jsonl", rec)
    except OSError:
        pass


def _post(url: str, key: str, payload: dict, timeout: float, opener=None) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    open_fn = opener or urllib.request.urlopen
    try:
        with open_fn(req, timeout=timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read() or b"{}").get("error", {}).get("message", "")
        except Exception:
            pass
        raise RemoteLLMError(f"HTTP {e.code}: {detail[:200]}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RemoteLLMError(f"request failed: {e}") from None
    try:
        return json.loads(body)
    except ValueError:
        raise RemoteLLMError("response is not JSON") from None


def _client_fault(e: Exception) -> bool:
    m = re.match(r"HTTP (\d{3})", str(e))
    return bool(m) and m.group(1).startswith("4") and m.group(1) not in ("408", "429")


def _acquire_slot(n: int, wait_s: float | None = None) -> int:
    """Hold one of `n` request slots (lock files in the store) and return
    its descriptor; while all are taken, wait for the first to free. With
    `wait_s`, raise NoSlotError once that many seconds pass without one."""
    deadline = None if wait_s is None else time.monotonic() + max(0.0, wait_s)
    while True:
        for i in range(n):
            fd = os.open(store_dir() / f"endpoint-{i}.lock",
                         os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                         0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                os.close(fd)
        if deadline is not None and time.monotonic() >= deadline:
            raise NoSlotError(f"no free request slot within {wait_s:g}s")
        time.sleep(SLOT_POLL_S)


def chat(messages: list[dict], *, purpose: str, schema: dict | None = None,
         schema_name: str = "answer", max_tokens: int = 1500,
         timeout: float = TIMEOUT_S, cfg: dict | None = None, opener=None,
         model: str | None = None, thinking: bool = False,
         temperature: float = 0.0, n: int = 1, sampling: dict | None = None,
         slot_wait: float | None = None) -> dict:
    """One chat completion. Returns {"content", "contents", "usage",
    "latency_ms", "model"}. With `schema`, the server constrains the reply
    to that JSON schema. `model` overrides the configured one for this call
    (the tuning harness compares models); thinking is off and temperature 0
    unless asked. `n` asks for that many answers in one request
    (`contents`). Raises RemoteLLMError.

    At most max_parallel() requests are in flight across all processes;
    a caller beyond that waits for a slot. `slot_wait` bounds that wait
    (NoSlotError past it); a `chat-*` purpose defaults to CHAT_SLOT_WAIT_S,
    every other purpose waits as long as it takes."""
    if not _owner():
        raise RemoteLLMError("the remote endpoint is used by owner processes only")
    cfg = cfg if cfg is not None else config()
    if not cfg.get("endpoint") or not cfg.get("model"):
        raise RemoteLLMError("remote-llm.json names no endpoint and model")
    key = api_key()
    if not key:
        raise RemoteLLMError(f"{SECRET_NAME} is not in the private secrets.env")
    breaker = _breaker(purpose)
    if breaker is not None and breaker.is_open():
        raise RemoteLLMError("circuit open after repeated failures")
    use_model = (model or cfg["model"]).strip()

    payload = {
        "model": use_model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "chat_template_kwargs": {"enable_thinking": bool(thinking)},
    }
    if n > 1:
        payload["n"] = int(n)
    # `sampling` carries the model's own recommended knobs for a lane
    # (top_p, top_k, presence_penalty, repetition_penalty); the tool
    # gateway sets them, the record lanes keep greedy decoding.
    for k in ("top_p", "top_k", "presence_penalty", "repetition_penalty", "min_p"):
        if sampling and sampling.get(k) is not None:
            payload[k] = sampling[k]
    if schema is not None:
        payload["response_format"] = {"type": "json_schema", "json_schema": {
            "name": schema_name, "schema": schema, "strict": True}}

    prompt_chars = sum(len(str(m.get("content") or "")) for m in messages)
    rec = {"ts": datetime.now().astimezone().isoformat(timespec="seconds"),
           "purpose": purpose, "model": use_model, "prompt_chars": prompt_chars}
    waited = time.monotonic()
    if slot_wait is None and purpose.startswith("chat-"):
        slot_wait = CHAT_SLOT_WAIT_S
    try:
        lock_fd = _acquire_slot(max_parallel(cfg), slot_wait)
    except NoSlotError as e:
        # A full queue says nothing about the endpoint: no breaker count.
        rec.update(ok=False, error=str(e), queue_ms=int((time.monotonic() - waited) * 1000))
        _log_call(rec)
        raise
    try:
        rec["queue_ms"] = int((time.monotonic() - waited) * 1000)
        t0 = time.monotonic()
        try:
            data = _post(cfg["endpoint"] + "/chat/completions", key, payload,
                         timeout, opener=opener)
            if "error" in data:
                msg = data["error"].get("message", "") if isinstance(data["error"], dict) else str(data["error"])
                raise RemoteLLMError(f"server error: {msg[:200]}")
            got_model = str(data.get("model") or "")
            if got_model != use_model:
                raise RemoteLLMError(f"answered by {got_model or '?'}, not {use_model}")
            choices = data.get("choices") or [{}]
            choice = choices[0]
            content = (choice.get("message") or {}).get("content") or ""
            contents = [(c.get("message") or {}).get("content") or "" for c in choices]
            if not content.strip():
                raise ModelOutputError(f"empty completion (finish: {choice.get('finish_reason')})")
        except RemoteLLMError as e:
            rec.update(ok=False, error=str(e)[:300],
                       latency_ms=int((time.monotonic() - t0) * 1000))
            _log_call(rec)
            # A bad request (4xx other than too-many-requests) is this
            # caller's fault, not the endpoint's: it does not count
            # toward opening the breaker.
            if breaker is not None and not isinstance(e, ModelOutputError) and not _client_fault(e):
                breaker.record_failure(str(e))
            raise
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)

    latency_ms = int((time.monotonic() - t0) * 1000)
    usage = data.get("usage") or {}
    rec.update(ok=True, latency_ms=latency_ms,
               prompt_tokens=usage.get("prompt_tokens"),
               completion_tokens=usage.get("completion_tokens"),
               finish=choice.get("finish_reason"))
    _log_call(rec)
    if breaker is not None:
        breaker.record_success()
    return {"content": content, "contents": contents, "usage": usage,
            "latency_ms": latency_ms, "model": got_model, "finish": choice.get("finish_reason")}


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")


def _parse_obj(text: str) -> dict:
    try:
        obj = json.loads(_FENCE.sub("", text.strip()))
    except ValueError:
        raise RemoteLLMError("completion is not valid JSON") from None
    if not isinstance(obj, dict):
        raise RemoteLLMError("completion is not a JSON object")
    return obj


def chat_json(messages: list[dict], *, purpose: str, schema: dict, **kw) -> tuple[dict, dict]:
    """chat() with a schema, parsed. Returns (object, call metadata); with
    n > 1 the metadata's `objects` holds every parsed answer that is valid
    JSON (the first answer must be)."""
    res = chat(messages, purpose=purpose, schema=schema, **kw)
    obj = _parse_obj(res["content"])
    if len(res.get("contents") or []) > 1:
        objs = [obj]
        for text in res["contents"][1:]:
            try:
                objs.append(_parse_obj(text))
            except RemoteLLMError:
                continue
        res["objects"] = objs
    return obj, res


# ── CLI ───────────────────────────────────────────────────────────────

SWITCHES = {"mail": ("off", "shadow", "enforce", "ghost", "sealed"),
            "scheduling": ("off", "shadow", "enforce"),
            "drafting": ("off", "ab", "remote"), "digests": ("off", "remote"),
            "briefings": ("off", "remote"), "audits": ("off", "remote")}


def switches(cfg: dict | None = None, today: date | None = None) -> dict:
    """{switch: {"set": value in the file, "effective": value in force}}.
    They differ when a shadow switch is past `shadow_until`."""
    cfg = cfg if cfg is not None else config()
    out = {}
    for key in SWITCHES:
        eff = mode(key, cfg, today) if key in PARTS else cfg.get(key, "off")
        out[key] = {"set": cfg.get(key, "off"), "effective": eff}
    return out


# The cutover values: each is refused unless the part's ghost week ended
# in a GO (ghost_week.gate), or the caller forces it.
CUTOVER = {("mail", "sealed"), ("scheduling", "enforce")}
# The ghost values: each opens a dated window the reports measure.
GHOSTS = {("mail", "ghost"), ("scheduling", "shadow")}


def set_switch(key: str, value: str, *, days: int | None = None, force: bool = False,
               today: date | None = None) -> dict:
    """Write one switch into the private config, keeping every other key.
    Owner process only; the value must be one that switch takes. A ghost
    value (`mail ghost`, `scheduling shadow`) opens a window of `days`
    (default GHOST_DAYS) from today, and for scheduling moves
    `shadow_until` to its end. A cutover value (`mail sealed`,
    `scheduling enforce`) needs the ghost week's GO, or `force`."""
    if key not in SWITCHES:
        raise ValueError(f"no switch {key!r}; the switches are {', '.join(SWITCHES)}")
    if value not in SWITCHES[key]:
        raise ValueError(f"{key} takes {' | '.join(SWITCHES[key])}, not {value!r}")
    if days is not None and (key, value) not in GHOSTS:
        raise ValueError("--days applies to `mail ghost` and `scheduling shadow` only")
    if not _owner():
        raise RemoteLLMError("only the owner can set a switch")
    if (key, value) in CUTOVER and not force:
        try:
            import ghost_week
            ok, why = ghost_week.gate(key)
        except Exception as e:
            ok, why = False, f"the ghost week could not be checked ({str(e)[:100]})"
        if not ok:
            raise RemoteLLMError(f"{key} {value} refused: {why}. Pass --force to set it anyway.")
    path = _owner_trust.private_dir() / CONFIG_FILE
    try:
        raw = json.loads(_owner_trust.read_owner_file(path))
    except FileNotFoundError:
        raise RemoteLLMError(f"{path} does not exist; write the endpoint and model first") from None
    if not isinstance(raw, dict):
        raise RemoteLLMError(f"{path} is not a JSON object")
    before = raw.get(key, "off")
    raw[key] = value
    out = {"switch": key, "before": before, "after": value}
    if (key, value) in GHOSTS and (days is not None or before != value or key == "mail"):
        today = today or date.today()
        n = max(1, int(days if days is not None else GHOST_DAYS))
        window = {"from": today.isoformat(), "until": (today + timedelta(days=n - 1)).isoformat()}
        ghost = raw.get("ghost") if isinstance(raw.get("ghost"), dict) else {}
        ghost[key] = window
        raw["ghost"] = ghost
        if key == "scheduling":
            raw["shadow_until"] = window["until"]
        out["window"] = window
    _owner_trust.write_owner_file(path, (json.dumps(raw, indent=2) + "\n").encode(), mode=0o600)
    out["effective"] = switches(today=today)[key]["effective"]
    return out


def init_config(endpoint: str, model: str, *, drafting: str | None = None) -> dict:
    """Write <private_dir>/remote-llm.json with the endpoint and model, every
    switch off unless the file already sets one. `drafting` ("remote" or
    "off") sets that switch in the same write, so a writer-only install is
    one command. Owner only; the endpoint must be https."""
    if not _owner():
        raise RemoteLLMError("only the owner can write the remote-LLM config")
    endpoint = (endpoint or "").strip().rstrip("/")
    model = (model or "").strip()
    if not endpoint.startswith("https://"):
        raise ValueError("the endpoint must start with https://")
    if not model:
        raise ValueError("the model name is empty")
    if drafting is not None and drafting not in SWITCHES["drafting"]:
        raise ValueError(f"drafting must be one of {', '.join(SWITCHES['drafting'])}")
    path = _owner_trust.private_dir() / CONFIG_FILE
    try:
        raw = json.loads(_owner_trust.read_owner_file(path))
        created = False
    except FileNotFoundError:
        raw, created = {}, True
    if not isinstance(raw, dict):
        raise RemoteLLMError(f"{path} is not a JSON object")
    raw["endpoint"], raw["model"] = endpoint, model
    for key in SWITCHES:
        raw.setdefault(key, "off")
    raw.setdefault("keep_from_anthropic", {"domains": [], "addresses": [], "admissions": True})
    if drafting is not None:
        raw["drafting"] = drafting
    _owner_trust.write_owner_file(path, (json.dumps(raw, indent=2) + "\n").encode(), mode=0o600)
    return {"path": str(path), "created": created, "endpoint": endpoint, "model": model,
            "switches": {k: raw.get(k, "off") for k in SWITCHES},
            "key_present": bool(api_key()), "key_name": SECRET_NAME}


def _status() -> dict:
    cfg = config()
    calls = read("calls.jsonl") if _owner() else []
    files = {}
    if _owner():
        for p in sorted(store_dir().rglob("*")):
            if p.is_file():
                files[str(p.relative_to(store_dir()))] = p.stat().st_size
    return {
        "owner_process": bool(_owner()),
        "endpoint": cfg["endpoint"], "model": cfg["model"],
        "shadow_until": cfg["shadow_until"], "ghost": cfg.get("ghost"),
        "mail": mode("mail", cfg), "scheduling": mode("scheduling", cfg),
        "drafting": cfg["drafting"], "digests": cfg["digests"], "briefings": cfg["briefings"],
        "audits": cfg["audits"],
        "keep_from_anthropic": cfg["keep_from_anthropic"],
        "key_present": bool(_owner() and api_key()),
        "store": files,
        "calls": len(calls),
        "errors": sum(1 for c in calls if not c.get("ok")),
        "last_calls": calls[-3:],
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("switches", help="each switch: the value set and the one in effect")
    ip = sub.add_parser("init", help="write the endpoint and model into the private config")
    ip.add_argument("--endpoint", required=True, help="https URL of an OpenAI-compatible server, ending in /v1")
    ip.add_argument("--model", required=True, help="the model name the server expects")
    ip.add_argument("--drafting", choices=SWITCHES["drafting"], default=None,
                    help="also set the drafting switch (remote = the writer is on)")
    sp = sub.add_parser("set", help="write one switch into the private config")
    sp.add_argument("switch", choices=sorted(SWITCHES))
    sp.add_argument("value")
    sp.add_argument("--days", type=int, default=None,
                    help="length of the ghost window (mail ghost, scheduling shadow); default 7")
    sp.add_argument("--force", action="store_true",
                    help="set a cutover value (mail sealed, scheduling enforce) without a GO")
    sub.add_parser("status")
    sub.add_parser("ping")
    a = ap.parse_args(argv)
    if a.cmd == "switches":
        print(json.dumps(switches(), indent=2))
        return 0
    if a.cmd == "init":
        try:
            out = init_config(a.endpoint, a.model, drafting=a.drafting)
        except (ValueError, RemoteLLMError) as e:
            print(f"init refused: {e}", file=sys.stderr)
            return 2
        print(json.dumps(out, indent=2))
        if not out["key_present"]:
            print(f"note: {SECRET_NAME} is not set; add it to the private secrets.env",
                  file=sys.stderr)
        return 0
    if a.cmd == "set":
        try:
            print(json.dumps(set_switch(a.switch, a.value, days=a.days, force=a.force)))
        except (ValueError, RemoteLLMError) as e:
            print(f"set refused: {e}", file=sys.stderr)
            return 2
        return 0
    if a.cmd == "status":
        print(json.dumps(_status(), indent=2))
        return 0
    try:
        res = chat([{"role": "user", "content": "Reply with the single word: ready"}],
                   purpose="ping", max_tokens=20)
    except RemoteLLMError as e:
        print(f"ping failed: {e}", file=sys.stderr)
        return 1
    print(json.dumps({"model": res["model"], "latency_ms": res["latency_ms"],
                      "reply": res["content"].strip()[:40]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

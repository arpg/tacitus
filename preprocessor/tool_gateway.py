#!/usr/bin/env python3
"""tool_gateway: tool calling for the remote model, done on this machine.

The remote endpoint serves the on-prem model with native tool calling off:
a request that carries `tools` gets plain text back. A harness that needs
tool calls (Claude Code through ANTHROPIC_BASE_URL, opencode, Qwen Code)
talks to this gateway on loopback instead. The gateway renders the
request's tool list into the model's own tool prompt (the `<tools>` block
and `<tool_call>` answer format its chat template teaches), sends a plain
chat completion upstream through remote_llm.chat (the owner's key, the
request slots, the breaker and the call log), and parses the `<tool_call>`
blocks the model answers with into tool-call objects of the face the
harness speaks:

  POST /v1/messages              the Anthropic Messages API (Claude Code)
  POST /v1/messages/count_tokens an estimate
  POST /v1/chat/completions      the OpenAI chat API (opencode, Qwen Code)
  GET  /v1/models                the configured model, under both shapes
  GET  /health

Streaming is answered after the completion is complete: the events of one
response in one burst. Nothing runs here. The harness runs the tools,
under its own allowlist, as the user it runs as. The gateway binds
loopback only and takes a bearer token (`token` prints it; the bot passes
it to the lane as ANTHROPIC_AUTH_TOKEN). Owner process: remote_llm.chat
refuses any other.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import owner_trust as _owner_trust  # noqa: E402
import remote_llm as _rl  # noqa: E402

HOST = "127.0.0.1"
PORT = int(os.environ.get("EA_TOOL_GATEWAY_PORT") or 3070)
TOKEN_FILE = "tool-gateway.token"
MAX_BODY = 8 * 1024 * 1024
MAX_TOKENS_CAP = 8000
DEFAULT_MAX_TOKENS = 4000
UPSTREAM_TIMEOUT_S = 330
THINKING = os.environ.get("EA_TOOL_GATEWAY_THINKING", "") == "1"
PURPOSE = "tool-gateway"

# The model's own recommended decoding for a tool-using turn. Greedy
# decoding (temperature 0) makes Qwen3 repeat itself, the model card
# says so, and a replay of past turns showed it: the same tool re-run
# with slightly varied arguments until the step budget. The thinking
# variant uses the card's thinking settings. A presence penalty pushes
# a turn that has said something once to move on.
SAMPLING = {"temperature": 0.7, "top_p": 0.8, "top_k": 20, "min_p": 0.0, "presence_penalty": 1.0}
SAMPLING_THINK = {"temperature": 0.6, "top_p": 0.95, "top_k": 20, "min_p": 0.0, "presence_penalty": 1.0}


def variant(model_name: str) -> dict:
    """The lane's variant, from suffixes on the model name the harness
    sends (`qwen3.8-mm+think+t0.5`): `think` turns thinking on, `greedy`
    is temperature 0, `tN.N` sets the temperature, `pN.N` the presence
    penalty. The upstream model is the configured one whatever the name
    says. Returns {"thinking", "sampling"}."""
    parts = str(model_name or "").split("+")[1:]
    think = "think" in parts
    slim = "slim" in parts
    s = dict(SAMPLING_THINK if think else SAMPLING)
    for p in parts:
        if p == "greedy":
            s.update(temperature=0.0, top_p=1.0, top_k=-1, presence_penalty=0.0)
        elif re.fullmatch(r"t\d(?:\.\d+)?", p):
            s["temperature"] = float(p[1:])
        elif re.fullmatch(r"p\d(?:\.\d+)?", p):
            s["presence_penalty"] = float(p[1:])
    return {"thinking": think or THINKING, "sampling": s, "slim": slim}


_ENV_SECTION = re.compile(r"^# Environment\n.*?(?=^# |\Z)", re.M | re.S)
SLIM_SYSTEM = ("You are the user's assistant, working through tools. The message that follows carries your "
               "operating instructions, the user's profile and the request; follow them exactly.")


def slim_system(text: str) -> str:
    """The harness's system prompt reduced to its Environment section
    (the date, the working directory) under a one-line role: the generic
    coding-agent guidance is weight a smaller model carries for nothing."""
    m = _ENV_SECTION.search(text or "")
    return SLIM_SYSTEM + ("\n\n" + m.group(0).strip() if m else "")

TOOLS_HEAD = (
    "# Tools\n\n"
    "You may call one or more functions to assist with the user query.\n\n"
    "You are provided with function signatures within <tools></tools> XML tags:\n<tools>\n")
TOOLS_TAIL = (
    "\n</tools>\n\n"
    "For each function call, return a json object with function name and arguments "
    "within <tool_call></tool_call> XML tags:\n"
    "<tool_call>\n{\"name\": <function-name>, \"arguments\": <args-json-object>}\n</tool_call>\n"
    "Write the call and stop; the results come back in <tool_response> blocks. "
    "Never invent a result. Use only the functions listed.")

GUIDANCE = (
    "Working rules for tool use: when a call fails, read the error before calling again; "
    "a call that failed with the same error twice is not tried a third time, use another tool "
    "or another argument (an id from a fresh search, not one quoted in the prompt), or say what "
    "failed and stop. Make each call count; do not re-run a call whose result you already have. "
    "A search that returns results has done its job: pick from what it showed, and refine at most "
    "once, by subject or sender address, before you work with what you have. When the prompt "
    "already quotes the message or thread you need, work from that text and read the thread by "
    "its conversation id instead of searching for it. After two or three reads, decide and answer.")

_TOOL_CALL = re.compile(r"<tool_call>\s*(.*?)\s*(?:</tool_call>|\Z)", re.S)
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")
# The coder-style call the same model also writes: <function=NAME> with
# either `arguments= {json}` or <parameter=key>value</parameter> blocks.
_FUNCTION = re.compile(r"<function=([\w.-]+)>\s*(.*?)\s*(?:</function>|\Z)", re.S)
_PARAM = re.compile(r"<parameter=([\w.-]+)>\s*(.*?)\s*</parameter>", re.S)


def _log(msg: str) -> None:
    print(f"tool_gateway: {msg}", file=sys.stderr, flush=True)


# ── the token ─────────────────────────────────────────────────────────

def token(create: bool = True) -> str:
    """The gateway's bearer token, in the owner's private directory; made
    on first use."""
    path = _owner_trust.private_dir() / TOKEN_FILE
    try:
        return _owner_trust.read_owner_file(path).decode().strip()
    except FileNotFoundError:
        if not create:
            return ""
    tok = secrets.token_urlsafe(32)
    _owner_trust.write_owner_file(path, (tok + "\n").encode(), mode=0o600)
    return tok


# ── the tool prompt and the parse ─────────────────────────────────────

def render_tools(tools: list[dict]) -> str:
    """The model's own tool block for a list of {name, description,
    parameters} functions. '' when there are none."""
    if not tools:
        return ""
    lines = []
    for t in tools:
        fn = {"name": t["name"], "description": str(t.get("description") or "")[:4000],
              "parameters": t.get("parameters") or {"type": "object", "properties": {}}}
        lines.append(json.dumps({"type": "function", "function": fn}, ensure_ascii=False))
    return TOOLS_HEAD + "\n".join(lines) + TOOLS_TAIL


_KEYS = "name|arguments|parameters|function|tool"
# A top-level key the model wrote without its opening quote, or with no
# quotes: `{"name": "Bash",\narguments": {...}}`. Only the call's own
# keys are repaired; a key inside an argument string is left alone.
_BARE_KEY = re.compile(r'([{,]\s*)(?:"?)(' + _KEYS + r')"?(\s*:)')


def _loads_lenient(s: str):
    s = _FENCE.sub("", s.strip())
    try:
        return json.loads(s)
    except ValueError:
        pass
    repaired = _BARE_KEY.sub(r'\1"\2"\3', s)
    if repaired != s:
        try:
            return json.loads(repaired)
        except ValueError:
            s = repaired
    # A call whose closing braces the model dropped (the text ends inside
    # the object): close one to three and retry.
    if s.startswith("{"):
        for n in (1, 2, 3):
            try:
                return json.loads(s + "}" * n)
            except ValueError:
                continue
    # The model sometimes runs on after the object; take the first object.
    depth, start = 0, None
    for i, ch in enumerate(s):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    return json.loads(s[start:i + 1])
                except ValueError:
                    return None
    return None


def _function_form(body: str) -> dict | None:
    """A <function=NAME> block as {"name", "arguments"}, or None."""
    m = _FUNCTION.search(body)
    if not m:
        return None
    name, inner = m.group(1), m.group(2)
    params = {k: v for k, v in _PARAM.findall(inner)}
    if params:
        args = {}
        for k, v in params.items():
            parsed = _loads_lenient(v) if v.strip()[:1] in "{[" or v.strip() in ("true", "false", "null") else None
            args[k] = parsed if parsed is not None else v
        return {"name": name, "arguments": args}
    inner = re.sub(r"^\s*arguments\s*=\s*", "", inner)
    args = _loads_lenient(inner) if inner.strip() else {}
    return {"name": name, "arguments": args if isinstance(args, dict) else {}}


def parse_tool_calls(text: str, known: set[str] | None = None) -> tuple[str, list[dict]]:
    """(remaining text, calls). A call is {"name", "arguments": dict}. A
    block that is not a call to a known function stays in the text."""
    calls = []
    kept = text or ""
    for m in list(_TOOL_CALL.finditer(text or "")):
        obj = _loads_lenient(m.group(1))
        if isinstance(obj, dict) and not obj.get("name"):
            # {"function": "Bash", ...}, {"tool": "Bash", ...} and the
            # OpenAI shape {"function": {"name": ..., "arguments": ...}}
            fn = obj.get("function") if obj.get("function") is not None else obj.get("tool")
            if isinstance(fn, dict):
                obj = {"name": fn.get("name"), "arguments": fn.get("arguments", obj.get("arguments", {}))}
            elif isinstance(fn, str):
                obj = dict(obj, name=fn)
        if not isinstance(obj, dict) or not obj.get("name"):
            obj = _function_form(m.group(1))
        if not isinstance(obj, dict) or not obj.get("name"):
            continue
        name = str(obj["name"])
        if known is not None and name not in known:
            continue
        args = obj.get("arguments", obj.get("parameters", {}))
        if isinstance(args, str):
            args = _loads_lenient(args)
        if not isinstance(args, dict):
            args = {}
        calls.append({"name": name, "arguments": args})
        kept = kept.replace(m.group(0), "", 1)
    return kept.strip(), calls


REASK = ("That call could not be read: it was not one JSON object with \"name\" and \"arguments\", "
         "or it named a function that is not in the list. Write the call again inside one "
         "<tool_call> block, or answer in words with what you have.")
MALFORMED_STOP = ("I could not complete this step: my tool call was malformed. "
                  "Tell me how you want to proceed.")
_MARKUP = re.compile(r"</?tool_call>|</?tool_response>|<function=[\w.-]+>|</function>")


def _residual_markup(text: str) -> bool:
    """Call or result markup left in text that parsed into no call."""
    return bool(_MARKUP.search(text or ""))


def strip_markup(text: str) -> str:
    """Text with every call and result block, and every stray tag,
    removed: what may reach a reader of the answer."""
    out = _TOOL_CALL.sub("", text or "")
    out = _FUNCTION.sub("", out)
    out = _MARKUP.sub("", out)
    return out.strip()


# ── the Anthropic face ────────────────────────────────────────────────

def _block_text(content) -> str:
    """The text of a tool result's content: a string or a list of blocks."""
    if isinstance(content, str):
        return content
    out = []
    for b in content or []:
        if isinstance(b, dict):
            if b.get("type") == "text":
                out.append(str(b.get("text") or ""))
            elif b.get("type") == "image":
                out.append("[image omitted]")
            elif b.get("type") == "document":
                out.append("[document omitted]")
        elif isinstance(b, str):
            out.append(b)
    return "\n".join(out)


def _merge(messages: list[dict]) -> list[dict]:
    """Consecutive turns of one role become one; the first turn is a
    user's."""
    out: list[dict] = []
    for m in messages:
        if not m["content"].strip() and m["role"] == "assistant":
            continue
        if out and out[-1]["role"] == m["role"]:
            out[-1]["content"] += "\n\n" + m["content"]
        else:
            out.append(dict(m))
    if out and out[0]["role"] == "assistant":
        out.insert(0, {"role": "user", "content": "(continue)"})
    if not out:
        out.append({"role": "user", "content": "(continue)"})
    return out


def _call_text(name: str, args) -> str:
    return "<tool_call>\n" + json.dumps({"name": name, "arguments": args}, ensure_ascii=False) + "\n</tool_call>"


def _result_text(content: str, name: str = "") -> str:
    head = f"<tool_response>\n" + (f"({name})\n" if name else "")
    return head + content + "\n</tool_response>"


def anthropic_to_chat(req: dict, *, slim: bool = False) -> tuple[list[dict], list[dict]]:
    """(upstream messages, functions) for one Messages-API request.
    Server-side tools (no input_schema) are dropped: nothing here runs
    them. With `slim` the harness's system prompt is cut to its
    Environment section."""
    sys_parts = []
    system = req.get("system")
    if isinstance(system, str):
        sys_parts.append(system)
    elif isinstance(system, list):
        sys_parts.extend(str(b.get("text") or "") for b in system
                         if isinstance(b, dict) and b.get("type") == "text")
    if slim and sys_parts:
        sys_parts = [slim_system("\n\n".join(sys_parts))]
    tools = [{"name": t["name"], "description": t.get("description", ""),
              "parameters": t.get("input_schema")}
             for t in req.get("tools") or []
             if isinstance(t, dict) and t.get("name") and isinstance(t.get("input_schema"), dict)]
    choice = req.get("tool_choice") or {}
    if isinstance(choice, dict) and choice.get("type") == "none":
        tools = []
    block = render_tools(tools)
    if block:
        sys_parts.append(block)
        sys_parts.append(GUIDANCE)
    msgs: list[dict] = []
    for m in req.get("messages") or []:
        role = "assistant" if m.get("role") == "assistant" else "user"
        content = m.get("content")
        if isinstance(content, str):
            msgs.append({"role": role, "content": content})
            continue
        parts = []
        for b in content or []:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "text":
                parts.append(str(b.get("text") or ""))
            elif t == "tool_use":
                parts.append(_call_text(str(b.get("name") or ""), b.get("input") or {}))
            elif t == "tool_result":
                parts.append(_result_text(_block_text(b.get("content"))))
            elif t == "image":
                parts.append("[image omitted]")
            elif t == "document":
                parts.append("[document omitted]")
            elif t in ("thinking", "redacted_thinking"):
                continue
        msgs.append({"role": role, "content": "\n".join(p for p in parts if p is not None)})
    out = _merge(msgs)
    if sys_parts:
        out.insert(0, {"role": "system", "content": "\n\n".join(p for p in sys_parts if p)})
    return out, tools


def _tool_id() -> str:
    return "toolu_" + secrets.token_hex(12)


def anthropic_response(req: dict, text: str, calls: list[dict], finish: str, usage: dict) -> dict:
    content = []
    if text:
        content.append({"type": "text", "text": text})
    for c in calls:
        content.append({"type": "tool_use", "id": _tool_id(), "name": c["name"], "input": c["arguments"]})
    if not content:
        content.append({"type": "text", "text": ""})
    stop = "tool_use" if calls else ("max_tokens" if finish == "length" else "end_turn")
    return {"id": "msg_" + secrets.token_hex(12), "type": "message", "role": "assistant",
            "model": str(req.get("model") or ""), "content": content,
            "stop_reason": stop, "stop_sequence": None,
            "usage": {"input_tokens": int(usage.get("prompt_tokens") or 0),
                      "output_tokens": int(usage.get("completion_tokens") or 0)}}


def anthropic_events(msg: dict) -> list[tuple[str, dict]]:
    """The streaming events of one finished message."""
    head = dict(msg, content=[], stop_reason=None)
    head["usage"] = {"input_tokens": msg["usage"]["input_tokens"], "output_tokens": 0}
    ev = [("message_start", {"type": "message_start", "message": head})]
    for i, block in enumerate(msg["content"]):
        if block["type"] == "text":
            ev.append(("content_block_start", {"type": "content_block_start", "index": i,
                                               "content_block": {"type": "text", "text": ""}}))
            ev.append(("content_block_delta", {"type": "content_block_delta", "index": i,
                                               "delta": {"type": "text_delta", "text": block["text"]}}))
        else:
            ev.append(("content_block_start", {"type": "content_block_start", "index": i,
                                               "content_block": {"type": "tool_use", "id": block["id"],
                                                                 "name": block["name"], "input": {}}}))
            ev.append(("content_block_delta", {"type": "content_block_delta", "index": i,
                                               "delta": {"type": "input_json_delta",
                                                         "partial_json": json.dumps(block["input"])}}))
        ev.append(("content_block_stop", {"type": "content_block_stop", "index": i}))
    ev.append(("message_delta", {"type": "message_delta",
                                 "delta": {"stop_reason": msg["stop_reason"], "stop_sequence": None},
                                 "usage": {"output_tokens": msg["usage"]["output_tokens"]}}))
    ev.append(("message_stop", {"type": "message_stop"}))
    return ev


# ── the OpenAI face ───────────────────────────────────────────────────

def openai_to_chat(req: dict) -> tuple[list[dict], list[dict]]:
    """(upstream messages, functions) for one chat-completions request."""
    tools = []
    for t in req.get("tools") or []:
        fn = t.get("function") if isinstance(t, dict) else None
        if isinstance(fn, dict) and fn.get("name"):
            tools.append({"name": fn["name"], "description": fn.get("description", ""),
                          "parameters": fn.get("parameters")})
    if req.get("tool_choice") == "none":
        tools = []
    block = render_tools(tools)
    sys_parts, msgs = [], []
    names: dict[str, str] = {}
    for m in req.get("messages") or []:
        role = m.get("role")
        content = m.get("content")
        text = _block_text(content) if not isinstance(content, str) else content
        if role in ("system", "developer"):
            sys_parts.append(text or "")
        elif role == "assistant":
            parts = [text] if text else []
            for c in m.get("tool_calls") or []:
                fn = c.get("function") or {}
                args = fn.get("arguments")
                if isinstance(args, str):
                    args = _loads_lenient(args) or {}
                names[str(c.get("id") or "")] = str(fn.get("name") or "")
                parts.append(_call_text(str(fn.get("name") or ""), args or {}))
            msgs.append({"role": "assistant", "content": "\n".join(parts)})
        elif role == "tool":
            msgs.append({"role": "user",
                         "content": _result_text(text or "", names.get(str(m.get("tool_call_id") or ""), ""))})
        else:
            msgs.append({"role": "user", "content": text or ""})
    if block:
        sys_parts.append(block)
        sys_parts.append(GUIDANCE)
    out = _merge(msgs)
    if sys_parts:
        out.insert(0, {"role": "system", "content": "\n\n".join(p for p in sys_parts if p)})
    return out, tools


def openai_response(req: dict, text: str, calls: list[dict], finish: str, usage: dict) -> dict:
    message = {"role": "assistant", "content": text or None}
    if calls:
        message["tool_calls"] = [{"id": "call_" + secrets.token_hex(12), "type": "function",
                                  "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])}}
                                 for c in calls]
    reason = "tool_calls" if calls else ("length" if finish == "length" else "stop")
    return {"id": "chatcmpl-" + secrets.token_hex(12), "object": "chat.completion",
            "created": int(time.time()), "model": str(req.get("model") or ""),
            "choices": [{"index": 0, "message": message, "finish_reason": reason}],
            "usage": {"prompt_tokens": int(usage.get("prompt_tokens") or 0),
                      "completion_tokens": int(usage.get("completion_tokens") or 0),
                      "total_tokens": int(usage.get("total_tokens") or 0)}}


def openai_chunks(resp: dict) -> list[dict]:
    """The streaming chunks of one finished completion."""
    base = {"id": resp["id"], "object": "chat.completion.chunk", "created": resp["created"],
            "model": resp["model"]}
    msg = resp["choices"][0]["message"]
    out = [dict(base, choices=[{"index": 0, "delta": {"role": "assistant", "content": ""},
                                "finish_reason": None}])]
    if msg.get("content"):
        out.append(dict(base, choices=[{"index": 0, "delta": {"content": msg["content"]},
                                        "finish_reason": None}]))
    for i, c in enumerate(msg.get("tool_calls") or []):
        out.append(dict(base, choices=[{"index": 0, "delta": {"tool_calls": [
            {"index": i, "id": c["id"], "type": "function",
             "function": {"name": c["function"]["name"], "arguments": c["function"]["arguments"]}}]},
            "finish_reason": None}]))
    out.append(dict(base, choices=[{"index": 0, "delta": {},
                                    "finish_reason": resp["choices"][0]["finish_reason"]}],
                    usage=resp["usage"]))
    return out


# ── upstream ──────────────────────────────────────────────────────────

def complete(messages: list[dict], *, max_tokens: int, temperature: float | None,
             chat=None, var: dict | None = None) -> tuple[str, str, dict]:
    """(text, finish reason, usage) from the remote model. `var` is the
    lane's variant; a temperature the harness set overrides its
    sampling temperature."""
    chat = chat or _rl.chat
    var = var or variant("")
    sampling = dict(var["sampling"])
    temp = sampling.pop("temperature", 0.7) if temperature is None else float(temperature)
    r = chat(messages, purpose=PURPOSE, max_tokens=max(16, min(int(max_tokens), MAX_TOKENS_CAP)),
             temperature=temp, thinking=var["thinking"], timeout=UPSTREAM_TIMEOUT_S, sampling=sampling)
    finish = "length" if r.get("finish") == "length" else "stop"
    return str(r.get("content") or ""), finish, r.get("usage") or {}


LOOP_MAX = 3
STEP_SOFT = 24     # tool calls in the turn after which the model is told to finish
STEP_HARD = 40     # tool calls after which the gateway answers for it
CONTEXT_TOKENS = int(os.environ.get("EA_TOOL_GATEWAY_CONTEXT") or 131072)
CHARS_PER_TOKEN = 2.0      # mail ids and JSON tokenize near two characters a token; 2.6 let a 292k-character turn through to a 400
CONTEXT_MARGIN = 4000
OVERFLOW_CUT = 0.7         # on the server's own overflow verdict the budget shrinks to this share and the call is made once more
KEEP_RECENT_RESULTS = 6    # tool results never trimmed: the newest ones


def _tokens(messages: list[dict]) -> int:
    return int(sum(len(str(m.get("content") or "")) for m in messages) / CHARS_PER_TOKEN) + 1


def _overflow(e: Exception) -> bool:
    """The server's own verdict that the request did not fit its window."""
    t = str(e).lower()
    return "context" in t and ("length" in t or "window" in t or "exceed" in t)


def trim_context(messages: list[dict], max_tokens: int, *, scale: float = 1.0,
                 keep_recent: int | None = None) -> tuple[list[dict], int]:
    """(messages, results trimmed). The harness believes an unknown model
    has a 200k window and compacts on that; the model's is CONTEXT_TOKENS.
    Past the budget the oldest tool results become one-line stubs, the
    newest KEEP_RECENT_RESULTS untouched; past that, assistant text
    between them is cut to its first line. `scale` shrinks the budget
    (the retry after the server's own overflow verdict), `keep_recent`
    narrows the untouched tail."""
    budget = int((CONTEXT_TOKENS - int(max_tokens) - CONTEXT_MARGIN) * scale)
    keep_recent = KEEP_RECENT_RESULTS if keep_recent is None else keep_recent
    if _tokens(messages) <= budget:
        return messages, 0
    out = [dict(m) for m in messages]
    results = [i for i, m in enumerate(out) if m.get("role") == "user" and "<tool_response>" in str(m.get("content") or "")]
    trimmed = 0
    # The oldest results first, up to the untouched tail; when the tail
    # itself is what does not fit (six large reads), the pass continues
    # into it and leaves only the newest result whole.
    for i in results[:max(0, len(results) - keep_recent)] + results[max(0, len(results) - keep_recent):-1]:
        if _tokens(out) <= budget:
            break
        n = len(str(out[i]["content"]))
        out[i]["content"] = f"<tool_response>\n[earlier result trimmed to fit the context: {n} characters]\n</tool_response>"
        trimmed += 1
    if _tokens(out) > budget:
        for i, m in enumerate(out[1:-1], 1):
            if m.get("role") == "assistant" and "<tool_call>" not in str(m.get("content") or ""):
                out[i]["content"] = str(m["content"]).split("\n", 1)[0][:300]
            if _tokens(out) <= budget:
                break
    return out, trimmed


def _turn_start(messages: list[dict]) -> int:
    """The index of the message that opened the current turn: the last
    user message that is not a tool result. A resumed session carries
    every earlier turn's calls in its history; they are not this turn's."""
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if m.get("role") == "user" and "<tool_response>" not in str(m.get("content") or ""):
            return i
    return 0


def _calls_so_far(messages: list[dict]) -> int:
    """Tool calls the turn has made: the <tool_call> blocks in its
    assistant turns since the user's message that opened it."""
    n = 0
    for m in messages[_turn_start(messages):]:
        if m.get("role") == "assistant":
            n += len(parse_tool_calls(str(m.get("content") or ""))[1])
    return n


def step_budget(messages: list[dict]) -> tuple[list[dict], str]:
    """(messages, stop text). Past STEP_SOFT calls the last user turn
    carries a note to finish with what the model has; past STEP_HARD the
    stop text is the answer and no completion is asked for. A turn that
    searches on with new queries each time never trips the loop guard;
    this bounds it."""
    n = _calls_so_far(messages)
    if n >= STEP_HARD:
        return messages, (f"I stopped after {n} tool calls without reaching an answer. "
                          "Tell me what you want me to do next.")
    if n >= STEP_SOFT and messages and messages[-1].get("role") == "user":
        out = list(messages)
        out[-1] = dict(out[-1], content=str(out[-1].get("content") or "")
                       + f"\n\n[You have made {n} tool calls in this turn. Finish now: answer with what you "
                         f"have, say what you could not find, and make no further calls unless one is "
                         f"essential.]")
        return out, ""
    return messages, ""


_ERROR_MARKS = ("error", "exit code", "traceback", "not found", "bad request", "permission")


def _last_result_failed(messages: list[dict]) -> bool:
    for u in reversed(messages):
        if u.get("role") == "user" and "<tool_response>" in str(u.get("content") or ""):
            body = str(u["content"]).split("<tool_response>", 1)[1][:600].lower()
            return any(k in body for k in _ERROR_MARKS)
    return False


def loop_guard(messages: list[dict], calls: list[dict]) -> str:
    """A stop text when the model's new calls repeat the calls of its
    last turns exactly: the third identical call, or the second when
    the last one failed (a tool that keeps failing the same way, and a
    model that keeps trying it); '' otherwise. The harness then ends
    the turn with the text instead of running the call again."""
    if not calls:
        return ""
    want = json.dumps(calls, sort_keys=True)
    limit = 2 if _last_result_failed(messages) else LOOP_MAX
    seen = 0
    for m in reversed(messages):
        if m.get("role") != "assistant":
            continue
        _text, prior = parse_tool_calls(str(m.get("content") or ""))
        if json.dumps(prior, sort_keys=True) != want:
            break
        seen += 1
        if seen >= limit - 1:
            names = ", ".join(sorted({c["name"] for c in calls}))
            last = ""
            for u in reversed(messages):
                if u.get("role") == "user" and "<tool_response>" in str(u.get("content") or ""):
                    last = str(u["content"]).split("<tool_response>", 1)[1].split("</tool_response>")[0]
                    last = " ".join(last.split())[:300]
                    break
            return (f"I stopped: the same call to {names} came back the same way {limit} times"
                    + (f" ({last})" if last else "") + ". Tell me how you want to proceed.")
    return ""


def _estimate_tokens(req: dict) -> int:
    return max(1, len(json.dumps(req.get("messages") or []) + json.dumps(req.get("system") or "")
                      + json.dumps(req.get("tools") or [])) // 4)


def answer(path: str, req: dict, *, chat=None, model: str = "") -> tuple[int, dict | list, bool]:
    """(status, body, streamed). The body of a streamed answer is the
    event list (Anthropic: (event, data) pairs; OpenAI: chunks)."""
    if path == "/v1/messages/count_tokens":
        return 200, {"input_tokens": _estimate_tokens(req)}, False
    var = variant(str(req.get("model") or ""))
    if path == "/v1/messages":
        to_chat, make, events = (lambda r: anthropic_to_chat(r, slim=var["slim"])), anthropic_response, anthropic_events
    elif path == "/v1/chat/completions":
        to_chat, make, events = openai_to_chat, openai_response, openai_chunks
    else:
        return 404, {"type": "error", "error": {"type": "not_found_error", "message": path}}, False
    messages, tools = to_chat(req)
    if os.environ.get("EA_TOOL_GATEWAY_DUMP") == "1":
        # The owner's debugging aid: the last request's system text and
        # the sizes of its turns, in the private store.
        try:
            sizes = [(m["role"], len(str(m.get("content") or ""))) for m in messages]
            _owner_trust.write_owner_file(_rl.store_dir() / "gateway-last-request.txt",
                                          (json.dumps(sizes) + "\n\n" + str(messages[0].get("content") or "")
                                           if messages and messages[0].get("role") == "system" else
                                           json.dumps(sizes)).encode(), mode=0o600)
        except Exception as e:
            _log(f"dump failed: {e}")
    try:
        temperature = float(req["temperature"]) if req.get("temperature") is not None else None
    except (TypeError, ValueError):
        temperature = None
    max_tokens = max(16, min(int(req.get("max_tokens") or DEFAULT_MAX_TOKENS), MAX_TOKENS_CAP))
    # A request with no functions (the harness's compaction summary, a
    # title) cannot make a call, so no budget applies to it: its answer
    # is the text the model writes, never a stop text.
    messages, stop = step_budget(messages) if tools else (messages, "")
    messages, trimmed = trim_context(messages, max_tokens)
    if trimmed:
        _log(f"context: trimmed {trimmed} earlier tool results")
    if stop:
        _log(f"step budget: {stop[:80]}")
        text, finish, usage = stop, "stop", {}
    else:
        try:
            try:
                text, finish, usage = complete(messages, max_tokens=max_tokens,
                                               temperature=temperature, chat=chat, var=var)
            except _rl.RemoteLLMError as e:
                if not _overflow(e):
                    raise
                # The estimate undercounted this turn's text. Trim to a
                # smaller budget with a shorter untouched tail and try once
                # more; the harness retrying the same request would only
                # repeat the 400.
                messages, more = trim_context(messages, max_tokens, scale=OVERFLOW_CUT, keep_recent=2)
                _log(f"context: server overflow, trimmed {more} more results and retried")
                text, finish, usage = complete(messages, max_tokens=max_tokens,
                                               temperature=temperature, chat=chat, var=var)
        except _rl.ModelOutputError:
            text, finish, usage = "", "stop", {}
        except _rl.RemoteLLMError as e:
            return 502, {"type": "error", "error": {"type": "api_error", "message": str(e)[:300]}}, False
    names = {t["name"] for t in tools}
    kept, calls = parse_tool_calls(text, names)
    if not calls and tools and _residual_markup(kept) and not stop:
        # The model wrote a call the parser could not take (broken JSON,
        # a function not in the list). Its text is markup, not an answer:
        # it is asked once to write the call again, and whatever markup
        # survives that is dropped rather than posted.
        _log(f"malformed call: {' '.join(kept.split())[:160]}")
        try:
            text, finish, usage = complete(
                messages + [{"role": "assistant", "content": text}, {"role": "user", "content": REASK}],
                max_tokens=max_tokens, temperature=temperature, chat=chat, var=var)
            kept, calls = parse_tool_calls(text, names)
        except (_rl.RemoteLLMError, _rl.ModelOutputError) as e:
            _log(f"re-ask failed: {str(e)[:120]}")
    if not calls and _residual_markup(kept):
        kept = strip_markup(kept) or MALFORMED_STOP
    stop = loop_guard(messages, calls)
    if stop:
        _log(f"loop guard: {stop[:120]}")
        kept, calls = stop, []
    body = make(req, kept, calls, finish, usage)
    if req.get("stream"):
        return 200, events(body), True
    return 200, body, False


# ── the server ────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "ea-tool-gateway/1"
    protocol_version = "HTTP/1.1"
    token = ""
    model = ""
    chat = None

    def log_message(self, fmt, *args):  # quiet; one line per answer below
        pass

    def _send(self, status: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self) -> bool:
        auth = self.headers.get("Authorization") or ""
        key = self.headers.get("x-api-key") or ""
        given = auth[7:].strip() if auth.lower().startswith("bearer ") else key.strip()
        return bool(self.token) and secrets.compare_digest(given, self.token)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/health":
            self._send(200, json.dumps({"ok": True, "model": self.model}).encode())
            return
        if not self._authed():
            self._send(401, b'{"type":"error","error":{"type":"authentication_error","message":"bad token"}}')
            return
        if path in ("/v1/models", "/models"):
            row = {"id": self.model, "object": "model", "type": "model", "display_name": self.model,
                   "created_at": "2026-01-01T00:00:00Z", "owned_by": "on-prem"}
            self._send(200, json.dumps({"object": "list", "data": [row], "has_more": False}).encode())
            return
        self._send(404, b'{"type":"error","error":{"type":"not_found_error","message":"no such path"}}')

    def do_POST(self):
        path = self.path.split("?")[0]
        if not self._authed():
            self._send(401, b'{"type":"error","error":{"type":"authentication_error","message":"bad token"}}')
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > MAX_BODY:
            self._send(413, b'{"type":"error","error":{"type":"invalid_request_error","message":"body size"}}')
            return
        try:
            req = json.loads(self.rfile.read(n))
            if not isinstance(req, dict):
                raise ValueError("not an object")
        except ValueError as e:
            self._send(400, json.dumps({"type": "error", "error": {
                "type": "invalid_request_error", "message": f"bad JSON: {e}"[:200]}}).encode())
            return
        t0 = time.monotonic()
        status, body, streamed = answer(path, req, chat=self.chat, model=self.model)
        ms = int((time.monotonic() - t0) * 1000)
        if not streamed:
            self._send(status, json.dumps(body).encode())
            _log(f"{path} {status} {ms}ms")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        if path == "/v1/messages":
            for name, data in body:
                self.wfile.write(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode())
        else:
            for chunk in body:
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        calls = sum(1 for _n, d in body if d.get("type") == "content_block_start"
                    and d["content_block"]["type"] == "tool_use") if path == "/v1/messages" else "?"
        _log(f"{path} 200 stream {ms}ms tool_calls={calls}")
        self.close_connection = True


def make_server(port: int = PORT, *, chat=None, host: str = HOST) -> ThreadingHTTPServer:
    cfg = _rl.config()
    if not cfg.get("endpoint"):
        raise SystemExit("remote-llm.json names no endpoint; nothing to gate")
    handler = type("GatewayHandler", (Handler,), {"token": token(), "model": cfg["model"],
                                                  "chat": staticmethod(chat) if chat else None})
    srv = ThreadingHTTPServer((host, port), handler)
    srv.daemon_threads = True
    return srv


def serve(port: int = PORT) -> int:
    srv = make_server(port)
    _log(f"listening on http://{HOST}:{port} for {srv.RequestHandlerClass.model}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def status(port: int = PORT) -> dict:
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://{HOST}:{port}/health", timeout=3) as r:
            return dict(json.loads(r.read()), port=port, up=True)
    except Exception as e:
        return {"up": False, "port": port, "error": str(e)[:120]}


def lane_env(port: int = PORT, *, model: str = "", variant_name: str = "") -> dict:
    """The environment a Claude Code lane needs to run on the gateway:
    the base URL and token, the configured model under every model name
    the CLI resolves (with the variant's suffixes, `+think`), and the
    harness's side traffic off."""
    model = model or _rl.config().get("model") or ""
    if variant_name:
        model = model + "".join("+" + p for p in str(variant_name).split("+") if p)
    # The token under both names: the harness reads ANTHROPIC_AUTH_TOKEN
    # normally and only ANTHROPIC_API_KEY under --bare; the gateway takes
    # either header.
    return {"ANTHROPIC_BASE_URL": f"http://{HOST}:{port}", "ANTHROPIC_AUTH_TOKEN": token(),
            "ANTHROPIC_API_KEY": token(),
            "ANTHROPIC_MODEL": model, "ANTHROPIC_SMALL_FAST_MODEL": model,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": model, "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
            "ANTHROPIC_DEFAULT_OPUS_MODEL": model, "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_BUG_COMMAND": "1", "DISABLE_TELEMETRY": "1", "DISABLE_ERROR_REPORTING": "1",
            # The turn's model for the mail release log: the owner's own.
            "EA_TURN_MODEL": "local"}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the gateway (loopback only)")
    s.add_argument("--port", type=int, default=PORT)
    sub.add_parser("token", help="print the bearer token (made on first use)")
    s = sub.add_parser("status", help="is the gateway up")
    s.add_argument("--port", type=int, default=PORT)
    s = sub.add_parser("env", help="the environment a harness needs, one VAR=value per line")
    s.add_argument("--port", type=int, default=PORT)
    a = ap.parse_args(argv)
    if a.cmd == "serve":
        return serve(a.port)
    if a.cmd == "token":
        print(token())
        return 0
    if a.cmd == "status":
        print(json.dumps(status(a.port)))
        return 0
    if a.cmd == "env":
        for k, v in lane_env(a.port).items():
            print(f"{k}={v}")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

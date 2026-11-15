#!/usr/bin/env python3
"""Voice evaluation: can the user tell their own email from the writer's?

Held-out items are real emails the user wrote (from the sent index, never
mail the EA wrote). For each, the remote model writes the brief an
assistant would have been given (instruction back-translation), and the
writer drafts from that brief once per arm:

    baseline   the writer as it is: instructions, style profile, voice guide
    examples   the same plus the user's own emails closest to the brief
               (sent_index.examples), drawn only from mail sent before the
               item and outside its conversation
    examples_counts  the same, with what those emails do counted
               (sent_index.describe: exclamation marks, asides,
               contractions, length)
    examples_only    examples_counts, with the style profile's own recent
               emails left out (its numbers kept): the retrieved emails
               are the only samples of the voice. This is what the writer
               does with `voice_examples` on.

Two measures, the second the one that decides:
  - judge: the remote model sees four other real emails by the user and the
    real and drafted emails in both orders, and names the real one. Its
    accuracy per arm is a quick proxy: lower means a closer voice.
  - votes: the user, on a private page, sees one pair per item (each item
    in one arm only, so no real email is seen twice) and picks theirs.
    A pick rate near half means the writer passes.

    voice_eval.py build [--n 40] [--start 2023-01-01] [--end 2026-01-01]
    voice_eval.py generate [--arm baseline --arm examples]
    voice_eval.py judge [--arm ...]
    voice_eval.py serve [--port 3091]     the blind page, tailnet only (PAGE_ARMS)
    voice_eval.py summary                 counts and rates, never text

Every call goes to the remote model the writer uses, never a hosted one.
Store: <private_dir>/remote-llm/voice-eval/, owner-only. The output of
every verb is counts and rates: no email text is printed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import signal
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from statistics import median
from urllib.parse import parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parent))
import remote_llm  # noqa: E402

DIR = "voice-eval"
ITEMS = f"{DIR}/items.jsonl"
BODIES = f"{DIR}/bodies.jsonl"
JUDGED = f"{DIR}/judged.jsonl"
VOTES = f"{DIR}/votes.jsonl"
TOKEN = f"{DIR}/review-token"
ARMS = ("baseline", "examples", "examples_counts", "examples_only")
# The page's arms: the writer as it is against the best candidate.
PAGE_ARMS = ("baseline", "examples_only")
PORT = 3091
MIN_WORDS, MAX_WORDS = 30, 250
MAX_RECIPIENTS = 3
PER_RECIPIENT = 2
N_REFS = 4
PICKS = ("1", "2", "unsure")
_ITEM = re.compile(r"^/item/(\d+)/?$")
_CONTRACTION = re.compile(r"\b\w+'(?:t|ll|ve|re|m|d|s)\b", re.IGNORECASE)

KINDS = ["reply-yes", "reply-no", "scheduling", "request", "update", "pitch", "thanks",
         "question", "introduction", "other"]
BRIEF_SCHEMA = {"type": "object", "additionalProperties": False,
                "required": ["recipient_name", "say", "kind"],
                "properties": {"recipient_name": {"type": "string"}, "say": {"type": "string"},
                               "kind": {"type": "string", "enum": KINDS}}}
BRIEF_PROMPT = (
    "You write the brief an executive assistant hands to a ghostwriter who writes as the "
    "user. Given an email the user sent, write what the ghostwriter must convey: the "
    "facts, the asks, the decisions, and every name, date, time, number and link the "
    "email states, in plain neutral words. Call the sender \"the user\". Give the "
    "recipient's name as the email addresses them (\"\" when it does not). Do not copy "
    "phrases, tone, greeting, sign-off, asides or punctuation from the email: the "
    "ghostwriter supplies the voice. 25 to 90 words. Also name the kind of email.")
JUDGE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["pick"],
                "properties": {"pick": {"type": "string", "enum": ["A", "B"]}}}
JUDGE_PROMPT = (
    "Below are reference emails one person wrote, then two candidate emails that say the "
    "same thing. One candidate was written by that person. The other was written by an AI "
    "imitating them. Judge by voice alone: length, sentences, word choice, punctuation, "
    "asides, openings and closings. Answer with the letter of the one the person wrote.")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed(*parts) -> int:
    return int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:12], 16)


# ── build ─────────────────────────────────────────────────────────────

def candidates(con, start: str, end: str) -> list[dict]:
    rows = con.execute(
        "SELECT id, conversation_id, sent_at, recipients, subject, text, words FROM mail "
        "WHERE ea_written = 0 AND sent_at >= ? AND sent_at < ? AND words BETWEEN ? AND ?",
        (start, end, MIN_WORDS, MAX_WORDS)).fetchall()
    out = []
    for r in rows:
        rcpt = json.loads(r["recipients"] or "[]")
        if not rcpt or len(rcpt) > MAX_RECIPIENTS:
            continue
        low = r["text"].lower()
        if "unsubscribe" in low or "forwarded message" in low:
            continue
        out.append({**dict(r), "recipients": rcpt})
    return out


def back_translate(row: dict, *, chat=None) -> dict:
    chat = chat or remote_llm.chat
    res = chat([{"role": "system", "content": BRIEF_PROMPT},
                {"role": "user", "content": f"Recipient: {', '.join(row['recipients'])}\n"
                                            f"Subject: {row['subject']}\n\nEmail:\n{row['text']}"}],
               purpose="voice-eval-brief", schema=BRIEF_SCHEMA, schema_name="brief",
               max_tokens=400)
    return json.loads(res["content"])


def build(n: int, start: str, end: str, *, seed: int = 7, con=None, chat=None) -> dict:
    import sent_index
    con = con or sent_index.connect()
    have = {x["id"] for x in remote_llm.read(ITEMS)}
    pool = [c for c in candidates(con, start, end) if c["id"] not in have]
    random.Random(seed).shuffle(pool)
    per: dict[str, int] = {}
    for x in remote_llm.read(ITEMS):
        per[x["to"]] = per.get(x["to"], 0) + 1
    added = failed = 0
    for row in pool:
        if len(have) + added >= n:
            break
        to = row["recipients"][0]
        if per.get(to, 0) >= PER_RECIPIENT:
            continue
        try:
            b = back_translate(row, chat=chat)
        except Exception:
            failed += 1
            continue
        name = (b.get("recipient_name") or "").strip()
        brief = (f"Recipient: {', '.join(row['recipients'])}"
                 f"{f' ({name})' if name else ''}.\nSubject: {row['subject']}\n"
                 f"SAY: {b['say'].strip()}")
        remote_llm.append(ITEMS, {"id": row["id"], "to": to, "recipients": row["recipients"],
                                  "sent_at": row["sent_at"], "conversation_id": row["conversation_id"],
                                  "subject": row["subject"], "real": row["text"], "kind": b["kind"],
                                  "brief": brief, "ts": _now()})
        per[to] = per.get(to, 0) + 1
        added += 1
    return {"items": len(have) + added, "added": added, "failed": failed, "pool": len(pool)}


# ── generate ──────────────────────────────────────────────────────────

def draft(item: dict, arm: str, *, chat=None, con=None, embedder=None) -> tuple[str, dict]:
    """The writer's body for an item in one arm, as of the item's date."""
    import draft_ab
    import email_writer
    import sent_index
    ex_text, ex_ids = "", []
    profile = None
    if arm in ("examples", "examples_counts", "examples_only"):
        say, _ = email_writer.brief_parts(item["brief"])
        found = sent_index.examples(say, item["to"], conversation_id=item["conversation_id"],
                                    before=item["sent_at"], exclude_ids=(item["id"],),
                                    con=con, embedder=embedder)
        ex_text = sent_index.render(found, counts=arm != "examples")
        ex_ids = [x["id"] for x in found]
        if arm == "examples_only":
            profile = email_writer.numbers_only(draft_ab._style_profile(item["to"]))
    elif arm != "baseline":
        raise ValueError(f"no arm {arm!r}")
    msgs = email_writer.messages(item["brief"], item["to"], examples=ex_text, profile=profile)
    body, meta = draft_ab.remote_body(item["brief"], item["to"], chat=chat, messages=msgs,
                                      purpose="voice-eval")
    return body, {**meta, "examples": ex_ids}


def generate(arms: list[str], *, chat=None, con=None, embedder=None) -> dict:
    done = {(b["item"], b["arm"]) for b in remote_llm.read(BODIES) if b.get("body")}
    made = failed = 0
    for item in remote_llm.read(ITEMS):
        for arm in arms:
            if (item["id"], arm) in done:
                continue
            try:
                body, meta = draft(item, arm, chat=chat, con=con, embedder=embedder)
            except Exception as e:
                body, meta = "", {"reason": f"{type(e).__name__}: {str(e)[:160]}"}
            remote_llm.append(BODIES, {"item": item["id"], "arm": arm, "body": body,
                                       "examples": meta.get("examples", []),
                                       "reason": meta.get("reason", ""), "ts": _now()})
            made += bool(body)
            failed += not body
    return {"made": made, "failed": failed}


def bodies() -> dict[tuple[str, str], dict]:
    """The latest body per (item, arm)."""
    out = {}
    for b in remote_llm.read(BODIES):
        if b.get("body"):
            out[(b["item"], b["arm"])] = b
    return out


# ── judge ─────────────────────────────────────────────────────────────

def references(item: dict, con, k: int = N_REFS) -> list[str]:
    rows = con.execute(
        "SELECT id, text FROM mail WHERE ea_written = 0 AND sent_at < ? AND id != ? "
        "AND conversation_id != ? AND words BETWEEN ? AND ?",
        (item["sent_at"], item["id"], item["conversation_id"], MIN_WORDS, 200)).fetchall()
    rows = sorted(rows, key=lambda r: r["id"])
    random.Random(_seed("refs", item["id"])).shuffle(rows)
    return [r["text"] for r in rows[:k]]


def judge(arms: list[str], *, chat=None, con=None) -> dict:
    import sent_index
    chat = chat or remote_llm.chat
    con = con or sent_index.connect()
    items = {x["id"]: x for x in remote_llm.read(ITEMS)}
    done = {(j["item"], j["arm"], j["order"]) for j in remote_llm.read(JUDGED)}
    calls = 0
    for (iid, arm), b in bodies().items():
        if arm not in arms or iid not in items:
            continue
        item = items[iid]
        refs = "\n\n".join(f"--- Reference {i} ---\n{t}"
                           for i, t in enumerate(references(item, con), 1))
        for order in (0, 1):
            if (iid, arm, order) in done:
                continue
            a, bb = (item["real"], b["body"]) if order == 0 else (b["body"], item["real"])
            try:
                res = chat([{"role": "system", "content": JUDGE_PROMPT},
                            {"role": "user", "content": f"{refs}\n\n=== Candidate A ===\n{a}\n\n"
                                                        f"=== Candidate B ===\n{bb}"}],
                           purpose="voice-eval-judge", schema=JUDGE_SCHEMA,
                           schema_name="pick", max_tokens=20)
                pick = json.loads(res["content"])["pick"]
            except Exception:
                continue
            real = "A" if order == 0 else "B"
            remote_llm.append(JUDGED, {"item": iid, "arm": arm, "order": order, "pick": pick,
                                       "correct": pick == real, "ts": _now()})
            calls += 1
    return {"judged": calls}


# ── the blind page ────────────────────────────────────────────────────

def assignment(items: list[dict], have: dict, arms=PAGE_ARMS) -> list[tuple[dict, str]]:
    """Each item in one arm, balanced, in a fixed order: (item, arm). An
    item without a body in its arm is left out."""
    order = sorted(items, key=lambda x: _seed("order", x["id"]))
    out = []
    for i, item in enumerate(order):
        arm = arms[i % len(arms)]
        if (item["id"], arm) in have:
            out.append((item, arm))
    return out


def sides(item: dict, arm: str) -> str:
    """Which side the real email is on: '1' or '2'."""
    return "1" if _seed("side", item["id"], arm) % 2 == 0 else "2"


def votes() -> dict[str, dict]:
    out = {}
    for v in remote_llm.read(VOTES):
        out[v["item"]] = v
    return out


def record(item: dict, arm: str, form: dict) -> dict:
    pick = form.get("pick", "")
    if pick not in PICKS:
        raise ValueError("pick one of the options")
    rec = {"item": item["id"], "arm": arm, "pick": pick,
           "result": "unsure" if pick == "unsure" else
           ("real" if pick == sides(item, arm) else "generated"), "ts": _now()}
    remote_llm.append(VOTES, rec)
    return rec


def token() -> str:
    import secrets
    old = [x for x in remote_llm.read(TOKEN) if x.get("token")]
    if old:
        return old[-1]["token"]
    tok = secrets.token_urlsafe(18)
    remote_llm.append(TOKEN, {"token": tok, "ts": _now()})
    return tok


def _e(s) -> str:
    import html
    return html.escape(str(s or ""))


CSS = ("body{font:16px/1.5 system-ui,sans-serif;margin:0;background:#fafafa;color:#111}"
       "main{max-width:980px;margin:0 auto;padding:16px}"
       ".pair{display:grid;grid-template-columns:1fr 1fr;gap:12px}"
       "@media(max-width:700px){.pair{grid-template-columns:1fr}}"
       "pre{white-space:pre-wrap;background:#fff;border:1px solid #ddd;border-radius:8px;"
       "padding:12px;margin:0;font:inherit}button{font:inherit;padding:8px 14px;margin:4px}"
       ".meta{color:#555;font-size:14px}a{color:#0645ad}"
       "@media(prefers-color-scheme:dark){body{background:#111;color:#eee}"
       "pre{background:#1b1b1b;border-color:#333}.meta{color:#aaa}a{color:#8ab4f8}}")


def _page(title: str, body: str) -> bytes:
    return ("<!doctype html><html lang=en><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            "<meta name=robots content=noindex><meta name=referrer content=no-referrer>"
            f"<title>{_e(title)}</title><style>{CSS}</style></head><body><main>{body}"
            "</main></body></html>").encode()


def index_page(base: str, pairs: list, vts: dict) -> bytes:
    done = sum(1 for item, _ in pairs if item["id"] in vts)
    nxt = next((i for i, (item, _) in enumerate(pairs, 1) if item["id"] not in vts), None)
    rows = "".join(f"<li><a href='{base}/item/{i}'>Item {i}</a>"
                   f"{' (voted)' if item['id'] in vts else ''}</li>"
                   for i, (item, _) in enumerate(pairs, 1))
    start = f"<p><a href='{base}/item/{nxt}'>Next unvoted item</a></p>" if nxt else ""
    return _page("Which one is yours?",
                 f"<h1>Which one is yours?</h1><p>Each item shows an email you sent and one "
                 f"the writer drafted from the same brief. Pick yours. {done} of "
                 f"{len(pairs)} voted.</p>{start}<ol>{rows}</ol>")


def item_page(base: str, pairs: list, n: int, vts: dict) -> bytes:
    item, arm = pairs[n - 1]
    gen = bodies()[(item["id"], arm)]["body"]
    real_side = sides(item, arm)
    one, two = (item["real"], gen) if real_side == "1" else (gen, item["real"])
    v = vts.get(item["id"])
    head = (f"<p><a href='{base}/'>All items</a> · Item {n} of {len(pairs)}</p>"
            f"<p class=meta>To {_e(', '.join(item['recipients']))} · {_e(item['sent_at'][:10])}"
            f" · {_e(item['subject'])}</p>")
    pair = (f"<div class=pair><div><h2>Email 1</h2><pre>{_e(one)}</pre></div>"
            f"<div><h2>Email 2</h2><pre>{_e(two)}</pre></div></div>")
    if v:
        said = {"real": "You picked your own email.", "generated": "You picked the writer's draft.",
                "unsure": "You could not tell."}[v["result"]]
        foot = (f"<p><b>{said}</b> Yours was Email {real_side}. Arm: {_e(arm)}.</p>"
                f"<details><summary>The brief</summary><pre>{_e(item['brief'])}</pre></details>")
        if n < len(pairs):
            foot += f"<p><a href='{base}/item/{n + 1}'>Next item</a></p>"
    else:
        foot = (f"<form method=post><button name=pick value=1>Email 1 is mine</button>"
                f"<button name=pick value=2>Email 2 is mine</button>"
                f"<button name=pick value=unsure>Can't tell</button></form>")
    return _page(f"Item {n}", head + pair + foot)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, location: str = "") -> None:
        self.send_response(code)
        if location:
            self.send_header("Location", location)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Robots-Tag", "noindex")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _route(self) -> str | None:
        login = self.headers.get("Tailscale-User-Login", "")
        if not login or login != self.server.owner_login:
            self._send(403, b"forbidden")
            return None
        base, path = self.server.base, self.path.split("?", 1)[0]
        if path != base and not path.startswith(base + "/"):
            self._send(404, b"not found")
            return None
        return path[len(base):] or "/"

    def do_GET(self) -> None:
        rest = self._route()
        if rest is None:
            return
        pairs = self.server.pairs
        if rest == "/":
            return self._send(200, index_page(self.server.base, pairs, votes()))
        m = _ITEM.match(rest)
        if m and 1 <= int(m.group(1)) <= len(pairs):
            return self._send(200, item_page(self.server.base, pairs, int(m.group(1)), votes()))
        self._send(404, b"not found")

    def do_POST(self) -> None:
        rest = self._route()
        if rest is None:
            return
        m = _ITEM.match(rest)
        pairs = self.server.pairs
        if not m or not 1 <= int(m.group(1)) <= len(pairs):
            return self._send(404, b"not found")
        n = int(m.group(1))
        size = min(int(self.headers.get("Content-Length") or 0), 2000)
        form = {k: v[0] for k, v in parse_qs(self.rfile.read(size).decode("utf-8", "replace")).items()}
        item, arm = pairs[n - 1]
        if item["id"] not in votes():
            try:
                record(item, arm, form)
            except ValueError:
                return self._send(400, b"pick one of the options")
        self._send(303, b"", location=f"{self.server.base}/item/{n}")

    def log_message(self, fmt, *args) -> None:
        sys.stderr.write(f"{self.log_date_time_string()} {self.command}\n")


def make_server(port: int, *, pairs: list, tok: str, owner_login: str) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.pairs, srv.owner_login = pairs, owner_login
    srv.base = f"/voice-eval-{tok}"
    return srv


def serve(port: int) -> None:
    import draft_review
    pairs = assignment(remote_llm.read(ITEMS), bodies())
    srv = make_server(port, pairs=pairs, tok=token(), owner_login=draft_review._owner_login())
    url = draft_review.route_on(srv.base, port)
    print(json.dumps({"url": url, "items": len(pairs)}), flush=True)

    def _stop(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _stop)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        draft_review.route_off(srv.base)
        srv.server_close()


# ── summary ───────────────────────────────────────────────────────────

def _style(text: str) -> dict:
    return {"words": len(text.split()), "contraction": bool(_CONTRACTION.search(text)),
            "exclaim": "!" in text, "paren": "(" in text}


def summary() -> dict:
    items = {x["id"]: x for x in remote_llm.read(ITEMS)}
    have = bodies()
    out: dict = {"items": len(items), "arms": {}}
    judged: dict = {}
    for j in remote_llm.read(JUDGED):
        judged[(j["item"], j["arm"], j["order"])] = j["correct"]
    vts = votes()
    real_style = [_style(x["real"]) for x in items.values()]
    out["real"] = {"median_words": median([s["words"] for s in real_style]) if real_style else 0}
    if real_style:
        out["real"].update({k: round(sum(s[k] for s in real_style) / len(real_style), 2)
                            for k in ("contraction", "exclaim", "paren")})
    for arm in ARMS:
        # Paired with the baseline: the items both drafted.
        paired = {i for i in items if (i, arm) in have and (i, "baseline") in have}
        bs = [(items[i], b) for (i, a), b in have.items() if a == arm and i in items]
        if not bs:
            continue
        gen = [_style(b["body"]) for _, b in bs]
        ratio = [len(b["body"].split()) / max(1, len(it["real"].split())) for it, b in bs]
        js = [c for (i, a, _), c in judged.items() if a == arm]
        jp = [c for (i, a, _), c in judged.items() if a == arm and i in paired]
        jb = [c for (i, a, _), c in judged.items() if a == "baseline" and i in paired]
        vs = [v for v in vts.values() if v["arm"] == arm]
        out["arms"][arm] = {
            "bodies": len(bs),
            "median_words": median([g["words"] for g in gen]),
            "median_length_ratio": round(median(ratio), 2),
            **{k: round(sum(g[k] for g in gen) / len(gen), 2)
               for k in ("contraction", "exclaim", "paren")},
            "judge_calls": len(js),
            "judge_found_real": round(sum(js) / len(js), 2) if js else None,
            "paired_items": len(paired),
            "judge_found_real_paired": round(sum(jp) / len(jp), 2) if jp else None,
            "baseline_on_same_items": round(sum(jb) / len(jb), 2) if jb else None,
            "needs": sum(1 for b in remote_llm.read(BODIES)
                         if b["arm"] == arm and not b.get("body")
                         and "NEED" in (b.get("reason") or "")),
            "votes": len(vs),
            "you_found_yours": round(sum(v["result"] == "real" for v in vs) / len(vs), 2)
            if vs else None,
            "unsure": sum(v["result"] == "unsure" for v in vs),
        }
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Voice evaluation", allow_abbrev=False)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("build", allow_abbrev=False)
    p.add_argument("--n", type=int, default=40)
    p.add_argument("--start", default="2023-01-01")
    p.add_argument("--end", default="2026-01-01")
    p.add_argument("--seed", type=int, default=7)
    for name in ("generate", "judge"):
        p = sub.add_parser(name, allow_abbrev=False)
        p.add_argument("--arm", action="append", choices=ARMS)
    p = sub.add_parser("serve", allow_abbrev=False)
    p.add_argument("--port", type=int, default=PORT)
    sub.add_parser("summary", allow_abbrev=False)
    a = ap.parse_args(argv)
    if a.cmd == "build":
        print(json.dumps(build(a.n, a.start, a.end, seed=a.seed)))
    elif a.cmd == "generate":
        print(json.dumps(generate(a.arm or list(ARMS))))
    elif a.cmd == "judge":
        print(json.dumps(judge(a.arm or list(ARMS))))
    elif a.cmd == "serve":
        serve(a.port)
    else:
        print(json.dumps(summary(), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())

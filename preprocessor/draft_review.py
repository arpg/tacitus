#!/usr/bin/env python3
"""Drafting review (experimental): the user's own blind vote on the
drafting backtest pairs (docs/remote-llm.md, "Drafting backtest").

Each item shows the task the email-author subagent got and the two
bodies, Claude's and the remote model's, as Draft 1 and Draft 2 in an
order drawn per item. Notes a writer left for its caller are cut from
both, as for the judge. The user picks the one he would rather send and
may mark how much each needs before sending. After the vote the page
shows who wrote which, what he sent, and the judge's verdicts. Pairs on
the kept list are included: the page goes to the user's browser only.

The server listens on 127.0.0.1, under a path that holds a random token,
and publishes that path with one `tailscale serve` route (tailnet only;
it will not start while a Funnel is on). A request must carry the
Tailscale-User-Login of the node's owner, which tailscale serve sets for
a tailnet user's device. The route is removed when the server stops.

    draft_review.py serve [--port 3090]   serve until stopped
    draft_review.py summary               the vote tally, as JSON
"""
from __future__ import annotations

import argparse
import html
import json
import random
import re
import secrets
import signal
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

sys.path.insert(0, str(Path(__file__).parent))
import remote_llm  # noqa: E402
import draft_backtest as bt  # noqa: E402

VOTES = f"{bt.DIR}/votes.jsonl"
TOKEN = f"{bt.DIR}/review-token"
PORT = 3090
PICKS = ("1", "2", "same", "neither")
SEND = {"as_is": "Send as is", "light": "Light edits", "rewrite": "Rewrite"}
_ITEM = re.compile(r"^/item/(\d+)/?$")


# ── data ──────────────────────────────────────────────────────────────

def load_items() -> list[dict]:
    """Every backtest pair with both bodies, oldest first."""
    import draft_ab
    remote = {r["draft_id"]: r for r in remote_llm.read(bt.REMOTE)
              if r.get("qwen_body") and not r.get("need")}
    judged: dict[str, list] = {}
    for r in remote_llm.read(bt.JUDGED):
        judged.setdefault(r["draft_id"], []).append(r)
    out = []
    for x in sorted(remote_llm.read(bt.ITEMS), key=lambda x: x.get("agent_ts") or ""):
        d = x["draft_id"]
        if d not in remote or not x.get("claude_body") or x.get("suspect_pair"):
            continue
        out.append({"draft_id": d, "ts": (x.get("agent_ts") or "")[:16].replace("T", " "),
                    "recipient": x.get("recipient") or "", "task": x.get("prompt") or "",
                    "claude": draft_ab.split_notes(x["claude_body"])[0],
                    "remote": draft_ab.split_notes(remote[d]["qwen_body"])[0],
                    "sent": x.get("sent_body") or "",
                    "kept": bool(x.get("keep_from_frontier") or x.get("keep_from_anthropic")),
                    "judged": judged.get(d, [])})
    return out


def token() -> str:
    """The path token, made once and kept so the link survives a restart."""
    path = remote_llm.store_dir() / TOKEN
    try:
        tok = path.read_text().strip()
        if re.fullmatch(r"[a-z0-9]{16,}", tok):
            return tok
    except FileNotFoundError:
        pass
    tok = "".join(secrets.choice("abcdefghijkmnpqrstuvwxyz23456789") for _ in range(20))
    remote_llm._owner_trust.write_owner_file(path, tok.encode(), mode=0o600)
    return tok


def claude_side(tok: str, draft_id: str) -> str:
    """'1' or '2': where Claude's body sits for this item. Stable per token."""
    return "1" if random.Random(f"{tok}:{draft_id}").random() < 0.5 else "2"


def votes() -> dict[str, dict]:
    """The vote per item; a later vote on the same item replaces it."""
    return {r["draft_id"]: r for r in remote_llm.read(VOTES) if r.get("draft_id")}


def record(item: dict, tok: str, form: dict) -> dict:
    pick = form.get("pick")
    if pick not in PICKS:
        raise ValueError("no pick")
    c = claude_side(tok, item["draft_id"])
    r = "2" if c == "1" else "1"
    writer = pick if pick in ("same", "neither") else "claude" if pick == c else "remote"
    send = {n: form.get(f"send{n}") if form.get(f"send{n}") in SEND else None for n in ("1", "2")}
    rec = {"draft_id": item["draft_id"], "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "pick": writer, "claude_send": send[c], "remote_send": send[r], "claude_was": c,
           "note": (form.get("note") or "").strip()[:2000], "kept": item["kept"]}
    remote_llm.append(VOTES, rec)
    return rec


def summary() -> dict:
    v = list(votes().values())
    n = {k: sum(1 for r in v if r.get("pick") == k) for k in ("claude", "remote", "same", "neither")}
    decided = n["claude"] + n["remote"]
    return {"votes": len(v), **n,
            "remote_win_share_of_decided": round(n["remote"] / decided, 3) if decided else None,
            "ci_95": bt._ci(n["remote"], decided),
            "sendable": {w: {s: sum(1 for r in v if r.get(f"{w}_send") == s) for s in SEND}
                         for w in ("claude", "remote")}}


# ── pages ─────────────────────────────────────────────────────────────

CSS = """
:root{--bg:#f7f7f5;--card:#fff;--ink:#1d1d1b;--mute:#6b6b66;--line:#deded8;--accent:#2f5d8a;
--claude:#b4572e;--remote:#2e7d5b;--pick:#e8eef5}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#161615;--card:#20201e;
--ink:#ecece8;--mute:#9a9a93;--line:#3a3a36;--accent:#8db4dc;--claude:#e08a62;--remote:#6cc49c;--pick:#26313d}}
:root[data-theme="dark"]{--bg:#161615;--card:#20201e;--ink:#ecece8;--mute:#9a9a93;--line:#3a3a36;
--accent:#8db4dc;--claude:#e08a62;--remote:#6cc49c;--pick:#26313d}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:1100px;margin:0 auto;padding:16px}a{color:var(--accent)}
h1{font-size:20px;margin:4px 0 2px}.mute{color:var(--mute);font-size:13px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 14px;margin:12px 0}
.task{max-height:14em;overflow:auto;white-space:pre-wrap;font-size:13px}
.pair{display:grid;grid-template-columns:1fr;gap:12px}@media(min-width:860px){.pair{grid-template-columns:1fr 1fr}}
pre{white-space:pre-wrap;word-wrap:break-word;font:14px/1.5 ui-serif,Georgia,serif;margin:8px 0}
.lab{font-weight:600}.claude{color:var(--claude)}.remote{color:var(--remote)}
.opts{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0}
.opts label{border:1px solid var(--line);border-radius:6px;padding:8px 12px;cursor:pointer;background:var(--card)}
.opts input{margin-right:6px}.opts label:has(input:checked){background:var(--pick);border-color:var(--accent)}
.send{font-size:13px;gap:6px}.send label{padding:4px 8px}
textarea{width:100%;min-height:3em;background:var(--card);color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:6px}
button{background:var(--accent);color:var(--bg);border:0;border-radius:6px;padding:10px 18px;font-size:15px;cursor:pointer}
.nav{display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap}
table{border-collapse:collapse;width:100%;font-size:14px}td,th{border-bottom:1px solid var(--line);padding:6px 4px;text-align:left}
.won{outline:2px solid var(--accent)}
"""


def _e(s) -> str:
    return html.escape(str(s or ""))


def _page(title: str, body: str) -> bytes:
    return ("<!doctype html><html lang=en><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            "<meta name=robots content=noindex><meta name=referrer content=no-referrer>"
            f"<title>{_e(title)}</title><style>{CSS}</style></head><body><main>{body}</main></body></html>"
            ).encode()


def _tally(s: dict) -> str:
    if not s["votes"]:
        return "<p class=mute>No votes yet.</p>"
    lo, hi = s["ci_95"]
    share = (f"Remote wins {round(100 * s['remote_win_share_of_decided'])}% of decided votes"
             f" (95% interval {round(100 * lo)}-{round(100 * hi)}%)." if s["remote_win_share_of_decided"]
             is not None else "")
    rows = "".join(f"<tr><td class={w}>{'Claude' if w == 'claude' else 'Remote (qwen)'}</td>"
                   + "".join(f"<td>{s['sendable'][w][k]}</td>" for k in SEND) + "</tr>"
                   for w in ("claude", "remote"))
    return (f"<p>{s['votes']} votes: <span class=claude>Claude {s['claude']}</span>, "
            f"<span class=remote>remote {s['remote']}</span>, same {s['same']}, neither {s['neither']}. "
            f"{share}</p><table><tr><th></th>" + "".join(f"<th>{v}</th>" for v in SEND.values())
            + f"</tr>{rows}</table>")


def index_page(base: str, items: list[dict], vts: dict) -> bytes:
    nxt = next((i for i, x in enumerate(items, 1) if x["draft_id"] not in vts), None)
    rows = []
    for i, x in enumerate(items, 1):
        v = vts.get(x["draft_id"])
        got = ("" if not v else {"claude": "<span class=claude>Claude</span>",
                                 "remote": "<span class=remote>Remote</span>"}.get(v["pick"], _e(v["pick"])))
        rows.append(f"<tr><td><a href='{base}/item/{i}'>{i}</a></td><td>{_e(x['ts'])}</td>"
                    f"<td>{_e(x['recipient'])}</td><td>{got}</td></tr>")
    start = (f"<p><a href='{base}/item/{nxt}'><button>{'Start' if len(vts) == 0 else 'Continue'}"
             f" with item {nxt}</button></a></p>" if nxt else "<p>All items have a vote.</p>")
    body = (f"<h1>Drafting review</h1><p class=mute>{len(items)} pairs from the drafting backtest. "
            "Each shows the task and two bodies in a random order. Pick the one you would rather "
            "send. Who wrote which shows after your vote. Twelve votes give a first read.</p>"
            f"{start}<div class=card>{_tally(summary())}</div>"
            f"<div class=card><table><tr><th>#</th><th>Written</th><th>To</th><th>Your pick</th></tr>"
            f"{''.join(rows)}</table></div>")
    return _page("Drafting review", body)


def _judge_lines(x: dict) -> str:
    out = []
    for r in x["judged"]:
        if r.get("kind") == "quality":
            w = bt._writer(r.get("better"), r.get("order"))
            out.append(f"Quality ({_e(r.get('order', '').replace('_', ' '))}): "
                       f"<span class={w}>{_e(w)}</span>. {_e(r.get('why'))}")
        elif r.get("kind") == "closer":
            w = bt._writer(r.get("closer"), r.get("order"))
            out.append(f"Closer to what you sent: <span class={w}>{_e(w)}</span>.")
    if not out:
        return "<p class=mute>No judge verdict (kept from hosted models).</p>" if x["kept"] else ""
    return "<p class=mute>Sonnet judge</p><ul>" + "".join(f"<li>{s}</li>" for s in out) + "</ul>"


def item_page(base: str, items: list[dict], n: int, tok: str, vts: dict) -> bytes:
    import draft_ab
    x = items[n - 1]
    c = claude_side(tok, x["draft_id"])
    bodies = {c: ("claude", x["claude"]), ("2" if c == "1" else "1"): ("remote", x["remote"])}
    v = vts.get(x["draft_id"])
    head = (f"<div class=nav><a href='{base}/'>All items</a><span class=mute>{n} of {len(items)}"
            f"{' · kept from hosted models' if x['kept'] else ''}</span></div>"
            f"<h1>To {_e(x['recipient'])}</h1><p class=mute>Written {_e(x['ts'])} UTC</p>"
            f"<div class=card><div class=lab>Task the writer got</div><div class=task>{_e(x['task'])}</div></div>")
    cards = []
    for k in ("1", "2"):
        w, text = bodies[k]
        if v:
            who = "Claude" if w == "claude" else "Remote (qwen)"
            ratio = (f" · edit ratio to sent {draft_ab.edit_ratio(text, x['sent']):.2f}" if x["sent"] else "")
            mark = SEND.get(v.get(f"{w}_send") or "", "")
            lab = (f"<div class='lab {w}'>Draft {k}: {who}</div><div class=mute>{len(text.split())} words"
                   f"{ratio}{' · you: ' + _e(mark) if mark else ''}</div>")
            won = " won" if v["pick"] == w else ""
            cards.append(f"<div class='card{won}'>{lab}<pre>{_e(text)}</pre></div>")
        else:
            opts = "".join(f"<label><input type=radio name=send{k} value={s}>{_e(t)}</label>"
                           for s, t in SEND.items())
            cards.append(f"<div class=card><div class=lab>Draft {k}</div><div class=mute>"
                         f"{len(text.split())} words</div><pre>{_e(text)}</pre>"
                         f"<div class='opts send'>{opts}</div></div>")
    pair = f"<div class=pair>{''.join(cards)}</div>"
    nxt = f"<a href='{base}/item/{n + 1}'>Next item</a>" if n < len(items) else f"<a href='{base}/'>Done: back to the list</a>"
    if v:
        pick = {"same": "About the same", "neither": "Neither is sendable"}.get(v["pick"])
        pick = pick or ("You picked " + ("Claude" if v["pick"] == "claude" else "the remote model"))
        sent = (f"<div class=card><div class=lab>What you sent</div><pre>{_e(x['sent'])}</pre></div>"
                if x["sent"] else "<div class='card mute'>No sent email on record.</div>")
        note = f"<p class=mute>Your note: {_e(v['note'])}</p>" if v.get("note") else ""
        body = (head + f"<div class=card><b>{_e(pick)}.</b>{note}</div>" + pair + sent
                + f"<div class=card>{_judge_lines(x)}</div><div class=nav><span></span>{nxt}</div>")
    else:
        picks = "".join(f"<label><input type=radio name=pick value={p} required>{t}</label>"
                        for p, t in (("1", "Draft 1"), ("2", "Draft 2"), ("same", "About the same"),
                                     ("neither", "Neither is sendable")))
        body = (head + f"<form method=post action='{base}/item/{n}'>" + pair
                + f"<div class=card><div class=lab>Which would you rather send?</div><div class=opts>{picks}</div>"
                "<textarea name=note placeholder='Optional note'></textarea>"
                "<p><button type=submit>Record vote</button></p></div></form>"
                f"<div class=nav><span></span><a href='{base}/item/{n + 1 if n < len(items) else 1}'>Skip</a></div>")
    return _page(f"Drafting review {n}", body)


# ── server ────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "draft-review"

    def _send(self, code: int, body: bytes = b"", location: str = "") -> None:
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
        """The path below the base, or None when the request is not ours."""
        login = self.headers.get("Tailscale-User-Login", "")
        if not login or login != self.server.owner_login:
            self._send(403, b"forbidden")
            return None
        base = self.server.base
        path = self.path.split("?", 1)[0]
        if path != base and not path.startswith(base + "/"):
            self._send(404, b"not found")
            return None
        return path[len(base):] or "/"

    def do_GET(self) -> None:
        rest = self._route()
        if rest is None:
            return
        s, items = self.server, self.server.items
        if rest == "/":
            return self._send(200, index_page(s.base, items, votes()))
        m = _ITEM.match(rest)
        if m and 1 <= int(m.group(1)) <= len(items):
            return self._send(200, item_page(s.base, items, int(m.group(1)), s.token, votes()))
        self._send(404, b"not found")

    def do_POST(self) -> None:
        rest = self._route()
        if rest is None:
            return
        m = _ITEM.match(rest)
        items = self.server.items
        if not m or not 1 <= int(m.group(1)) <= len(items):
            return self._send(404, b"not found")
        n = int(m.group(1))
        size = min(int(self.headers.get("Content-Length") or 0), 20000)
        form = {k: v[0] for k, v in parse_qs(self.rfile.read(size).decode("utf-8", "replace")).items()}
        try:
            record(items[n - 1], self.server.token, form)
        except ValueError:
            return self._send(400, b"pick one of the options")
        self._send(303, b"", location=f"{self.server.base}/item/{n}")

    def log_message(self, fmt, *args) -> None:
        sys.stderr.write(f"{self.log_date_time_string()} {self.command} {args[1] if len(args) > 1 else ''}\n")


def make_server(port: int, *, items: list[dict], tok: str, owner_login: str) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.items, srv.token, srv.owner_login = items, tok, owner_login
    srv.base = f"/draft-review-{tok}"
    return srv


def _owner_login() -> str:
    import deck_serve as ds
    st = json.loads(ds._run(["status", "--json"]).stdout)
    return st["User"][str(st["Self"]["UserID"])]["LoginName"]


def route_on(base: str, port: int) -> str:
    import deck_serve as ds
    if ds.funnel_active(ds.serve_config()):
        raise SystemExit("a Funnel is on for this node; not serving")
    r = ds._run(["serve", "--bg", f"--https={ds.HTTPS_PORT}", f"--set-path={base}",
                 f"http://127.0.0.1:{port}{base}"])
    if r.returncode != 0:
        raise SystemExit(f"tailscale serve failed: {r.stderr.strip()[:300]}")
    cfg = ds.serve_config()
    if ds.funnel_active(cfg) or ds.routes(cfg).get(base) != port:
        route_off(base)
        raise SystemExit("the route is not tailnet-only as set; removed it")
    return f"https://{ds.dns_name()}{base}/"


def route_off(base: str) -> None:
    import deck_serve as ds
    ds._run(["serve", f"--https={ds.HTTPS_PORT}", f"--set-path={base}", "off"])


def serve(port: int) -> None:
    tok = token()
    srv = make_server(port, items=load_items(), tok=tok, owner_login=_owner_login())
    url = route_on(srv.base, port)
    print(json.dumps({"url": url, "items": len(srv.items)}), flush=True)

    def _stop(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _stop)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        route_off(srv.base)
        srv.server_close()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sv = sub.add_parser("serve")
    sv.add_argument("--port", type=int, default=PORT)
    sub.add_parser("summary")
    a = p.parse_args(argv)
    if a.cmd == "serve":
        serve(a.port)
    else:
        print(json.dumps(summary(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

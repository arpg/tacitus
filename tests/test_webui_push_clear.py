"""The owner's reply clears the conversation's notifications everywhere
(docs/webchat.md, Notifications (Web Push)): the service worker closes the
notifications a clear push names, and shows a quiet "Handled." in their
place only where the browser would count a push that showed nothing
(`clearPlan`, per push service); the page closes the notifications of the
thread it shows and tells the bot, which then owes that browser no clear;
and the app drops a thread's unread count when the owner's message arrives
on the stream, from this browser or mirrored from elsewhere, also with the
thread closed. The rest of the app's tests are in tests/test_webui.py,
whose browser fixtures this file borrows; the server's side is in
tests/test_web_push_policy.py."""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))

from test_webui import WEBUI, app, browser, stub_factory  # noqa: E402,F401  (the fixtures)

# sw.js in a node `vm` context, once per scenario: a `self` whose registration
# holds the notifications open (`held`, by tag) and records what the worker
# shows and closes, whose subscription is on `endpoint`, whose windows have the
# visibility states in `windows`, and whose timers wait for `runTimers`.
SW_HARNESS = r"""
const vm = require('vm');
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
function load({ endpoint = '', held = [], windows = [], noSubscription = false } = {}) {
  const out = { shown: [], closed: [], posted: [], timers: [] };
  let open = [];
  const note = (tag, data) => {
    const n = { tag, data: data || {}, close: () => { out.closed.push(tag); open = open.filter((x) => x !== n); } };
    return n;
  };
  open = held.map((tag) => note(tag));
  const handlers = {};
  const self = {
    addEventListener: (name, fn) => { handlers[name] = fn; },
    registration: {
      scope: 'https://host.example.test/chat/',
      showNotification: (title, opts) => {
        out.shown.push({ title, body: opts.body, tag: opts.tag || null, silent: Boolean(opts.silent),
          handled: Boolean(opts.data && opts.data.handled) });
        open = open.filter((x) => !opts.tag || x.tag !== opts.tag);   // a tag replaces its notification
        open.push(note(opts.tag, opts.data));
        return Promise.resolve();
      },
      getNotifications: (filter) => Promise.resolve(open.filter((n) => !filter || !filter.tag || n.tag === filter.tag)),
      pushManager: { getSubscription: () => (noSubscription ? Promise.reject(new Error('gone'))
        : Promise.resolve(endpoint ? { endpoint } : null)) },
    },
    clients: { matchAll: () => Promise.resolve(windows.map((v) => ({ visibilityState: v,
      postMessage: (m) => out.posted.push(m) }))) },
  };
  const ctx = vm.createContext({ self, importScripts: () => {}, URL, console,
    setTimeout: (fn, ms) => out.timers.push([fn, ms]) });
  vm.runInContext(src, ctx);
  const fire = (data) => {
    const waits = [];
    handlers.push({ data: { json: () => data }, waitUntil: (p) => waits.push(p) });
    return Promise.all(waits);
  };
  const runTimers = async () => {
    for (const [fn] of out.timers.splice(0)) fn();
    await new Promise((r) => setTimeout(r, 0));
  };
  return { ctx, fire, out, runTimers, open: () => open.map((n) => n.tag) };
}
const FCM = 'https://fcm.googleapis.com/fcm/send/made-up';
const MOZ = 'https://updates.push.services.mozilla.com/wpush/v2/made-up';
const APPLE = 'https://web.push.apple.com/made-up';
const CLEAR = { type: 'clear', tag: 'main/t1', title: 'Tacitus replied' };
const snap = (o) => JSON.parse(JSON.stringify(o)); // a copy the later steps leave as it is
(async () => {
  const got = {};
  const { ctx } = load();
  got.plan = {};
  for (const service of ['apple', 'mozilla', 'chromium', '']) {
    for (const left of [0, 1]) {
      for (const inView of [false, true]) got.plan[`${service}|${left}|${inView}`] = ctx.clearPlan({ service, left, inView });
    }
  }
  got.service = [FCM, 'https://jmt17.google.com/fcm/send/made-up', 'https://wns2-par02p.notify.windows.com/w/?token=made-up',
    MOZ, APPLE, 'https://push.example.test/made-up', 'not a url', ''].map((e) => ctx.pushService(e));

  // Two of the thread's notifications and another thread's: the clear closes the two, and one stays open.
  let w = load({ endpoint: FCM, held: ['main/t1', 'main/t1', 'dm/t2'] });
  await w.fire(CLEAR);
  await w.fire({ type: 'clear' });
  await w.fire({ title: 'Tacitus replied', body: 'Two meetings.', tag: 'main/t3' });
  got.left_open = { ...snap(w.out), open: w.open() };

  // Chrome with the thread's only notification and no window in view: a quiet "Handled.", closed later.
  w = load({ endpoint: FCM, held: ['main/t1'] });
  await w.fire(CLEAR);
  got.chromium_alone = { ...snap(w.out), timers: w.out.timers.map(([, ms]) => ms), open: w.open() };
  await w.runTimers();
  got.chromium_alone.after = w.open();

  // A real push under the tag before the timer: the timer closes only the "Handled.".
  w = load({ endpoint: FCM, held: ['main/t1'] });
  await w.fire(CLEAR);
  await w.fire({ title: 'Tacitus replied', body: 'Moved.', tag: 'main/t1' });
  await w.runTimers();
  got.real_push_stays = w.open();

  // Chrome with a window of the app in view needs nothing; Firefox does.
  w = load({ endpoint: FCM, windows: ['visible'] });
  await w.fire(CLEAR);
  got.chromium_in_view = w.out.shown;
  w = load({ endpoint: MOZ, windows: ['visible'] });
  await w.fire(CLEAR);
  got.mozilla_in_view = w.out.shown;
  w = load({ endpoint: MOZ, held: ['dm/t2'] });
  await w.fire(CLEAR);
  got.mozilla_other_open = w.out.shown;

  // Safari counts only a notification shown: always the replacement, also with others open.
  w = load({ endpoint: APPLE, held: ['main/t1', 'dm/t2'], windows: ['visible'] });
  await w.fire(CLEAR);
  got.apple = { shown: w.out.shown, closed: w.out.closed };

  // No subscription to read: the rule that shows the replacement.
  w = load({ noSubscription: true, windows: ['visible'] });
  await w.fire(CLEAR);
  got.unknown = w.out.shown;
  process.stdout.write(JSON.stringify(got));
})();
"""


def run_worker(tmp_path) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("no node to run the service worker")
    harness = tmp_path / "sw_harness.js"
    harness.write_text(SW_HARNESS)
    out = subprocess.run([node, str(harness), str(WEBUI / "sw.js")], capture_output=True,
                         text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


HANDLED = {"title": "Tacitus replied", "body": "Handled.", "tag": "main/t1", "silent": True,
           "handled": True}


def test_a_clear_push_closes_the_threads_notifications_and_shows_none_beside_others(tmp_path):
    got = run_worker(tmp_path)["left_open"]
    assert got["closed"] == ["main/t1", "main/t1"]        # only that thread's, all of them
    assert got["open"] == ["dm/t2", "main/t3"]            # a clear with no tag closes none
    assert [s["title"] for s in got["shown"]] == ["Tacitus replied"]   # the clears show nothing
    assert got["posted"] == [] and got["timers"] == []
    assert got["shown"][0]["tag"] == "main/t3" and not got["shown"][0]["silent"]


def test_the_clear_decision_per_browser(tmp_path):
    """clearPlan: no browser counts a closed notification as one shown.
    WebKit counts only showNotification, so Safari always gets the
    replacement; Chrome and Edge need an open notification of the app or a
    window of it in view; Firefox needs an open notification three seconds
    on, and a page in view does not count. An unknown service takes the
    strict rule."""
    got = run_worker(tmp_path)
    plan = got["plan"]
    for left in (0, 1):
        for view in ("false", "true"):
            assert plan[f"apple|{left}|{view}"] == "replace"
    assert plan["chromium|1|false"] == plan["chromium|1|true"] == plan["chromium|0|true"] == "none"
    assert plan["chromium|0|false"] == "replace"
    for service in ("mozilla", ""):
        assert plan[f"{service}|1|false"] == plan[f"{service}|1|true"] == "none"
        assert plan[f"{service}|0|false"] == plan[f"{service}|0|true"] == "replace"
    assert got["service"] == ["chromium", "chromium", "chromium", "mozilla", "apple", "", "", ""]


def test_a_clear_that_leaves_nothing_open_shows_a_quiet_handled_and_closes_it(tmp_path):
    got = run_worker(tmp_path)
    alone = got["chromium_alone"]
    assert alone["closed"] == ["main/t1"] and alone["shown"] == [HANDLED]
    assert alone["open"] == ["main/t1"]                  # open as the push ends, when Chrome counts
    assert alone["timers"] == [8000] and alone["after"] == []
    assert got["real_push_stays"] == ["main/t1"]          # the thread's next push is not closed
    assert got["chromium_in_view"] == []                  # a window in view: Chrome needs none
    assert got["mozilla_in_view"] == [HANDLED]            # Firefox counts no window
    assert got["mozilla_other_open"] == []                # another notification of the app open
    assert got["apple"]["shown"] == [HANDLED] and got["apple"]["closed"] == ["main/t1"]
    assert got["unknown"] == [HANDLED]


def test_the_page_clears_the_thread_it_shows_without_a_push():
    """The page closes the notifications of the thread in view and tells the
    bot (POST /push/seen), and a push for that thread while it is in view
    is closed after SEEN_MS, past Firefox's three seconds. A headless
    browser cannot grant push, so these are file checks."""
    js = (WEBUI / "app.js").read_text()
    seen = js[js.index("async function seenHere"):js.index("/* -- Installing the app")]
    assert "getNotifications({ tag })" in seen and "n.close()" in seen
    assert "call('POST', '/push/seen', { endpoint: sub.endpoint, tag })" in seen
    assert "e.data && e.data.shown" in seen and "SEEN_MS" in seen
    assert re.search(r"const SEEN_MS = (\d+);", js).group(1) == "5000"
    assert "watchSeen();" in js[js.index("async function initPush"):js.index("async function seenHere")]


def test_the_owners_message_clears_its_threads_unread_count(app):
    from playwright.sync_api import expect
    import webui_stub_server as W
    page, stub, errors = app()
    expect(page.locator("#live.on")).to_be_attached()
    new = stub.emit_bot(W.DM, "A brand new thread")
    item = page.locator(".thread-item:has-text('A brand new thread')")
    expect(item.locator(".badge")).to_have_text("1")
    # the owner answers elsewhere (Zulip, mirrored), the thread still closed here
    stub.add(W.DM, "On it from my phone", author="owner", thread_key=new["thread_key"],
             event="message")
    expect(item.locator(".badge")).to_have_count(0)
    # in another channel: its count, the server's before it was opened, goes as the server counts
    main = page.locator('a.chan[href="#/stream%3A9"] .badge')
    expect(main).to_have_text("2")
    stub.add(W.MAIN, "Thanks", author="owner", thread_key=W.WEEKLY, event="message")
    expect(main).to_have_count(0)
    expect(page).to_have_title("Tacitus")
    assert not errors, errors

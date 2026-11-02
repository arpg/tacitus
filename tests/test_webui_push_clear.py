"""The owner's reply clears the conversation's notifications everywhere
(docs/webchat.md, Notifications (Web Push)): the service worker closes the
notifications a clear push names and shows none, and the app drops a
thread's unread count when the owner's message arrives on the stream, from
this browser or mirrored from elsewhere, also with the thread closed. The
rest of the app's tests are in tests/test_webui.py, whose browser fixtures
this file borrows."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))

from test_webui import WEBUI, app, browser, stub_factory  # noqa: E402,F401  (the fixtures)

# sw.js in a node `vm` context: a `self` whose registration records what it
# shows, and holds three notifications (two under one tag) for a clear to close.
SW_HARNESS = r"""
const vm = require('vm');
const fs = require('fs');
const shown = [], closed = [], asked = [], handlers = {};
const held = () => [['main/t1'], ['main/t1'], ['dm/t2']].map(([tag]) => ({ tag, close: () => closed.push(tag) }));
const self = {
  addEventListener: (name, fn) => { handlers[name] = fn; },
  registration: {
    scope: 'https://host.example.test/chat/',
    showNotification: (title, opts) => { shown.push([title, opts.tag]); return Promise.resolve(); },
    getNotifications: (filter) => {
      asked.push(filter || null);
      return Promise.resolve(held().filter((n) => !filter || !filter.tag || n.tag === filter.tag));
    },
  },
};
const ctx = vm.createContext({ self, importScripts: () => {}, URL, console });
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), ctx);
const fire = (data) => {
  const waits = [];
  handlers.push({ data: { json: () => data }, waitUntil: (p) => waits.push(p) });
  return Promise.all(waits);
};
(async () => {
  await fire({ type: 'clear', tag: 'main/t1' });
  await fire({ type: 'clear' });
  await fire({ title: 'Tacitus replied', body: 'Two meetings.', tag: 'main/t3' });
  process.stdout.write(JSON.stringify({ shown, closed, asked }));
})();
"""


def test_a_clear_push_closes_the_threads_notifications_and_shows_none(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("no node to run the service worker")
    harness = tmp_path / "sw_harness.js"
    harness.write_text(SW_HARNESS)
    out = subprocess.run([node, str(harness), str(WEBUI / "sw.js")], capture_output=True,
                         text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout)
    assert got["asked"] == [{"tag": "main/t1"}]           # a clear with no tag asks for none
    assert got["closed"] == ["main/t1", "main/t1"]        # only that thread's, all of them
    assert got["shown"] == [["Tacitus replied", "main/t3"]]   # a clear shows nothing


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

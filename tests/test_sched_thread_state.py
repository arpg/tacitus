"""sched_thread_state: the thread's scheduling state from its times alone.
The user offers clock times; the next inbound that names one of them is
an acceptance, other times are a counter, none is nothing. Both message
shapes read; the Sent-folder offers index; the CLI."""
import json
import subprocess
import sys
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))
import sched_thread_state as st  # noqa: E402

TZ = ZoneInfo("America/Denver")
TODAY = date(2026, 10, 22)
ME = "kit.calloway@example.edu"
THEM = "morgan.vasquez41@example.org"

OFFER = ("Hi Morgan, The week of Nov 2 works. Here are three 30-minute slots: - Mon Nov 2, 2:30-3:00 PM MT "
         "- Tue Nov 3, 10:45-11:15 AM MT - Wed Nov 4, 2:45-3:15 PM MT By phone or Zoom. Kit")
ACCEPT = "Hi Kit, Wed Nov 4, 2:45-3:15 PM MT is best; I probably just need 15 min. Phone is easier; can I call you?"


def _conv(*msgs):
    return [{"from": {"address": a}, "sent": t, "text": x} for a, t, x in msgs]


def test_an_offer_then_a_pick_is_an_acceptance():
    msgs = _conv((THEM, "2026-10-20T21:05:00Z", "I'm out of town on 10/30. Is the next week doable?"),
                 (ME, "2026-10-21T22:32:00Z", OFFER),
                 (THEM, "2026-10-22T22:53:00Z", ACCEPT))
    a = st.analyze(msgs, {ME}, today=TODAY, tz=TZ)
    assert a["state"] == "accepted"
    assert [o["start"] for o in a["offered"]] == ["2026-11-02T14:30:00-07:00", "2026-11-03T10:45:00-07:00",
                                                   "2026-11-04T14:45:00-07:00"]
    assert a["accepted"] == {"start": "2026-11-04T14:45:00-07:00", "end": "2026-11-04T15:15:00-07:00",
                             "named_end": True,
                             "window": {"start": "2026-11-04T14:45:00-07:00", "end": "2026-11-04T15:15:00-07:00"}}
    assert a["offered_at"] == "2026-10-21T22:32:00Z" and a["inbound_at"] == "2026-10-22T22:53:00Z"


def test_an_offer_with_no_reply_waits_and_a_counter_is_a_counter():
    waiting = st.analyze(_conv((ME, "2026-10-21T22:32:00Z", OFFER)), {ME}, today=TODAY, tz=TZ)
    assert waiting["state"] == "awaiting-pick" and waiting["accepted"] is None
    counter = st.analyze(_conv((ME, "2026-10-21T22:32:00Z", OFFER),
                               (THEM, "2026-10-22T10:00:00Z", "None of those work. Could you do Thu Nov 5 at 9:00 AM MT?")),
                         {ME}, today=TODAY, tz=TZ)
    assert counter["state"] == "counter" and counter["counter"][0]["start"] == "2026-11-05T09:00:00-07:00"
    none = st.analyze(_conv((ME, "2026-10-21T22:32:00Z", OFFER), (THEM, "2026-10-22T10:00:00Z", "Thanks, let me check.")),
                      {ME}, today=TODAY, tz=TZ)
    assert none["state"] == "awaiting-pick", "no clock in the reply: the offer stands"
    assert st.analyze(_conv((THEM, "2026-10-22T10:00:00Z", "Can we meet Thursday at 9?")), {ME}, today=TODAY, tz=TZ)["state"] == "none"


def test_a_pick_of_two_offered_times_is_several_and_the_latest_offer_counts():
    two = st.analyze(_conv((ME, "2026-10-21T22:32:00Z", OFFER),
                           (THEM, "2026-10-22T10:00:00Z", "Either Mon Nov 2 2:30 PM MT or Wed Nov 4 2:45 PM MT works.")),
                     {ME}, today=TODAY, tz=TZ)
    assert two["state"] == "several" and len(two["counter"]) == 2
    re_offer = st.analyze(_conv((ME, "2026-10-20T20:59:00Z", "Friday Oct 30 is a possibility: 10:30 or 11:00 AM MT (30 minutes)."),
                                (THEM, "2026-10-20T21:05:00Z", "I'm out on 10/30, is next week doable?"),
                                (ME, "2026-10-21T22:32:00Z", OFFER),
                                (THEM, "2026-10-22T22:53:00Z", "Fri Oct 30 at 10:30 AM MT works after all.")),
                          {ME}, today=TODAY, tz=TZ)
    assert re_offer["state"] == "counter", "a pick of a withdrawn offer is a counter against the latest offer"


def test_graph_shapes_and_the_sent_offers_index():
    sent = [{"conversationId": "c1", "sentDateTime": "2026-10-21T22:32:00Z",
             "from": {"emailAddress": {"address": ME}}, "body": {"content": OFFER}, "bodyPreview": OFFER[:40]},
            {"conversationId": "c2", "sentDateTime": "2026-10-21T10:00:00Z",
             "from": {"emailAddress": {"address": ME}}, "bodyPreview": "Thanks, will do."}]
    offers = st.offers_from_sent(sent, today=TODAY, tz=TZ)
    assert set(offers) == {"c1"} and len(offers["c1"]["times"]) == 3 and offers["c1"]["at"] == "2026-10-21T22:32:00Z"
    kind, picks, others = st.classify_reply(ACCEPT, offers["c1"]["times"], today=TODAY, tz=TZ)
    assert kind == "accepted" and picks[0]["start"] == "2026-11-04T14:45:00-07:00" and others == []
    inbound = {"from": {"emailAddress": {"address": THEM}}, "receivedDateTime": "2026-10-22T22:53:00Z", "bodyPreview": ACCEPT}
    a = st.analyze(sent[:1] + [inbound], {ME}, today=TODAY, tz=TZ)
    assert a["state"] == "accepted"


def test_only_the_replys_own_words_count():
    """A reply quotes the offer below an attribution line; a preview cuts
    that line short. The quoted times and the attribution's clock are not
    picks."""
    offer = ("Hi Morgan, any of these for a 30-minute Zoom? - Tue Nov 3, 3:30-4:00 PM MT (5:30-6:00 PM ET) "
             "- Wed Nov 4, 3:15-3:45 PM MT (5:15-5:45 PM ET) Kit")
    times = st.offers_from_sent([{"conversationId": "c1", "sentDateTime": "2026-11-02T19:14:28Z",
                                  "from": {"emailAddress": {"address": ME}}, "body": {"content": offer}}],
                                today=TODAY, tz=TZ)["c1"]["times"]
    assert len(times) == 2, "the other side's clock is the same slot"
    full = ("Hi Kit,\r\nTue Nov 3, 3:30-4:00 PM MT (5:30-6:00 PM ET)\r\nSee you tomorrow.\r\n-- Morgan\r\n\r\n"
            "On Mon, Nov 2, 2026 at 10:14 AM Kit Calloway <kit.calloway@example.edu> wrote:\r\n" + offer)
    assert st.classify_reply(full, times, today=TODAY, tz=TZ)[0] == "accepted"
    assert st.classify_reply(full[:150], times, today=TODAY, tz=TZ)[0] == "accepted", "a preview cut mid-attribution"
    assert st.new_text("Works.\n> Tue Nov 3, 3:30 PM") == "Works.\n"
    assert st.new_text("Works.\n________________________________\nFrom: Kit\nSent: Monday") == "Works.\n"
    assert st.new_text("On Tuesday 3:30 works for me.") == "On Tuesday 3:30 works for me.", "not an attribution"
    html = ('<html><body><div>Tue Nov 3 at 3:30 PM works.</div><div id="divRplyFwdMsg">From: Kit<br>'
            'Wed Nov 4, 3:15 PM</div></body></html>')
    assert "Wed Nov 4" not in st.new_text(html) and "3:30 PM works." in st.new_text(html)
    assert "Wed" not in st.new_text('<p>Yes, Tuesday.</p><blockquote>Wed Nov 4, 3:15 PM</blockquote>')


def test_a_time_inside_an_offered_window_takes_that_window():
    offered = [{"start": "2026-11-03T15:45:00-07:00", "end": "2026-11-03T16:45:00-07:00"},
               {"start": "2026-11-04T11:00:00-07:00", "end": "2026-11-04T12:00:00-07:00"}]
    kind, picks, others = st.classify_reply("Tue Nov 3, 4:00-4:30 PM works for me.", offered, today=TODAY, tz=TZ)
    assert kind == "accepted" and others == []
    assert picks[0] == {"start": "2026-11-03T16:00:00-07:00", "end": "2026-11-03T16:30:00-07:00", "named_end": True,
                        "window": offered[0]}
    # past the window's end is a counter, not a pick
    assert st.classify_reply("Tue Nov 3, 4:30-5:30 PM works.", offered, today=TODAY, tz=TZ)[0] == "counter"
    # the record's time inside a window grounds it too
    slot = st.match_offered(["2026-11-04T11:30:00"], offered, tz=TZ)
    assert slot["start"] == "2026-11-04T11:30:00-07:00" and slot["window"] == offered[1] and not slot["named_end"]
    assert st.match_offered(["2026-11-04T12:30:00"], offered, tz=TZ) is None


def test_the_cli_reads_a_thread_file(tmp_path):
    f = tmp_path / "thread.json"
    f.write_text(json.dumps({"messages": _conv((ME, "2026-10-21T22:32:00Z", OFFER), (THEM, "2026-10-22T22:53:00Z", ACCEPT))}))
    r = subprocess.run([sys.executable, str(ROOT / "preprocessor" / "sched_thread_state.py"), "analyze",
                        "--thread-file", str(f), "--user", ME, "--tz", "America/Denver"],
                       capture_output=True, text=True, env={"PATH": "/usr/bin:/bin", "HOME": str(Path.home())})
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["state"] == "accepted"

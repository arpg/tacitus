#!/usr/bin/env python3
"""Post the daily outbound-web review to the EA channel.

Reads three logs, each from its own cursor (outbound_review.review):
fetch_url.py's log and the other modules' records in ea-bot space
(fetch-url.log), the owner-side log of the model's own WebFetch and
WebSearch calls (web_tool_log.py), and the owner-side log of requests and
remote-writer briefs from owner processes (outbound_log.py). Summarises
what left the machine and flags blocked fetches, exfil-shaped URLs and
searches, web tools in a cron lane, volume spikes, new hosts, many
subdomains, poll submissions under another name and any sign that the
ea-bot log was tampered with (see outbound_review). Posts when a check
tripped; a clear day posts its short all-clear on Mondays only
(HEARTBEAT_WEEKDAY), so the review is visibly running and a quiet day adds
nothing to the health channel. A clear day not posted still advances the
cursors: its window was read. The review is a diagnostic, posted only in
debug mode, unless a protection did not hold (protection_failed): that is
an alert, posted in any mode (chat_notify, Debug mode). The text goes to
the cron log either way.

With no cursor (a first run, or the file was removed) or a cursor that is
refused or is not JSON, each log is read from its start (bounded by size)
and the post opens with a LOG CHECK line that says why; a bad cursor is
set aside before the new one is written. No run skips a window silently.
A failed post does not advance the cursors, so a day is never skipped.

Cron (owner, daily):  45 6 * * *  python3 .../scripts/outbound-review.py
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "preprocessor"))


# The weekday (Monday 0) a clear review still posts its all-clear.
HEARTBEAT_WEEKDAY = 0


def _today() -> datetime:
    return datetime.now()


def protection_failed(report: dict, no_cursor: str) -> bool:
    """True when the review shows a protection that did not hold: a web
    tool in a cron lane (the deny list), or a log check other than a first
    run's missing cursor (an ea-bot log rewritten, a cursor refused)."""
    return bool(report.get("web_cron_lanes")
                or [a for a in report.get("log_anomalies") or [] if a != no_cursor])


def main() -> int:
    import outbound_review as R
    from preprocessor import chat_notify

    # The state lives in the owner-private directory (R.CURSOR), so the
    # resident cannot advance a cursor past records it wants unseen.
    state = R.read_state()
    report, new_state = R.review(state)

    text = R.render(report)
    flagged = R.has_flags(report)
    print(text)
    if (flagged or _today().weekday() == HEARTBEAT_WEEKDAY) and not chat_notify.post(
            chat_notify.health_channel_key(), text,
            diagnostic=not protection_failed(report, R.NO_CURSOR)):
        print("outbound-review: post failed; cursors not advanced", file=sys.stderr)
        return 1
    if state is not None and state.get("cursor_problem"):
        aside = R.set_aside_cursor()
        print(f"outbound-review: the cursor {state['cursor_problem']}; "
              f"set aside as {aside}", file=sys.stderr)
    R.write_state(new_state)
    print(f"outbound-review: {report['fetches']} fetches, {report['web_fetches']} WebFetch, "
          f"{report['web_searches']} WebSearch, {report.get('briefs', 0)} writer briefs, "
          f"{len(report.get('log_anomalies') or [])} log anomalies, "
          f"{'flagged' if flagged else 'clear'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

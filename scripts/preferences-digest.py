#!/usr/bin/env python3
"""Daily digest of preference-rule changes, posted to the EA channel.

Every preference rule shapes what the EA surfaces or does, and the resident
can add one from a Slack message, so an email that steered the resident
could add a rule the user never asked for ("never show me X"). This is the
visibility layer: once a day it posts the rules added or retired since the
last run, so the user reviews them and can catch one he did not set.

It advances a cursor (preferences.py digest), so it reports each change once
and is silent when nothing changed. The first run seeds the cursor and posts
nothing. Idempotent and safe to leave in cron.

Cron (owner, daily):  0 7 * * *  python3 .../scripts/preferences-digest.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "preprocessor"))


def main() -> int:
    import preferences
    from preprocessor import chat_notify

    data = preferences.load()
    # The cursor lives in the owner-private directory (preferences.DIGEST_CURSOR),
    # so the resident cannot advance it past a rule it added.
    seen = preferences.read_digest_cursor()
    report, new_seen = preferences.digest(data, seen)
    text = preferences.render_digest(report)

    posted = True
    if text:
        # Normal priority: held during quiet hours and delivered in the
        # morning digest, like other non-urgent posts. An alert: a rule the
        # owner did not set must reach them, so it posts whether or not
        # debug mode is on (chat_notify, Debug mode).
        posted = chat_notify.post(chat_notify.health_channel_key(), text)

    if posted:
        preferences.write_digest_cursor(new_seen)
        n = len(report.get("added") or []) + len(report.get("retired") or [])
        print(f"preferences-digest: {n} change(s) {'posted' if text else '(none)'}")
        return 0
    # A failed post must not advance the cursor, or the change is never shown.
    print("preferences-digest: post failed; cursor not advanced", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())

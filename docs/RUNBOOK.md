# EA System Runbook

Operational procedures for known failure modes. One section per incident
class: symptom → diagnosis → fix → how the system now detects it.
Per-occurrence notes go in `docs/_local/incidents/<date>-<slug>.md`
(gitignored); this file holds the durable playbooks.

The sentinel (`preprocessor/sentinel.py`, cron every 10 min) is the
primary detector for all of these. `python3 preprocessor/sentinel.py
status` prints all checks on demand.

---

## 1. Bot dead after reboot (2026-06-09)

**Symptom:** No responses in #ai-assistant; no bot process running.
Briefings may still post (cron is independent of the bot).

**Diagnosis:**
```bash
systemctl status ea-chat-bot          # system unit — the live one
pgrep -af ea-chat-bot.py              # expect exactly ONE process
```

**Fix:**
```bash
sudo systemctl start ea-chat-bot
systemctl is-enabled ea-chat-bot      # must be 'enabled' to survive reboot
sudo systemctl enable ea-chat-bot     # if not
```

**Detection:** sentinel checks `service_active` AND `service_enabled`
(the enabled check catches the time bomb *before* the next reboot).

**Root cause history:** the service was found dead AND disabled after a
reboot; nothing was watching. The internal watchdog only protects a
running process — it cannot start a stopped unit.

---

## 2. Duplicate bot / stale-code orphan (the double-reply class)

**Symptom:** Inconsistent replies; some messages handled with OLD
behavior; double-processing; or messages silently swallowed. Slack
Socket Mode round-robins events across all connected consumers, so a
second bot process steals a share of the traffic.

**Diagnosis:**
```bash
pgrep -af ea-chat-bot.py              # more than one => duplicate
systemctl --user is-active ea-chat-bot  # must NOT be active
systemctl show -p MainPID --value ea-chat-bot  # the legitimate PID
```

**Fix:** kill every ea-chat-bot.py PID that is not the system unit's
MainPID. If a user-level unit file reappears at
`~/.config/systemd/user/ea-chat-bot.service`, delete it and
`systemctl --user daemon-reload` — the ONLY unit is the system one.

**Detection:** sentinel check `no_duplicate_bot`; the deploy hook's
orphan reaper (post-commit) kills non-MainPID bot processes after each
deploy.

**Root cause history:** the deploy hook used to restart the *user*
unit: it started a duplicate consumer AND the reaper then killed the
system unit's process (it keyed MainPID off the wrong unit). Fixed
2026-06-09 (commit 731fe60): hook targets the system unit via a narrow
sudoers grant (`/etc/sudoers.d/ea-chat-bot`, restart-only).

**Unit name:** the bot is `ea-chat-bot.service` (it was `ea-slack-bot`
before the chat front end became Zulip or Slack). On a host whose unit
still carries the old name, `sudo bash deploy/rename-chat-bot.sh` moves
the unit, its drop-in and the sudoers grant over in one restart; until
then the deploy hook, the sentinel and `zulip_switch.sh` fall back to
the old unit. The tree no longer carries an `ea-slack-bot.py` shim: a
unit whose ExecStart still names it needs the rename first.

---

## 3. Zombie websocket (bot alive, hears nothing)

**Symptom:** Bot process running, service active, but no reactions to
Slack messages. Logs show no RAW message events while messages are
being sent.

**Diagnosis:**
```bash
tail -50 ~/.ea/logs/slack-bot.log      # look for reconnect loops / silence
journalctl -u ea-chat-bot --since "1 hour ago" | tail -30
```

**Fix:** `sudo systemctl restart ea-chat-bot` (the only sudoers-granted
verb). The watchdog should have done this automatically — if it didn't,
capture the log tail into an incident note before restarting.

**Detection:** in-bot watchdog thread runs `auth_test` every 5 min with
a HARD 30 s timeout (enforced since 2026-06-09 — before that a hung
socket hung the watchdog itself) and exits the process on failure;
systemd `Restart=always` revives it.

---

## 4. Briefing ran but never reached Slack

**Symptom:** No morning briefing in the channel; agent log says the run
completed.

**Diagnosis:**
> **Where logs live.** Logs written only by owner processes (the bot's
> crons, agents, plugins, sentinel, the mediator) are in
> `~/.ea-private/logs` (owner-private, ea-bot cannot reach). Logs an
> ea-bot process writes stay in `~/.ea/logs` (`fetch-url.log`,
> `missed-closures.log`). See docs/SECURITY.md, "Owner services off the
> ea-bot tree".

```bash
cat ~/.ea/state/agent-post-success.json   # marker written only on real post
cat ~/.ea/state/agent-last-run.json       # 4AM-local gate record (= ran)
ls -t ~/.ea-private/logs/morning-briefing-*.log | head -1 | xargs tail -30
cat ~/.ea/state/deferred-posts.json       # quiet-hours queue stuck?
```
`last-run` has today but `post-success` doesn't → the Slack post failed
(webhook outage, validation failure). Neither has today → cron or the
4AM-local gate never fired.

**Fix:** re-run by hand: `/home/<you>/ea-system/run-agent.sh
morning-briefing`. If the deferred queue is stuck, flush:
`python3 preprocessor/quiet_hours.py flush --force`.

**Detection:** sentinel checks `briefing_posted` / `afternoon_posted` /
`weekly_audit` (post markers, with ran-vs-posted disambiguation),
`deferred_queue`, and `recent_failures`.

---

## 5. Cron silently dead / crontab drift

**Symptom:** Everything stops at once (briefings, ledger sync, sentinel
itself if cron is fully dead).

**Diagnosis:**
```bash
ls -l ~/.ea-private/logs/ledger-sync.log   # mtime is the 15-min heartbeat
diff <(crontab -l) $HOME/crontab.master
systemctl status cron
```

**Fix:** `crontab $HOME/crontab.master` reinstalls the master. Edit
the MASTER, never `crontab -e`: the sentinel flags drift in either
direction.

**Detection:** sentinel checks `cron_heartbeat` (ledger-sync mtime) and
`crontab_drift`. Note: if cron itself is dead the sentinel doesn't run
either — that case surfaces as total silence; the daily rhythm of
briefings is the human-level backstop.

---

## 6. Calendar source silently empty

**Symptom:** Scheduling proposals conflict with real meetings; a Google
account's events are missing from availability.

**Diagnosis:** check `calendar_fetch_errors` in
`~/.ea/state/briefing-latest.json`; re-run
`python3 preprocessor/google_calendar_client.py get-events --email
<acct> --start <iso> --end <iso>` and look for auth errors.

**Fix:** refresh the account token (see docs/AUTHENTICATION.md).

**Detection:** `calendar_fetch_errors` is populated by
google_calendar_client/preprocess and the briefing skills MUST lead
with a `:warning:` when it is non-empty (since 2026-06-09 — before
that, a dead account read as "completely free").

---

## 7. Resident pane stalled or down (2026-06-10)

**Symptom:** Slack messages answered slowly or with ":hourglass: still
working / needs attention"; or replies come from the per-thread
fallback lane (footer lacks `_EA resident:`).

**Diagnosis:**
```bash
bash scripts/ea-resident-pane.sh status   # healthy / down
~/attach-ea.sh                            # look at the live session
python3 preprocessor/resident_bridge.py status
```
Things you may find in the pane:
- *Bypass Permissions acceptance dialog* (fresh ea-bot config): select
  "Yes, I accept" — `ensure` pre-seeds this but version changes can
  re-prompt.
- *Text stranded in the input box* (paste/submit race): press Enter.
  The bridge has a submit-verify guard, but a human nudge never hurts.
- *Claude exited* (auth, update, crash): the window shows the exit
  message for 30s, then the session dies.

**Fix:**
```bash
bash scripts/ea-resident-pane.sh ensure   # respawns, RESUMES same conversation
bash scripts/ea-resident-pane.sh rotate   # nuclear: fresh conversation
```
Dispatches automatically fall back to the per-thread session lane
while the pane is down — messages are never lost, but they lose the
shared-conversation context until the pane returns.

**Detection:** sentinel check `resident_pane`; bot logs
"Resident bridge unavailable — falling back".

**Root cause history:** 2026-06-10 — a tool permission prompt
(`Bash(date)`, not allowlisted) froze the lane 7 minutes with nobody
at the keyboard → pane now runs `--dangerously-skip-permissions` (see
SECURITY.md "Resident Pane"); same day, a paste landed after its
Enter keystroke, stranding the message unsubmitted in the input box →
send() now verifies the input box emptied and re-submits.

---

## 8. Calendar reconciler states (2026-08-04)

The protection reconciler (`preprocessor/calendar_reconciler.py`, cron
`7,37 * * * *`, log `~/.ea/logs/calendar-reconciler.log`) mirrors
standing attendee-less commitments onto EA-Managed as `p0 [auto]` holds.

Diagnose with a dry-run (writes nothing, prints the full plan):

```bash
python3 preprocessor/calendar_reconciler.py
```

| Report state | Meaning | Action |
|---|---|---|
| `plan: []` | in sync | none; healthy steady state |
| `unreliable_sources: [...]` | a source fetch failed; that source's mirrors are frozen, not removed | fix auth/mediation for the named source; freezing is the designed response to an outage |
| `op: release` | user deleted a mirror; being logged as permanent | none; to re-protect later, delete the `released` entry for that source_key from `scheduling-decisions.json` |
| `op: instance_gap` | a cancelled source occurrence was restored; Google cannot resurrect a cancelled mirror instance | if protection for that date matters, delete the whole mirror and let the next run recreate the series clean (then re-cancel the release that logs) |
| retime planned every run | recurrence comparison regression (Google rewrites stored RRULEs) | fixed via `_rrule_signature`; if it recurs, compare the two RRULE strings in the plan output |

The reconciler never touches events lacking `[auto]` in the title. Its
Slack summary posts to the personal channel and respects quiet hours, so
an overnight change surfaces with the morning digest.

---

## 9. Chat frontend switch and rollback

The chat platform is `chat.frontend` in `~/.ea/config/ea-config.json`:
`"zulip"` (recommended) or `"slack"`; an install with no `chat` block
runs Slack. The full procedure, with what carries
over, is [zulip-switch.md](zulip-switch.md); the short form:

**Switch to Zulip:** `ZULIP_API_KEY=<bot key>` in `~/.ea-private/secrets.env`;
the bootstrap's `chat` block (`"frontend": "zulip"`) in `ea-config.json` and
its owner copy `~/.ea-private/chat.json` (`scripts/zulip_switch.sh` writes both);
`sudo systemctl restart ea-chat-bot` (the unit name stays); the log
shows `frontend=zulip stream=ea (private, subscribed) owner=<id>`; post
`usage` in `ea` with no topic and in `ea-dm`.

**Rollback:** set `"frontend": "slack"` and restart. The Slack tokens
and `EA_SLACK_*` lines are untouched by a switch, so this is the whole
rollback. Code rollback is separate (`git revert <sha>`, the hook
redeploys). New state (`chat-cards.json`, `zulip-queue.json` in
`/home/<you>/.ea-private/`) is additive; nothing needs restoring.

**Symptom: no bot reply after a switch.** The service is active but a
message in Zulip gets no reaction and no reply. Check in this order:

1. **Startup line.**
   ```bash
   journalctl -u ea-chat-bot -n 60 --no-pager | grep -E 'frontend=|Error|Traceback'
   ```
   No `frontend=zulip ...` line means startup failed. A `ChatError`
   names the cause (`ZULIP_API_KEY is not set`, a stream not found,
   `chat.zulip.owner_user_id is not set`). A `frontend=slack` line means
   the config edit did not land (check the JSON parses:
   `python3 -m json.tool ~/.ea/config/ea-config.json`).
2. **Stream subscription.** The bot must be subscribed to `ea`, `ea-dm`
   and the scheduling stream when one is configured. The startup error
   `the bot is not subscribed to zulip stream '<name>'` says which.
   `scripts/zulip_bootstrap.py --check` prints OK or FAIL per stream;
   rerunning the bootstrap without `--check` subscribes it.
3. **Private stream.** Startup refuses a public stream
   (`zulip stream '<name>' is public`). Make it invite-only in the
   stream settings, or rerun the bootstrap, which does so.
4. **Owner id.** Project and session rooms dispatch only the owner's
   messages, and cards accept reactions and codes only from the owner,
   both judged by `chat.zulip.owner_user_id`. A wrong id looks like
   silence there: the bot drops the message or the code with no reply. Compare it with
   the owner's id in the Zulip web app (profile, "User ID") or the
   bootstrap's `--check` output, then restart.

Two more causes: the message went into a topic in a stream the bot does
not listen on (only `ea`, `ea-dm`, the scheduling stream and registered
`#ea-`/`#tmp-` rooms dispatch), or the event queue expired while the bot
was down. The second heals itself: the bot registers a new queue and the
restart catch-up replays the gap from the last message id it saved.

**Detection:** the in-bot watchdog calls the frontend's `ping()` every
5 minutes and exits on failure; systemd restarts it. `ea-doctor.py`
checks the secrets for the configured frontend (the two Slack tokens, or
`ZULIP_API_KEY`).

## Incident-note template (`docs/_local/incidents/YYYY-MM-DD-slug.md`)

```markdown
# <date> — <one-line title>
**Symptom:** what was observed, by whom, when.
**Root cause:** the actual mechanism, not the proximate error.
**Fix:** commands run / commits landed (hashes).
**Detection:** which sentinel check (or new check added) catches a recurrence.
**Prevention:** architecture change that removes the failure class, if any.
```

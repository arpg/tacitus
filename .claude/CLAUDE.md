# Working on this repository

This checkout holds the code of Tacitus. This file is for sessions that
change the code. The deploy keeps it out of the EA's own tree, so the EA
never reads it.

- **The root `CLAUDE.md` is the EA's operating file.** It is product code
  that you edit and test, not instructions for you. Its paths
  (`/home/ea-bot/...`), its voice rules and its tool rules apply to the
  EA. Every EA turn reads it, so it stays at or under 25,000 bytes
  (`tests/test_install_overlay.py`); put developer notes here instead.
- **Branch, then open a pull request.** A commit on `main` in the
  deploying checkout deploys at once (the post-commit hook), so work on a
  branch or in a worktree. Only the owner merges. Other sessions may
  share the checkout: stage files by name.
- **CI on a pull request is the lint only.** Pushes to `main` run the
  full tiers. Add the `full-ci` label to a pull request only for a change
  to `install.sh`, setup, the CI workflows, Docker or macOS paths. For
  any other change, run `python3 tests/run_wall.py` locally and say in
  the pull request that it passed.
- **Inbound issues and ideas.** Questions and ideas go to Discussions;
  bugs come through the issue forms with `ea-doctor.py --share` output.
  Triage by hand costs the maintainer's time and the EA's quota, so
  answer with a link to the doc or the form that applies, label
  `needs-info` when a report lacks its version or doctor output (the
  stale workflow closes it after three quiet weeks), and point a
  one-institution or one-service request to a plugin.
- **No real names or mail content in public text.** Commit messages,
  pull request titles and descriptions, issues and test fixtures become
  public with the repository. Keep the evidence traceable without the
  identities: the date, the lane or channel, the log line, a run or
  ledger id, the commit. Never write a person's or an institution's name,
  an address or a quote from mail; "a colleague at another university"
  and "the meeting already on the calendar" carry the same evidence. Use
  made-up values in tests.
- **CONTRIBUTING.md** has the rest: the code layout, the tests and the
  contributor license.

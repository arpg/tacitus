## Usage tracking

The bot appends claude.ai plan usage (session % and weekly %) to every
response footer. The user can also type "usage" or "quota" in chat for
a detailed report, answered by the bot without a model turn.

The numbers come from the Claude runs the EA starts. Each
`claude -p --output-format stream-json` run prints a `rate_limit_event`
with the plan's windows. The owner processes that read the run (the bot
for the chat lanes, `log_claude_cost.py` for the cron and plugin pipes)
save the latest one to `claude-rate-limits.json` in the owner-private
dir (`preprocessor/rate_limits.py`). These runs all use the resident's
Claude login (ea-bot), the account whose quota the EA spends. The owner's
own Claude login is a different account with its own quota.

- The footer reads the saved reading and makes no API call per reply. A
  reading older than 10 minutes shows its age ("checked 2h ago"). When
  the reading is older than 30 minutes, the footer asks the usage
  endpoint, at most once every 15 minutes, and saves its answer.
- The `usage` command reads the saved reading when it is under 30
  minutes old. Otherwise it starts a one-word Claude run on the cheapest
  model (at most once every 5 minutes), whose stream saves a new reading.
  This run spends a little quota, so a footer never starts one. The
  usage endpoint (`https://api.anthropic.com/api/oauth/usage`, read as
  the resident) is the last fallback, then the older reading with its
  age.
- The long-lived `CLAUDE_CODE_OAUTH_TOKEN` (macOS) cannot read the usage
  endpoint. The saved readings work with it.

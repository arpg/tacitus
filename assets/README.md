# Assets

`tacitus-avatar.png` is the EA's picture: the bot's avatar on Zulip and
the app icon on Slack. It is 512 by 512 pixels, the smallest app icon
Slack accepts.

`scripts/zulip_bootstrap.py` uploads it to the bot it creates, and to an
existing bot with `--avatar`. On Slack, upload it by hand under **Basic
Information > Display Information > App icon** (docs/SETUP.md "The Slack
app").

The picture is a square crop of
[Tacitus portrait](https://commons.wikimedia.org/wiki/File:Tacitus_portrait.jpg),
a 1915 drawing by Julien of the Roman historian Cornelius Tacitus, after
an antique bust. Wikimedia Commons lists it as public domain.

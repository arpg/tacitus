# botcore

Shared plumbing for Claude-driven chat bots. It knows nothing about email
or any user's data; a bot built on it brings its own skills, commands and
storage. The EA's chat layer (`preprocessor/chat`) runs on it.

## What is in it

`botcore.chat` is one interface in front of Slack, the Tacitus web chat or
an in-memory fake:

- `ChatFrontend` (`base.py`): every call a bot makes to a chat platform,
  plus a registry for message, button and event handlers.
- `SlackFrontend`, `WebFrontend`, `MemoryFrontend`: the implementations.
  Import the one you use from its module; a bot on the web chat never
  imports the Slack client.
- `ConfirmationCard` (`cards.py`): approval buttons that render natively on
  each platform (Slack buttons, the web chat's own buttons).
- `files.use(make_dir, replace_bytes)`: install your own writers for files
  the bot downloads. The default writes atomically to plain paths.

## Install

From a checkout:

    pip install ./botcore            # core
    pip install "./botcore[slack]"   # plus the Slack client

From GitHub:

    pip install "botcore[slack] @ git+https://github.com/arpg/tacitus#subdirectory=botcore"

## Use

```python
from botcore import chat
from botcore.chat.memory_frontend import MemoryFrontend

fe = MemoryFrontend.from_config({})
card = chat.ConfirmationCard(
    kind="send", title="Send the draft?",
    actions=[chat.CardAction("approve_send", "Send", "v1", "primary", "send"),
             chat.CardAction("cancel_send", "Cancel", "v1", "danger", "cancel")],
    block_id="b1", fallback_text="Send the draft?")
fe.post_card(fe.main_channel, card)
```

Run its tests with `python -m pytest botcore/tests`. The main suite
(`tests/`) covers the frontends in depth through `preprocessor/chat`.

`preprocessor/chat/<module>.py` and `preprocessor/card_text.py` are aliases:
each replaces itself in `sys.modules` with the botcore module, so
`chat.slack_frontend` and `botcore.chat.slack_frontend` are one object and a
test that patches either patches both.

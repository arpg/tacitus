#!/usr/bin/env python3
"""The web chat relay's key file, for the owner (docs/webchat-relay.md).

    webchat-relay.py init    --key-file ~/.ea-private/webchat-relay.json --relay-url https://<relay>
    webchat-relay.py show    --key-file ~/.ea-private/webchat-relay.json
    webchat-relay.py pair    --key-file ~/.ea-private/webchat-relay.json (--public-url <url> | --relay-origin) [--print-link]
    webchat-relay.py approve --key-file ~/.ea-private/webchat-relay.json --device <id>
    webchat-relay.py revoke  --key-file ~/.ea-private/webchat-relay.json --device <id>

`init` creates the key file and prints the install id and the `relay`
block for the chat config. `pair` prints a one-time pairing as a QR code
on the terminal (`--print-link`: the link as text, `#tr2=`; it expires in
ten minutes). With `--public-url` (the web block's) the link opens the app
on the bot's own origin. `--relay-origin` opens the relay's copy of the
app instead, as "Pair through the relay" in the web chat does: on a build
that signs its app bundle the link carries the bundle hash, the phone
checks its code against it, and the bot lets it read only until it has;
on a build that signs none, whoever runs the relay could read that
phone's chat and type as the owner, and the bot lets it in only when
chat.json sets `relay_origin_access`. A new device waits until the owner
approves it: `show` lists each device with its origin, whether a relay
copy's code is verified, and a waiting device's pairing code, which must
match the code on the phone; `approve` lets it in. Never send a pairing
link through chat or mail. Run it as the owner: the key file lives in the
owner-private directory.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "botcore"))

from botcore.chat.relay_client import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())

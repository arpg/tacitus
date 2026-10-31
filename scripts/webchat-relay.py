#!/usr/bin/env python3
"""The web chat relay's key file, for the owner (docs/webchat-relay.md).

    webchat-relay.py init   --key-file ~/.ea-private/webchat-relay.json --relay-url https://<relay>
    webchat-relay.py show   --key-file ~/.ea-private/webchat-relay.json
    webchat-relay.py pair   --key-file ~/.ea-private/webchat-relay.json [--public-url <url>]
    webchat-relay.py revoke --key-file ~/.ea-private/webchat-relay.json --device <id>

`init` creates the key file and prints the install id and the `relay`
block for the chat config. `pair` prints a one-time pairing link (the
text of the QR code; it expires in ten minutes). With `--public-url` (the
web block's) the link opens the app on the bot's own origin; without it,
the relay's copy of the app. Run it as the owner: the
key file lives in the owner-private directory.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "botcore"))

from botcore.chat.relay_client import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())

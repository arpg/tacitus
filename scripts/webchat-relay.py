#!/usr/bin/env python3
"""The web chat relay's key file, for the owner (docs/webchat-relay.md).

    webchat-relay.py init    --key-file ~/.ea-private/webchat-relay.json --relay-url https://<relay>
    webchat-relay.py show    --key-file ~/.ea-private/webchat-relay.json
    webchat-relay.py pair    --key-file ~/.ea-private/webchat-relay.json (--public-url <url> | --relay-origin) [--print-link]
    webchat-relay.py approve --key-file ~/.ea-private/webchat-relay.json --device <id> [--cards]
    webchat-relay.py revoke  --key-file ~/.ea-private/webchat-relay.json --device <id>
    webchat-relay.py rotate  --key-file ~/.ea-private/webchat-relay.json --devices keep|drop

`init` creates the key file and prints the install id and the `relay`
block for the chat config. `pair` prints a one-time pairing as a QR code
on the terminal (`--print-link`: the link as text, `#tr2=`; it expires in
ten minutes). With `--public-url` (the web block's) the link opens the app
on the bot's own origin. `--relay-origin` opens the relay's copy of the
app instead, as "Pair through the relay" in the web chat does: the link
carries the hash of the bot's signed app bundle, and the phone checks its
code against it before its chat runs. Whoever runs the relay serves that
phone's code, so once approved it reads and chats, and presses card
buttons (Send, Create) only after `approve --cards`. A new device waits
until the owner approves it: `show` lists each device with its origin,
for a relay copy whether its code is verified and against which release,
and a waiting device's pairing code, which must match the code on the
phone; `approve` lets it in. Never send a pairing link through chat or
mail. Run it as the owner: the key file lives in the owner-private
directory.

`rotate` gives the key file a new relay token, and with it a new install
id and client key: the client key in every pairing link shown so far
stops reaching the bot. `--devices keep` keeps the bot's static key and
the paired devices; `--devices drop` makes a new static key and removes
every device. Both drop the open pairings and copy the old file to a
backup beside it (mode 0600). The running bot takes the new key at its
next poll. It prints the new install id: the relay's allow file and the
`install_id` pin in chat.json must name it.
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "botcore"))

from botcore.chat import relay_crypto as rc  # noqa: E402
from botcore.chat.relay_client import ROTATE_DEVICES, RelayError, main, rotate  # noqa: E402


def rotate_steps(out: dict) -> list[str]:
    """What the owner does after a rotation, one line each."""
    new, old = out["install_id"], out["old_install_id"]
    lines = [f"The install id is now {new} (it was {old}). The relay knows the install by "
             "this id, and the old client key reaches nothing."]
    if out["install_id_changed"]:
        lines += [
            f"Hosted relay: give the operator {new} for the allow file "
            f"(relay/install.sh --install-id {new}) and ask that {old} be removed. A relay of "
            "your own without an allow file needs nothing.",
            f"chat.json: when the relay block pins install_id, set it to {new}. With the old "
            "id the next start of the bot leaves the relay off.",
        ]
    lines.append("The running bot takes the new key at its next poll (within a minute). "
                 "A restart (sudo systemctl restart ea-chat-bot) does the same.")
    if out["devices"] == "kept":
        lines.append(f"{out['device_count']} paired device(s) stay approved, with the same bot "
                     "key. Each phone still holds the old install id and client key: pair it "
                     "again on the tailnet and remove its old entry, until the app learns the "
                     "new ones from the bot (docs/webchat-relay.md, Later work).")
    else:
        lines.append(f"{out['device_count']} paired device(s) are removed and the bot key is "
                     "new: pair each phone again on the tailnet. Their push subscriptions go "
                     "at the next push.")
    lines.append(f"The backup {out['backup']} holds the old token and keys: delete it once the "
                 "new key works.")
    return lines


def rotate_main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="webchat-relay rotate",
                                 description="A new relay token, install id and client key.")
    ap.add_argument("--key-file", required=True)
    ap.add_argument("--devices", required=True, choices=ROTATE_DEVICES,
                    help="keep: the bot key and the paired devices stay; drop: a new bot key "
                         "and no device")
    args = ap.parse_args(argv)
    if not rc.AVAILABLE:
        print("the relay needs the cryptography package", file=sys.stderr)
        return 2
    path = Path(os.path.expanduser(args.key_file)).absolute()
    try:
        out = rotate(path, args.devices)
    except (RelayError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(out, indent=1))
    for line in rotate_steps(out):
        print(line, file=sys.stderr)
    return 0


def run(argv=None) -> int:
    """`rotate` here; every other command is relay_client.main's."""
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] in (["-h"], ["--help"]):
        print(__doc__)
        return 0
    if argv[:1] == ["rotate"]:
        return rotate_main(argv[1:])
    return main(argv)


if __name__ == "__main__":
    sys.exit(run())

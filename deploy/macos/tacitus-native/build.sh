#!/usr/bin/env bash
# Build (and with --install, install) tacitus-native, the macOS helper of the
# native-accounts backend (docs/macos-native-accounts.md). No Xcode project:
# swiftc from the Command Line Tools (`xcode-select --install`).
#
#   bash deploy/macos/tacitus-native/build.sh              build into ./build
#   sudo bash deploy/macos/tacitus-native/build.sh --install
#       also copy the binary and mail.js to /usr/local/libexec/tacitus-native
#       (root:wheel 755) and load the owner's launch agent
#       (~/Library/LaunchAgents/org.ea-system.tacitus-native.plist)
#
# The Info.plist goes into the binary's __TEXT,__info_plist section: macOS
# reads the usage descriptions for the Calendars, Contacts and Automation
# prompts from there, and refuses access without them. The binary is signed
# ad hoc. A permission grant is tied to the code signature, so a rebuild with
# changed code asks again; build once per release.
#
# Status: written on Linux, not yet run on a Mac.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd -P)"
OUT="$HERE/build"
DEST=/usr/local/libexec/tacitus-native
LABEL=org.ea-system.tacitus-native

[ "$(uname -s)" = Darwin ] || { echo "build.sh: macOS only" >&2; exit 1; }
command -v swiftc >/dev/null || { echo "build.sh: swiftc missing; run xcode-select --install" >&2; exit 1; }

mkdir -p "$OUT"
swiftc -O \
    -target "$(uname -m)-apple-macos14.0" \
    -o "$OUT/tacitus-native" \
    -Xlinker -sectcreate -Xlinker __TEXT -Xlinker __info_plist -Xlinker "$HERE/Info.plist" \
    "$HERE/main.swift"
codesign --force --sign - --identifier "$LABEL" "$OUT/tacitus-native"
otool -s __TEXT __info_plist "$OUT/tacitus-native" >/dev/null   # the plist is in the binary
echo "built $OUT/tacitus-native"

[ "${1:-}" = "--install" ] || exit 0
[ "$(id -u)" -eq 0 ] || { echo "build.sh --install: run with sudo" >&2; exit 1; }
OWNER="${SUDO_USER:?run with sudo from the owner account}"
OWNER_HOME="$(dscl . -read "/Users/$OWNER" NFSHomeDirectory | awk '{print $2}')"
QUEUE="$OWNER_HOME/.ea-private/native-queue"

install -d -o root -g wheel -m 755 "$DEST"
install -o root -g wheel -m 755 "$OUT/tacitus-native" "$DEST/tacitus-native"
install -o root -g wheel -m 644 "$HERE/mail.js" "$DEST/mail.js"
sudo -u "$OWNER" mkdir -p -m 700 "$QUEUE"
chmod 700 "$QUEUE"

# The owner's own launch agent: it loads only in the owner's login session
# (Aqua), where Mail runs and the prompts can show. A system-wide agent in
# /Library/LaunchAgents would also start in any other user's session. The
# plist lives in the owner's home, which ea-bot cannot write; the program it
# names is the root-owned copy above.
PLIST="$OWNER_HOME/Library/LaunchAgents/$LABEL.plist"
install -d -o "$OWNER" -m 755 "$OWNER_HOME/Library/LaunchAgents"
sed -e "s|@DEST@|$DEST|g" -e "s|@QUEUE@|$QUEUE|g" -e "s|@OWNER_HOME@|$OWNER_HOME|g" \
    "$HERE/$LABEL.plist.in" > "$PLIST.tmp"
plutil -lint "$PLIST.tmp" >/dev/null
chown "$OWNER" "$PLIST.tmp"; chmod 644 "$PLIST.tmp"; mv -f "$PLIST.tmp" "$PLIST"

OWNER_UID="$(id -u "$OWNER")"
launchctl bootout "gui/$OWNER_UID/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$OWNER_UID" "$PLIST"
echo "installed $DEST and $PLIST; next, as $OWNER: python3 preprocessor/native_accounts.py request-access"

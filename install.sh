#!/usr/bin/env bash
#
# The one-command front door to the EA system.
#
#   git clone https://github.com/arpg/tacitus.git ea-system && cd ea-system && ./install.sh
#   curl -fsSL https://raw.githubusercontent.com/arpg/tacitus/main/install.sh | bash
#
# Checks the prerequisites, installs Claude Code for the invoking user,
# clones the repository when it is not already here, installs the Python
# packages the way scripts/install/setup.sh does, and starts Claude Code in the checkout
# with the setup skill (.claude/skills/setup/SKILL.md) as the first
# message. The skill asks the setup questions one at a time and ends with
# scripts/ea-doctor.py green.
#
#   --check       print the prerequisite table and exit 0 (all present) or 1
#   --no-launch   do everything but start Claude Code; print the two
#                 commands to run by hand
#   EA_SRC        where to clone (default: $HOME/ea-system)
#   EA_REPO       the clone URL (default: the arpg/tacitus GitHub URL)
#   EA_PYTHON     the python3 to use when the first on PATH is too old
#
# Runs under bash 3.2 (macOS ships it): no arrays, no line-to-array
# reads, no case-folding expansions. Every step says when it is already done, so a
# second run moves on. The prerequisite check uses shell builtins only, so
# it can name what is missing on a machine with almost nothing on PATH.
# Everything is wrapped in main so a piped run reads the whole script
# before it runs any of it.

set -eu
# The PATH the user's shell had, for the hand-off hint (the script adds
# ~/.local/bin to its own).
ORIG_PATH="$PATH"

MIN_PY_MAJOR=3
MIN_PY_MINOR=10
# The newest minor the requirements publish wheels for (ortools is the
# last to catch up); a newer interpreter fails pip on a package build.
MAX_PY_MINOR=14
DEFAULT_REPO="https://github.com/arpg/tacitus.git"
CLAUDE_INSTALLER="https://claude.ai/install.sh"

MODE="install"
PLATFORM=""
PKG_HINT=""
PY=""
PY_VERSION=""
SRC=""
MISSING=0

say()  { printf '%s\n' "$*"; }
# A tool on PATH or in /usr/bin: the hints stay right on a bare PATH.
have() { command -v "$1" >/dev/null 2>&1 || [ -x "/usr/bin/$1" ]; }
row()  { printf '  %-8s %-12s %s\n' "$1" "$2" "$3"; }
die()  { printf 'install.sh: %s\n' "$*" >&2; exit 1; }

usage() {
    say "usage: install.sh [--check | --no-launch]"
    say "  --check      print the prerequisite table; exit 0 when all are present"
    say "  --no-launch  install everything, then print the commands instead of starting Claude Code"
}

# ── Platform ────────────────────────────────────────────────────────

detect_platform() {
    # $OSTYPE is set by bash itself, so this needs no uname on PATH.
    case "${OSTYPE:-}" in
        linux*)  PLATFORM="linux" ;;
        darwin*) PLATFORM="macos" ;;
        *) die "unsupported platform '${OSTYPE:-unknown}': the EA runs on Linux (WSL2 included) and macOS." ;;
    esac
}

# The one install command for a missing tool on this machine.
pkg_hint() {
    # $1: the apt/dnf/pacman package name; $2: the Homebrew name
    if [ "$PLATFORM" = "macos" ]; then
        if have brew || [ -x /opt/homebrew/bin/brew ] || [ -x /usr/local/bin/brew ]; then
            say "brew install $2"
        else
            say "install Homebrew (https://brew.sh), then: brew install $2"
        fi
        return
    fi
    if have apt-get; then
        say "sudo apt-get install -y $1"
    elif have dnf; then
        say "sudo dnf install -y $1"
    elif have pacman; then
        say "sudo pacman -S --needed $1"
    else
        say "install '$1' with your package manager"
    fi
}

# ── Python ──────────────────────────────────────────────────────────

# "Python 3.12.3" -> 0 when at least MIN_PY_MAJOR.MIN_PY_MINOR; sets PY_VERSION.
py_version_ok() {
    local out major minor rest
    out="$("$1" --version 2>&1)" || return 1
    case "$out" in
        Python\ *) ;;
        *) return 1 ;;
    esac
    out="${out#Python }"
    major="${out%%.*}"
    rest="${out#*.}"
    minor="${rest%%.*}"
    case "$major$minor" in
        *[!0-9]*) return 1 ;;
    esac
    PY_VERSION="$out"
    [ "$major" -eq "$MIN_PY_MAJOR" ] || return 1
    if [ "$minor" -gt "$MAX_PY_MINOR" ]; then
        PY_TOO_NEW="$1 ($out)"
        return 1
    fi
    [ "$minor" -ge "$MIN_PY_MINOR" ]
}

# The first python3 that is new enough: EA_PYTHON, then the python3 on
# PATH (what scripts/install/setup.sh and run-agent.sh call), then versioned names and
# Homebrew's paths. On macOS Apple's /usr/bin/python3 is 3.9, so the
# Homebrew one is usually the answer.
find_python() {
    local c path
    # Homebrew's python@3.1x keeps an unversioned python3 in libexec/bin,
    # so that directory can go first on PATH for the scripts that call
    # `python3`.
    for c in "${EA_PYTHON:-}" python3 \
             /opt/homebrew/opt/python@3.13/libexec/bin/python3 /opt/homebrew/opt/python@3.12/libexec/bin/python3 \
             /usr/local/opt/python@3.13/libexec/bin/python3 /usr/local/opt/python@3.12/libexec/bin/python3 \
             python3.14 python3.13 python3.12 python3.11 python3.10 \
             /opt/homebrew/bin/python3 /usr/local/bin/python3; do
        [ -n "$c" ] || continue
        path="$(command -v "$c" 2>/dev/null)" || continue
        if py_version_ok "$path"; then
            PY="$path"
            return 0
        fi
    done
    return 1
}

# ── The prerequisite table ──────────────────────────────────────────

check_prereqs() {
    local v path hint sudo_bin
    say "Prerequisites ($PLATFORM):"

    if path="$(command -v git 2>/dev/null)"; then
        v="$(git --version 2>/dev/null || true)"
        row ok git "${v#git version }"
    else
        if [ "$PLATFORM" = "macos" ]; then hint="xcode-select --install"; else hint="$(pkg_hint git git)"; fi
        row MISSING git "$hint"
        MISSING=$((MISSING + 1))
    fi

    if path="$(command -v curl 2>/dev/null)"; then
        v="$(curl --version 2>/dev/null || true)"
        v="${v#curl }"
        row ok curl "${v%% *}"
    else
        row MISSING curl "$(pkg_hint curl curl)"
        MISSING=$((MISSING + 1))
    fi

    if find_python; then
        row ok python3 "$PY_VERSION ($PY)"
        if "$PY" -m pip --version >/dev/null 2>&1; then
            row ok pip "$PY -m pip"
        else
            if [ "$PLATFORM" = "macos" ]; then
                hint="$PY -m ensurepip --upgrade"
            else
                hint="$(pkg_hint python3-pip python)"
            fi
            row MISSING pip "$hint"
            MISSING=$((MISSING + 1))
        fi
    else
        if [ "$PLATFORM" = "macos" ]; then
            hint="$(pkg_hint python3 python@3.12) (Apple's /usr/bin/python3 is too old)"
        else
            hint="$(pkg_hint "python3 python3-pip" python)"
        fi
        if [ -n "${PY_TOO_NEW:-}" ]; then
            hint="$PY_TOO_NEW is newer than the requirements support; $hint"
        fi
        row MISSING python3 "3.$MIN_PY_MINOR to 3.$MAX_PY_MINOR; $hint"
        MISSING=$((MISSING + 1))
    fi

    # The setup skill creates the ea-bot service account with sudo. A
    # password prompt later is fine; no sudo at all is not.
    sudo_bin="$(command -v sudo 2>/dev/null || true)"
    if [ -z "$sudo_bin" ] && [ -x /usr/bin/sudo ]; then
        sudo_bin=/usr/bin/sudo
    fi
    if [ -n "$sudo_bin" ]; then
        if "$sudo_bin" -n true 2>/dev/null; then
            row ok sudo "passwordless"
        else
            # A password prompt later is fine; the account must have the
            # right, which membership of the admin group shows without asking.
            case " $(id -nG 2>/dev/null) " in
                *" sudo "*|*" wheel "*|*" admin "*)
                    row ok sudo "prompts for your password when the setup skill creates the ea-bot account" ;;
                *)
                    row MISSING sudo "your account has no sudo rights: ask an administrator to add you to the sudo (or wheel) group"
                    MISSING=$((MISSING + 1)) ;;
            esac
        fi
    else
        row MISSING sudo "as root: $(pkg_hint sudo sudo | sed 's/^sudo //'), then add your account to the sudo (or wheel) group"
        MISSING=$((MISSING + 1))
    fi

    # Claude Code's native installer is a self-contained binary in
    # ~/.local/bin, so node is not a prerequisite.
    if path="$(command -v claude 2>/dev/null)"; then
        v="$(claude --version 2>/dev/null || true)"
        row ok "Claude Code" "${v%% *} ($path)"
    elif [ -x "$HOME/.local/bin/claude" ]; then
        row ok "Claude Code" "$HOME/.local/bin/claude (not on PATH; this script adds it)"
    else
        row todo "Claude Code" "not installed; this script runs: curl -fsSL $CLAUDE_INSTALLER | bash"
    fi

    if [ "$MISSING" -gt 0 ]; then
        say ""
        say "$MISSING prerequisite(s) missing. Install them with the commands above, then run install.sh again."
        return 1
    fi
    return 0
}

# ── Steps ───────────────────────────────────────────────────────────

install_claude() {
    export PATH="$HOME/.local/bin:$PATH"
    if command -v claude >/dev/null 2>&1; then
        say "Claude Code: already installed ($(command -v claude))"
        return 0
    fi
    say "Claude Code: installing for ${USER:-you} with the native installer..."
    # Downloaded to a file and run from it: `curl | bash </dev/null` would
    # hand bash /dev/null as its script and leave curl writing to a closed pipe.
    inst="$(mktemp "${TMPDIR:-/tmp}/claude-install.XXXXXX")"
    if ! curl -fsSL "$CLAUDE_INSTALLER" -o "$inst"; then
        rm -f "$inst"
        die "could not download the Claude Code installer from $CLAUDE_INSTALLER"
    fi
    if ! bash "$inst" </dev/null; then
        rm -f "$inst"
        die "the Claude Code installer failed; run it yourself: curl -fsSL $CLAUDE_INSTALLER | bash"
    fi
    rm -f "$inst"
    if ! command -v claude >/dev/null 2>&1; then
        die "the installer finished but 'claude' is not in $HOME/.local/bin; see https://claude.ai/claude-code"
    fi
    say "Claude Code: installed ($(command -v claude)). If a new shell cannot find it, add \$HOME/.local/bin to PATH."
}

is_checkout() { [ -f "$1/scripts/install/setup.sh" ] && [ -d "$1/preprocessor" ]; }

# SRC: the checkout this script is in, the checkout the user is standing
# in, or a fresh clone at EA_SRC.
find_or_clone_src() {
    local here top
    if [ -n "${BASH_SOURCE:-}" ] && [ -f "${BASH_SOURCE:-}" ]; then
        here="$(cd "$(dirname "${BASH_SOURCE:-}")" && pwd)"
        if is_checkout "$here"; then
            SRC="$here"
            say "Checkout: $SRC (this one)"
            return 0
        fi
    fi
    if is_checkout "$PWD"; then
        SRC="$PWD"
        say "Checkout: $SRC (the current directory)"
        return 0
    fi
    if top="$(git rev-parse --show-toplevel 2>/dev/null)" && is_checkout "$top"; then
        SRC="$top"
        say "Checkout: $SRC (the repository around the current directory)"
        return 0
    fi
    SRC="${EA_SRC:-$HOME/ea-system}"
    if is_checkout "$SRC"; then
        say "Checkout: already cloned at $SRC"
        return 0
    fi
    if [ -e "$SRC" ]; then
        die "$SRC exists and is not an ea-system checkout; set EA_SRC to another path"
    fi
    say "Checkout: cloning ${EA_REPO:-$DEFAULT_REPO} to $SRC..."
    if ! git clone "${EA_REPO:-$DEFAULT_REPO}" "$SRC" </dev/null; then
        die "clone failed. The repository is private: you need access, and either 'gh auth login' or an SSH key (then EA_REPO=git@github.com:arpg/tacitus.git)."
    fi
}

# The same step as scripts/install/setup.sh: the packages go into the python3 the scripts
# call. The import probe names the load-bearing packages, so a second
# run skips pip.
install_python_packages() {
    if "$PY" -c 'import msal, requests, bs4, googleapiclient, google_auth_oauthlib, slack_bolt, zulip, ortools, docx' >/dev/null 2>&1; then
        say "Python packages: already installed"
        return 0
    fi
    say "Python packages: installing requirements.txt into $PY..."
    # PEP 668: the flag goes only to an externally managed interpreter, and
    # pip runs once, so a failure shows its own error, not the refusal.
    pip_flags=""
    if "$PY" -c 'import os, sys, sysconfig; sys.exit(0 if os.path.exists(os.path.join(sysconfig.get_path("stdlib"), "EXTERNALLY-MANAGED")) else 1)' 2>/dev/null; then
        pip_flags="--break-system-packages"
    fi
    if ! "$PY" -m pip install $pip_flags -r "$SRC/requirements.txt" </dev/null; then
        die "pip could not install requirements.txt into $PY ($("$PY" -V 2>&1)). The lines above name the package; a Python newer than its published wheels is the usual cause: install Python 3.12 and run again."
    fi
    say "Python packages: installed"
}

# The scripts call `python3`; when the one that qualified is elsewhere,
# its directory goes first on PATH for the session this script starts.
prefer_python() {
    local on_path
    on_path="$(command -v python3 2>/dev/null || true)"
    if [ "$on_path" != "$PY" ]; then
        # A shim directory whose python3 is the chosen one: the directory
        # of a versioned binary (python3.12) may hold a different python3.
        shim="$HOME/.local/share/ea-system/bin"
        mkdir -p "$shim"
        ln -sf "$PY" "$shim/python3"
        export PATH="$shim:$PATH"
        say "python3: using $PY (first on PATH for this session through $shim)"
    fi
}

launch() {
    cd "$SRC"
    say ""
    if [ "$MODE" = "no-launch" ]; then
        say "Ready. To finish the install, run:"
        case ":$ORIG_PATH:" in
            *":$HOME/.local/bin:"*) ;;
            *) say "  export PATH=\"\$HOME/.local/bin:\$PATH\"   # and add it to your shell profile" ;;
        esac
        say "  cd $SRC"
        say '  claude "/setup"'
        exit 0
    fi
    say "Starting Claude Code with the setup skill..."
    # A piped run has the script on stdin; the session needs the terminal.
    if [ ! -t 0 ] && [ -r /dev/tty ]; then
        exec claude "/setup" </dev/tty
    fi
    exec claude "/setup"
}

main() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --check) MODE="check" ;;
            --no-launch) MODE="no-launch" ;;
            -h|--help) usage; exit 0 ;;
            *) usage >&2; exit 2 ;;
        esac
        shift
    done
    detect_platform
    if ! check_prereqs; then
        exit 1
    fi
    if [ "$MODE" = "check" ]; then
        exit 0
    fi
    say ""
    install_claude
    find_or_clone_src
    install_python_packages
    prefer_python
    launch
}

{
    main "$@"
    exit $?
}

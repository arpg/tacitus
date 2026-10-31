#!/usr/bin/env bash
# Entrypoint of the EA image.
#
# It starts as root, prepares the volumes (ea-bot space as ea-bot, the
# owner's private volume as root), then drops to the user the role needs:
# the owner for the chat bot, the mail mediator, the scheduler and owner
# commands; ea-bot for the agent shell. The split is the host
# install's split: the owner holds the tokens and the secrets, ea-bot runs
# Claude and the agent tools, and the code reaches ea-bot through
# `sudo -u ea-bot`. The image's sudoers lets the owner run any command as
# ea-bot without a password, because that is how the bot, run-agent.sh
# and owner-io.sh start Claude and the agent tools as ea-bot; it grants
# nothing else, and ea-bot has no sudo at all.
#
# Owner services run from /opt/ea-system, the copy whose parent root
# owns. The deployed copy in ea-bot's home is where Claude sessions run;
# its parent is ea-bot's, so an owner process never runs code from it
# (docs/SECURITY.md, owner services off the ea-bot tree).
#
# Roles:
#   init             prepare the volumes, render the install overlay and
#                    exit (the one-shot service)
#   setup [--answers FILE]
#                    the first configuration, in order: the config phases
#                    as ea-bot, ~/.ea/.env and the owner's secrets, the
#                    Claude login as ea-bot, the mail sign-in and the
#                    style baseline (tone) as the owner, the overlay, then
#                    the doctor (its exit status).
#                    --answers takes every answer from a file, asks
#                    nothing and skips the logins.
#   bot              the chat bot (assistant profile)
#   health           the bot container's health check: the web chat
#                    answers on the container's loopback (compose.yaml)
#   mediator         the mail mediator, the owner-side executor of the
#                    agent's safe mail operations
#   scheduler        supercronic with the profile's cron lines
#   agent [cmd ...]  a command as ea-bot in its deployed tree (default: bash)
#   owner [cmd ...]  a command as the owner with the EA environment loaded
#                    (default: bash)
# Anything else runs as given, as root.
set -euo pipefail

OWNER_USER="${EA_OWNER_USER:?EA_OWNER_USER names the owner user in the image}"
OWNER_HOME="$(getent passwd "$OWNER_USER" | cut -d: -f6)"
EA_BOT_HOME="$(getent passwd ea-bot | cut -d: -f6)"
EA_DIR="$EA_BOT_HOME/.ea"
PRIV_DIR="$OWNER_HOME/.ea-private"
CODE_DIR=/opt/ea-system
DEPLOY_DIR="$EA_BOT_HOME/ea-system"

log() { printf '[entrypoint] %s\n' "$*" >&2; }

# The image gives every new volume its directories, owners and modes, the
# ones scripts/install/setup-ea-user.sh sets on a host. A volume without them was made some
# other way (a bind mount, say); init stops there rather than chown a tree
# another user can change.
check_volume() {
    local owner
    owner="$(stat -c %U -- "$1" 2>/dev/null || echo '?')"
    [ "$owner" = "$2" ] && return 0
    log "$1 belongs to $owner, not $2: use the named volumes compose.yaml defines (docs/docker.md, Volumes)"
    return 1
}

prepare_volumes() {
    local ok=1
    check_volume "$EA_DIR" ea-bot || ok=0
    check_volume "$EA_BOT_HOME/.claude" ea-bot || ok=0
    check_volume "$PRIV_DIR" "$OWNER_USER" || ok=0
    [ "$ok" = 1 ] || exit 1

    # ea-bot space is prepared as ea-bot, so a symlink planted there reaches
    # only what ea-bot can reach already: the templates, copied-in config,
    # the profile, mail mediation and ea-bot's Claude settings.
    runuser -u ea-bot -- python3 "$CODE_DIR/deploy/docker/prepare_ea_dir.py" \
        --code-dir "$CODE_DIR" --profile "${EA_PROFILE:-email}" \
        --mediation "${EA_MAIL_MEDIATION:-1}"

    prepare_private_volume
}

# The owner's private volume: ea-bot cannot enter it, so root fixes it by
# name. Files `docker compose cp` brings in arrive as root. The web chat
# serves only from a 0700 directory of the owner's, with its settings
# (chat.json), store and pairing token at 0600 (docs/webchat.md, Identity
# and auth); the bot makes the store and the token itself.
prepare_private_volume() {
    local f
    chmod 0700 "$PRIV_DIR"
    install -d -o "$OWNER_USER" -g "$OWNER_USER" -m 0700 "$PRIV_DIR/logs"
    for f in "$PRIV_DIR/secrets.env" "$PRIV_DIR/bot.env" "$PRIV_DIR/crontab.extra" \
             "$PRIV_DIR/chat.json" "$PRIV_DIR/webchat-pair.token" "$PRIV_DIR/webchat-relay.json"; do
        if [ -f "$f" ] && [ ! -L "$f" ]; then
            chown "$OWNER_USER:$OWNER_USER" "$f"
            chmod 0600 "$f"
        fi
    done
}

# The install overlay (preprocessor/install_overlay.py): the install's own
# facts, rendered from the config in ea-data. ea-bot renders it into
# ~/.ea/config/CLAUDE.install.md, its own space. The owner, who owns the
# deployed tree, copies it there as CLAUDE.local.md, which Claude Code
# loads beside CLAUDE.md. The tree is in the image, not in a volume, so
# each container that runs Claude in it, or checks it, renders again and
# installs its own copy at start. A failure is logged and the role still
# starts; the prompts then carry the overlay themselves.
render_overlay() {
    runuser -u ea-bot -- env HOME="$EA_BOT_HOME" EA_DIR="$EA_DIR" \
        python3 "$CODE_DIR/preprocessor/install_overlay.py" render >/dev/null
}

install_overlay() {
    if ! render_overlay; then
        log "install overlay: render failed"
        return 0
    fi
    setpriv --reuid="$OWNER_USER" --regid="$OWNER_USER" --init-groups \
        env HOME="$OWNER_HOME" USER="$OWNER_USER" LOGNAME="$OWNER_USER" EA_DIR="$EA_DIR" \
        python3 "$CODE_DIR/preprocessor/install_overlay.py" install --tree "$DEPLOY_DIR" >/dev/null \
        || log "install overlay: could not install CLAUDE.local.md in $DEPLOY_DIR"
}

as_owner() {
    exec setpriv --reuid="$OWNER_USER" --regid="$OWNER_USER" --init-groups \
        env HOME="$OWNER_HOME" USER="$OWNER_USER" LOGNAME="$OWNER_USER" SHELL=/bin/bash \
            EA_DIR="$EA_DIR" "$@"
}

# The owner's EA environment: ~/.ea/.env (config keys only) and the
# owner-private secrets, read by the same loader the host install uses.
with_env() {
    local key value
    while IFS= read -r -d '' key && IFS= read -r -d '' value; do
        [[ "$key" =~ ^[A-Z][A-Z0-9_]*$ ]] && export "$key=$value"
    done < <("$CODE_DIR/scripts/owner-io.sh" env "$EA_DIR" "$PRIV_DIR")
    exec "$@"
}

# A command as the owner, like as_owner, that returns: the setup role runs
# several in sequence. With the EA environment (~/.ea/.env and the
# owner-private secrets) when the command reads the Graph or chat settings.
run_as_owner() {
    setpriv --reuid="$OWNER_USER" --regid="$OWNER_USER" --init-groups \
        env HOME="$OWNER_HOME" USER="$OWNER_USER" LOGNAME="$OWNER_USER" SHELL=/bin/bash \
            EA_DIR="$EA_DIR" "$@"
}

run_as_owner_with_env() {
    run_as_owner "$0" __with_env "$@"
}

# A command as ea-bot in its deployed tree, as the agent role runs one, with
# umask 027: a config file scripts/install/user_init.py writes lands at 0640, readable by
# the owner through group ea and by no one else.
run_as_ea_bot() {
    setpriv --reuid=ea-bot --regid=ea --init-groups \
        env HOME="$EA_BOT_HOME" USER=ea-bot LOGNAME=ea-bot SHELL=/bin/bash \
            EA_DIR="$EA_DIR" PATH="$EA_BOT_HOME/.local/bin:$PATH" \
            bash -c 'umask 027; cd "$1"; shift; exec "$@"' _ "$DEPLOY_DIR" "$@"
}

# ---------------------------------------------------------------------
# The setup role: the first configuration in one run (docs/docker.md,
# First run). Who writes what follows the host split: ea-bot writes its
# config and ~/.ea/.env, the owner writes the secrets, the push settings
# and the tokens. Every question goes through ask(), which with --answers
# reads the file by key (the keys of scripts/install/user_init.py --answers, listed in
# docs/install-testing.md, plus the ones named below) and never the
# terminal; the logins need a terminal and are skipped with --answers.
# ---------------------------------------------------------------------

SETUP_ANSWERS=""
# The copy of the answers ea-bot and the owner read: the secret keys are
# removed, so ea-bot never sees a client secret or a chat token.
SETUP_SHARED_ANSWERS=/run/ea-setup/answers.json
SETUP_SECRET_KEYS='["graph-client-secret", "slack-bot-token", "slack-app-token", "zulip-api-key", "claude-token"]'
# The config phases ea-bot runs first, in the setup skill's order. Not
# here: auth (the .env step below keeps the secret and the sign-in with
# the owner), tone (it reads sent mail with the owner's token and writes
# ea-bot's config, so it runs after the mail sign-in: setup_tone_phase),
# push (the owner's: push.json is owner-private), summary (prints).
# state runs after the chat questions, since it carries the chat block.
SETUP_PHASES="identity scheduling inbox autonomy groups deadlines style reimbursement vip"
SETUP_PROFILE=""
SETUP_GRAPH_METHOD=skip
SETUP_GRAPH_SIGNED_IN=0
SETUP_CLAUDE_LOGIN=0
SETUP_CHAT_ARGS=()
SETUP_PHASE_ARGS=()

say() { printf '\n== %s ==\n' "$*"; }

# answer KEY: the answers file's value for KEY, or nothing. A list joins
# with commas and a boolean becomes y or n, the way the prompts read them.
answer() {
    [ -n "$SETUP_ANSWERS" ] || return 0
    jq -r --arg k "$1" '
        if has($k) and .[$k] != null then
            (.[$k] | if type == "array" then join(", ")
                     elif type == "boolean" then (if . then "y" else "n" end)
                     else tostring end)
        else empty end' "$SETUP_ANSWERS"
}

# ask KEY QUESTION [DEFAULT]: the answer in REPLY, from the file or the
# terminal; an empty answer is the default.
ask() {
    local value=""
    if [ -n "$SETUP_ANSWERS" ]; then
        value="$(answer "$1")"
    elif [ -n "${3:-}" ]; then
        read -r -p "  $2 [$3]: " value
    else
        read -r -p "  $2: " value
    fi
    REPLY="${value:-${3:-}}"
}

# ask_secret KEY QUESTION: the same, with no echo on the terminal.
ask_secret() {
    local value=""
    if [ -n "$SETUP_ANSWERS" ]; then
        value="$(answer "$1")"
    else
        read -r -s -p "  $2: " value
        echo
    fi
    REPLY="$value"
}

# ask_yn KEY QUESTION y|n: true for a yes.
ask_yn() {
    local value=""
    if [ -n "$SETUP_ANSWERS" ]; then
        value="$(answer "$1")"
    elif [ "$3" = y ]; then
        read -r -p "  $2 [Y/n]: " value
    else
        read -r -p "  $2 [y/N]: " value
    fi
    value="${value:-$3}"
    case "${value,,}" in y*) return 0 ;; *) return 1 ;; esac
}

# A JSON object of KEY=VALUE pairs into ~/.ea/.env, written as ea-bot at
# mode 0640: ea-bot's file, which the owner's loader reads through group
# ea for the config keys (scripts/owner-io.sh env).
write_ea_env() {
    printf '%s' "$1" | run_as_ea_bot python3 "$DEPLOY_DIR/deploy/docker/env_file.py" \
        --path "$EA_DIR/.env" --mode 0640
}

# write_owner_secret KEY VALUE: one line into the owner-private
# secrets.env, mode 0600. The value travels on stdin, not in an argument.
write_owner_secret() {
    printf '%s\n' "$2" | jq -R --arg k "$1" '{($k): .}' \
        | run_as_owner python3 "$CODE_DIR/deploy/docker/env_file.py" \
            --path "$PRIV_DIR/secrets.env" --mode 0600
}

# A JSON object into the owner-private chat.json, mode 0600: the chat
# settings the bot trusts (preprocessor/owner_trust.py CHAT_FILE), written
# as the owner through the owner checks (deploy/docker/chat_seed.py).
write_owner_chat() {
    printf '%s' "$1" | run_as_owner python3 "$CODE_DIR/deploy/docker/chat_seed.py" write
}

# The web chat into the owner-private chat.json (docs/docker.md, Web chat):
# the container's bind and port, the identity header off, public_url the
# argument (empty keeps the one there). The owner runs the same command
# later through the owner service.
write_owner_web_chat() {
    run_as_owner python3 "$CODE_DIR/deploy/docker/chat_seed.py" web --public-url "$1"
}

setup_answers_copy() {
    [ -n "$SETUP_ANSWERS" ] || return 0
    install -d -m 0755 "$(dirname "$SETUP_SHARED_ANSWERS")"
    jq --argjson keys "$SETUP_SECRET_KEYS" 'delpaths([$keys[] | [.]])' "$SETUP_ANSWERS" \
        > "$SETUP_SHARED_ANSWERS"
    chmod 0644 "$SETUP_SHARED_ANSWERS"
    SETUP_PHASE_ARGS=(--answers "$SETUP_SHARED_ANSWERS")
}

setup_config_phases() {
    local phase
    say "The configuration questions (scripts/install/user_init.py, as ea-bot)"
    echo "  Each phase writes one file under ~/.ea/config. Enter takes the default."
    echo "  Not asked here: tone (it reads your sent mail after the mail sign-in) and"
    echo "  auth (the next step)."
    for phase in $SETUP_PHASES; do
        say "scripts/install/user_init.py --phase $phase"
        run_as_ea_bot python3 "$DEPLOY_DIR/scripts/install/user_init.py" "${SETUP_PHASE_ARGS[@]}" --phase "$phase"
    done
}

setup_graph_env() {
    local method pairs
    say "Outlook: the Microsoft Graph app registration (docs/SETUP.md, Microsoft Graph app registration)"
    echo "  The non-secret settings go in ~/.ea/.env (ea-bot's, mode 0640); a client"
    echo "  secret goes in the owner-private secrets.env (mode 0600)."
    echo "    1) confidential  auth-code flow + client secret (public client flows off)"
    echo "    2) device_code   MSAL device code (public client flows on)"
    echo "    4) skip          no app registration yet (docs/it-request.md asks IT for one)"
    ask graph-auth-method "Graph auth method" 1
    case "$REPLY" in
        1|confidential) method=confidential ;;
        2|device_code) method=device_code ;;
        3|interactive)
            echo "  The MSAL browser sign-in needs a browser in the container; device_code instead."
            method=device_code ;;
        4|skip) method=skip ;;
        *) log "Graph auth method '$REPLY' is not one of 1, 2, 4"; exit 2 ;;
    esac
    if [ "$method" = skip ]; then
        echo "  Skipped. docs/SETUP.md, Microsoft Graph app registration says what to put in ~/.ea/.env later."
        return 0
    fi
    ask graph-client-id "Application (client) ID"
    if [ -z "$REPLY" ]; then
        log "the Graph client id is required (graph-client-id)"
        exit 2
    fi
    pairs="$(jq -n --arg m "$method" --arg id "$REPLY" \
        '{GRAPH_AUTH_METHOD: $m, GRAPH_CLIENT_ID: $id}')"
    ask graph-tenant-id "Tenant ID (blank = multi-tenant 'common')" ""
    if [ -n "$REPLY" ]; then
        # graph_client reads the tenant as the sign-in authority.
        pairs="$(jq --arg t "$REPLY" \
            '. + {GRAPH_AUTHORITY: ("https://login.microsoftonline.com/" + $t)}' <<<"$pairs")"
    fi
    if [ "$method" = confidential ]; then
        ask graph-redirect-uri "Redirect URI (Web platform)" "http://localhost:8400/callback"
        pairs="$(jq --arg r "$REPLY" '. + {GRAPH_REDIRECT_URI: $r}' <<<"$pairs")"
        ask_secret graph-client-secret "Client secret value (not echoed)"
        if [ -n "$REPLY" ]; then
            write_owner_secret GRAPH_CLIENT_SECRET "$REPLY"
        else
            echo "  No secret given: put GRAPH_CLIENT_SECRET=... in $PRIV_DIR/secrets.env before the sign-in."
        fi
    fi
    write_ea_env "$pairs"
    SETUP_GRAPH_METHOD="$method"
}

setup_chat_env() {
    local site bot_email owner_id pairs url
    [ "$SETUP_PROFILE" = assistant ] || return 0
    say "Chat platform (assistant profile; docs/docker.md, Web chat)"
    echo "  The web chat is recommended: the bot serves it itself, with no chat server."
    echo "  Zulip runs as a server in this stack (--profile zulip); Slack keeps every"
    echo "  message on Slack's servers."
    ask chat-frontend "Chat platform: web, zulip or slack" web
    case "${REPLY,,}" in
        web)
            echo "  The address you open the chat at, behind your TLS proxy: https://<host>/chat."
            echo "  WEBCHAT_PUBLIC_URL in compose's .env is the default (docs/docker.md, Web chat)."
            ask webchat-public-url "The web chat's address (Enter: the default, or later)" \
                "${WEBCHAT_PUBLIC_URL:-}"
            url="$REPLY"
            if ! write_owner_web_chat "$url"; then
                log "the web chat settings were not written: give an https:// address"
                exit 2
            fi
            SETUP_CHAT_ARGS=(--chat web)
            echo "  Once the stack runs, the bot writes the pairing token; read it with"
            echo "    docker compose run --rm owner sh -c 'cat \"\$HOME/.ea-private/webchat-pair.token\"'"
            ;;
        zulip)
            echo "  deploy/zulip/README.md and scripts/zulip_bootstrap.py give these values;"
            echo "  inside compose the bot reaches the zulip profile at http://zulip.localhost."
            ask zulip-site "Zulip server URL" "http://zulip.localhost"
            site="$REPLY"
            ask zulip-bot-email "The EA bot's Zulip email" ""
            bot_email="$REPLY"
            ask zulip-owner-id "Your Zulip user id" ""
            owner_id="$REPLY"
            if [ -n "$bot_email" ] && [ -n "$owner_id" ]; then
                SETUP_CHAT_ARGS=(--chat zulip --zulip-site "$site" --zulip-bot-email "$bot_email"
                                 --zulip-owner-id "$owner_id")
                write_owner_chat "$(jq -n --arg s "${site%/}" --arg b "$bot_email" --arg o "$owner_id" \
                    '{frontend: "zulip", zulip: {site: $s, bot_email: $b, owner_user_id: $o}}')"
            else
                echo "  Bot email or user id missing: the chat block stays as the template has it."
                echo "  scripts/zulip_bootstrap.py prints it when the server is up."
            fi
            ask_secret zulip-api-key "The bot's Zulip API key (ZULIP_API_KEY; not echoed)"
            if [ -n "$REPLY" ]; then
                write_owner_secret ZULIP_API_KEY "$REPLY"
            else
                echo "  No key given: put ZULIP_API_KEY=... in $PRIV_DIR/secrets.env later."
            fi
            ;;
        slack)
            echo "  docs/SETUP.md, Slack: the app, its tokens, the private channel and your member id."
            ask slack-channel-id "EA Slack channel ID (C...)" ""
            pairs="$(jq -n --arg v "$REPLY" 'if $v == "" then {} else {EA_SLACK_CHANNEL_ID: $v} end')"
            ask slack-channel-name-env "EA Slack channel name" "ea-assistant"
            pairs="$(jq --arg v "$REPLY" '. + {EA_SLACK_CHANNEL_NAME: $v}' <<<"$pairs")"
            ask slack-owner-id "Your Slack member ID (U...)" ""
            pairs="$(jq --arg v "$REPLY" 'if $v == "" then . else . + {EA_SLACK_OWNER_ID: $v} end' <<<"$pairs")"
            write_ea_env "$pairs"
            ask_secret slack-bot-token "Slack bot token (xoxb-...; not echoed)"
            [ -z "$REPLY" ] || write_owner_secret SLACK_BOT_TOKEN "$REPLY"
            ask_secret slack-app-token "Slack app token (xapp-...; not echoed)"
            [ -z "$REPLY" ] || write_owner_secret SLACK_APP_TOKEN "$REPLY"
            ;;
        *)
            log "chat platform '$REPLY' is not web, zulip or slack"
            exit 2 ;;
    esac
}

setup_state_phase() {
    say "scripts/install/user_init.py --phase state"
    run_as_ea_bot python3 "$DEPLOY_DIR/scripts/install/user_init.py" "${SETUP_PHASE_ARGS[@]}" \
        "${SETUP_CHAT_ARGS[@]}" --phase state
}

setup_push_phase() {
    [ "$SETUP_PROFILE" != assistant ] || return 0
    say "scripts/install/user_init.py --phase push (as the owner: push.json is owner-private)"
    run_as_owner python3 "$CODE_DIR/scripts/install/user_init.py" "${SETUP_PHASE_ARGS[@]}" --phase push
}

claude_logged_in() {
    run_as_ea_bot test -e "$EA_BOT_HOME/.claude/.credentials.json" && return 0
    [ "$(run_as_ea_bot python3 -c '
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except (OSError, ValueError):
    d = {}
print("yes" if (d.get("env") or {}).get("CLAUDE_CODE_OAUTH_TOKEN") else "no")
' "$EA_BOT_HOME/.claude/settings.json")" = yes ]
}

# The token on stdin, into env.CLAUDE_CODE_OAUTH_TOKEN of ea-bot's Claude
# settings, as ea-bot (docs/macos-claude-binary.md does the same by hand).
write_claude_token() {
    run_as_ea_bot python3 -c '
import json, os, sys
path = sys.argv[1]
token = sys.stdin.read().strip()
try:
    d = json.load(open(path))
except (OSError, ValueError):
    d = {}
d.setdefault("env", {})["CLAUDE_CODE_OAUTH_TOKEN"] = token
fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
with os.fdopen(fd, "w") as f:
    f.write(json.dumps(d, indent=2) + "\n")
os.replace(path + ".tmp", path)
os.chmod(path, 0o600)
' "$EA_BOT_HOME/.claude/settings.json"
}

setup_claude_login() {
    say "Claude login for ea-bot (kept in the ea-bot-claude volume)"
    if claude_logged_in; then
        echo "  ea-bot is signed in already."
        SETUP_CLAUDE_LOGIN=1
        return 0
    fi
    if [ -n "$SETUP_ANSWERS" ]; then
        echo "  Skipped with --answers: a login needs a terminal. Later:"
        echo "    docker compose run --rm agent claude auth login"
        return 0
    fi
    echo "  ea-bot runs the agents on its own Claude login (a Max or Pro subscription)."
    echo "    1) browser   claude auth login: open the link it prints, sign in, paste the code back"
    echo "    2) token     claude setup-token: a long-lived token, kept in ea-bot's settings (renew yearly)"
    echo "    3) skip      later: docker compose run --rm agent claude auth login"
    ask claude-login "Claude login" 1
    case "$REPLY" in
        1|browser)
            run_as_ea_bot claude auth login || log "claude auth login did not finish"
            ;;
        2|token)
            run_as_ea_bot claude setup-token || log "claude setup-token did not finish"
            ask_secret claude-token "Paste the token it printed (Enter to skip; not echoed)"
            [ -z "$REPLY" ] || printf '%s' "$REPLY" | write_claude_token
            ;;
        *)
            echo "  Skipped."
            return 0 ;;
    esac
    if claude_logged_in; then
        echo "  ea-bot is signed in."
        SETUP_CLAUDE_LOGIN=1
    else
        log "no Claude login found for ea-bot; the doctor reports it"
    fi
}

setup_graph_signin() {
    [ "$SETUP_GRAPH_METHOD" != skip ] || return 0
    say "Microsoft Graph sign-in (as the owner; the token lands in ~/.ea/auth)"
    if [ -n "$SETUP_ANSWERS" ]; then
        echo "  Skipped with --answers: a sign-in needs you at the terminal. Later:"
        echo "    docker compose run --rm owner python3 /opt/ea-system/preprocessor/graph_client.py"
        return 0
    fi
    if ! ask_yn graph-sign-in-now "Sign in to Microsoft Graph now?" y; then
        echo "  Later: docker compose run --rm owner python3 /opt/ea-system/preprocessor/graph_client.py"
        return 0
    fi
    if [ "$SETUP_GRAPH_METHOD" = confidential ]; then
        echo "  Open the link it prints in a browser on this machine. The redirect comes back"
        echo "  to localhost:8400; the setup service runs on the host's network for that."
    else
        echo "  Open the link it prints in any browser and enter the code."
    fi
    if run_as_owner_with_env python3 "$CODE_DIR/preprocessor/graph_client.py"; then
        SETUP_GRAPH_SIGNED_IN=1
        if [ "$SETUP_GRAPH_METHOD" = confidential ]; then
            # The requested scopes follow what the app grants (docs/SETUP.md, Microsoft Graph app registration).
            run_as_owner_with_env python3 "$CODE_DIR/preprocessor/graph_client.py" --align --write \
                || log "scope alignment failed; later: graph_client.py --align --write as the owner"
        fi
    else
        log "the Graph sign-in did not finish; the doctor reports the missing token"
    fi
}

# The tone phase splits as the host's does: the owner holds the mail token
# and computes the style baseline into a directory only the owner and root
# can enter; ea-bot writes it into its config at umask 027.
setup_tone_phase() {
    [ "$SETUP_GRAPH_SIGNED_IN" = 1 ] || return 0
    say "scripts/install/user_init.py --phase tone (read as the owner, written as ea-bot)"
    local dir
    dir="$(mktemp -d /run/ea-setup-tone.XXXXXX)" || { log "tone: no temp dir"; return 0; }
    chown "$OWNER_USER" "$dir"
    if run_as_owner_with_env python3 "$CODE_DIR/scripts/install/user_init.py" "${SETUP_PHASE_ARGS[@]}" \
            --phase tone --out "$dir/baseline.json" && [ -s "$dir/baseline.json" ]; then
        run_as_ea_bot sh -c 'cat > "$1.new" && mv "$1.new" "$1"' _ \
            "$EA_DIR/config/writing-style-baseline.json" < "$dir/baseline.json" \
            || log "tone: ea-bot could not write writing-style-baseline.json"
    fi
    rm -rf "$dir"
}

setup_google_signin() {
    [ -z "$SETUP_ANSWERS" ] || return 0
    say "Google Calendar (optional; docs/SETUP.md, Google OAuth app)"
    if ! run_as_owner test -e "$EA_DIR/auth/google_client_secret.json"; then
        echo "  No ~/.ea/auth/google_client_secret.json yet. Put it there as the owner, then:"
        echo "    docker compose run --rm owner python3 /opt/ea-system/preprocessor/google_calendar_client.py --setup"
        return 0
    fi
    if ask_yn google-calendar-now "Set up Google Calendar now?" y; then
        run_as_owner_with_env python3 "$CODE_DIR/preprocessor/google_calendar_client.py" --setup \
            || log "the Google sign-in did not finish; the doctor warns on the tokens"
    fi
}

setup_doctor() {
    # The misses a container has at this point: the EA-Managed calendar
    # (docs/SETUP.md, Google OAuth app, in Google Calendar) and whichever login was skipped.
    local expect="EA-Managed calendar" rc=0
    [ "$SETUP_CLAUDE_LOGIN" = 1 ] || expect="$expect,ea-bot: Claude login"
    [ "$SETUP_GRAPH_SIGNED_IN" = 1 ] || expect="$expect,auth: msal_token_cache.json"
    say "ea-doctor.py --profile $SETUP_PROFILE --expect-missing \"$expect\""
    run_as_owner_with_env python3 "$CODE_DIR/scripts/ea-doctor.py" \
        --profile "$SETUP_PROFILE" --expect-missing "$expect" || rc=$?
    if [ "$rc" = 0 ]; then
        say "setup done: docker compose up -d starts the stack (docs/docker.md)"
    else
        say "setup: the doctor exited $rc; fix what it names, then run setup again"
    fi
    return "$rc"
}

setup_role() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --answers) SETUP_ANSWERS="${2:?--answers needs a file}"; shift 2 ;;
            --answers=*) SETUP_ANSWERS="${1#--answers=}"; shift ;;
            *) echo "usage: entrypoint.sh setup [--answers FILE]" >&2; exit 2 ;;
        esac
    done
    if [ -n "$SETUP_ANSWERS" ]; then
        if ! jq -e 'type == "object"' "$SETUP_ANSWERS" >/dev/null 2>&1; then
            log "$SETUP_ANSWERS is not a JSON object of answers (docs/install-testing.md)"
            exit 2
        fi
    elif [ ! -t 0 ]; then
        log "setup asks its questions on a terminal: docker compose run --rm setup, or give --answers FILE"
        exit 2
    fi
    prepare_volumes
    setup_answers_copy
    SETUP_PROFILE="$(run_as_ea_bot python3 -c '
import json, sys
try:
    print(json.load(open(sys.argv[1])).get("profile") or "email")
except (OSError, ValueError, AttributeError):
    print("email")
' "$EA_DIR/config/ea-config.json")"
    say "EA setup in the container: profile $SETUP_PROFILE (EA_PROFILE in compose's .env)"
    setup_config_phases
    setup_graph_env
    setup_chat_env
    setup_state_phase
    setup_push_phase
    setup_claude_login
    setup_graph_signin
    setup_tone_phase
    setup_google_signin
    say "The install overlay: rendered as ea-bot, installed as the owner"
    install_overlay
    setup_doctor
}

role="${1:-}"
[ $# -gt 0 ] && shift

case "$role" in
    init)
        prepare_volumes
        render_overlay || log "install overlay: render failed"
        log "volumes ready"
        ;;
    setup)
        setup_role "$@"
        ;;
    bot)
        # The bot runs from the owner's copy; the Claude sessions it starts
        # run in ea-bot's deployed tree, which is why the overlay goes there.
        install_overlay
        cd "$CODE_DIR"
        as_owner "$0" __with_env python3 "$CODE_DIR/ea-chat-bot.py"
        ;;
    health)
        # compose.yaml's health check of the bot container, as the owner,
        # who reads the chat block; it asks the web chat on loopback.
        as_owner python3 "$CODE_DIR/deploy/docker/healthcheck.py"
        ;;
    mediator)
        cd "$CODE_DIR"
        as_owner "$0" __with_env python3 "$CODE_DIR/preprocessor/mail_mediator.py"
        ;;
    scheduler)
        cd /
        as_owner bash -euc '
            umask 077
            python3 "$1/deploy/docker/crontab.py" --code-dir "$1" > "$HOME/.ea-private/crontab.new"
            mv "$HOME/.ea-private/crontab.new" "$HOME/.ea-private/crontab"
            exec supercronic "$HOME/.ea-private/crontab"
        ' _ "$CODE_DIR"
        ;;
    agent)
        install_overlay
        cd "$DEPLOY_DIR"
        exec setpriv --reuid=ea-bot --regid=ea --init-groups \
            env HOME="$EA_BOT_HOME" USER=ea-bot LOGNAME=ea-bot SHELL=/bin/bash \
                EA_DIR="$EA_DIR" PATH="$EA_BOT_HOME/.local/bin:$PATH" \
                bash -c 'umask 002; exec "$@"' _ "${@:-bash}"
        ;;
    owner)
        install_overlay
        cd "$OWNER_HOME"
        as_owner "$0" __with_env "${@:-bash}"
        ;;
    __with_env)
        with_env "$@"
        ;;
    "")
        echo "usage: entrypoint.sh init|setup [--answers FILE]|bot|health|mediator|scheduler|agent [cmd]|owner [cmd]" >&2
        exit 2
        ;;
    *)
        exec "$role" "$@"
        ;;
esac

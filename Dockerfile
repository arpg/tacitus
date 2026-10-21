# The EA in one image, for compose.yaml (see docs/docker.md).
#
# Two users, as on a host install (scripts/install/setup-ea-user.sh):
#   owner   runs the chat bot, the mail mediator and the cron jobs; holds the
#           tokens and the secrets.
#   ea-bot  runs Claude Code and the agent tools; the owner reaches it only
#           through `sudo -u ea-bot`.
# Every download is pinned: the base image by digest, the Python packages by
# exact version (deploy/docker/requirements.lock), supercronic and Claude Code
# by version and checksum.

ARG PYTHON_IMAGE=python:3.14.4-slim-trixie@sha256:2ca02f32b4d9d893863367ce07ec1972819f476dd38d8612f2a9cb6a41cbb727
FROM ${PYTHON_IMAGE}

ARG OWNER_USER=owner
ARG OWNER_UID=1000
ARG OWNER_GID=1000
ARG EA_GID=1001
ARG EA_BOT_UID=999
# Claude Code: the release manifest at
# https://downloads.claude.ai/claude-code-releases/<version>/manifest.json
# publishes the SHA-256 of each platform's binary.
ARG CLAUDE_CODE_VERSION=2.1.289
ARG CLAUDE_CODE_SHA256_X64=a186b99e4a9c88366cd49df2f7dad56c61fc306ef0140b19ee64b7c42a8d1348
ARG CLAUDE_CODE_SHA256_ARM64=d100d5e41dcbee220c80d3a3099292e4b5a508b57cafd181856efedf29f84f28
# supercronic: the release notes publish the SHA-1 of each binary.
ARG SUPERCRONIC_VERSION=v0.2.49
ARG SUPERCRONIC_SHA1_AMD64=e63c11a9726b775a6a11801e81af4f3fb926aa68
ARG SUPERCRONIC_SHA1_ARM64=0b6c5bb743e0b0dafed1132198c81807927ac413
# 1 installs Playwright's Chromium (When2Meet polls, JavaScript pages, email
# to PDF, reimbursement portals). 0 builds a smaller image without it.
ARG WITH_BROWSER=1
ARG TARGETARCH

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright \
    EA_OWNER_USER=${OWNER_USER}

# Debian packages come from the trixie archive at build time. The base
# image keeps its interpreter at /usr/local/bin/python3 and installs no
# Debian python3; the host tools name /usr/bin/python3 (the doctor's
# ea-bot probes, run-plugin.sh, the sudoers render line), so the image
# gives that path to the same interpreter rather than a second one
# without the pinned packages.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ca-certificates curl git jq less poppler-utils procps rsync sudo tmux tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && ln -s /usr/local/bin/python3 /usr/bin/python3

# TARGETARCH comes from BuildKit; the classic builder leaves it empty, so
# the architecture falls back to the base image's own.
RUN set -eu; \
    arch="${TARGETARCH:-$(dpkg --print-architecture)}"; \
    case "$arch" in \
        amd64) sum="$SUPERCRONIC_SHA1_AMD64" ;; \
        arm64) sum="$SUPERCRONIC_SHA1_ARM64" ;; \
        *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSLo /usr/local/bin/supercronic \
        "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-${arch}"; \
    echo "${sum}  /usr/local/bin/supercronic" | sha1sum -c -; \
    chmod 0755 /usr/local/bin/supercronic

# The users and the one sudo rule: the owner may run any command as ea-bot
# without a password, which is how the bot, run-agent.sh and owner-io.sh
# start Claude and the agent tools as ea-bot; nothing else is granted and
# ea-bot has no sudo. The owner is in group ea, so it can read ea-bot's
# space; ea-bot cannot enter the owner's home.
RUN set -eu; \
    # The IDs come from the host (compose passes the builder's own), so any
    # of them may already be taken in the base image or by one another:
    # an existing owner group is reused, and the ea group takes the next
    # free number when EA_GID is in use (it is used by name everywhere).
    getent group "$OWNER_GID" >/dev/null || groupadd -g "$OWNER_GID" "$OWNER_USER"; \
    if getent passwd "$OWNER_UID" >/dev/null; then \
        echo "OWNER_UID $OWNER_UID is taken in the base image" >&2; exit 1; fi; \
    useradd -m -u "$OWNER_UID" -g "$OWNER_GID" -s /bin/bash "$OWNER_USER"; \
    if getent group "$EA_GID" >/dev/null; then groupadd ea; else groupadd -g "$EA_GID" ea; fi; \
    usermod -aG ea "$OWNER_USER"; \
    useradd -r -m -u "$EA_BOT_UID" -g ea -s /usr/sbin/nologin ea-bot; \
    chmod 0750 "/home/$OWNER_USER" /home/ea-bot; \
    printf '%s ALL=(ea-bot) NOPASSWD: ALL\nDefaults:%s env_keep += "PYTHONPATH"\n' \
        "$OWNER_USER" "$OWNER_USER" > /etc/sudoers.d/ea-bot; \
    chmod 0440 /etc/sudoers.d/ea-bot; \
    visudo -cf /etc/sudoers.d/ea-bot

# The directories each named volume starts with. Docker copies them, with
# their owners and modes, into a new volume, so no step has to chown a tree
# ea-bot can change. The modes are the ones scripts/install/setup-ea-user.sh sets on a host.
RUN set -eu; \
    ea=/home/ea-bot/.ea; \
    install -d -o ea-bot -g ea -m 0750 "$ea" "$ea/config"; \
    for d in state logs receipts uploads letters templates exports plugins; do \
        install -d -o ea-bot -g ea -m 2775 "$ea/$d"; \
    done; \
    install -d -o ea-bot -g ea -m 2770 "$ea/state/mail-queue"; \
    install -d -o "$OWNER_USER" -g ea -m 0750 "$ea/auth"; \
    install -d -o ea-bot -g ea -m 0710 /home/ea-bot/.claude; \
    install -d -o "$OWNER_USER" -g "$OWNER_GID" -m 0700 \
        "/home/$OWNER_USER/.ea-private" "/home/$OWNER_USER/.ea-private/logs"

COPY deploy/docker/requirements.lock /tmp/requirements.lock
RUN pip install --no-deps -r /tmp/requirements.lock \
 && pip check \
 && rm /tmp/requirements.lock

RUN set -eu; \
    install -d -m 0755 /opt/ms-playwright; \
    if [ "$WITH_BROWSER" = 1 ]; then \
        apt-get update; \
        python3 -m playwright install --with-deps chromium; \
        rm -rf /var/lib/apt/lists/*; \
        chmod -R a+rX /opt/ms-playwright; \
    fi; \
    for home in "/home/$OWNER_USER" /home/ea-bot; do \
        install -d -m 0755 "$home/.cache"; \
        ln -s /opt/ms-playwright "$home/.cache/ms-playwright"; \
    done; \
    chown -R "$OWNER_USER:$OWNER_GID" "/home/$OWNER_USER/.cache"; \
    chown -R ea-bot:ea /home/ea-bot/.cache

# Claude Code for ea-bot, at the pinned version. The launcher in
# ~/.local/bin points Claude Code at ~/.claude for its config file as well
# as its login, so both live in the ea-bot-claude volume.
RUN set -eu; \
    arch="${TARGETARCH:-$(dpkg --print-architecture)}"; \
    case "$arch" in \
        amd64) platform=linux-x64; sum="$CLAUDE_CODE_SHA256_X64" ;; \
        arm64) platform=linux-arm64; sum="$CLAUDE_CODE_SHA256_ARM64" ;; \
        *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSLo /tmp/claude \
        "https://downloads.claude.ai/claude-code-releases/${CLAUDE_CODE_VERSION}/${platform}/claude"; \
    echo "${sum}  /tmp/claude" | sha256sum -c -; \
    install -d -o ea-bot -g ea -m 0755 /home/ea-bot/.local/bin /home/ea-bot/.local/share/claude/versions; \
    install -o ea-bot -g ea -m 0755 /tmp/claude "/home/ea-bot/.local/share/claude/versions/${CLAUDE_CODE_VERSION}"; \
    rm -f /tmp/claude; \
    printf '#!/bin/sh\n# ea-bot'"'"'s Claude Code launcher in the container image.\nexport CLAUDE_CONFIG_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"\nexec /home/ea-bot/.local/share/claude/versions/%s "$@"\n' \
        "$CLAUDE_CODE_VERSION" > /home/ea-bot/.local/bin/claude; \
    chmod 0755 /home/ea-bot/.local/bin/claude; \
    chown -R ea-bot:ea /home/ea-bot/.local; \
    su -s /bin/sh ea-bot -c '/home/ea-bot/.local/bin/claude --version'

# The code: /opt/ea-system for the owner's jobs and services, and the
# deployed copy in ea-bot's home, where the chat lanes run Claude. Both
# belong to the owner and no one else can write them (docs/SECURITY.md,
# owner trust rules); the deployed copy is readable by group ea. The
# install overlay (CLAUDE.local.md) is not in the image: the entrypoint
# renders it from the config and installs it in the deployed copy at start.
COPY --chown=${OWNER_UID}:${OWNER_GID} . /opt/ea-system
RUN set -eu; \
    chmod -R u+rwX,go+rX,go-w /opt/ea-system; \
    cp -a /opt/ea-system /home/ea-bot/ea-system; \
    chown -R "$OWNER_USER:ea" /home/ea-bot/ea-system; \
    chmod -R u+rwX,g+rX,g-w,o-rwx /home/ea-bot/ea-system; \
    ln -s /home/ea-bot/.ea "/home/$OWNER_USER/.ea"; \
    chown -h "$OWNER_USER:$OWNER_GID" "/home/$OWNER_USER/.ea"; \
    install -m 0755 /opt/ea-system/deploy/docker/entrypoint.sh /usr/local/bin/ea-entrypoint

# compose.yaml mounts named volumes on ~ea-bot/.ea, ~owner/.ea-private and
# ~ea-bot/.claude; the image declares none, so a bare `docker run` leaves
# no anonymous volumes behind.
ENTRYPOINT ["/usr/local/bin/ea-entrypoint"]
CMD ["init"]

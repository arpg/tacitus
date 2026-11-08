#!/usr/bin/env bash
# tests/install/docker-smoke.sh: the install in throwaway containers.
#
# Two proofs from the EA image (docs/docker.md), neither touching this
# machine's install:
#
#   setup  the container path: the entrypoint's setup role with
#          tests/install/answers-reviewer.json, against three named volumes
#          made for the run, the way `docker compose run --rm setup
#          --answers FILE` runs it. The role runs the config phases as
#          ea-bot, writes ~/.ea/.env, skips the logins (they need a
#          terminal) and ends with the doctor; the doctor's exit status is
#          the result, and its summary line is printed.
#   host   the host path of tests/install/smoke.sh: a container from the
#          image gets a fresh owner user with passwordless sudo and this
#          tree as a git repository in that user's home, and runs the
#          smoke as that user. The image already holds Claude Code for
#          ea-bot, so that download does not happen; pip still reaches the
#          index for requirements.txt. The image's prepared ea-bot space
#          and deployed copy are removed first, since a host has neither
#          before the installers run.
#
#   bash tests/install/docker-smoke.sh              # both, image ea-system:worktree-check
#   bash tests/install/docker-smoke.sh setup        # one of them: setup or host
#   EA_SMOKE_IMAGE=ea-system:latest bash tests/install/docker-smoke.sh
#
# Settings: EA_SMOKE_IMAGE, EA_SMOKE_PROFILE (the setup role's mode,
# default reviewer), EA_SMOKE_ANSWERS (the answers file), EA_SMOKE_OWNER (the
# host part's owner user). Every container and volume the script makes
# is removed at the end, whatever the result.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
IMAGE="${EA_SMOKE_IMAGE:-ea-system:worktree-check}"
OWNER="${EA_SMOKE_OWNER:-smoke}"
PROFILE="${EA_SMOKE_PROFILE:-reviewer}"
ANSWERS="${EA_SMOKE_ANSWERS:-$REPO/tests/install/answers-reviewer.json}"
PART="${1:-all}"
NAME="ea-install-smoke-$$"
VOLUMES=("$NAME-data" "$NAME-private" "$NAME-claude")

cleanup() {
    docker rm -f "$NAME" "$NAME-setup" >/dev/null 2>&1 || true
    docker volume rm -f "${VOLUMES[@]}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

log() { printf '\n=== docker-smoke: %s ===\n' "$*"; }
die() { echo "docker-smoke: $*" >&2; exit 2; }

case "$PART" in
    setup|host|all) ;;
    *) die "usage: docker-smoke.sh [setup|host|all]" ;;
esac
[ -f "$ANSWERS" ] || die "answers file $ANSWERS is missing"
# A bind mount takes an absolute path.
ANSWERS="$(cd "$(dirname "$ANSWERS")" && pwd)/$(basename "$ANSWERS")"

# The owner user the image was built with (OWNER_USER): the private volume
# mounts on that user's home, as compose.yaml mounts it.
image_owner() {
    docker image inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$IMAGE" \
        | sed -n 's/^EA_OWNER_USER=//p'
}

smoke_setup() {
    local owner v out rc=0
    owner="$(image_owner)"
    [ -n "$owner" ] || die "$IMAGE names no owner user (EA_OWNER_USER)"
    log "setup role: mode $PROFILE, answers $ANSWERS, owner $owner, volumes ${VOLUMES[*]}"
    for v in "${VOLUMES[@]}"; do
        docker volume create "$v" >/dev/null
    done
    out="$(mktemp)"
    docker run --name "$NAME-setup" \
        -v "${VOLUMES[0]}:/home/ea-bot/.ea" \
        -v "${VOLUMES[1]}:/home/$owner/.ea-private" \
        -v "${VOLUMES[2]}:/home/ea-bot/.claude" \
        -v "$ANSWERS:/answers.json:ro" \
        -e "EA_PROFILE=$PROFILE" -e TZ=UTC \
        "$IMAGE" setup --answers /answers.json 2>&1 | tee "$out" || rc=$?
    docker rm -f "$NAME-setup" >/dev/null 2>&1 || true
    log "doctor: $(grep -E '^[0-9]+ ok, [0-9]+ warn, [0-9]+ FAIL' "$out" | tail -1)"
    if ! grep -q '^no unexpected FAIL$' "$out"; then
        echo "docker-smoke: the doctor reported an unexpected FAIL (or did not run)" >&2
        [ "$rc" != 0 ] || rc=1
    fi
    rm -f "$out"
    log "setup role exit $rc"
    return "$rc"
}

smoke_host() {
    log "host path: smoke.sh as user $OWNER in a container from $IMAGE"
    docker run -d --name "$NAME" --entrypoint sleep "$IMAGE" infinity >/dev/null

    docker exec -i -e "OWNER=$OWNER" "$NAME" bash -s <<'ROOT'
set -euo pipefail
rm -rf /home/ea-bot/.ea /home/ea-bot/ea-system
useradd -m -s /bin/bash "$OWNER"
printf '%s ALL=(ALL) NOPASSWD: ALL\n' "$OWNER" > "/etc/sudoers.d/$OWNER"
chmod 440 "/etc/sudoers.d/$OWNER"
install -d -o "$OWNER" -g "$OWNER" "/home/$OWNER/ea-system"
ROOT

    # The tree: every file git tracks or would track (ignored files stay
    # out), streamed as a tar into the owner's home.
    (cd "$REPO" && git ls-files -z --cached --others --exclude-standard \
        | tar --null -T - --ignore-failed-read -cf -) \
        | docker cp - "$NAME:/home/$OWNER/ea-system"

    docker exec -i -e "OWNER=$OWNER" "$NAME" bash -s <<'ROOT'
set -euo pipefail
chown -R "$OWNER:$OWNER" "/home/$OWNER/ea-system"
su - "$OWNER" -c 'cd ~/ea-system && git init -q && git add -A \
    && git -c user.name=smoke -c user.email=smoke@example.invalid commit -qm "smoke tree"'
ROOT

    docker exec -i "$NAME" su - "$OWNER" -c 'bash ~/ea-system/tests/install/smoke.sh'
    docker rm -f "$NAME" >/dev/null 2>&1 || true
}

case "$PART" in
    setup) smoke_setup ;;
    host) smoke_host ;;
    all) smoke_setup; smoke_host ;;
esac

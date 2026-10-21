# shellcheck shell=bash
# tree-custody.sh: what the root installers do to the resident's trees and
# to the owner's auth directory. Sourced by scripts/install/setup-ea-user.sh,
# scripts/install/setup-ea-user-macos.sh and deploy/enable-mail-mediation.sh; it defines
# functions only and changes nothing when sourced.
#
# Everything under the resident's home is ea-bot space (docs/SECURITY.md
# "Owner trust rules"): ea-bot can rename or replace any entry there, so a
# path a root step checked can name another object, or a link, by the time
# the step acts on it. Four rules follow.
#
# * The auth directory and everything in it belong to the owner at every
#   moment, and root adopts nothing. auth_lock takes the directory only when
#   the owner or root already owns it. Any other owner means the real one
#   was renamed away and another user made its own, so the run stops with
#   the recovery steps (tc_auth_refused). The directory is then handed over
#   first (the owner, group ea, 0750), so ea-bot can no longer add, remove
#   or rename an entry in it, and only then are the entries treated, by
#   name inside the directory it entered and checked. An entry the owner
#   does not own, or a file with more than one link, is never chowned: it
#   moves into an owner-only quarantine directory in auth/ with a note
#   (tc_quarantine). The owner checks accept any file the owner owns, so a
#   chown would make another user's file (a scheduled-send key it knows, a
#   token it chose) a trust anchor. Of the owner's own entries a link is
#   left as it is (the owner checks refuse one), a credential becomes the
#   owner's alone, any other file keeps group read without group write, and
#   a directory (an old browser profile) becomes the owner's alone when
#   everything under it is the owner's already, else it is quarantined
#   whole. Modes are only tightened. The first move of a fresh install is
#   no exception: its auth/ comes from the owner's home, so its entries are
#   files the owner made, and only those keep their place.
# * A file's other names are outside every check made on this one. macOS
#   has no fs.protected_hardlinks, so there ea-bot can link any file it can
#   reach, and a Linux host may have the setting off. So each walk here
#   leaves out, or quarantines, a non-directory with more than one link, on
#   both systems.
# * Root changes no mode in a tree ea-bot controls. Mode and group fixes
#   run as ea-bot (tc_as_user), which can change only what it owns, so a
#   link swapped in mid-walk gains it nothing.
# * Root changes ownership in ea-bot's state tree only on the run that moves
#   it in from the owner's home (tc_give_moved_tree). Until that walk
#   reaches a directory, the directory is still the owner's, and the walk
#   reaches a directory only after its entries (find -depth), so ea-bot can
#   change nothing the walk has still to visit. Each chown runs in the
#   directory find opened (-execdir) and changes a link itself (-h). A file
#   with more than one link is left out, since its other name may be a file
#   outside the tree. On a later run root hands nothing to ea-bot; an entry
#   ea-bot does not own is reported and left as it is.

# The physical path of a directory, or nothing.
tc_phys() { ( cd -P -- "$1" 2>/dev/null && pwd -P ); }

# tc_enter PHYS: cd into PHYS (a physical path) and prove that no link led
# there. The directory entered stays the same one however its name is
# changed afterwards. Use it in a subshell.
tc_enter() { cd -P -- "$1" 2>/dev/null && [ "$(pwd -P)" = "$1" ]; }

# tc_as_user USER CMD...: run CMD as USER from /, with this shell's stdin;
# a failure warns and returns 1, so a step on a tree that changed under it
# never stops the install.
tc_as_user() {
    local user=$1; shift
    if ! ( cd / && sudo -u "$user" "$@" ); then
        echo "WARNING: as $user: '$*' did not finish (see above)" >&2
        return 1
    fi
}

# The uid that owns PATH itself (a link not followed), and its link count;
# ls -n reads the same on GNU and BSD.
tc_uid()   { ls -lnd -- "$1" 2>/dev/null | awk 'NR == 1 {print $3}'; }
tc_links() { ls -lnd -- "$1" 2>/dev/null | awk 'NR == 1 {print $2}'; }

# tc_foreign_in DIR UID: the entries at or under DIR (links not followed)
# that UID does not own, and the non-directories with more than one link.
tc_foreign_in() {
    find -P "$1" \( ! -uid "$2" -o \( ! -type d -links +1 \) \) -print 2>/dev/null
}

# tc_auth_refused SHOWN UID OWNER: why the run stops at an auth directory
# the owner does not own, and the safe way back. Nothing in that directory
# is trusted, since a token or key in it may be another user's.
tc_auth_refused() {
    cat >&2 <<MSG
ERROR: $1 is owned by uid $2, not by $3 or root. Its files may be another
user's (a scheduled-send key it knows, a token it chose), so this run adopts
none of them and changed nothing in it. To recover:
  1. As root, move it aside and keep it only to inspect:
       mv -- '$1' '$1.untrusted-$(date -u +%Y%m%d%H%M%S)'
  2. As root, make a new one for $3:
       install -d -o '$3' -g ea -m 0750 -- '$1'
  3. As $3, put token files back only from your own backup, or sign in
     again: python3 preprocessor/graph_client.py, and
     python3 preprocessor/google_calendar_client.py --setup per account.
  4. As $3, make a new scheduled-send key (docs/SETUP.md "The deferred-send
     signing key"). A deferred send signed with the old key is held and
     needs a new card.
  5. Run this script again.
Never chown a file from the old directory to $3.
MSG
}

# tc_quarantine ENTRY WHY: move auth/ENTRY (in the current, locked auth
# directory) into this run's quarantine directory, owner-only, and say why
# in its NOTE.txt. Root renames it: both names are in the owner's closed
# directory, and the owner could not move another user's directory to a new
# parent. Uses the caller's owner, ogroup and qdir.
tc_quarantine() {
    local f=$1 why=$2
    if [ -z "$qdir" ]; then
        qdir=".quarantine-$(date -u +%Y%m%d%H%M%S)-$$"
        mkdir -m 0700 -- "$qdir" && chown "$owner:$ogroup" "$qdir" || return 1
    fi
    mv -- "$f" "$qdir/" || return 1
    ( umask 077; printf '%s: %s\n' "${f#./}" "$why" >> "$qdir/NOTE.txt" ) || return 1
    chown "$owner:$ogroup" "$qdir/NOTE.txt" || return 1
    echo "WARNING: auth/${f#./} $why; moved to auth/$qdir (see NOTE.txt there)" >&2
}

# tc_quarantine_close: end this run's quarantine note with what to do.
tc_quarantine_close() {
    [ -n "$qdir" ] || return 0
    ( umask 077
      printf '%s\n' "" \
        "These entries were in auth/ but were not the owner's own files, or had" \
        "other names elsewhere. Their contents are not trusted: never move one" \
        "back or chown it. Put a token back only from your own backup, or sign" \
        "in again; make a new scheduled-send key (docs/SETUP.md \"The deferred-send" \
        "signing key\"). Then delete this directory." >> "$qdir/NOTE.txt" ) || return 1
    echo "WARNING: auth/$qdir holds what was not the owner's; make a new scheduled-send key and sign in again for any token among it" >&2
}

# auth_dir_lock_here OWNER OWNER_UID DIR_GROUP SHOWN: the current directory
# is the auth directory at SHOWN. Stop (status 3, tc_auth_refused) unless
# OWNER_UID or root owns it; then give it to OWNER:DIR_GROUP, mode 0750.
auth_dir_lock_here() {
    local uid
    uid="$(tc_uid .)"
    if [ "$uid" != "$2" ] && [ "$uid" != 0 ]; then
        tc_auth_refused "$4" "${uid:-unknown}" "$1"
        return 3
    fi
    chmod go-w . && chown "$1:$3" . && chmod 0750 . || return 1
}

# auth_entries_lock_here OWNER OWNER_UID DIR_GROUP OWNER_GROUP: in the
# locked auth directory (the current one), tighten the owner's entries and
# quarantine every other one.
auth_entries_lock_here() {
    local owner=$1 ouid=$2 dgroup=$3 ogroup=$4 qdir='' f uid n
    # The directory is the owner's and closed to writes now: no entry below
    # can be added, removed or renamed by anyone but the owner and root.
    for f in ./* ./.[!.]* ./..?*; do
        [ -e "$f" ] || [ -L "$f" ] || continue
        [ "$f" = "./$qdir" ] && continue
        uid="$(tc_uid "$f")"
        if [ "$uid" != "$ouid" ]; then
            tc_quarantine "$f" "was owned by uid ${uid:-unknown}, not by $owner" || return 1
        elif [ -L "$f" ]; then
            echo "WARNING: auth/${f#./} is a symbolic link; left as it is (the owner checks refuse one)" >&2
        elif [ -d "$f" ]; then
            case "${f#./}" in .quarantine-*) chmod 0700 "$f" || return 1; continue ;; esac
            # Every entry under it must be the owner's before a mode or group
            # changes. The check runs again once the modes are tightened: a
            # directory group ea could write may have gained an entry.
            if [ -n "$(tc_foreign_in "$f" "$ouid")" ]; then
                tc_quarantine "$f" "held an entry another user owns, or a file with other names" || return 1
                continue
            fi
            chmod 0700 "$f" && chmod -R go-rwx "$f" || return 1
            if [ -n "$(tc_foreign_in "$f" "$ouid")" ]; then
                tc_quarantine "$f" "gained an entry another user owns" || return 1
                continue
            fi
            chown -R "$owner:$ogroup" "$f" || return 1
        elif [ -f "$f" ]; then
            n="$(tc_links "$f")"
            if [ "$n" != 1 ]; then
                tc_quarantine "$f" "had $n links (another name may be a file outside auth/)" || return 1
                continue
            fi
            case "${f#./}" in
                *token*|*secret*|*.key|.pre-mediation-perms|mediation-disabled)
                    chmod go-rwx "$f" && chown "$owner:$ogroup" "$f" || return 1 ;;
                *)  chmod g-w,o-rwx "$f" && chown "$owner:$dgroup" "$f" || return 1 ;;
            esac
        fi
    done
    tc_quarantine_close
}

# auth_lock_here OWNER OWNER_UID DIR_GROUP OWNER_GROUP SHOWN: the current
# directory is the auth directory at SHOWN; lock it, then its entries.
auth_lock_here() {
    auth_dir_lock_here "$1" "$2" "$3" "$5" || return
    auth_entries_lock_here "$1" "$2" "$3" "$4"
}

# auth_lock AUTH_PHYS OWNER OWNER_UID DIR_GROUP OWNER_GROUP: auth_lock_here
# in the directory at AUTH_PHYS. Nothing to do when there is none; exit 1
# when the name is a link or leads elsewhere, 3 when another user owns it.
auth_lock() (
    [ -e "$1" ] || [ -L "$1" ] || exit 0
    if ! tc_enter "$1"; then
        echo "ERROR: $1 is not a real directory at that path (a link, or moved); left as it is" >&2
        exit 1
    fi
    auth_lock_here "$2" "$3" "$4" "$5" "$1"
)

# tc_give_moved_tree UID GID: in the current directory, a state tree moved
# in from the owner's home this run, give every entry but auth/ and
# .env.secrets to UID:GID, entries before their directory.
tc_give_moved_tree() {
    find -P . -xdev -depth \
        ! \( -path ./auth -o -path './auth/*' -o -path ./.env.secrets \) \
        ! \( ! -type d -links +1 \) \
        \( ! -uid "$1" -o ! -gid "$2" \) \
        -execdir chown -h "$1:$2" {} +
}

# tc_not_resident TOP UID: how many entries under TOP (auth/ and
# .env.secrets aside) UID does not own. Reads only.
tc_not_resident() {
    find -P "$1" -xdev \( -path "$1/auth" -o -path "$1/.env.secrets" \) -prune \
        -o ! -uid "$2" -print 2>/dev/null | wc -l | tr -d ' '
}

#!/usr/bin/env bash
# stage-proc-hidepid.sh: stage, for the next boot, a /proc on which the ea-bot
# user sees only its own processes.
#
#     sudo bash deploy/stage-proc-hidepid.sh               # stage (idempotent)
#     sudo bash deploy/stage-proc-hidepid.sh --status      # any user; as root it also asks ea-bot
#     sudo bash deploy/stage-proc-hidepid.sh --rollback    # unstage, for the next boot
#     sudo bash deploy/stage-proc-hidepid.sh --owner NAME  # when $SUDO_USER is not the owner
#
# On a default /proc every user lists every process: ps as ea-bot shows the
# owner's bot, mediator, shells and editors with their command lines. With
# hidepid=2 the kernel hides the /proc/<pid> directory of another user's
# process from a reader that neither holds CAP_SYS_PTRACE nor belongs to the
# gid= group, so ps, top and pgrep show that reader only its own processes.
# The owner and the services that need a full view keep it through that
# group. One root step, in this order, so the fstab line never stands without
# its exemptions:
#
#   1. the exemption group, procview (a system group); ea-bot is never a
#      member, and a run takes it out if someone put it in;
#   2. its members: the owner and polkit's service account;
#   3. a systemd-logind drop-in (SupplementaryGroups=procview);
#   4. the /proc line in /etc/fstab, after a timestamped backup
#      (/etc/fstab.bak-proc-hidepid-<UTC time>). It names the gid by number,
#      which the kernel takes as is, and libmount must read it back before the
#      file is replaced. An active /proc line this script did not write is
#      commented out beside it, and rollback puts it back as it was.
#
# Nothing is remounted, and no daemon is reloaded or restarted. At the next
# boot systemd-remount-fs applies the fstab line to /proc, systemd reads the
# drop-in, and every process starts with its new groups. Until then
# scripts/boundary-audit.py reports "staged, not active"; after it, the audit
# checks that the option is live and that ea-bot cannot see the owner's
# processes.
#
# Who keeps the full view, and why:
#   the owner ($SUDO_USER, or --owner NAME): its services (ea-chat-bot and
#       ea-mail-mediator are User= units, which take the owner's groups), its
#       crons and its logins. The one owner code path that inspects ea-bot's
#       processes is the bot's start-up reap of orphaned dispatch runs: it
#       lists them with `pgrep -af claude` and reads each /proc/<pid>/status
#       for its parent. The group keeps that working, and keeps the owner's
#       tmux server naming the resident window after the pane's process.
#   polkit's account (User= of polkit.service; polkitd on Ubuntu): it reads
#       /proc/<pid>/status, stat and cgroup of the process that asks for an
#       authorization (uid, start time, session). Without the view it refuses
#       every request from an unprivileged caller: GNOME settings, udisks
#       mounts, pkexec, systemctl run by a user.
#   systemd-logind: it finds a caller's session from /proc/<pid>/cgroup. It
#       runs as root without CAP_SYS_PTRACE in its bounding set and takes no
#       groups from /etc/group, so the drop-in gives it the group.
#
# Checked on the reference host (Ubuntu 26.04, systemd 259) and left out:
#   NetworkManager: when /proc hides the caller it passes a start time of 0
#       (it does so on purpose for hidepid), and polkitd reads the time.
#   rtkit-daemon: it clears its supplementary groups when it drops to the
#       rtkit user, so no group can reach it; it stops granting realtime
#       priority to other users' threads. The owner's PipeWire threads run
#       without it already (SCHED_OTHER, nice 0).
#   dbus-daemon (messagebus): it takes the caller's uid and pid from the
#       socket; /proc gives only the command name in its log lines.
#   geoclue: it reads /proc/<pid>/cgroup only to recognise a Flatpak client.
#   systemd-oomd: it reads memory pressure from cgroup files and kills
#       through them.
#   ModemManager, upowerd, systemd-resolved: they authorize a caller through
#       polkit by its bus name and read no caller's /proc.
#   rsyslog: it reads /proc only for trusted-property annotation, which is
#       off in its configuration.
#   chrony, colord, fwupd, hf-trial-data, power-profiles-daemon: they run
#       with ProtectProc=invisible, on a /proc of their own.
#   root services with CAP_SYS_PTRACE (journald, udevd, cron, sshd, dockerd,
#       containerd, snapd, tailscaled, gdm, udisks2, accounts-daemon): the
#       capability passes the hidepid check.
#   containers (the Zulip stack): each one mounts its own /proc instance.
#
# Still visible to ea-bot: the process IDs in /sys/fs/cgroup/*/cgroup.procs,
# and each unit's configured command line, which PID 1 answers over D-Bus
# (systemctl show). The command lines of the running processes are not.
#
# Owner-agnostic: the owner comes from $SUDO_USER or --owner NAME, and the
# polkit account from the polkit unit. Idempotent: a second run changes
# nothing and writes no backup. Rollback removes the fstab line (after a
# backup); while the live /proc still hides processes it keeps the group, its
# members and the drop-in, which the next run after the boot removes. It
# never remounts either.
set -uo pipefail

GROUP=procview
RESIDENT=ea-bot
FSTAB=/etc/fstab
LOGIND_DROPIN=/etc/systemd/system/systemd-logind.service.d/ea-proc-hidepid.conf
TAG="# ea-proc-hidepid:"
DISABLED="# ea-proc-hidepid-disabled:"
PROC_OPTS="nosuid,nodev,noexec,relatime"

red(){ printf '\033[31m%s\033[0m\n' "$*"; }
grn(){ printf '\033[32m%s\033[0m\n' "$*"; }
ylw(){ printf '\033[33m%s\033[0m\n' "$*"; }
fail(){ red "FAIL: $*"; exit 1; }
need_root(){ [ "$(id -u)" = 0 ] || fail "run as root:  sudo bash $0 ${1:-}"; }

# ── the fstab edit: a file in, a file out (tests drive these on temp files) ──

proc_line(){  # $1 = gid
  printf 'proc  /proc  proc  %s,hidepid=2,gid=%s  0  0\n' "$PROC_OPTS" "$1"
}

tag_line(){  # $1 = gid, $2 = group name
  printf '%s ea-bot sees only its own processes; gid %s (%s) keeps the full view (deploy/stage-proc-hidepid.sh)\n' \
    "$TAG" "$1" "$2"
}

# An active /proc line is a line that is not a comment and names /proc as its
# mount point. The staged line is the one right after the tag line.
AWK_IS_PROC='function is_proc() { return NF >= 3 && $1 !~ /^#/ && ($2 == "/proc" || $2 == "/proc/") }'

render_staged(){  # $1 = fstab, $2 = gid, $3 = group; the staged fstab on stdout
  awk -v tag="$TAG" -v dis="$DISABLED" -v newtag="$(tag_line "$2" "$3")" -v newline="$(proc_line "$2")" "$AWK_IS_PROC"'
    index($0, dis) == 1 { drop = 0; print; next }
    index($0, tag) == 1 { drop = 1; if (!done) { print newtag; print newline; done = 1 }; next }
    drop { drop = 0; if (is_proc()) next }
    is_proc() { print dis " " $0; next }
    { print }
    END { if (!done) { print newtag; print newline } }
  ' "$1"
}

render_unstaged(){  # $1 = fstab; the fstab without the staged line on stdout
  awk -v tag="$TAG" -v dis="$DISABLED" "$AWK_IS_PROC"'
    index($0, dis " ") == 1 { drop = 0; print substr($0, length(dis) + 2); next }
    index($0, tag) == 1 { drop = 1; next }
    drop { drop = 0; if (is_proc()) next }
    { print }
  ' "$1"
}

replace_file(){  # $1 = target, $2 = new content; prints the backup path when it replaced the target
  local target="$1" new="$2" backup
  if cmp -s -- "$target" "$new"; then rm -f -- "$new"; return 0; fi
  backup="$target.bak-proc-hidepid-$(date -u +%Y%m%dT%H%M%SZ)"
  [ -e "$backup" ] && backup="$backup.$$"
  cp -p -- "$target" "$backup" || { rm -f -- "$new"; return 1; }
  chmod --reference="$target" -- "$new" || { rm -f -- "$new"; return 1; }
  chown --reference="$target" -- "$new" 2>/dev/null || true
  mv -f -- "$new" "$target" || { rm -f -- "$new"; return 1; }
  echo "$backup"
}

scratch_beside(){  # $1 = file; a new empty file in the same directory, for an atomic replace
  mktemp "$(dirname -- "$1")/.$(basename -- "$1").proc-hidepid.XXXXXX"
}

stage_fstab(){  # $1 = fstab, $2 = gid, $3 = group; prints the backup path when the file changed
  local fstab="$1" tmp got
  tmp="$(scratch_beside "$fstab")" || return 1
  render_staged "$fstab" "$2" "$3" > "$tmp" || { rm -f -- "$tmp"; return 1; }
  if command -v findmnt >/dev/null 2>&1; then
    # libmount reads the file the way the boot's remount will.
    got="$(findmnt --tab-file "$tmp" -M /proc -no OPTIONS 2>/dev/null)"
    if [ "$got" != "$PROC_OPTS,hidepid=2,gid=$2" ]; then
      rm -f -- "$tmp"
      echo "libmount reads the staged /proc options as '${got:-nothing}'" >&2
      return 1
    fi
  fi
  replace_file "$fstab" "$tmp"
}

unstage_fstab(){  # $1 = fstab; prints the backup path when the file changed
  local fstab="$1" tmp
  tmp="$(scratch_beside "$fstab")" || return 1
  render_unstaged "$fstab" > "$tmp" || { rm -f -- "$tmp"; return 1; }
  replace_file "$fstab" "$tmp"
}

staged_line(){  # $1 = fstab; the staged /proc line, or nothing
  awk -v tag="$TAG" "$AWK_IS_PROC"'
    after && is_proc() { print; exit }
    { after = (index($0, tag) == 1) }
  ' "$1" 2>/dev/null
}

# ── the live system (read only) ──────────────────────────────────────────

live_opts(){ findmnt -rno OPTIONS -M /proc 2>/dev/null | tail -1; }

hidepid_of(){  # $1 = option string; off, noaccess, invisible or ptraceable
  case ",$1," in
    *,hidepid=2,*|*,hidepid=invisible,*)  echo invisible ;;
    *,hidepid=1,*|*,hidepid=noaccess,*)   echo noaccess ;;
    *,hidepid=4,*|*,hidepid=ptraceable,*) echo ptraceable ;;
    *) echo off ;;
  esac
}

gid_of(){ printf ',%s,' "$1" | grep -oE ',gid=[0-9]+,' | tr -dc '0-9'; }

group_gid(){ getent group "$GROUP" | cut -d: -f3; }
group_members(){ getent group "$GROUP" | cut -d: -f4 | tr ',' ' '; }
in_group(){ id -nG "$1" 2>/dev/null | tr ' ' '\n' | grep -qxF "$GROUP"; }

polkit_user(){
  local u
  u="$(systemctl show -p User --value polkit.service 2>/dev/null || true)"
  if [ -n "$u" ] && [ "$u" != root ] && getent passwd "$u" >/dev/null; then echo "$u"; fi
}

has_logind(){ [ "$(systemctl show -p LoadState --value systemd-logind.service 2>/dev/null)" = loaded ]; }

dropin_text(){
  cat <<EOF
# deploy/stage-proc-hidepid.sh: logind finds a caller's session from
# /proc/<pid>/cgroup, so it keeps the full view of /proc under hidepid.
[Service]
SupplementaryGroups=$GROUP
EOF
}

resolve_owner(){
  [ -n "$OWNER" ] && [ "$OWNER" != root ] \
    || fail "cannot tell which account owns the services; re-run with --owner NAME"
  [ "$OWNER" != "$RESIDENT" ] || fail "the owner cannot be $RESIDENT"
  getent passwd "$OWNER" >/dev/null || fail "no account '$OWNER'"
}

# ── stage, status, rollback ──────────────────────────────────────────────

add_member(){  # $1 = account, $2 = why it needs the view
  if in_group "$1"; then
    echo "  $1: already a member ($2)"
  else
    usermod -aG "$GROUP" "$1" || fail "usermod -aG $GROUP $1"
    grn "  $1: added ($2)"
  fi
}

row(){ printf '  %-10s %s\n' "$1" "$2"; }

next_boot(){  # $1 = gid
  echo "At the next boot (nothing changes before it):"
  row "/proc" "now   $(live_opts)"
  row "" "then  $PROC_OPTS,hidepid=2,gid=$1 (systemd-remount-fs applies the fstab line)"
  row "$RESIDENT" "ps, top, pgrep and /proc/<pid> show it only its own processes"
  row "full view" "the members of $GROUP ($(group_members)) and systemd-logind (drop-in)"
  row "groups" "a process takes a new group when it starts; the boot starts all of them"
  row "audit" "scripts/boundary-audit.py --dry-run reports 'staged, not active' until then"
  row "undo" "sudo bash $0 --rollback"
}

stage(){
  need_root
  resolve_owner
  getent passwd "$RESIDENT" >/dev/null || fail "the $RESIDENT user does not exist (run scripts/install/setup-ea-user.sh first)"
  [ -f "$FSTAB" ] && [ ! -L "$FSTAB" ] || fail "$FSTAB is not a regular file"

  ylw "[1/4] exemption group $GROUP"
  if getent group "$GROUP" >/dev/null; then
    echo "  $GROUP exists"
  else
    groupadd --system "$GROUP" || fail "groupadd --system $GROUP"
    grn "  created $GROUP"
  fi
  local gid pk backup
  gid="$(group_gid)"
  [ -n "$gid" ] && [ "$gid" != 0 ] || fail "no usable gid for $GROUP"
  [ "$gid" != "$(id -g "$RESIDENT")" ] || fail "gid $gid is $RESIDENT's own group; pick another group"
  echo "  gid $gid"
  if in_group "$RESIDENT"; then
    gpasswd -d "$RESIDENT" "$GROUP" >/dev/null || fail "gpasswd -d $RESIDENT $GROUP"
    ylw "  took $RESIDENT out of $GROUP: it must not hold the exemption"
  fi

  ylw "[2/4] members"
  add_member "$OWNER" "the owner's services, crons and logins"
  pk="$(polkit_user)"
  if [ -n "$pk" ]; then
    add_member "$pk" "polkit reads the asking process's /proc/<pid>"
  else
    echo "  polkit runs as root or is not installed; no account to add"
  fi

  ylw "[3/4] systemd-logind drop-in"
  if has_logind; then
    if [ -f "$LOGIND_DROPIN" ] && [ "$(cat "$LOGIND_DROPIN")" = "$(dropin_text)" ]; then
      echo "  $LOGIND_DROPIN is in place"
    else
      install -d -m 755 "$(dirname "$LOGIND_DROPIN")" || fail "mkdir $(dirname "$LOGIND_DROPIN")"
      dropin_text > "$LOGIND_DROPIN.tmp" && chmod 644 "$LOGIND_DROPIN.tmp" \
        && mv -f "$LOGIND_DROPIN.tmp" "$LOGIND_DROPIN" || fail "write $LOGIND_DROPIN"
      grn "  wrote $LOGIND_DROPIN (systemd reads it at the next boot)"
    fi
  else
    echo "  no systemd-logind unit; skipped"
  fi

  ylw "[4/4] $FSTAB"
  backup="$(stage_fstab "$FSTAB" "$gid" "$GROUP")" || fail "could not stage the /proc line; $FSTAB is unchanged"
  if [ -n "$backup" ]; then
    grn "  staged: $(staged_line "$FSTAB")"
    echo "  the previous file is $backup"
  else
    echo "  already staged: $(staged_line "$FSTAB")"
  fi
  echo; next_boot "$gid"
}

status(){
  local gid line now mode
  gid="$(group_gid)"
  line="$(staged_line "$FSTAB")"
  now="$(live_opts)"
  mode="$(hidepid_of "$now")"
  echo "proc-hidepid status"
  if [ -n "$gid" ]; then row "group" "$GROUP (gid $gid): $(group_members)"; else row "group" "$GROUP is absent"; fi
  if [ -n "$gid" ] && in_group "$RESIDENT"; then
    red "$(row "$RESIDENT" "a member of $GROUP (it must not be)")"
  else
    row "$RESIDENT" "not a member of $GROUP"
  fi
  if [ -f "$LOGIND_DROPIN" ]; then row "logind" "$LOGIND_DROPIN"; else row "logind" "no drop-in"; fi
  if [ -n "$line" ]; then row "fstab" "$line"; else row "fstab" "no staged /proc line"; fi
  row "live" "/proc ${now:-?}"
  if [ "$mode" = invisible ] && [ -n "$gid" ] && [ "$(gid_of "$now")" = "$gid" ]; then
    grn "$(row "state" "active (hidepid=invisible, gid $gid)")"
  elif [ "$mode" != off ]; then
    ylw "$(row "state" "hidepid=$mode is live, which is not the staged setting")"
  elif [ -n "$line" ]; then
    ylw "$(row "state" "staged, not active (the next boot applies it)")"
  else
    row "state" "not staged"
  fi
  if [ "$(id -u)" = 0 ] && getent passwd "$RESIDENT" >/dev/null; then
    if runuser -u "$RESIDENT" -- test -e /proc/1/status; then
      row "view" "$RESIDENT sees PID 1, a root process"
    else
      grn "$(row "view" "$RESIDENT does not see PID 1, a root process")"
    fi
  fi
}

rollback(){
  need_root --rollback
  local backup gid now
  ylw "[1/3] $FSTAB"
  backup="$(unstage_fstab "$FSTAB")" || fail "could not remove the /proc line; $FSTAB is unchanged"
  if [ -n "$backup" ]; then grn "  removed the staged /proc line; the previous file is $backup"; else echo "  nothing staged"; fi

  gid="$(group_gid)"
  now="$(live_opts)"
  if [ -n "$gid" ] && [ "$(hidepid_of "$now")" != off ] && [ "$(gid_of "$now")" = "$gid" ]; then
    ylw "  the live /proc hides processes, with gid $gid exempt, until the next boot."
    echo "  The group, its members and the logind drop-in stay until then;"
    echo "  run  sudo bash $0 --rollback  again after the boot to remove them."
    echo "  To lift it before the boot:  sudo mount -o remount,hidepid=0 /proc"
    return 0
  fi

  ylw "[2/3] systemd-logind drop-in"
  if [ -f "$LOGIND_DROPIN" ]; then
    rm -f -- "$LOGIND_DROPIN" && grn "  removed $LOGIND_DROPIN"
    rmdir -- "$(dirname "$LOGIND_DROPIN")" 2>/dev/null || true
  else
    echo "  none"
  fi

  ylw "[3/3] group $GROUP"
  if [ -n "$gid" ]; then
    groupdel "$GROUP" && grn "  removed $GROUP (gid $gid)" || fail "groupdel $GROUP"
  else
    echo "  none"
  fi
  grn "Rolled back. The next boot mounts /proc as $FSTAB says without the staged line."
}

main(){
  OWNER="${SUDO_USER:-}"
  local action=--stage
  while [ $# -gt 0 ]; do
    case "$1" in
      --stage|--status|--rollback) action="$1"; shift ;;
      --owner) [ -n "${2:-}" ] || fail "--owner needs a NAME"; OWNER="$2"; shift 2 ;;
      *) echo "usage: sudo bash $0 [--stage|--status|--rollback] [--owner NAME]"; exit 2 ;;
    esac
  done
  case "$action" in
    --stage)    stage ;;
    --status)   status ;;
    --rollback) rollback ;;
  esac
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
fi

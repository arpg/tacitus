#!/usr/bin/env python3
"""
The install overlay: the facts CLAUDE.md leaves out because they belong
to one install (the user's name and institution, the chat rooms, the
sites the EA keeps, the lists it digests), rendered from config.

CLAUDE.md is the product core and names no person or institution. This
module renders the rest from user-profile.json, ea-config.json,
github.json and list-digests.json, plus the owner's free-form notes in
~/.ea/config/install-notes.md, into ~/.ea/config/CLAUDE.install.md.

Two ways it reaches a session:
  - `install` copies it into the resident's deployed tree as
    CLAUDE.local.md (never into an owner's own checkout: the text is the
    resident's), which Claude Code loads beside CLAUDE.md in that working
    directory with no approval step (an `@~/...` import would be an external import, which a `-p`
    session loads only after an interactive approval).
  - A prompt builder for a run that does not load CLAUDE.local.md itself
    (a --bare run, or a working directory without the file; see
    claude_cli.discovered_memory) embeds the rendered copy:
    prompt_logic.build_claude_md, or `core_with_overlay()`.

  python3 install_overlay.py render [--out PATH|-]
  python3 install_overlay.py install --tree DIR [--tree DIR] [--from PATH]
  python3 install_overlay.py show
"""

from __future__ import annotations

import argparse
import json
import os
import pwd
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ea_config  # noqa: E402
import institution  # noqa: E402
import owner_trust  # noqa: E402
import resident_io  # noqa: E402

OVERLAY_NAME = "CLAUDE.install.md"
LOCAL_NAME = "CLAUDE.local.md"
NOTES_NAME = "install-notes.md"


def _ea_dir() -> Path:
    """EA_DIR, else `.ea` in this euid's passwd home. Not $HOME: `sudo -u
    ea-bot` may keep the caller's, and the render runs that way with no
    environment of its own (the setup sudoers rule names the bare
    command)."""
    if os.environ.get("EA_DIR"):
        return Path(os.environ["EA_DIR"])
    try:
        return Path(pwd.getpwuid(os.geteuid()).pw_dir) / ".ea"
    except KeyError:
        return Path.home() / ".ea"


def overlay_path() -> Path:
    return _ea_dir() / "config" / OVERLAY_NAME


def _json(name: str) -> dict:
    try:
        data = json.loads((_ea_dir() / "config" / name).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _text(name: str) -> str:
    try:
        return (_ea_dir() / "config" / name).read_text().strip()
    except OSError:
        return ""


def _join(items) -> str:
    return ", ".join(str(i) for i in items if str(i).strip())


def _user_section(profile: dict) -> list[str]:
    lines = ["## The user", ""]
    name = profile.get("name") or ""
    preferred = profile.get("preferred_name") or ""
    if name:
        lines.append(f"- Name: {name}" + (f" (goes by {preferred})" if preferred and preferred != name else ""))
    if profile.get("pronouns"):
        lines.append(f"- Pronouns: {profile['pronouns']}")
    if profile.get("email"):
        lines.append(f"- Work address: {profile['email']}")
    if profile.get("role_description"):
        lines.append(f"- Role: {profile['role_description']}")
    unit = _join([profile.get("department", ""), institution.name()])
    if unit:
        lines.append(f"- Department and institution: {unit}")
    if profile.get("office"):
        lines.append(f"- Office: {profile['office']}")
    if len(lines) == 2:
        lines.append("- user-profile.json has no name yet. Run /setup.")
    return lines


def _institution_section() -> list[str]:
    lines = ["## Institution", ""]
    internal = institution.domains()
    if internal:
        lines.append(f"- Internal mail domains: {_join(internal)}")
    related = institution.related_domains()
    if related:
        lines.append(f"- Related campuses (internal for the admissions filter): {_join(related)}")
    lines.append(f"- Safety rail: {institution.safety_rail_phrase()} is never sent without the user's approval.")
    vid = institution.video()
    if vid["platform"] or vid["link"]:
        platform = vid["platform"] or "the video platform"
        link = f" The user's link: {vid['link']}." if vid["link"] else ""
        lines.append(f"- Online meetings use {platform}.{link} Never let the calendar "
                     "auto-attach a meeting from another platform.")
    return lines


def _chat_section(cfg: dict, profile: dict) -> list[str]:
    chat = cfg.get("chat") if isinstance(cfg.get("chat"), dict) else {}
    frontend = str(chat.get("frontend") or "slack")
    lines = ["## Chat", ""]
    profile_name = cfg.get("profile") or "assistant"
    if profile_name in ("email", "review-only"):
        # No chat platform on these profiles: a report is a file, and the
        # user works with the EA in the Claude Code shell.
        lines.append(f"- Install profile: `{profile_name}`. There is no chat channel and no "
                     "buttons: reports land in `~/.ea/state/reports/<agent>-latest.md` and go "
                     "out by phone push when one is configured; the user works with the EA "
                     "in the Claude Code shell and sends drafts from Outlook.")
        return lines
    if frontend == "web":
        web = _web_prompt_strings()
        if ea_config.web_sched_on(chat):
            channels = ("Channels: `ea` (the EA channel), `dm` (the DM) and `Scheduling` "
                        "(the scheduling channel, where each scheduling request found in "
                        "mail opens a conversation); project rooms are rooms in the web "
                        "chat.")
        else:
            channels = ("Channels: `ea` (the EA channel) and `dm` (the DM); project rooms "
                        "are rooms in the web chat. There is no scheduling channel: "
                        "meeting requests come in `ea`.")
        lines.append(f"- Platform: the {web['platform']}, which the bot serves itself. "
                     + channels)
        lines.append(f"- Each confirmation card goes to the DM with a one-line pointer in the "
                     f"thread: say \"{web['confirm_location']}\", never \"above\".")
    elif frontend == "zulip":
        z = chat.get("zulip") if isinstance(chat.get("zulip"), dict) else {}
        streams = [f"`{z[k]}` ({label})" for k, label in
                   (("stream", "the EA channel"), ("dm_stream", "the DM stream"),
                    ("sched_stream", "the scheduling channel")) if z.get(k)]
        lines.append(f"- Platform: Zulip. Streams: {_join(streams) or 'see the chat block'}; "
                     "project rooms are `ea-<project>` streams.")
    else:
        channel = profile.get("slack_channel_name") or ""
        lines.append(f"- Platform: Slack. EA channel: `{channel or 'see user-profile.json'}`.")
    if frontend != "web" and chat.get("shadow") == "web":
        lines.append(f"- The {_web_prompt_strings()['platform']} mirrors every post (shadow "
                     "mode); the user may answer in either.")
    lines.append(f"- Install profile: `{profile_name}`.")
    return lines


# The web chat's own phrases (WebFrontend.PROMPT_STRINGS), for a render
# where the chat package does not load.
_WEB_WORDING = {"platform": "Tacitus web chat",
                "confirm_location": "the Send button is on the card in your DM thread"}


def _web_prompt_strings() -> dict:
    """The web front end's prompt wording: its platform name and where
    the Send button is, as the bot's own prompts say them."""
    try:
        from chat.web_frontend import WebFrontend
        got = WebFrontend.PROMPT_STRINGS
        return {k: str(got.get(k) or v) for k, v in _WEB_WORDING.items()}
    except Exception:
        return dict(_WEB_WORDING)


def _accounts_section(profile: dict) -> list[str]:
    gmail = [a for a in profile.get("gmail_accounts") or [] if isinstance(a, str)]
    cals = [a for a in profile.get("calendar_accounts") or [] if isinstance(a, str)]
    if not (gmail or cals):
        return []
    lines = ["## Accounts", ""]
    if gmail:
        lines.append(f"- Gmail accounts the EA may draft from: {_join(gmail)}")
    if cals:
        lines.append(f"- Google calendars read for availability: {_join(cals)}")
    return lines


def _github_section(gh: dict) -> list[str]:
    lines: list[str] = []
    sites = gh.get("sites") if isinstance(gh.get("sites"), dict) else {}
    review = gh.get("doc_review") if isinstance(gh.get("doc_review"), dict) else {}
    if not (sites or review.get("repo") or gh.get("bot_login")):
        return lines
    lines += ["## GitHub", ""]
    if gh.get("bot_login"):
        lines.append(f"- The EA commits as `{gh['bot_login']}`.")
    if review.get("repo"):
        owner = review.get("owner_login") or "the owner"
        lines.append(f"- Working documents go to `{review['repo']}` as pull requests; "
                     f"only `{owner}` merges.")
    for slug, site in sorted(sites.items()):
        if not isinstance(site, dict):
            continue
        title = site.get("title") or slug
        url = site.get("url") or ""
        private = " (private, behind sign-in)" if site.get("private") else ""
        lines.append(f"- Site `{slug}`: {title}{private}" + (f", {url}" if url else ""))
    return lines


def _lists_section(lists_cfg: dict) -> list[str]:
    lists = lists_cfg.get("lists") if isinstance(lists_cfg.get("lists"), list) else []
    named = [f"{x.get('name') or x.get('id')} ({x.get('mode', 'filter')})"
             for x in lists if isinstance(x, dict) and (x.get("name") or x.get("id"))]
    if not named:
        return []
    return ["## Mailing-list digests", "", f"- The list-digest agent handles: {_join(named)}"]


def render_text() -> str:
    """The overlay for the install EA_DIR points at."""
    profile = institution.profile()
    cfg = _json("ea-config.json")
    sections = [
        ["# Install facts", "",
         "Generated by `preprocessor/install_overlay.py render` from the config "
         "in ~/.ea/config/. Do not edit this file: change the config and render again."],
        _user_section(profile),
        _institution_section(),
        _chat_section(cfg, profile),
        _accounts_section(profile),
        _github_section(_json("github.json")),
        _lists_section(_json("list-digests.json")),
    ]
    notes = _text(NOTES_NAME)
    if notes:
        sections.append(["## Install notes", "", notes])
    return "\n\n".join("\n".join(s) for s in sections if s).rstrip() + "\n"


def render(out: str | None = None) -> str:
    """Write the overlay into the config directory (resident space: the
    write goes through resident_io, as the resident when the caller is
    the owner)."""
    text = render_text()
    if out == "-":
        sys.stdout.write(text)
        return text
    target = Path(out) if out else overlay_path()
    if not resident_io.replace_text(target, text, 0o640):
        raise SystemExit(f"install_overlay: could not write {target}")
    return text


def read_overlay() -> str:
    """The rendered overlay, or '' when it has not been rendered."""
    try:
        return overlay_path().read_text().strip()
    except OSError:
        return ""


def core_with_overlay(core_text: str) -> str:
    """CLAUDE.md text with the install overlay appended, for prompt
    builders that embed CLAUDE.md themselves."""
    overlay = read_overlay()
    if not core_text or not overlay:
        return core_text
    return core_text.rstrip() + "\n\n" + overlay + "\n"


def _resident_roots() -> list[str]:
    """The resolved resident space: the home that holds the EA tree in use
    (the owner's ~/.ea is a link into the resident's home) and the
    resident's home from the user database."""
    roots = [os.path.dirname(os.path.realpath(_ea_dir()))]
    try:
        roots.append(os.path.realpath(pwd.getpwnam(resident_io.RESIDENT_USER).pw_dir))
    except KeyError:
        pass
    return roots


def in_resident_space(tree) -> bool:
    """True when `tree` resolves inside the resident's space: the lanes'
    deployed copy (the resident's ~/ea-system), not an owner's own checkout."""
    real = os.path.realpath(os.fspath(tree))
    return any(real == r or real.startswith(r.rstrip(os.sep) + os.sep)
               for r in _resident_roots())


def install(trees: list[str], source: str | None = None) -> list[Path]:
    """Copy the rendered overlay into each tree as CLAUDE.local.md.

    The overlay is resident space (rendered by the resident from config the
    resident writes, free text included), and Claude Code reads
    CLAUDE.local.md as instructions. So it goes only into a tree in the
    resident's space, the lanes' deployed copy, whose sessions already work
    on that config; an owner's own checkout outside it is skipped with a
    note, never written. The source is read with the resident's access, a
    link refused (resident_io); the write checks the tree's directory on
    its open descriptor (owner_trust.write_owner_file), so a tree swapped
    for a link or a directory someone else can write is refused."""
    src = Path(source) if source else overlay_path()
    text = resident_io.read_text(src)
    if text is None:
        raise SystemExit(f"install_overlay: cannot read {src} (missing, a link, or not "
                         f"readable by the resident); run `render` first")
    written = []
    for tree in trees:
        target = Path(tree) / LOCAL_NAME
        if not in_resident_space(tree):
            print(f"install_overlay: skipped {target}: the overlay comes from the resident's "
                  f"config and goes only into the resident's own tree, never an owner's "
                  f"checkout", file=sys.stderr)
            continue
        try:
            owner_trust.write_owner_file(target, text, mode=0o644)
        except (OSError, owner_trust.OwnerTrustError) as e:
            raise SystemExit(f"install_overlay: refused {target}: {e}")
        written.append(target)
    return written


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Render and install the EA's install overlay.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("render", help="write ~/.ea/config/CLAUDE.install.md from the config")
    p.add_argument("--out", help="output path, or - for stdout")
    p = sub.add_parser("install", help="copy the overlay into a tree as CLAUDE.local.md")
    p.add_argument("--tree", action="append", required=True)
    p.add_argument("--from", dest="source", help="overlay file to copy (default: the rendered one)")
    sub.add_parser("show", help="print the overlay the config would render")
    args = ap.parse_args(argv)
    # The modules this one reads through (institution.py) take EA_DIR, so
    # the tree found from the passwd home reaches them too.
    os.environ.setdefault("EA_DIR", str(_ea_dir()))
    if args.cmd == "render":
        render(args.out)
        if args.out != "-":
            print(f"rendered {args.out or overlay_path()}")
    elif args.cmd == "install":
        for path in install(args.tree, args.source):
            print(f"installed {path}")
    else:
        sys.stdout.write(render_text())
    return 0


if __name__ == "__main__":
    sys.exit(main())

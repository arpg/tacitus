# Security policy

Tacitus reads mail and calendars and drafts in the user's name. Its
design limits what the model can do without the user's click;
[docs/SECURITY.md](docs/SECURITY.md) has the threat model, the five
layers and what a prompt injection cannot do.

## Reporting a vulnerability

Report privately through
[a security advisory](https://github.com/arpg/tacitus/security/advisories/new).
Never open a public issue for a vulnerability. Expect a first reply
within seven days; the project has one maintainer.

A report helps most when it says which layer it crosses: a way to send
mail, create or change an event, or delete, without the user's click; a
way for the `ea-bot` user to read the owner's tokens or private
directory; a way for mail content to reach the model when the mail gate
keeps it; or a way for a plugin to act beyond the capabilities it
declared.

## Supported versions

The `main` branch and the latest tagged release. Older tags get no fixes.

## What is not a vulnerability here

The model writing a poor draft, which the user reviews before it is
sent; a reporter's own machine, chat server or tokens being exposed by
their configuration; and Claude Code's own behaviour, which belongs to
Anthropic's program.

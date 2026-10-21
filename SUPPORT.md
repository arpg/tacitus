# Support

Tacitus has one maintainer, who runs it on their own mail and works on it
beside a full-time job. Help comes from the docs first, then from other
users, then from the maintainer.

| You want | Go to |
|---|---|
| To install, upgrade or uninstall | [docs/INSTALL.md](docs/INSTALL.md), then [docs/SETUP.md](docs/SETUP.md) for the step that failed |
| The health check | `python3 scripts/ea-doctor.py`; each FAIL names its fix and its docs/SETUP.md section |
| A question, or help with your install | [Discussions > Q&A](https://github.com/arpg/tacitus/discussions/categories/q-a) |
| To propose a feature | [Discussions > Ideas](https://github.com/arpg/tacitus/discussions/categories/ideas); a design becomes an issue |
| To report a bug | [New issue > Bug report](https://github.com/arpg/tacitus/issues/new/choose), with `ea-doctor.py --share` output |
| An install that never got going | [New issue > Install or first-week feedback](https://github.com/arpg/tacitus/issues/new/choose) |
| A vulnerability | [A private security advisory](https://github.com/arpg/tacitus/security/advisories/new), see [SECURITY.md](SECURITY.md) |
| A feature for your institution or a service you use | Write a plugin: [docs/plugins.md](docs/plugins.md) |

What to expect: a first reply within about a week. Bugs in the approval
gates, the mediator or data leaving the machine come first; install
failures on Linux and macOS next; everything else as time allows. An
issue that waits two weeks for information from its reporter is closed
and can be reopened. Pull requests with tests move faster than issues.

What is out of scope today: Windows and WSL2, a Gmail-only install
(Outlook is required), a hosted service, and chat platforms other than
Zulip and Slack. Ideas for these are welcome in Discussions, but they
need a contributor.

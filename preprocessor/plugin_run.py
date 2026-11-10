#!/usr/bin/env python3
"""plugin_run: run a plugin's own script from a Codex turn.

    plugin_run.py <script.py> [the script's arguments]

A Codex turn runs its commands in a sandbox with no network, and a plugin
script that reads a site or an account fails there. This runs the script
through the mediator instead: as the resident, outside the sandbox, with
the network, in the turn's working directory. The mediator runs a script
only when a Claude turn may run the same call: the plugin's lock grants it
(plugin_registry.plugin_tool_grants) and no deny rule matches it
(plugin_registry.script_call_error). The script gets the resident's access
and nothing of the owner's.

Everywhere else (a Claude turn, the owner's shell, a ghost run, a host with
no mediator) it runs the script in this process's place, as if it were
called by name.

Output and exit status are the script's own. A refusal is
{"status": "error", ...} on stderr with exit 2; exit 3 means the mediator
did not answer or stopped the script at its time limit.
"""
from __future__ import annotations

import json
import os
import sys

try:
    import mail_mediation
except ModuleNotFoundError:  # imported as preprocessor.plugin_run
    from preprocessor import mail_mediation


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0 if argv else 2
    script = os.path.abspath(os.path.expanduser(argv[0]))
    call = [mail_mediation.PLUGIN_RUN_VERB, script, *argv[1:]]
    mail_mediation.sandbox_forward(mail_mediation.PLUGIN_RUN_TOOL, call)
    err = mail_mediation.plugin_run_error(mail_mediation.PLUGIN_RUN_VERB, call)
    if err:
        print(json.dumps({"status": "error", "message": err}), file=sys.stderr)
        return 2
    try:
        os.execv(sys.executable, [sys.executable, script, *argv[1:]])
    except OSError as e:
        print(json.dumps({"status": "error", "message": f"could not run {script}: {e}"}),
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

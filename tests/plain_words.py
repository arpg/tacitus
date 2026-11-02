"""The words a person on the web chat never reads. The app's own text
(tests/test_webui.py) and the text the bot serves to the app's Settings
screen (tests/test_model_routing.py) are checked against this one list."""
import re

WORDS = ("channel", "thread", "session", "context", "specialist", "ledger", "lane", "relay", "tailnet",
         "tailscale", "bundle", "manifest", "token", "origin", "sse", "csp", "payload", "endpoint",
         "frontend", "pwa", "standalone")
# One of WORDS as a whole word, or its plural, in any case.
JARGON = re.compile(r"\b(" + "|".join(WORDS) + r")s?\b", re.I)

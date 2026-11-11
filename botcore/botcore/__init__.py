"""botcore: the shared plumbing for Claude-driven chat bots.

Nothing here knows about email or any user's data. A bot built
on it supplies its own skills, commands and storage.

    botcore.chat   one interface in front of Slack, the Tacitus web chat or
                   an in-memory fake, with platform-neutral confirmation cards
"""
__version__ = "0.1.0"

"""The review kind: the owner is asked to review or referee something and
return a report or a form (a paper, a proposal, a contract, a thesis, a
colleague's draft). Its parts are the things to return.

Not this kind: the owner as an editor who picks reviewers (a plugin's
work), reviews of the owner's own paper, and a colleague's "have a look
at the draft" (a reply, a flat row)."""
from . import Decision, Kind, Rules

KIND = Kind(
    name="review", noun="Review", part_noun="report", part_nouns="reports", delivered="returned",
    subkinds=("paper", "proposal", "contract", "draft", "thesis", "application"),
    decision=Decision(question="Will you do this review?"),
    material_kinds=("document", "form", "instructions", "other"),
    ladder=(14, 7, 3, 1, 0, -1), guide="reviews.md", lead=10, model_kinds=("review",),
    autonomy={"draft": "ask-first", "deliver": "ask-first"},
    rules=Rules(
        request=r"\b(?:invit\w+ to (?:serve as an? )?(?:review|referee)|review(?:er)? invitation|"
                r"(?:request|invitation) to (?:review|referee)|(?:complete|submit|return) (?:the |your |"
                r"a |an )?(?:peer )?(?:review|referee report|evaluation)|peer review|referee report|"
                r"review assignment|review (?:is |are )?(?:overdue|due))\b",
        confirm=r"\b(?:thank(?:s| you) for (?:submitting|completing|returning) (?:your |the )?"
                r"(?:review|referee report|evaluation)|(?:review|referee report) (?:has been|was) "
                r"(?:received|submitted))\b",
        exclude=r"\b(?:associate editor cent(?:er|re)|as (?:the |an |handling )?associate editor|"
                r"ae (?:center|recommendation|decision)|select (?:the )?reviewers|"
                r"reviewer selections?|invite (?:additional |more )?reviewers|reviewers? unavailable|"
                r"preliminary decision|editor'?s? decision|decision on (?:your )?manuscript|"
                r"(?:reviews?|comments) (?:of|on|for) your (?:paper|manuscript|submission|proposal)|"
                r"revis(?:e|ed|ion) (?:and resubmit|your manuscript)|rebuttal|camera.ready|author|"
                r"code review|performance review|tenure|dossier|promotion|"
                r"review (?:meeting|session|panel discussion)|"
                # A notice to another reviewer the owner is copied on.
                r"(?:remind\w*|ask\w*|invit\w*|request\w*)\s+(?:dr|prof|professor|mr|ms|mrs)\.?\s+\w+)\b",
        item=r"\b(?:manuscript|paper|submission|proposal)\s+(?:id\s*:?\s*|no\.?\s*|number\s*|#\s*)?"
             r"([A-Za-z0-9][\w.-]*\d[\w-]*)"))

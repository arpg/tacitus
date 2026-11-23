"""The report kind: a document the owner owes by a date (a progress or
annual report, a section, a slide deck, a budget). Its parts are the
pieces to deliver; an agency's "report received" notice is delivery.

Not this kind: a colleague's draft to comment on (a reply or a review),
and a report someone else owes."""
from . import Decision, Kind, Rules

KIND = Kind(
    name="report", noun="Report", part_noun="part", part_nouns="parts", delivered="delivered",
    subkinds=("progress", "annual", "final", "section", "slides", "budget"),
    decision=Decision(question="Will you do it?", choices=("Yes", "Yes, ask for materials", "No"),
                      yes=("Yes", "Yes, ask for materials"), no=("No",),
                      then={"Yes, ask for materials": "ask_materials"}),
    material_kinds=("document", "instructions", "template", "other"),
    ladder=(14, 7, 3, 1, 0, -1), guide="reports.md", lead=7, model_kinds=("report",),
    autonomy={"draft": "ask-first", "deliver": "ask-first"},
    response_mode="document",
    rules=Rules(
        request=r"\b(?:(?:annual|final|interim|progress|project|technical|quarterly|monthly)\s+"
                r"(?:project\s+)?(?:progress\s+)?report\s+(?:is\s+|are\s+)?(?:due|overdue|past due|"
                r"required|outstanding|not yet submitted)|(?:submit|complete|upload|file)\s+(?:the\s+|"
                r"your\s+|an?\s+)?(?:annual|final|interim|progress|project|technical|quarterly|"
                r"monthly)\s+(?:project\s+)?(?:progress\s+)?report)\b",
        confirm=r"\b(?:(?:annual|final|interim|progress|project)\s+(?:project\s+)?report\s+(?:has been|"
                r"was)\s+(?:received|submitted|approved|accepted)|thank(?:s| you) for submitting "
                r"(?:the |your )?(?:annual|final|interim|progress|project) report)\b",
        exclude=r"\b(?:expense report|incident report|bug report|grade report|credit report|"
                r"report (?:a|an|the) (?:problem|issue|bug)|reported by|your students?'?s?)\b",
        item=r"\b(?:award|grant|contract|project)\s+(?:id\s*:?\s*|no\.?\s*|number\s*|#\s*)?"
             r"([A-Za-z0-9][\w.-]*\d[\w-]*)"))

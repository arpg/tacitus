"""The form kind: paperwork to fill in and return or submit (a form, a
survey with a deadline, a signature, a compliance training, a
registration). Its parts are the forms. A signing service's "completed"
notice is its delivery.

Not this kind: a training reminder (compliance mail many owners call
noise: open one in chat when it is real work), a registration that is
only a chance to attend, and a form he is told about but not asked to
fill in."""
from . import Decision, Kind, Rules

KIND = Kind(
    name="form", noun="Form", part_noun="form", part_nouns="forms", delivered="submitted",
    subkinds=("survey", "training", "registration", "signature", "disclosure", "evaluation"),
    decision=Decision(question="Will you fill it in?"),
    material_kinds=("form", "instructions", "document", "other"),
    ladder=(7, 3, 1, 0, -1), guide="forms.md", lead=3, model_kinds=("form",),
    response_mode="form",
    detect_default=True, autonomy={"fill": "ask-first", "deliver": "ask-first"},
    rules=Rules(
        request=r"\b(?:(?:review and )?sign\b(?! up)[^.]{0,60}\b(?:document|form|agreement|report|"
                r"envelope|letter|contract)|please (?:review and )?sign|"
                r"(?:complete|fill (?:in|out)|submit)\s+(?:the |your |this |a |an )?(?:\w+\s+){0,3}"
                r"(?:form|survey|questionnaire|disclosure|attestation|certification)\b)",
        confirm=r"(?:^\s*completed\s*:|\b(?:document|envelope|form) (?:has been|was) (?:completed|"
                r"signed|submitted)\b|\ball parties have (?:completed|signed)\b|\bthank(?:s| you) for "
                r"(?:completing|submitting) (?:the |your )?(?:form|survey|disclosure)\b)",
        exclude=r"\b(?:sign[- ]?up|sign in|signed in|log ?in|password|newsletter|webinar|"
                r"unsubscribe|voluntary|optional|if you (?:wish|would like|are interested))\b",
        automated_only=True,
        item=r"\b(?:envelope|form|request|ticket)\s+(?:id\s*|no\.?\s*|number\s*|#\s*)"
             r"([A-Za-z0-9][-\w]{3,})"))

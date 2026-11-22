"""A letters store for tests (letters_fake): the semantics of letters.py
that letters_work.py relies on (accept, decline, record_draft, set_state,
add_material, set_due, view), in memory, and a made-up case."""
import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "preprocessor"))
import letters_work as lw  # noqa: E402


class FakeStore:
    def __init__(self, cases):
        self.cases = {c["id"]: c for c in copy.deepcopy(cases)}
        self.events = []

    def _dest(self, dest_id):
        for c in self.cases.values():
            for d in c["destinations"]:
                if d["id"] == dest_id:
                    return c, d
        raise ValueError(dest_id)

    def case(self, case_id):
        c = self.cases.get(case_id)
        return copy.deepcopy(c) if c else None

    def set_state(self, dest_id, state, evidence, actor):
        c, d = self._dest(dest_id)
        d["state"] = state
        self.events.append(("state", dest_id, state, evidence, actor))
        if all(x["state"] in ("declined", "submitted") for x in c["destinations"]):
            c["status"] = "closed"
        return {"dest": dest_id, "state": state}

    def accept(self, case_id, actor, quote):
        c = self.cases[case_id]
        c["decision"] = "accepted"
        for d in c["destinations"]:
            if d["state"] == "asked":
                d["state"] = "accepted"
        self.events.append(("accept", case_id, quote, actor))

    def decline(self, case_id, actor, quote):
        c = self.cases[case_id]
        c["decision"] = "declined"
        for d in c["destinations"]:
            if d["state"] in lw.OPEN_STATES:
                d["state"] = "declined"
        c["status"] = "declined"
        self.events.append(("decline", case_id, quote, actor))

    def record_draft(self, case_id, path, dest_id=""):
        c = self.cases[case_id]
        targets = [d for d in c["destinations"] if not dest_id or d["id"] == dest_id]
        if dest_id:
            targets[0]["draft"] = path
        else:
            c["base_letter"] = path
        for d in targets:
            if d["state"] in ("asked", "accepted"):
                d["state"] = "drafted"
        self.events.append(("draft", case_id, path, dest_id))

    def add_material(self, case_id, kind, ref):
        self.cases[case_id]["materials"].append({"kind": kind, "ref": ref})

    def set_due(self, dest_id, due):
        _c, d = self._dest(dest_id)
        d["due"] = due
        return True

    def view(self, case):
        out = dict(case, person_name="", person_email="", base_letter="",
                   materials=[{"kind": m.get("kind")} for m in case.get("materials") or []])
        out["destinations"] = [{k: d.get(k) for k in ("id", "due", "method", "state", "ledger_id")}
                               for d in case.get("destinations") or []]
        return out


def dest(i, name, due="", method="unknown", link="", state="asked"):
    return {"id": i, "name": name, "due": due, "method": method, "link": link, "state": state,
            "ledger_id": f"row-{i}", "draft": "", "submitted_at": "", "evidence": ""}


def a_case(**kw):
    c = {"id": "lc-1", "person_name": "Rowan Vale", "person_email": "rowan.vale@example.org",
         "kind": "recommendation", "status": "open", "decision": "", "opened": "2026-11-20",
         "materials": [], "base_letter": "", "notes": "", "keep_level": "full",
         "case_ledger_id": "row-case",
         "destinations": [dest("d1", "Harbor Tech PhD", "2026-12-01", "portal", "https://apply.example.com/r/1"),
                          dest("d2", "Pinecrest College MS", "2026-12-10", "email",
                               "mailto:letters@pinecrest.example.edu")]}
    c.update(kw)
    return c


ROWS = {"row-case": {"source_message_id": "MSG-REQ", "conversation_id": "CONV-REQ",
                     "from_email": "rowan.vale@example.org"},
        "row-d1": {"source_message_id": "MSG-PORTAL", "conversation_id": "CONV-P",
                   "from_email": "noreply@apply.example.com"}}

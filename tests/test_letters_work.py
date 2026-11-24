"""letters_work.py: the work on a letter case once it exists.

The owner's first decision (Yes / Yes, ask for materials / No) and what
each does, materials saved from the request, the brief for the
email-author subagent, questions instead of guessed facts and ratings,
per-destination copies with no other destination's name left in them,
rendering, approval and the submission step by method. Every person,
place and address here is made up.

The store is a fake with the letters.py semantics this tool relies on
(accept, decline, record_draft, set_state, add_material, set_due, view);
test_real_store_round_trip runs the same steps on letters.py itself when
the module is present.
"""
import json
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "preprocessor"))
import letters_work as lw  # noqa: E402

sys.path.insert(0, str(ROOT / "tests"))
from letters_fake import FakeStore, a_case, dest, ROWS  # noqa: E402,F401

SECRET = "uniquely-careful-thinker"   # a phrase only the letter text holds


@pytest.fixture
def ea(tmp_path, monkeypatch):
    for d in ("state", "config", "letters"):
        (tmp_path / d).mkdir()
    monkeypatch.setattr(lw, "EA_DIR", tmp_path)
    monkeypatch.setenv("EA_DIR", str(tmp_path))
    monkeypatch.setattr(lw, "_today", lambda: date(2026, 11, 23))
    monkeypatch.setattr(lw, "owner_rules", lambda: [])
    monkeypatch.setattr(lw, "_ledger_row", lambda i: dict(ROWS.get(i, {})))

    def no_mail(*a, **k):
        raise AssertionError("no mail call expected")
    monkeypatch.setattr(lw, "run_tool", no_mail)
    monkeypatch.setattr(lw, "_letterhead_template", lambda: None)
    return tmp_path


# ── words ─────────────────────────────────────────────────────────────

def test_file_names_name_their_material():
    assert lw.material_kind("Rowan_Vale_CV.pdf") == "cv"
    assert lw.material_kind("resume-2026.docx") == "cv"
    assert lw.material_kind("Statement of Purpose.pdf") == "statement"
    assert lw.material_kind("transcript_unofficial.pdf") == "transcript"
    assert lw.material_kind("programs and deadlines.xlsx") == "list"
    assert lw.material_kind("job-description.pdf") == "description"
    assert lw.material_kind("talking points.docx") == "draft"
    assert lw.material_kind("photo.png") == "other"


def test_the_owners_list_becomes_kinds():
    assert lw.parse_want("a CV and their statement") == ["cv", "statement"]
    assert lw.parse_want("resume, transcript; list of programs") == ["cv", "transcript", "list of programs"]
    assert lw.parse_want("") == []


# ── the first decision ────────────────────────────────────────────────

def test_yes_accepts_and_sends_nothing(ea):
    st = FakeStore([a_case()])
    out = lw.decide(st, "lc-1", "yes", "yes, I will write it")
    assert out["email"] == "none" and "next" not in out
    assert st.cases["lc-1"]["decision"] == "accepted"
    assert {d["state"] for d in st.cases["lc-1"]["destinations"]} == {"accepted"}
    assert "No email sent" in out["owner_line"] and "Dec 1" in out["owner_line"]
    assert not list((ea / "state").glob("*brief*"))


def test_yes_with_a_reply_asked_for_writes_a_short_yes(ea):
    st = FakeStore([a_case()])
    out = lw.decide(st, "lc-1", "yes", "say yes and tell her I am on it", reply=True)
    assert out["email"] == "yes-brief"
    brief = Path(out["brief_file"]).read_text()
    assert "SAY:" in brief and "KNOW:" in brief and "rowan.vale@example.org" in brief
    assert "create-reply" in out["next"][1] and "--message-id MSG-REQ" in out["next"][1]
    assert "--ledger-id" not in out["next"][1].split("(")[0], "a reply never names a letter row"


def test_ask_for_materials_already_attached_asks_for_nothing(ea):
    """The owner says "yes, ask for a statement and CV" and both came with
    the request: no email, and the owner is told they are already here."""
    c = a_case(materials=[{"kind": "cv", "ref": "MSG-REQ#Rowan_Vale_CV.pdf"},
                          {"kind": "statement", "ref": "MSG-REQ#statement.pdf"}])
    st = FakeStore([c])
    out = lw.decide(st, "lc-1", "materials", "yes I will write, ask for her statement and CV",
                    want=lw.parse_want("statement and CV"))
    assert out["missing"] == [] and out["email"] == "none" and "next" not in out
    assert "Rowan_Vale_CV.pdf" in out["owner_line"] and "statement.pdf" in out["owner_line"]
    assert "Nothing to ask for" in out["owner_line"]
    assert st.cases["lc-1"]["decision"] == "accepted"


def test_ask_for_materials_asks_only_for_what_is_missing(ea):
    c = a_case(materials=[{"kind": "cv", "ref": "MSG-REQ#Rowan_Vale_CV.pdf"}])
    c["destinations"][1]["due"] = ""
    st = FakeStore([c])
    out = lw.decide(st, "lc-1", "materials", "yes, ask for what you need")
    assert out["missing"] == ["statement", "list"]
    brief = Path(out["brief_file"]).read_text()
    say = brief.split("SAY:")[1].split("KNOW:")[0]
    assert "statement" in say and "the due date for Pinecrest College MS" in say
    assert "CV" not in say, "never ask again for a file they sent"
    assert "Already received" in brief.split("KNOW:")[1] and "Rowan_Vale_CV.pdf" in brief
    assert "only for" in out["owner_line"] and "Rowan_Vale_CV.pdf" in out["owner_line"]


def test_with_nothing_recorded_the_request_mail_is_read_before_asking(ea, monkeypatch):
    """The fault this guards: a reply asked for a CV and a statement the
    request already carried."""
    st = FakeStore([a_case()])
    monkeypatch.setattr(lw, "_list_attachments", lambda prov, acct, mid: ([
        {"id": "a1", "name": "CV-Rowan.pdf", "is_inline": False},
        {"id": "a2", "name": "personal_statement.pdf", "is_inline": False},
        {"id": "a3", "name": "logo.png", "is_inline": True, "is_image": True, "small": True}], "")
        if mid == "MSG-REQ" else ([], "wrong message"))
    out = lw.decide(st, "lc-1", "materials", "yes, ask for her CV and statement",
                    want=["cv", "statement"])
    assert out["checked_mail"]["files"] == ["CV-Rowan.pdf", "personal_statement.pdf"]
    assert out["missing"] == [] and out["email"] == "none"
    assert "CV-Rowan.pdf" in out["owner_line"]


def test_a_decline_needs_the_owners_words(ea):
    st = FakeStore([a_case()])
    with pytest.raises(lw.LetterError) as e:
        lw.decide(st, "lc-1", "no", "")
    assert "Will you write it?" in str(e.value)
    assert st.cases["lc-1"]["decision"] == "" and not st.events


def test_no_declines_the_case_and_stages_a_reply_for_the_send_button(ea):
    st = FakeStore([a_case()])
    out = lw.decide(st, "lc-1", "no", "no, I do not know her work well enough")
    assert st.cases["lc-1"]["decision"] == "declined"
    assert {d["state"] for d in st.cases["lc-1"]["destinations"]} == {"declined"}
    assert out["email"] == "decline-brief"
    assert not any("send-draft" in step for step in out["next"]), "nothing sends here"
    brief = Path(out["brief_file"]).read_text()
    assert "Say no" in brief and "do not know her work well enough" in brief
    assert "waits for your Send" in out["owner_line"]


def test_a_decline_after_a_yes_says_so(ea):
    st = FakeStore([a_case(decision="accepted")])
    out = lw.decide(st, "lc-1", "no", "I can't do this one after all")
    assert "no longer write" in Path(out["brief_file"]).read_text()


def test_a_kept_case_gets_no_names_and_no_reply(ea):
    st = FakeStore([a_case(keep_level="local",
                           materials=[{"kind": "cv", "ref": "MSG-REQ#Rowan_Vale_CV.pdf"}])])
    out = lw.decide(st, "lc-1", "materials", "yes, ask for the rest")
    assert out["email"] == "none" and "next" not in out
    text = json.dumps(out)
    assert "Rowan" not in text and "Harbor" not in text and "example.org" not in text
    assert "kept on your own model" in out["owner_line"]
    for verb in (lambda: lw.brief(st, "lc-1", owner_said="draft it"),
                 lambda: lw.collect(st, "lc-1", ["MSG-REQ"])):
        with pytest.raises(lw.LetterError):
            verb()
    status = json.dumps(lw.status(st, "lc-1"))
    assert "Rowan" not in status and "Harbor" not in status


def test_the_decision_line_names_what_the_request_included(ea):
    c = a_case(materials=[{"kind": "cv", "ref": "MSG-REQ#cv.pdf"}])
    line = lw.status(FakeStore([c]), "lc-1")["decision"]
    assert line.startswith("Rowan Vale asks for a recommendation letter.")
    assert "Harbor Tech PhD (Dec 1)" in line and "cv.pdf" in line
    assert line.endswith("Will you write it? Yes / Yes, ask for materials / No")
    assert "decision" not in lw.status(FakeStore([a_case(decision="accepted")]), "lc-1")


# ── the Action requests buttons ───────────────────────────────────────

def test_buttons_yes_and_yes_ask(ea):
    # The yes carries the case's one date (the earliest a place states) and
    # says when the case comes back: one tap, no second question.
    st = FakeStore([a_case()])
    out = lw.answer("lc-1", "yes", store=st)
    assert out["ok"] and "turn" not in out
    assert out["said"] == "You said yes, by Tue Dec 1. I bring it back on Tue Nov 24."
    assert st.cases["lc-1"]["do_by"] == "2026-12-01"
    st = FakeStore([a_case(materials=[{"kind": "cv", "ref": "x#cv.pdf"},
                                      {"kind": "statement", "ref": "x#sop.pdf"}])])
    out = lw.answer("lc-1", "yes_ask", store=st)
    assert out["ok"] and "turn" not in out and "Nothing to ask for" in out["said"]
    st = FakeStore([a_case()])
    out = lw.answer("lc-1", "yes_ask", store=st)
    assert "turn" in out and "--answer materials" in out["turn"]
    assert st.cases["lc-1"]["decision"] == "accepted"


def test_button_no_closes_and_hands_the_reply_to_a_turn(ea):
    st = FakeStore([a_case()])
    out = lw.answer("lc-1", "no", store=st)
    assert out["ok"] and st.cases["lc-1"]["decision"] == "declined"
    assert "--answer no" in out["turn"] and "Never send" in out["turn"]
    st = FakeStore([a_case(decision="accepted")])
    out = lw.answer("lc-1", "decline", store=st)
    assert "--after-yes" in out["turn"]


def test_button_submitted_counts_and_closes(ea):
    c = a_case(decision="accepted")
    for d in c["destinations"]:
        d["state"] = "ready"
    st = FakeStore([c])
    assert lw.answer("lc-1", "submitted", dest="d1", store=st)["said"] == "Submitted. 1 of 2 done."
    out = lw.answer("lc-1", "submitted", dest="d2", store=st)
    assert "Submitted: all. The letter for Rowan Vale is off your list." in out["said"]
    assert lw.answer("lc-1", "submitted", dest="d2", store=st)["ok"] is False


def test_button_date(ea):
    c = a_case()
    c["destinations"][1]["due"] = ""
    st = FakeStore([c])
    out = lw.answer("lc-1", "date", text="Dec 12", store=st)
    assert out["ok"] and st.cases["lc-1"]["destinations"][1]["due"].endswith("-12-12")
    assert lw.answer("lc-1", "date", text="soon", store=st)["ok"] is False


def test_buttons_on_a_kept_case_start_no_turn(ea):
    st = FakeStore([a_case(keep_level="facts")])
    for choice in ("yes_ask", "draft"):
        out = lw.answer("lc-1", choice, store=st)
        assert "turn" not in out and "Rowan" not in out["said"]
    st = FakeStore([a_case(keep_level="facts")])
    assert "turn" not in lw.answer("lc-1", "no", store=st)


def test_button_lines_are_plain_words(ea):
    sys.path.insert(0, str(ROOT / "tests"))
    import plain_words
    lines = []
    for choice, kw in (("yes", {}), ("yes_ask", {}), ("draft", {}), ("no", {}),
                       ("date", {"text": "Dec 12", "dest": "d2"}), ("date", {"text": "soon"}),
                       ("submitted", {"dest": "d1"}), ("bogus", {})):
        lines.append(lw.answer("lc-1", choice, store=FakeStore([a_case()]), **kw)["said"])
    for line in lines:
        assert not plain_words.JARGON.search(line), line
        assert chr(0x2014) not in line and len(line) < 200, line


def test_button_answer_reads_no_file(ea, monkeypatch):
    """The owner's process runs answer(): it reads the store only."""
    def no_file(*a, **k):
        raise AssertionError("answer() read a file in the resident's space")
    monkeypatch.setattr(lw, "_requirements", no_file)
    monkeypatch.setattr(lw, "case_dir", no_file)
    monkeypatch.setattr(lw, "tier", no_file)
    st = FakeStore([a_case()])
    for choice in ("yes", "draft", "no"):
        assert lw.answer("lc-1", choice, store=st)["ok"]


# ── materials ─────────────────────────────────────────────────────────

def test_collect_saves_the_request_files(ea, monkeypatch):
    calls = []

    def tool(script, args):
        calls.append((script, args))
        if args[0] == "get-attachments" and args[-1] != "MSG-REQ":
            return 0, []
        if args[0] == "get-attachments":
            return 0, [{"id": "A1", "name": "Rowan Vale CV.pdf", "size": 5, "is_inline": False},
                       {"id": "A2", "name": "image001.png", "is_inline": True, "is_image": True},
                       {"id": "A3", "name": "Programs.xlsx", "size": 5, "is_inline": False}]
        out = Path(args[args.index("--output") + 1])
        out.write_bytes(b"x" * 5)
        return 0, {"saved": str(out)}
    monkeypatch.setattr(lw, "run_tool", tool)
    # Intake recorded the file names the request mail carried.
    st = FakeStore([a_case(materials=[{"kind": "cv", "ref": "Rowan Vale CV.pdf"}])])
    out = lw.collect(st, "lc-1")
    assert {s["kind"] for s in out["saved"]} == {"cv", "list"}
    assert all(Path(s["path"]).parent.name == "materials" for s in out["saved"])
    assert Path(out["folder"]).parent == ea / "letters"
    assert out["skipped"][0]["name"] == "image001.png"
    assert {m["kind"] for m in st.cases["lc-1"]["materials"]} == {"cv", "list"}
    assert calls[0] == ("graph_actions.py", ["get-attachments", "--message-id", "MSG-REQ"])
    assert lw.present_materials(st.case("lc-1"))["cv"] == ["Rowan Vale CV.pdf"], "the mailed and the saved file are one"
    again = lw.collect(st, "lc-1", ["MSG-REQ"])
    assert again["saved"] == [] and len(st.cases["lc-1"]["materials"]) == 3
    st.cases["lc-1"]["decision"] = "accepted"
    for d in st.cases["lc-1"]["destinations"]:
        d["state"] = "accepted"
    assert lw.brief(st, "lc-1", owner_said="draft it")["unsaved_materials"] == []


def test_status_lists_what_is_missing(ea):
    c = a_case(decision="accepted")
    c["destinations"][0].update(state="accepted")
    c["destinations"][1].update(state="accepted", due="", method="unknown")
    out = lw.status(FakeStore([c]), "lc-1")
    assert out["missing"]["dates"] == ["Pinecrest College MS"]
    assert out["missing"]["methods"] == ["Pinecrest College MS"]
    assert set(out["missing"]["materials"]) == {"cv", "statement"}
    assert out["destinations"][0]["next"] == "draft the letter (brief)"
    assert out["tiers"] == {"draft": "ask-first", "upload": "do-and-tell"}


def test_a_later_destination_joins_and_gets_its_copy(ea):
    """A second portal mail arrives after the owner said yes: the place
    joins the case and gets its copy of the base letter; no new decision."""
    c = a_case(decision="accepted")
    c["destinations"][0]["state"] = "ready"
    c["destinations"][1]["state"] = "asked"
    st = FakeStore([c])
    cdir = lw.case_dir(st.case("lc-1"))
    base = cdir / "letter.md"
    base.write_text(f"Dear {{{{addressee}}}},\n\nI recommend Rowan Vale for {{{{program}}}}. {SECRET}.\n\nSincerely,\nKit\n")
    st.cases["lc-1"]["base_letter"] = str(base)
    out = lw.extend(st, "lc-1", "d2")
    assert st.cases["lc-1"]["destinations"][1]["state"] == "drafted"
    made = out["variant"]["variants"]
    assert len(made) == 1 and "Pinecrest College MS" in Path(made[0]["file"]).read_text()
    assert "Harbor" not in Path(made[0]["file"]).read_text()
    assert SECRET not in json.dumps(out)


# ── questions ─────────────────────────────────────────────────────────

def test_a_rating_is_the_owners_answer(ea):
    st = FakeStore([a_case(decision="accepted")])
    lw.require(st, "lc-1", "d1", question="Overall rating", options="Top 1%|Top 5%|Top 10%", rating=True)
    with pytest.raises(lw.LetterError):
        lw.answered(st, "lc-1", "d1", "q1", "Top 5%", "")
    with pytest.raises(lw.LetterError):
        lw.answered(st, "lc-1", "d1", "q1", "Top 50%", "top fifty")
    out = lw.answered(st, "lc-1", "d1", "q1", "top 5%", "put her in the top 5 percent")
    assert out["answer"] == "Top 5%" and out["open_questions"] == []


# ── the brief ─────────────────────────────────────────────────────────

def test_drafting_asks_first_by_default(ea):
    st = FakeStore([a_case(decision="accepted")])
    out = lw.brief(st, "lc-1")
    assert out["status"] == "ask-first" and "Shall I draft the letter for Rowan Vale?" in out["ask"]
    assert not list((ea / "letters").rglob("brief*.txt"))


def test_a_promoted_tier_drafts_ahead(ea):
    (ea / "config" / "autonomy-tiers.json").write_text(json.dumps(
        {"task_tiers": {"draft_letter": {"tier": "do-and-tell"}}}))
    st = FakeStore([a_case(decision="accepted", destinations=[dest("d1", "Harbor Tech PhD", state="accepted")])])
    assert lw.brief(st, "lc-1")["status"] == "brief"


def test_the_brief_carries_points_materials_and_rules(ea, monkeypatch):
    c = a_case(decision="accepted")
    for d in c["destinations"]:
        d["state"] = "accepted"
    st = FakeStore([c])
    cdir = lw.case_dir(st.case("lc-1"))
    (cdir / "materials").mkdir()
    cv = cdir / "materials" / "cv.txt"
    cv.write_text("Rowan Vale. Built a sensor rig in 2025.")
    st.cases["lc-1"]["materials"] = [{"kind": "cv", "ref": str(cv)},
                                     {"kind": "statement", "ref": "MSG-REQ#sop.pdf"}]
    earlier = ea / "letters" / "rowan-vale-rec-2025.tex"
    earlier.write_text("An earlier letter.")
    (ea / "letters" / "someone-else-rec-2026.tex").write_text("Another letter of this kind.")
    monkeypatch.setattr(lw, "_excerpt", lambda p: Path(p).read_text())
    (ea / "config" / "user-profile.json").write_text(json.dumps(
        {"name": "Kit Moss", "role_description": "A shop manager at a bike cooperative. Twenty staff."}))
    lw.require(st, "lc-1", "d1", question="Rate their teamwork", options="High|Medium|Low", rating=True)
    lw.require(st, "lc-1", "d2", length="two pages")
    notes = ea / "state" / "dictation.txt"
    notes.write_text("Rowan ran the evening shift for a year.\nReliable with customers.")
    out = lw.brief(st, "lc-1", notes_file=str(notes), owner_said="draft the letter for Rowan")
    text = Path(out["brief"]).read_text()
    assert "WRITER: Kit Moss, A shop manager at a bike cooperative." in text
    assert "Rowan ran the evening shift for a year." in text
    assert "Built a sensor rig in 2025." in text
    assert "rowan-vale-rec-2025.tex" in text.split("this person")[1].split("\n")[0]
    assert "someone-else-rec-2026.tex" in text and "never their facts" in text
    assert "{{program}}" in text and "{{addressee}}" in text
    assert "FORM QUESTIONS" in text and "Rate their teamwork" in text
    assert "[ASK:" in text and "Never state a rating" in text
    assert "LENGTH: two pages." in text
    assert out["unsaved_materials"] == ["sop.pdf"] and "collect" in out["next"][-1]
    assert out["next"][0].startswith("Agent email-author, in the foreground")


# ── checks and copies ─────────────────────────────────────────────────

def _drafted(ea, base_text):
    c = a_case(decision="accepted")
    for d in c["destinations"]:
        d["state"] = "drafted"
    st = FakeStore([c])
    cdir = lw.case_dir(st.case("lc-1"))
    base = cdir / "letter.md"
    base.write_text(base_text)
    st.cases["lc-1"]["base_letter"] = str(base)
    return st, cdir, base


def test_check_finds_questions_and_records(ea):
    st, _cdir, base = _drafted(ea, "Dear {{addressee}},\n\nRowan led [ASK: which project?] for "
                                   "{{program}}.\n\nSincerely,\nKit\n")
    out = lw.check(st, "lc-1", str(base), record=True)
    assert out["questions"] == ["which project?"] and out["recorded"] is True
    assert "which project?" in out["owner_line"]
    assert st.events[-1][0] == "draft"


def test_a_base_letter_naming_one_place_is_not_recorded(ea):
    st, _cdir, base = _drafted(ea, "Dear Committee,\n\nRowan is ready for Harbor Tech PhD.\n\nSincerely,\nKit\n")
    out = lw.check(st, "lc-1", str(base), record=True)
    assert out["other_destinations"] == ["Harbor Tech PhD"] and out["recorded"] is False


def test_copies_per_destination_carry_no_other_name(ea):
    st, cdir, base = _drafted(ea, f"Dear {{{{addressee}}}},\n\nI recommend Rowan Vale for the "
                                  f"{{{{program}}}}. {SECRET}.\n\nSincerely,\nKit\n")
    lw.require(st, "lc-1", "d1", addressee="Admissions Committee", program="doctoral program in robotics")
    out = lw.variants(st, "lc-1", str(base))
    files = {v["dest"]: Path(v["file"]).read_text() for v in out["variants"]}
    assert "Dear Admissions Committee," in files["d1"] and "doctoral program in robotics" in files["d1"]
    assert "Pinecrest College MS" in files["d2"] and "Harbor" not in files["d2"]
    assert "Pinecrest" not in files["d1"]
    assert SECRET not in json.dumps(out)
    assert {st.cases["lc-1"]["destinations"][i]["draft"] for i in (0, 1)} == set(v["file"] for v in out["variants"])


def test_a_copy_that_keeps_another_name_is_refused(ea):
    st, _cdir, base = _drafted(ea, "Dear {{addressee}},\n\nRowan is a fit for {{program}}, as for "
                                   "Harbor Tech PhD.\n\nSincerely,\nKit\n")
    out = lw.variants(st, "lc-1", str(base))
    assert [r["dest"] for r in out["refused"]] == ["d2"]
    assert "Harbor Tech PhD" in out["refused"][0]["problems"][0]


def test_a_letter_written_for_one_place_is_swapped_for_the_others(ea):
    st, _cdir, base = _drafted(ea, "Dear Committee,\n\nRowan fits Harbor Tech PhD well.\n\nSincerely,\nKit\n")
    out = lw.variants(st, "lc-1", str(base), base_dest="d1")
    files = {v["dest"]: Path(v["file"]).read_text() for v in out["variants"]}
    assert "Rowan fits Pinecrest College MS well." in files["d2"] and "Harbor" not in files["d2"]


def test_one_letter_with_no_names_serves_every_place(ea):
    st, _cdir, base = _drafted(ea, "Dear Committee,\n\nRowan is excellent.\n\nSincerely,\nKit\n")
    out = lw.variants(st, "lc-1", str(base))
    assert out["variants"] == [] and "one letter serves every place" in out["note"]


# ── rendering, approval, submission ───────────────────────────────────

def _fake_compile(tex, pdf):
    pdf.write_bytes(b"%PDF-1.4 fake")
    return True


def test_render_plain_and_on_the_letterhead(ea, monkeypatch):
    st, cdir, base = _drafted(ea, f"Dear Committee,\n\nRowan is a {SECRET} & a fine colleague (100%).\n\n"
                                  "Sincerely,\nKit Moss\n")
    monkeypatch.setattr(lw, "_compile", _fake_compile)
    (ea / "config" / "user-profile.json").write_text(json.dumps({"name": "Kit Moss", "department": "Repairs"}))
    out = lw.render(st, "lc-1", str(base))
    tex = Path(out["tex"]).read_text()
    assert out["letterhead"] is False and Path(out["pdf"]).exists()
    assert r"\& a fine colleague (100\%)" in tex and "Kit Moss" in tex and "Repairs" in tex
    assert "Dear Committee," in tex and "Sincerely," in tex and "<<" not in tex
    assert SECRET not in json.dumps(out), "the output names the file, never the text"
    tpl = ea / "lh" / "template.tex"
    tpl.parent.mkdir()
    tpl.write_text("HEAD <<RECIPIENT_BLOCK>> | <<SALUTATION>> | <<BODY>> | <<CLOSING>> FOOT")
    monkeypatch.setattr(lw, "_letterhead_template", lambda: tpl)
    out = lw.render(st, "lc-1", str(base))
    tex = Path(out["tex"]).read_text()
    assert out["letterhead"] is True and tex.startswith("HEAD $\\quad$ | Dear Committee, |")
    assert tex.endswith("| Sincerely, FOOT") and "Kit Moss" not in tex


def test_render_refuses_a_letter_with_placeholders(ea, monkeypatch):
    st, _cdir, base = _drafted(ea, "Dear {{addressee}},\n\nFor {{program}}.\n\nSincerely,\nKit\n")
    monkeypatch.setattr(lw, "_compile", _fake_compile)
    with pytest.raises(lw.LetterError) as e:
        lw.render(st, "lc-1", str(base))
    assert "variants" in str(e.value)


def test_approval_needs_the_owner_and_a_clean_rendered_letter(ea, monkeypatch):
    st, cdir, base = _drafted(ea, "Dear Committee,\n\nRowan led [ASK: which shift?].\n\nSincerely,\nKit\n")
    monkeypatch.setattr(lw, "_compile", _fake_compile)
    with pytest.raises(lw.LetterError):
        lw.approve(st, "lc-1", "")
    out = lw.approve(st, "lc-1", "looks good")
    assert out["ready"] == [] and any("question(s) left" in w for b in out["blocked"] for w in b["why"])
    base.write_text("Dear Committee,\n\nRowan led the evening shift.\n\nSincerely,\nKit\n")
    out = lw.approve(st, "lc-1", "looks good")
    assert out["ready"] == [] and "not rendered yet (render)" in out["blocked"][0]["why"]
    lw.render(st, "lc-1", str(base))
    lw.require(st, "lc-1", "d1", question="Rating", options="High|Low", rating=True)
    out = lw.approve(st, "lc-1", "looks good, send them")
    assert [r["dest"] for r in out["ready"]] == ["d2"] and out["blocked"][0]["dest"] == "d1"
    assert st.cases["lc-1"]["destinations"][1]["state"] == "ready"


def _ready(ea, monkeypatch):
    st, cdir, base = _drafted(ea, f"Dear Committee,\n\nRowan is {SECRET}.\n\nSincerely,\nKit\n")
    monkeypatch.setattr(lw, "_compile", _fake_compile)
    lw.render(st, "lc-1", str(base))
    for d in st.cases["lc-1"]["destinations"]:
        d["state"] = "ready"
    return st, base.with_suffix(".pdf")


def test_submit_by_email_rides_the_send_button(ea, monkeypatch):
    st, pdf = _ready(ea, monkeypatch)
    out = lw.submit(st, "lc-1", "d2")
    assert out["method"] == "email" and out["tier"] == "button-required"
    draft = out["steps"][1]
    assert "create-draft" in draft and "--to letters@pinecrest.example.edu" in draft
    assert "--ledger-id row-d2" in draft, "the send that carries the letter closes its place"
    assert f"--file {pdf}" in out["steps"][2]
    brief = sorted((ea / "state").glob("*cover*"))[0].read_text()
    assert "Attached is my recommendation letter for Rowan Vale" in brief
    assert SECRET not in json.dumps(out)


def test_submit_to_a_portal_waits_for_the_owners_answers(ea, monkeypatch):
    st, pdf = _ready(ea, monkeypatch)
    lw.require(st, "lc-1", "d1", question="Rank the applicant", options="Top 5%|Top 25%", rating=True)
    out = lw.submit(st, "lc-1", "d1")
    assert out["blocked"] == ["Rank the applicant"] and "steps" not in out
    lw.answered(st, "lc-1", "d1", "q1", "Top 5%", "top five percent")
    out = lw.submit(st, "lc-1", "d1")
    assert out["tier"] == "do-and-tell" and "web_browser.py fill" in out["steps"][1]
    assert "https://apply.example.com/r/1" in out["owner_line"] and str(pdf) in out["owner_line"]


def test_submit_by_the_owner_gives_the_link_and_the_file(ea, monkeypatch):
    st, pdf = _ready(ea, monkeypatch)
    st.cases["lc-1"]["destinations"][0].update(method="upload", link="https://jobs.example.net/ref")
    out = lw.submit(st, "lc-1", "d1")
    assert out["tier"] == "owner" and "https://jobs.example.net/ref" in out["owner_line"]
    assert str(pdf) in out["owner_line"] and "record-submitted --dest d1" in out["record"]


def test_submit_refuses_an_unapproved_letter(ea):
    st = FakeStore([a_case(decision="accepted")])
    with pytest.raises(lw.LetterError) as e:
        lw.submit(st, "lc-1", "d1")
    assert "approves the text first" in str(e.value)


# ── the CLI ───────────────────────────────────────────────────────────

def test_cli_prints_json_and_refuses_cleanly(ea, capsys):
    st = FakeStore([a_case()])
    assert lw.main(["decide", "--case", "lc-1", "--answer", "yes", "--owner-said", "yes"], store=st) == 0
    assert json.loads(capsys.readouterr().out)["answer"] == "yes"
    assert lw.main(["status", "--case", "nope"], store=st) == 2
    assert "no letter case" in json.loads(capsys.readouterr().out)["error"]


def test_every_verb_parses_with_its_documented_flags():
    p = lw._parser()
    for argv in (["status", "--case", "c"],
                 ["decide", "--case", "c", "--answer", "materials", "--owner-said", "x", "--want", "cv",
                  "--dest", "d", "--reply", "--no-reply", "--after-yes"],
                 ["collect", "--case", "c", "--message-id", "m", "--provider", "gmail", "--account", "a"],
                 ["extend", "--case", "c", "--dest", "d"],
                 ["require", "--case", "c", "--dest", "d", "--length", "1 page", "--format", "pdf",
                  "--addressee", "a", "--program", "p", "--organization", "o", "--question", "q",
                  "--options", "a|b", "--rating"],
                 ["answered", "--case", "c", "--dest", "d", "--question", "q1", "--value", "v",
                  "--owner-said", "x"],
                 ["brief", "--case", "c", "--dest", "d", "--notes-file", "f", "--owner-said", "x"],
                 ["check", "--case", "c", "--file", "f", "--dest", "d", "--record"],
                 ["variants", "--case", "c", "--base", "f", "--base-dest", "d"],
                 ["render", "--case", "c", "--file", "f", "--dest", "d", "--docx", "--template", "t"],
                 ["approve", "--case", "c", "--dest", "d", "--owner-said", "x"],
                 ["submit", "--case", "c", "--dest", "d"]):
        p.parse_args(argv)


# ── the real store ────────────────────────────────────────────────────

def test_real_store_round_trip(tmp_path, monkeypatch):
    """The same steps on letters.py itself, once it is in the tree."""
    try:
        import letters
    except ImportError:
        pytest.skip("letters.py is not in this tree yet")
    import ledger
    for d in ("state", "config", "letters"):
        (tmp_path / d).mkdir()
    monkeypatch.setattr(lw, "EA_DIR", tmp_path)
    monkeypatch.setattr(lw, "owner_rules", lambda: [])
    monkeypatch.setattr(lw, "_ledger_row", lambda i: {})
    monkeypatch.setattr(lw, "_compile", _fake_compile)
    monkeypatch.setattr(lw, "_letterhead_template", lambda: None)
    conn = letters.connect(tmp_path / "state" / "ledger.db")
    st = lw.Store(conn, letters)
    cid = letters.ensure_case(conn, "Rowan Vale", "rowan.vale@example.org", "recommendation")
    d1 = letters.add_destination(conn, cid, "Harbor Tech PhD", due="2026-12-01", method="email",
                                 link="mailto:letters@harbor.example.edu")
    letters.add_material(conn, cid, "cv", "MSG-REQ#Rowan_Vale_CV.pdf")
    letters.add_material(conn, cid, "statement", "MSG-REQ#statement.pdf")
    conn.commit()
    out = lw.decide(st, cid, "materials", "yes, ask for the CV and statement", want=["cv", "statement"])
    assert out["missing"] == [] and st.case(cid)["decision"] == "accepted"
    cdir = lw.case_dir(st.case(cid))
    base = cdir / "letter.md"
    base.write_text("Dear Committee,\n\nRowan is excellent.\n\nSincerely,\nKit\n")
    assert lw.check(st, cid, str(base), record=True)["recorded"]
    lw.render(st, cid, str(base))
    assert lw.approve(st, cid, "ready")["ready"][0]["dest"] == d1
    assert "--ledger-id" in lw.submit(st, cid, d1)["steps"][1]
    assert lw.answer(cid, "submitted", dest=d1, store=st)["ok"]
    assert st.case(cid)["status"] == "closed"
    del ledger

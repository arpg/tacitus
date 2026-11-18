"""The outbound-web review flags what the user chose and clears otherwise.

Picks: blocked (SSRF guard), errored (count), exfil-shaped URLs (an
address, a long or encoded path segment, query value or DNS label), many
subdomains, volume spikes and unusual volume, new hosts, poll names and
tampering with the ea-bot log. Each log keeps its own byte-offset cursor.
It always returns a line so the daily review is visibly running.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "preprocessor"))
import outbound_review as R  # noqa: E402
import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_owner_hosts(monkeypatch):
    """No test reads an owner-private host list from the host it runs on."""
    monkeypatch.setattr(R, "_OWNER_HOSTS", {"token_hosts": frozenset(),
                                            "id_path_hosts": frozenset(),
                                            "own_addresses": frozenset()})


def _rec(event, url="https://example.com/a", host="example.com", **kw):
    return {"ts": kw.pop("ts", "2026-09-28T10:00:00Z"), "event": event,
            "url": url, "host": host, **kw}


def test_exfil_flags_only_an_email_in_the_url():
    assert R.exfil_reason("https://en.wikipedia.org/wiki/Robotics") == ""
    assert R.exfil_reason("https://www.google.com/search?q=snow+radar") == ""
    assert "email" in R.exfil_reason("https://x.com/p?to=mika.kowalczyk75@example.edu")
    # long OAuth-style URLs and base64 tokens do NOT flag (they are normal)
    assert R.exfil_reason("https://login.microsoftonline.com/" + "a" * 1500) == ""
    assert R.exfil_reason("https://x.com/cb?code=" + "QWxs" * 30) == ""
    # but length is tracked as an informational signal
    assert R.long_url("https://x.com/" + "a" * 400) is True
    assert R.long_url("https://x.com/short") is False


def test_analyze_flags_blocked_exfil_and_spikes():
    recs = [_rec("fetch", host="a.com")] * 3
    recs += [_rec("blocked", url="http://169.254.169.254/latest/meta-data",
                  host="169.254.169.254", reason="link-local address")]
    recs += [_rec("error", reason="Timeout")] * 4
    recs += [_rec("fetch", url="https://out.co/p?to=mika.kowalczyk75@example.edu", host="out.co")]
    recs += [_rec("fetch", url="https://ok.co/" + "/".join(["section"] * 60), host="ok.co")]
    recs += [_rec("fetch", host="loud.com")] * 31
    rep = R.analyze(recs, spike_n=30)
    assert rep["fetches"] == 3 + 1 + 1 + 31
    assert rep["long_urls"] == 1
    assert rep["errored"] == 4 and len(rep["errored_sample"]) == 3
    assert [b["reason"] for b in rep["blocked"]] == ["link-local address"]
    assert [e["host"] for e in rep["exfil"]] == ["out.co"]
    assert rep["spikes"] == [("loud.com", 31)]
    assert R.has_flags(rep) is True


def test_analyze_all_clear():
    rep = R.analyze([_rec("fetch", host="a.com"), _rec("done"), _rec("error", reason="x")])
    assert not R.has_flags(rep)
    line = R.render(rep, window="today")
    assert "Outbound web review (today)" in line and "Nothing looked off" in line
    assert "1 failed" in line


def test_render_lists_each_flag():
    rep = R.analyze(
        [_rec("blocked", host="10.0.0.1", reason="private address"),
         _rec("fetch", url="https://o.co/?to=rio.sorensen85@example.com", host="o.co")] +
        [_rec("fetch", host="loud.com")] * 40, spike_n=30)
    out = R.render(rep)
    assert "BLOCKED 10.0.0.1: private address" in out
    assert "CHECK URL" in out and "o.co" in out
    assert "HIGH VOLUME loud.com: 40" in out


def test_read_records_windows_by_ts(tmp_path):
    log = tmp_path / "fetch-url.log"
    lines = [json.dumps(_rec("fetch", ts="2026-09-27T10:00:00Z")),
             json.dumps(_rec("fetch", ts="2026-09-28T10:00:00Z")),
             "{not valid json",
             json.dumps(_rec("blocked", ts="2026-09-28T12:00:00Z", reason="x"))]
    log.write_text("\n".join(lines) + "\n")
    recs = R.read_records("2026-09-28T00:00:00Z", path=log)
    assert [r["event"] for r in recs] == ["fetch", "blocked"]
    assert R.latest_ts(recs) == "2026-09-28T12:00:00Z"
    assert len(R.read_records(None, path=log)) == 3   # None = all valid lines
    assert R.read_records(None, path=tmp_path / "missing.log") == []


# --- the model's own WebFetch and WebSearch (web_tool_log) -------------------

import pytest  # noqa: E402
import resident_io  # noqa: E402
import web_tool_log as W  # noqa: E402


def _web(event, ts="2026-09-28T10:00:00Z", lane="mono", **kw):
    return {"ts": ts, "event": event, "lane": lane, "run_id": "r1", **kw}


def test_the_web_tool_record_keeps_the_url_or_the_query_and_skips_other_tools(tmp_path):
    log = tmp_path / "web-tools.jsonl"
    W.note("WebFetch", {"url": "https://Example.org/p?q=1", "prompt": "summarise"},
           lane="mono", run_id="run-1", path=log)
    W.note("WebSearch", {"query": "snow radar", "allowed_domains": ["nasa.gov"]},
           lane="cron:morning-briefing", path=log)
    W.note("Bash", {"command": "ls"}, path=log)
    W.note("WebFetch", "not a dict", path=tmp_path / "no-dir" / "x.jsonl")  # never raises
    recs = [json.loads(ln) for ln in log.read_text().splitlines()]
    assert [r["event"] for r in recs] == ["webfetch", "websearch"]
    assert recs[0]["host"] == "example.org" and recs[0]["run_id"] == "run-1"
    assert "prompt" not in recs[0]
    assert recs[1]["query"] == "snow radar" and recs[1]["allowed_domains"] == ["nasa.gov"]
    assert (log.stat().st_mode & 0o777) == 0o600


B64 = "U2VjcmV0IG1lZXRpbmcgbm90ZXMgZm9yIDIwMjY"


@pytest.mark.parametrize("rec,why", [
    (_web("webfetch", url="https://en.wikipedia.org/wiki/Simultaneous_localization_and_mapping"), ""),
    (_web("webfetch", url="https://github.com/arpg/x/commit/" + "0123456789abcdef" * 2 + "01234567"), ""),
    (_web("webfetch", url="https://www.nsf.gov/pubs/2026/nsf26501/nsf26501.htm?WT.mc_id=USNSF_25"), ""),
    (_web("webfetch", url="https://site.example/The-2024-Robotics-Workshop-Agenda-And-Speakers-Page"), ""),
    (_web("websearch", query="ICRA 2027 submission deadline"), ""),
    (_web("webfetch", url="https://x.example/c?to=mika.kowalczyk75%40example.edu"), "email address"),
    (_web("webfetch", url="https://x.example/c?d=" + "abc" * 80), "query string of"),
    (_web("webfetch", url="https://x.example/c?d=" + B64), "encoded payload"),
    (_web("webfetch", url="https://x.example/" + B64 + "/a.png"), "encoded payload"),
    (_web("webfetch", url="https://x.example/c?h=" + "deadbeef" * 5), "encoded payload"),
    (_web("webfetch", url="https://x.example/c?t=" + "%41%42" * 8), "encoded payload"),
    (_web("websearch", query="rio.sorensen85@example.com contract"), "email address"),
    (_web("websearch", query="x " * 120), "search of"),
    (_web("websearch", query=B64), "encoded payload"),
])
def test_model_web_requests_flag_emails_long_queries_and_encoded_payloads(rec, why):
    got = R.web_tool_reason(rec)
    assert (why in got) if why else got == ""


def test_the_review_reports_model_web_requests_and_a_web_tool_in_a_cron_lane():
    recs = [_web("webfetch", url="https://a.example/x", host="a.example"),
            _web("webfetch", url="https://out.example/?d=" + B64, host="out.example"),
            _web("websearch", query="snow radar"),
            _web("websearch", query="snow radar", lane="cron:morning-briefing")]
    rep = R.analyze([])
    rep.update(R.analyze_web_tools(recs))
    assert (rep["web_fetches"], rep["web_searches"], rep["web_hosts"]) == (2, 2, 2)
    assert [e["host"] for e in rep["web_exfil"]] == ["out.example"]
    assert rep["web_cron_lanes"] == ["cron:morning-briefing"]
    assert R.has_flags(rep)
    out = R.render(rep, window="today")
    assert "Model web tools: 2 WebFetch to 2 host(s), 2 WebSearch." in out
    assert "CHECK WebFetch (encoded payload in the URL) in mono" in out
    assert "WEB TOOL IN A CRON LANE cron:morning-briefing" in out
    clear = R.analyze([])
    clear.update(R.analyze_web_tools(recs[:1] + recs[2:3]))
    assert not R.has_flags(clear) and "Nothing looked off" in R.render(clear)


def test_the_web_tool_log_windows_by_ts_and_is_never_read_from_resident_space(tmp_path, monkeypatch):
    log = tmp_path / "web-tools.jsonl"
    log.write_text("\n".join([json.dumps(_web("webfetch", ts="2026-09-27T10:00:00Z", url="u1")),
                              "{broken",
                              json.dumps(_web("websearch", ts="2026-09-28T10:00:00Z", query="q"))])
                   + "\n")
    monkeypatch.setattr(resident_io, "_owner_check", lambda: False)
    assert [r["event"] for r in R.read_web_records("2026-09-28T00:00:00Z", path=log)] == ["websearch"]
    assert len(R.read_web_records(None, path=log)) == 2
    assert R.read_web_records(None, path=tmp_path / "missing.jsonl") == []
    # an owner process does not take a copy that sits in ea-bot space
    monkeypatch.setattr(resident_io, "_owner_check", lambda: True)
    monkeypatch.setattr(R._owner_trust, "ea_dir", lambda: tmp_path)
    assert R.read_web_records(None, path=log) == []


# --- exfil shapes in path segments, query values and DNS labels --------------

HEX40 = "0123456789abcdef" * 2 + "01234567"


@pytest.mark.parametrize("url", [
    "https://en.wikipedia.org/wiki/Simultaneous_localization_and_mapping",
    "https://github.com/arpg/x/commit/" + HEX40,
    "https://www.nsf.gov/pubs/2026/nsf26501/nsf26501.htm?WT.mc_id=USNSF_25",
    "https://site.example/The-2024-Robotics-Workshop-Agenda-And-Speakers-Page",
    "https://site.example/ICRA2027-CameraReady-Paper123",
    "https://docs.google.com/document/d/1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms/edit?usp=sharing",
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://example.edu/events/2026/10/camera-ready?utm_source=newsletter&utm_medium=email"
    "&utm_campaign=fall2026",
    "https://api.cal.com/v2/slots?eventTypeSlug=30min&username=jdoe&start=2026-10-20"
    "&timeZone=America%2FDenver",
    "https://arxiv.org/abs/2410.12345",
    "https://d1a2b3c4d5e6f7.cloudfront.net/assets/app.css",
    "https://forms.example.org/r/3f2a9c1d-7e8b-4a6f-9c1d-7e8b4a6f9c1d",
    "https://www.when2meet.com/?12345678-AbCdE",
    "https://my-load-balancer-1234567890.us-west-2.elb.amazonaws.com/health",
    "https://ec2-203-0-113-25.compute-1.amazonaws.com/",
    "https://login.microsoftonline.com/common/oauth2/authorize?client_id=abc&state=" + B64,
    "https://news.example.org/?redirect=https://example.com/a",
])
def test_ordinary_urls_do_not_flag(url):
    assert R.exfil_reason(url) == ""


@pytest.mark.parametrize("url", [
    # commit ids, content hashes and slugs with numbers in them
    "https://github.com/arpg/x/blob/" + "0123456789abcdef" * 2 + "01234567/README.md",
    "https://github.com/arpg/x/compare/" + "a1b2c3d4e5" * 4 + "..." + "f6e5d4c3b2" * 4,
    "https://gitlab.com/g/p/-/commit/" + "0123456789abcdef" * 4,
    "https://raw.githubusercontent.com/arpg/x/" + "0123456789abcdef" * 4 + "/setup.py",
    "https://cdn.example.com/assets/main.3f2a9c1d7e8b4a6f.js",
    "https://cdn.example.com/static/js/chunk-vendors.3f2a9c1d7e8b4a6f9c1d.js?v=20261019",
    "https://example.org/papers/10.1109/ICRA48891.2023.10160123",
    "https://example.edu/news/the-department-of-computer-science-announces-new-faculty-2026",
    "https://example.com/internationalizationandlocalizationguide2026",
    "https://example.com/proceedingsoftheinternationalconference2027volume3",
    "https://example.com/files/3f2a9c1d-7e8b-4a6f-9c1d-7e8b4a6f9c1d/report.pdf",
    "https://zenodo.org/records/12345678/files/data_2026-10-19.csv?download=1",
    "https://www.example.com/search?q=snow+radar+2026&page=2",
])
def test_hashes_and_slugs_with_numbers_do_not_flag_in_either_check(url):
    assert R.exfil_reason(url) == ""
    assert R.web_tool_reason({"event": "webfetch", "url": url}) == ""


HEX64 = "0123456789abcdef" * 4
B32_90 = "nbswy3dpeb3w64tmmqqgc3tdnfxgo5bamfxgiidxn5zgyzbamjqxgzjthiqgcytdmrswmz3inf2gs3dnmfxgg3dpm4"


@pytest.mark.parametrize("url,why", [
    ("https://x.example/d/" + HEX64, "hex run of 64"),
    ("https://x.example/d/" + "deadbeef" * 6, "hex run of 48"),
    ("https://x.example/d/" + "-".join(["deadbeefdeadbeef"] * 3), "hex run of 48"),
    ("https://x.example/a/" + "/".join(["0123456789abcdef0123456789abcdef"] * 3),
     "hex runs of 96 characters across the path"),
    ("https://x.example/c?h=" + HEX64, "hex run of 64 characters in a query value"),
    ("https://github.com/attacker/x/" + HEX64 + "ff", "hex run of 66"),     # not a commit id
    ("https://x.example/" + B32_90 + "/a.png", "base32-like run in the path"),
    ("https://x.example/?d=" + B32_90.upper() + "====", "base32-like run in a query value"),
    ("https://x.example/" + B32_90[:40], "base32-like run"),
])
def test_hex_and_base32_payloads_flag(url, why):
    assert why in R.exfil_reason(url)
    assert R.web_tool_reason({"event": "webfetch", "url": url})


def test_a_tenant_host_is_exempt_only_when_the_owner_names_it(monkeypatch):
    payload = "https://attacker.sharepoint.com/sites/x/" + B64
    assert R.exfil_reason(payload) == "encoded path segment"            # any tenant: checked
    assert "email" in R.exfil_reason("https://attacker.sharepoint.com/?to=pat%40example.edu")
    assert R.exfil_reason("https://attacker.zoom.us/x/" + B64) == "encoded path segment"
    monkeypatch.setattr(R, "_OWNER_HOSTS", {
        "token_hosts": frozenset({"ours.sharepoint.com"}),
        "id_path_hosts": frozenset({"ours.zoom.us"}), "own_addresses": frozenset()})
    assert R.exfil_reason("https://ours.sharepoint.com/sites/x/" + B64) == ""
    assert R.exfil_reason(payload) == "encoded path segment"             # exact, not suffix
    assert R.exfil_reason("https://sub.ours.sharepoint.com/sites/x/" + B64)
    assert R.exfil_reason("https://ours.zoom.us/rec/" + B64) == ""
    # the email check runs on every host, the owner's tenant included
    assert "email" in R.exfil_reason("https://ours.sharepoint.com/?to=pat%40example.edu")
    assert "email" in R.exfil_reason("https://login.microsoftonline.com/?login_hint=pat%40example.edu")


SAFELINK = ("https://nam02.safelinks.protection.outlook.com/?url=https%3A%2F%2Fexample.com%2Fa"
            "&data=05%7C01%7Cpat%40example.edu%7C" + B64)


def test_safe_links_may_name_only_the_owners_own_mailbox(monkeypatch):
    assert "email" in R.exfil_reason(SAFELINK)                          # no list: flagged
    monkeypatch.setattr(R, "_OWNER_HOSTS", {"token_hosts": frozenset(),
                                            "id_path_hosts": frozenset(),
                                            "own_addresses": frozenset({"pat@example.edu"})})
    assert R.exfil_reason(SAFELINK) == ""
    other = SAFELINK.replace("pat%40example.edu", "rio.sorensen85%40example.com")
    assert "email" in R.exfil_reason(other)
    elsewhere = SAFELINK + "&to=pat%40example.edu"                    # only in data=
    assert "email" in R.exfil_reason(elsewhere)
    assert "email" in R.exfil_reason("https://x.example/?to=pat%40example.edu")


def test_the_owner_host_list_is_read_only_owner_side(tmp_path, monkeypatch):
    priv = tmp_path / "priv"
    priv.mkdir(mode=0o700)
    (priv / R.OWNER_HOSTS_FILE).write_text(json.dumps({
        "token_hosts": ["Ours.SharePoint.com", "not a host", 3],
        "id_path_hosts": ["ours.zoom.us"], "own_addresses": ["Pat@Example.edu", "nope"]}))
    (priv / R.OWNER_HOSTS_FILE).chmod(0o600)
    monkeypatch.setattr(R._owner_trust, "private_dir", lambda: priv)
    monkeypatch.setattr(R, "_OWNER_HOSTS", None)
    monkeypatch.setattr(R._owner_trust, "is_owner_process", lambda: False)
    assert R.owner_hosts()["token_hosts"] == frozenset()                # not an owner process
    monkeypatch.setattr(R, "_OWNER_HOSTS", None)
    monkeypatch.setattr(R._owner_trust, "is_owner_process", lambda: True)
    got = R.owner_hosts()
    assert got["token_hosts"] == {"ours.sharepoint.com"}
    assert got["id_path_hosts"] == {"ours.zoom.us"}
    assert got["own_addresses"] == {"pat@example.edu"}
    monkeypatch.setattr(R, "_OWNER_HOSTS", None)
    (priv / R.OWNER_HOSTS_FILE).chmod(0o666)                          # fails the owner checks
    assert R.owner_hosts()["token_hosts"] == frozenset()


@pytest.mark.parametrize("url,why", [
    ("https://x.example/c?d=" + B64, "encoded query value"),
    ("https://x.example/" + B64 + "/a.png", "encoded path segment"),
    ("https://x.example/p/" + "a" * 200, "path segment of 200"),
    ("https://x.example/?v=" + "word+" * 40, "query value of 200"),
    ("https://x.example/c?to=pat%40example.edu", "email address"),
    ("https://mfrggzdfmztwq2lknnwg23tpobyxe43uov3ho6dzpi.attacker.example/", "DNS label of 42"),
    ("https://nbswy3dpeb3w64tmmqqgc3tdnfxgo5a.attacker.example/x", "encoded DNS label"),
    ("https://3f2a9c1d7e8b4a6f9c1d7e8b4a6f9c1d.attacker.example/x", "encoded DNS label"),
])
def test_data_in_a_path_a_value_or_a_label_flags(url, why):
    assert why in R.exfil_reason(url)
    assert R.web_tool_reason({"event": "webfetch", "url": url})


def test_a_webfetch_to_an_encoded_dns_label_flags():
    rec = _web("webfetch", url="https://nbswy3dpeb3w64tmmqqgc3tdnfxgo5a.attacker.example/x")
    assert R.web_tool_reason(rec) == "encoded DNS label"


def test_entropy_and_the_opaque_token_rule():
    assert R.entropy("") == 0 and R.entropy("aaaa") == 0
    assert R.opaque_token(B64) and R.opaque_token("x8Fq2LmP9rT4vW7yB3nK6cJ1")
    assert not R.opaque_token(HEX40)                                # hex ids
    assert not R.opaque_token("3f2a9c1d-7e8b-4a6f-9c1d-7e8b4a6f9c1d")
    assert not R.opaque_token("Simultaneous_localization_and_mapping")
    assert not R.opaque_token("QWxs" * 30)                          # repeats: low entropy


def test_many_subdomains_new_hosts_and_unusual_volume():
    counts = {f"h{i}.attacker.example": 1 for i in range(13)}
    counts.update({"en.wikipedia.org": 3, "docs.google.com": 2, "drive.google.com": 1,
                   "loud.example": 40, "steady.example": 4, "fresh.example": 12})
    history = {"en.wikipedia.org": {"first": "2026-09-01", "last": "2026-10-18",
                                    "days": {"2026-10-16": 2, "2026-10-17": 3, "2026-10-18": 4}},
               "docs.google.com": {"first": "2026-09-01", "last": "2026-10-18", "days": {}},
               "drive.google.com": {"first": "2026-09-01", "last": "2026-10-18", "days": {}},
               "loud.example": {"first": "2026-09-01", "last": "2026-10-18",
                                "days": {"2026-10-16": 1, "2026-10-17": 2, "2026-10-18": 1}},
               "steady.example": {"first": "2026-09-01", "last": "2026-10-18",
                                  "days": {"2026-10-16": 4, "2026-10-17": 4, "2026-10-18": 4}}}
    for i in range(13):
        history[f"h{i}.attacker.example"] = {"first": "2026-10-18", "last": "2026-10-18", "days": {}}
    got = R.analyze_hosts(counts, history, "2026-10-19")
    assert got["subdomains"] == [("attacker.example", 13)]
    assert got["new_hosts"] == [("fresh.example", 12)]
    assert got["new_host_volume"] == [("fresh.example", 12)]
    assert [v[0] for v in got["volume"]] == ["loud.example"]
    # with no history yet nothing is new
    assert R.analyze_hosts(counts, None, "2026-10-19")["new_hosts"] == []
    assert R.registrable("a.b.example.co.uk") == "example.co.uk"
    assert R.registrable("cs.colorado.edu") == "colorado.edu"


def test_the_host_history_rolls_and_prunes():
    hist = R.update_history(None, {"a.example": 2}, "2026-10-19", seen={"b.example"})
    assert hist["a.example"]["days"] == {"2026-10-19": 2} and "b.example" in hist
    hist["old.example"] = {"first": "2026-01-01", "last": "2026-01-02", "days": {"2026-01-02": 1}}
    hist = R.update_history(hist, {"a.example": 1}, "2026-11-10")
    assert "old.example" not in hist
    assert hist["a.example"]["days"] == {"2026-11-10": 1}               # older than 14 days rolled


def test_the_post_lists_new_hosts_polls_and_writer_briefs_compactly():
    rep = R.analyze([_rec("fetch", host=f"n{i}.example") for i in range(12)]
                    + [{"ts": "2026-10-19T08:00:00Z", "event": "poll_submit",
                        "host": "www.when2meet.com", "poll_id": "123-Ab", "name": "Kit Lane",
                        "slots": 4, "bytes": 90, "outcome": "ok"}])
    rep.update(R.analyze_web_tools([]))
    rep.update(R.analyze_briefs([{"event": "remote_brief", "brief_chars": 300, "sent_chars": 5000},
                                 {"event": "remote_brief", "brief_chars": 100, "sent_chars": 900}]))
    rep.update(R.analyze_hosts(rep["host_counts"], {}, "2026-10-19"))
    R.analyze_polls(rep, None)
    out = R.render(rep)
    assert "Nothing looked off" in out
    assert "First seen today (12): " in out and "(+2 more)" in out
    # no owner-private profile.json: the names are listed, marked unchecked
    assert ("Polls: 1 submission(s) to www.when2meet.com, under 'Kit Lane' (names "
            "unchecked: no owner-private profile.json to compare with).") in out
    assert "Remote writer: 2 brief(s), 400 characters of brief, 5900 characters sent" in out
    R.analyze_polls(rep, "Robin Vale")
    assert R.has_flags(rep)
    out = R.render(rep)
    assert "POLL NAME www.when2meet.com 123-Ab: submitted as 'Kit Lane'" in out
    assert "unchecked" not in out and "Polls: 1 submission(s) to www.when2meet.com." in out
    R.analyze_polls(rep, "  kit  lane ")
    assert not rep["poll_names"]


def test_unchecked_poll_names_are_each_listed_once():
    rep = R.analyze([{"ts": "2026-10-19T08:00:00Z", "event": "poll_submit", "host": "h.example",
                      "poll_id": str(i), "name": n} for i, n in enumerate(["Kit Lane", "Kit  Lane",
                                                                          "Robin Vale"])])
    R.analyze_polls(rep, None)
    assert rep["poll_names_unchecked"] == ["Kit Lane", "Robin Vale"]
    assert not R.has_flags(rep)
    assert "under 'Kit Lane', 'Robin Vale' (names unchecked" in R.render(rep)


def test_check_lines_are_capped():
    recs = [_rec("fetch", url=f"https://x{i}.example/c?d={B64}", host=f"x{i}.example")
            for i in range(11)]
    out = R.render(R.analyze(recs))
    assert out.count("- CHECK URL") == R.MAX_CHECK_LINES
    assert "(and 3 more of these)" in out


# --- the cursors: one per log, by byte offset ---------------------------------

from datetime import datetime, timezone  # noqa: E402

NOW = datetime(2026, 10, 19, 6, 45, tzinfo=timezone.utc)


def _line(ts, event="fetch", host="a.example", **kw):
    rec = {"ts": ts, "event": event, "host": host, "url": f"https://{host}/p"}
    rec.update(kw)
    return (json.dumps(rec) + "\n").encode()


def test_the_window_is_the_complete_lines_after_the_offset():
    data = _line("2026-10-18T09:00:00Z") + _line("2026-10-18T10:00:00Z", host="b.example")
    recs, anom, cur = R.read_window(data, None, now=NOW)            # no cursor: from the start
    assert [r["host"] for r in recs] == ["a.example", "b.example"]
    assert anom == [] and cur["offset"] == len(data)
    tail = b'{"ts": "2026-10-19T02'
    more = data + _line("2026-10-19T01:00:00Z", host="c.example") + tail
    recs, anom, cur2 = R.read_window(more, cur, now=NOW, check_ts=True)
    assert [r["host"] for r in recs] == ["c.example"] and anom == []
    assert cur2["offset"] == len(more) - len(tail)                   # the partial line waits


def test_a_forged_future_time_hides_nothing_and_is_named():
    day1 = _line("2026-10-18T09:00:00Z")
    _, _, cur = R.read_window(day1, None, now=NOW)
    forged = day1 + _line("2099-01-01T00:00:00Z", host="evil.example") + _line(
        "2026-10-19T03:00:00Z", host="out.example", url="https://out.example/?d=" + B64)
    recs, anom, cur = R.read_window(forged, cur, now=NOW, check_ts=True, label="fetch-url.log")
    assert [r["host"] for r in recs] == ["out.example"]              # the later record is read
    assert anom == ["fetch-url.log: 1 record(s) with a time in the future, left out"]
    assert cur["last_ts"] == "2026-10-19T03:00:00Z"
    later = forged + _line("2026-10-20T03:00:00Z", host="next.example")
    recs, anom, _ = R.read_window(later, cur, now=datetime(2026, 10, 20, 6, 45, tzinfo=timezone.utc),
                                  check_ts=True)
    assert [r["host"] for r in recs] == ["next.example"] and anom == []


def test_bad_times_out_of_order_records_and_junk_lines_are_named():
    data = (_line("2026-10-19T05:00:00Z") + _line("yesterday") + _line("2026-10-19T01:00:00Z")
            + b"not json\n" + b"[1, 2]\n")
    recs, anom, _ = R.read_window(data, {"offset": 0, "sha": R._sha(b""), "inode": None,
                                         "last_ts": ""}, now=NOW, check_ts=True, label="L")
    assert len(recs) == 2                                            # out of order is kept
    assert "L: 2 line(s) that are not JSON records" in anom
    assert "L: 1 record(s) with no ISO time, left out" in anom
    assert "L: 1 record(s) earlier than a record before them" in anom


def test_a_shrunk_rewritten_replaced_or_missing_log_is_named():
    data = _line("2026-10-18T09:00:00Z") + _line("2026-10-18T10:00:00Z")
    _, _, cur = R.read_window(data, None, inode=11, now=NOW)
    recs, anom, _ = R.read_window(data[:10], cur, inode=11, now=NOW, label="L")
    assert any("shrank" in a for a in anom)
    edited = data.replace(b"a.example", b"z.example", 1) + _line("2026-10-19T01:00:00Z")
    recs, anom, _ = R.read_window(edited, cur, inode=11, now=NOW, label="L")
    assert anom == ["L: records before the last review changed"] and len(recs) == 1
    recs, anom, _ = R.read_window(data, cur, inode=12, now=NOW, label="L")
    assert anom == ["L was replaced by a new file since the last review"]
    recs, anom, _ = R.read_window(None, cur, now=NOW, label="L")
    assert anom and "is gone" in anom[0]


def test_a_legacy_time_cursor_windows_once_by_time():
    data = _line("2026-10-17T09:00:00Z") + _line("2026-10-18T09:00:00Z", host="b.example")
    recs, _, cur = R.read_window(data, None, legacy_ts="2026-10-17T12:00:00Z", now=NOW,
                                 check_ts=True)
    assert [r["host"] for r in recs] == ["b.example"] and cur["offset"] == len(data)


@pytest.fixture
def logs(tmp_path, monkeypatch):
    monkeypatch.setattr(resident_io, "_owner_check", lambda: False)
    paths = {"fetch_url": tmp_path / "fetch-url.log", "web_tools": tmp_path / "web-tools.jsonl",
             "owner_outbound": tmp_path / "outbound-owner.jsonl"}
    specs = [("fetch_url", paths["fetch_url"], "resident"),
             ("web_tools", paths["web_tools"], "owner"),
             ("owner_outbound", paths["owner_outbound"], "owner")]
    for p in paths.values():
        p.write_bytes(b"")
    return paths, specs


def _add(path, *lines):
    with open(path, "ab") as f:
        for ln in lines:
            f.write(ln if isinstance(ln, bytes) else (json.dumps(ln) + "\n").encode())


def test_each_log_keeps_its_own_cursor(logs):
    paths, specs = logs
    _add(paths["fetch_url"], _line("2026-10-18T09:00:00Z", host="old.example"))
    _add(paths["web_tools"], _web("webfetch", ts="2026-10-18T09:00:00Z",
                                  url="https://old.example/", host="old.example"))
    report, state = R.review(None, now=NOW, specs=specs)
    # no cursor: each log is read from its start, and the post says why
    assert report["fetches"] == 1 and report["web_fetches"] == 1
    assert report["log_anomalies"] == [R.NO_CURSOR]
    assert report["new_hosts"] == [] and state["hosts"]["old.example"]
    # ea-bot forges a far-future time; the owner logs and later records still count
    _add(paths["fetch_url"], _line("2999-01-01T00:00:00Z", host="evil.example"),
         _line("2026-10-19T03:00:00Z", host="old.example"),
         {"ts": "2026-10-19T05:00:00Z", "event": "remote_brief", "brief_chars": 9})
    _add(paths["web_tools"], _web("webfetch", ts="2026-10-19T04:00:00Z",
                                  url="https://new.example/a", host="new.example"))
    _add(paths["owner_outbound"], {"ts": "2026-10-19T05:00:00Z", "event": "remote_brief",
                                   "brief_chars": 120, "sent_chars": 3000})
    report, state2 = R.review(state, now=NOW, specs=specs)
    assert report["fetches"] == 1 and report["web_fetches"] == 1
    assert report["briefs"] == 1 and report["brief_sent_chars"] == 3000   # the owner log only
    assert report["log_anomalies"] == [
        "fetch-url.log: 1 record(s) with a time in the future, left out"]
    assert report["new_hosts"] == [("new.example", 1)]
    out = R.render(report)
    assert "LOG CHECK fetch-url.log: 1 record(s) with a time in the future" in out
    assert "First seen today (1): new.example." in out
    for name in ("fetch_url", "web_tools", "owner_outbound"):
        assert state2["logs"][name]["offset"] == paths[name].stat().st_size
    # nothing new: an empty window, no anomalies
    report, _ = R.review(state2, now=NOW, specs=specs)
    assert report["fetches"] == 0 and report["log_anomalies"] == [] and not R.has_flags(report)


def test_the_state_round_trips_and_a_legacy_cursor_reads_as_legacy(tmp_path):
    cur = tmp_path / "seen.json"
    R.write_cursor("2026-10-18T00:00:00Z", path=cur)
    st = R.read_state(cur)
    assert st["legacy"] and st["last_ts"] == "2026-10-18T00:00:00Z" and st["hosts"] is None
    R.write_state({"last_ts": "2026-10-19T06:45:00Z", "logs": {"fetch_url": {"offset": 5}},
                   "hosts": {"a.example": {"days": {}}}}, path=cur)
    st = R.read_state(cur)
    assert "legacy" not in st and st["logs"]["fetch_url"]["offset"] == 5
    assert R.read_cursor(cur) == "2026-10-19T06:45:00Z"
    assert R.read_state(tmp_path / "missing.json") is None


def _script(monkeypatch, tmp_path, specs):
    import importlib.util
    monkeypatch.setattr(R, "CURSOR", tmp_path / "seen.json")
    monkeypatch.setattr(R, "_log_specs", lambda: specs)
    spec = importlib.util.spec_from_file_location(
        "outbound_review_script",
        Path(__file__).resolve().parent.parent / "scripts" / "outbound-review.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_daily_script_posts_an_anomaly_and_holds_the_cursor_on_a_failed_post(
        logs, tmp_path, monkeypatch):
    from preprocessor import chat_notify
    paths, specs = logs
    mod = _script(monkeypatch, tmp_path, specs)
    posts = []
    monkeypatch.setattr(chat_notify, "health_channel_key", lambda: "health")
    monkeypatch.setattr(chat_notify, "post", lambda key, text: posts.append(text) or True)
    assert mod.main() == 0 and len(posts) == 1                       # first run: posted
    assert f"LOG CHECK {R.NO_CURSOR}" in posts[0]
    seeded = R.read_state()
    _add(paths["fetch_url"], _line("2999-01-01T00:00:00Z"))
    monkeypatch.setattr(chat_notify, "post", lambda key, text: bool(posts.append(text)))
    assert mod.main() == 1 and R.read_state() == seeded              # a failed post: no advance
    monkeypatch.setattr(chat_notify, "post", lambda key, text: posts.append(text) or True)
    assert mod.main() == 0
    assert "LOG CHECK fetch-url.log: 1 record(s) with a time in the future" in posts[-1]
    assert R.read_state()["logs"]["fetch_url"]["offset"] == paths["fetch_url"].stat().st_size


def test_a_clear_review_posts_on_mondays_only_and_still_advances(logs, tmp_path, monkeypatch):
    """A day with nothing flagged adds nothing to the health channel but on
    Monday, the weekly sign that the review runs; its window is read either
    way, so the cursors move."""
    from datetime import datetime
    from preprocessor import chat_notify
    paths, specs = logs
    mod = _script(monkeypatch, tmp_path, specs)
    posts = []
    monkeypatch.setattr(chat_notify, "health_channel_key", lambda: "health")
    monkeypatch.setattr(chat_notify, "post", lambda key, text: posts.append(text) or True)
    monkeypatch.setattr(mod, "_today", lambda: datetime(2026, 11, 17))   # a Tuesday
    assert mod.main() == 0 and len(posts) == 1                          # the first run is flagged
    seeded = R.read_state()
    assert mod.main() == 0 and len(posts) == 1                          # clear: nothing posted
    assert R.read_state()["last_ts"] >= seeded["last_ts"]
    monkeypatch.setattr(mod, "_today", lambda: datetime(2026, 11, 16))   # a Monday
    assert mod.main() == 0 and len(posts) == 2 and "Nothing looked off." in posts[-1]


# --- a lost, refused or bad cursor never skips a window silently -------------

def test_a_cursor_that_is_not_json_is_set_aside_and_each_log_is_read_from_its_start(
        logs, tmp_path, monkeypatch):
    from preprocessor import chat_notify
    paths, specs = logs
    mod = _script(monkeypatch, tmp_path, specs)
    posts = []
    monkeypatch.setattr(chat_notify, "health_channel_key", lambda: "health")
    monkeypatch.setattr(chat_notify, "post", lambda key, text: posts.append(text) or True)
    _add(paths["fetch_url"], _line("2026-10-18T09:00:00Z"))
    assert mod.main() == 0
    # the cursor is broken; a record lands before the next run
    (tmp_path / "seen.json").write_text("{broken")
    _add(paths["fetch_url"], _line("2026-10-19T01:00:00Z", host="out.example",
                                   url="https://out.example/?d=" + B64))
    assert R.read_state()["cursor_problem"] == "is not JSON"
    assert mod.main() == 0
    assert ("LOG CHECK the review cursor is not JSON; it is set aside and each log was read "
            "from its start") in posts[-1]
    assert "CHECK URL (encoded query value) to out.example" in posts[-1]
    aside = list(tmp_path.glob("seen.json.bad-*"))
    assert len(aside) == 1 and aside[0].read_text() == "{broken"
    st = R.read_state()
    assert "cursor_problem" not in st
    assert st["logs"]["fetch_url"]["offset"] == paths["fetch_url"].stat().st_size


def test_a_failed_post_keeps_a_bad_cursor_where_it_is(logs, tmp_path, monkeypatch):
    from preprocessor import chat_notify
    paths, specs = logs
    mod = _script(monkeypatch, tmp_path, specs)
    (tmp_path / "seen.json").write_text("[1, 2]")
    monkeypatch.setattr(chat_notify, "health_channel_key", lambda: "health")
    monkeypatch.setattr(chat_notify, "post", lambda key, text: False)
    assert mod.main() == 1
    assert (tmp_path / "seen.json").read_text() == "[1, 2]"
    assert R.read_state()["cursor_problem"] == "is not a JSON object"


def test_a_refused_owner_cursor_is_named_and_set_aside(tmp_path, monkeypatch):
    priv = tmp_path / "priv"
    priv.mkdir(mode=0o700)
    cur = priv / "outbound-review-seen.json"
    cur.write_text(json.dumps({"version": 2, "logs": {}, "last_ts": ""}))
    cur.chmod(0o666)
    monkeypatch.setattr(R._owner_trust, "private_dir", lambda: priv)
    st = R.read_state(cur)
    assert st["cursor_problem"].startswith("was refused")
    rep, new = R.review(st, now=NOW, specs=[])
    assert rep["log_anomalies"][0].startswith("the review cursor was refused")
    assert R.has_flags(rep)
    aside = R.set_aside_cursor(cur, now=NOW)
    assert aside.name == "outbound-review-seen.json.bad-20261019T064500Z" and not cur.exists()
    R.write_state(new, cur)
    assert "cursor_problem" not in R.read_state(cur)


def test_a_read_with_no_cursor_is_bounded_and_says_what_it_left_out():
    data = b"".join(_line(f"2026-10-18T0{i}:00:00Z", host=f"h{i}.example") for i in range(6))
    one = len(_line("2026-10-18T00:00:00Z", host="h0.example"))
    recs, anom, cur = R.read_window(data, None, now=NOW, label="L", max_bytes=2 * one + 3)
    assert [r["host"] for r in recs] == ["h4.example", "h5.example"]
    assert anom == [f"L: only its last {2 * one} of {6 * one} bytes were read; the "
                    f"{4 * one} bytes before them were not reviewed"]
    assert cur["offset"] == len(data)


def test_a_legacy_or_cursor_time_in_the_future_is_ignored_and_named():
    data = _line("2026-10-17T09:00:00Z") + _line("2026-10-18T09:00:00Z", host="b.example")
    recs, anom, cur = R.read_window(data, None, legacy_ts="2099-01-01T00:00:00Z", now=NOW,
                                    check_ts=True, label="L")
    assert len(recs) == 2                                            # the cut is not applied
    assert anom == ["L: the legacy cursor time 2099-01-01T00:00:00Z is in the future; ignored"]
    assert cur["last_ts"] == "2026-10-18T09:00:00Z"
    prior = {"offset": 0, "sha": R._sha(b""), "inode": None, "last_ts": "2099-01-01T00:00:00Z"}
    recs, anom, cur = R.read_window(data, prior, now=NOW, label="L")
    assert len(recs) == 2 and cur["last_ts"] == "2026-10-18T09:00:00Z"
    assert anom == ["L: the cursor time 2099-01-01T00:00:00Z is in the future; ignored"]


def test_a_time_only_cursor_with_a_future_time_reads_every_log_whole(logs, tmp_path):
    paths, specs = logs
    _add(paths["fetch_url"], _line("2026-10-18T09:00:00Z", host="a.example"))
    cur = tmp_path / "seen.json"
    R.write_cursor("2099-01-01T00:00:00Z", path=cur)
    rep, _ = R.review(R.read_state(cur), now=NOW, specs=specs)
    assert rep["fetches"] == 1
    assert rep["log_anomalies"] == [
        "the legacy cursor time 2099-01-01T00:00:00Z is in the future; ignored, and each log "
        "was read from its start"]


def test_a_log_missing_from_the_state_is_read_from_its_start_and_named(logs):
    paths, specs = logs
    _add(paths["fetch_url"], _line("2026-10-18T09:00:00Z"))
    _add(paths["web_tools"], _web("webfetch", ts="2026-10-18T09:00:00Z",
                                  url="https://a.example/", host="a.example"))
    _, state = R.review(None, now=NOW, specs=specs)
    del state["logs"]["web_tools"]
    rep, _ = R.review(state, now=NOW, specs=specs)
    assert rep["web_fetches"] == 1 and rep["fetches"] == 0
    assert rep["log_anomalies"] == [
        "web-tools.jsonl had no cursor in the review state; read from its start"]


@pytest.mark.parametrize("which", [0, -1])
def test_a_same_size_rewrite_of_a_reviewed_line_is_named(which):
    lines = [_line("2026-10-18T09:00:00Z", host="a.example"),
             _line("2026-10-18T10:00:00Z", host="b.example"),
             _line("2026-10-18T11:00:00Z", host="c.example")]
    data = b"".join(lines)
    _, _, cur = R.read_window(data, None, inode=7, now=NOW)
    lines[which] = lines[which].replace(b".example", b".exampl3")
    edited = b"".join(lines)
    assert len(edited) == len(data)
    recs, anom, _ = R.read_window(edited, cur, inode=7, now=NOW, label="L")
    assert recs == [] and anom == ["L: records before the last review changed"]

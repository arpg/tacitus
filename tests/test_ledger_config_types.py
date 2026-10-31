"""An install adds ledger types in ea-config.json (`ledger_types`), with
every trait stated (issue #71, step b). No block means no change."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "preprocessor"))
import ledger as ledger_mod  # noqa: E402

ALL_OFF = {t: False for t in ledger_mod.TRAITS}


def _spec(**traits):
    return {"meaning": "a weekly report to read", "traits": {**ALL_OFF, **traits}}


def test_a_complete_type_is_accepted():
    types, errors = ledger_mod.config_types({"Weekly-Report": _spec(on_user=True)})
    assert errors == []
    assert list(types) == ["weekly_report"]
    assert types["weekly_report"].on_user and not types["weekly_report"].deliverable


def test_no_block_is_no_types():
    assert ledger_mod.config_types(None) == ({}, [])


def test_each_bad_definition_is_refused_with_a_reason():
    missing = _spec(); del missing["traits"]["waiting"]
    unknown = _spec(); unknown["traits"]["flies"] = True
    cases = {
        "task": _spec(),                                   # a built-in
        "action_item": _spec(),                            # an alias
        "awaiting": _spec(),                               # a status word
        "no_meaning": {"traits": ALL_OFF},
        "no_traits": {"meaning": "x"},
        "missing": missing,
        "unknown": unknown,
        "not_bool": _spec(waiting="yes"),
    }
    types, errors = ledger_mod.config_types(cases)
    assert types == {}
    assert len(errors) == len(cases), errors
    assert any("missing traits waiting" in e for e in errors)
    assert any("unknown traits flies" in e for e in errors)
    assert ledger_mod.config_types(["weekly_report"])[0] == {}


def _run(ea_dir, *args):
    env = {**os.environ, "EA_DIR": str(ea_dir)}
    return subprocess.run([sys.executable, str(ROOT / "preprocessor" / "ledger.py"), *args],
                          env=env, capture_output=True, text=True)


def test_a_config_type_reaches_the_rules_and_add():
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "config").mkdir()
        (Path(d) / "config" / "ea-config.json").write_text(json.dumps({"ledger_types": {
            "weekly_report": _spec(closes_on_reply=True, reply_shaped=True),
            "bad": {"meaning": "x", "traits": {}}}}))
        out = json.loads(_run(d, "types").stdout)
        assert "weekly_report" in out["types"] and "bad" not in out["types"]
        assert out["config_types"]["weekly_report"]["closes_on_reply"] is True
        assert len(out["config_errors"]) == 1
        added = _run(d, "add", "--id", "t-1", "--type", "weekly_report", "-d", "read it")
        assert json.loads(added.stdout)["entry"]["type"] == "weekly_report", added.stdout
        probe = _run_py(d, "import ledger as L; print('weekly_report' in L.REPLY_TYPES,"
                           " 'weekly_report' in L.REPLY_SHAPED_TYPES,"
                           " 'weekly_report' in L.ON_USER_TYPES)")
        assert probe == "True True False", probe


def test_without_a_block_the_types_are_the_built_ins():
    with tempfile.TemporaryDirectory() as d:
        out = json.loads(_run(d, "types").stdout)
        assert out["config_types"] == {} and out["config_errors"] == []
        assert set(out["types"]) == set(ledger_mod.TYPES) - set(ledger_mod.CONFIG_TYPES)


def _run_py(ea_dir, code):
    env = {**os.environ, "EA_DIR": str(ea_dir)}
    return subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                          text=True, cwd=ROOT / "preprocessor").stdout.strip()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"  {len(tests)} passed, 0 failed")

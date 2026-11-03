"""A plugin may declare its ledger types in plugin.json (issue #71, step c).
The declaration is optional: an undeclared plugin type keeps exactly
today's behaviour, and a manifest without one keeps its consent digest."""
import copy
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "preprocessor"))
import ledger as ledger_mod  # noqa: E402
import plugin_manifest as PM  # noqa: E402

FIX = HERE / "fixtures" / "plugins"
ALL_OFF = {t: False for t in ledger_mod.TRAITS}


def _spec(**traits):
    return {"meaning": "a docket item to review", "traits": {**ALL_OFF, **traits}}


def _raw() -> dict:
    return json.loads((FIX / "example-notes" / "plugin.json").read_text())


def test_a_declaration_is_part_of_the_consent_summary():
    raw = _raw()
    before, _ = PM.normalize(copy.deepcopy(raw))
    raw["capabilities"]["ledger_types"] = {"notes_docket": _spec(deliverable=True)}
    after, errors = PM.normalize(raw)
    assert errors == []
    assert after["capabilities"]["ledger_types"] == {"notes_docket": _spec(deliverable=True)}
    a, b = PM.capability_summary(before), PM.capability_summary(after)
    assert "ledger_types" not in a  # a lock from before keeps its digest
    assert PM.capability_digest(a) != PM.capability_digest(b)
    assert "ledger type: notes_docket (deliverable)" in PM.describe(b)


def test_a_badly_shaped_declaration_is_a_manifest_error():
    raw = _raw()
    raw["capabilities"]["ledger_types"] = ["notes_docket"]
    _, errors = PM.normalize(raw)
    assert any("ledger_types" in e for e in errors)


def test_declared_types_are_checked_like_config_types():
    types, errors = ledger_mod.plugin_types({
        "a": {"docket_a": _spec(deliverable=True), "task": _spec(),
              "no_traits": {"meaning": "x"}},
        "b": {"shared": _spec()},
        "c": {"shared": _spec()},
    })
    assert list(types) == ["docket_a"]
    assert types["docket_a"].deliverable
    assert any("plugin a: 'task'" in e for e in errors)
    assert any("plugin a: 'no_traits'" in e for e in errors)
    assert any("another plugin declares it" in e for e in errors)


def test_a_declared_type_reaches_the_rules_and_an_undeclared_one_does_not():
    saved = dict(ledger_mod.PLUGIN_TYPES)
    try:
        ledger_mod.PLUGIN_TYPES.clear()
        assert ledger_mod.types_with("deliverable") == ledger_mod.DELIVERABLE_TYPES
        ledger_mod.PLUGIN_TYPES.update(ledger_mod.plugin_types(
            {"a": {"docket_a": _spec(deliverable=True)}})[0])
        assert "docket_a" in ledger_mod.types_with("deliverable")
        assert "docket_a" not in ledger_mod.types_with("closes_on_reply")
        # A main-EA row still cannot use a plugin's type.
        try:
            ledger_mod.checked_type("docket_a", None)
            raise AssertionError("a main-EA row took a plugin type")
        except ValueError:
            pass
        assert ledger_mod.checked_type("docket_a", "plugin:a") == "docket_a"
    finally:
        ledger_mod.PLUGIN_TYPES.clear()
        ledger_mod.PLUGIN_TYPES.update(saved)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"  {len(tests)} passed, 0 failed")

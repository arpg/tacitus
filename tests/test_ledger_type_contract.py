"""No code outside ledger.py names a ledger type by hand (issue #71, step d).

A rule reads a trait (ledger.types_with and the sets derived from it). The
few sites that still name a type are listed in EXEMPT with the reason; a
new one fails this test, and so does an exemption whose site is gone."""
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "preprocessor"))
import ledger as ledger_mod  # noqa: E402

# Every core spelling, plus the one plugin type core still names.
KNOWN = set(ledger_mod.TYPES) | set(ledger_mod.TYPE_ALIASES) | {"ae_review_request"}

EXEMPT = {
    ("preprocessor/closure_scrubber.py", '_etype(entry) == "draft_pending"'):
        (3, "a staged draft is closed by its own send (draft_events), never by "
            "thread evidence; the check is about that mechanism, not a kind of work"),
    ("preprocessor/closure_scrubber.py",
     '_DELIVERABLE_TYPES = ledger_mod.DELIVERABLE_TYPES | {"ae_review_request"}'):
        (1, "an undeclared plugin type keeps today's behaviour (step c); it goes "
            "when that plugin declares the type"),
    ("preprocessor/preprocess.py", 'ob.get("type") == "deadline"'):
        (1, "the ranking's credit for a dated submission whose row has no date"),
    ("preprocessor/remote_audit.py", 'o.get("type") == "reimbursement"'):
        (1, "the audit's reimbursement section reports that one kind, as stored"),
    ("preprocessor/mail_privacy.py", 'kept_row_type(res) == "scheduling"'):
        (1, "kept_row_type's own result, one of the core types it writes"),
}


def _names(node) -> set:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value} & KNOWN
    if isinstance(node, (ast.Set, ast.Tuple, ast.List)):
        return set().union(set(), *(_names(e) for e in node.elts))
    return set()


def _sites(path: Path) -> list[str]:
    """Comparisons of a type expression with a type literal, and *_TYPES
    sets that spell type names."""
    src = path.read_text()
    out = []
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Compare) and any(
                isinstance(o, (ast.Eq, ast.NotEq, ast.In, ast.NotIn)) for o in n.ops):
            sides = [n.left, *n.comparators]
            other = " ".join(ast.get_source_segment(src, s) or "" for s in sides if not _names(s))
            if any(_names(s) for s in sides) and "type" in other.lower():
                out.append(ast.get_source_segment(src, n))
        elif isinstance(n, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id.endswith("_TYPES") for t in n.targets) \
                and any(_names(x) for x in ast.walk(n.value)):
            out.append(ast.get_source_segment(src, n))
    return out


def _found() -> dict:
    files = [*(ROOT / "preprocessor").glob("*.py"), *(ROOT / "scripts").glob("*.py"),
             *ROOT.glob("*.py")]
    found: dict = {}
    for p in files:
        if p.name == "ledger.py":
            continue
        for seg in _sites(p):
            key = (str(p.relative_to(ROOT)), seg)
            found[key] = found.get(key, 0) + 1
    return found


def test_only_the_exempt_sites_name_a_type():
    found = _found()
    new = {k: n for k, n in found.items() if k not in EXEMPT}
    assert not new, f"read a trait (ledger.types_with) or add an exemption: {new}"
    changed = {k: (found.get(k, 0), n) for k, (n, _) in EXEMPT.items() if found.get(k, 0) != n}
    assert not changed, f"exemption count changed (found, listed): {changed}"


def test_the_scanner_sees_a_new_site():
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write('if row.get("type") == "task":\n    pass\n'
                'X = row["type"] in ("scheduling", "other")\n'
                'MY_TYPES = BASE | {"deadline"}\n'
                'ok = row.get("status") == "open"\n')
    try:
        assert len(_sites(Path(f.name))) == 3
    finally:
        Path(f.name).unlink()


def test_the_plugin_prefix_rule_is_the_known_one():
    # The ff-watcher plugin's rows are kept out of mail evidence by type
    # prefix. No trait says "mail is never evidence", so the rule stays named.
    text = (ROOT / "preprocessor" / "closure_scrubber.py").read_text()
    assert text.count('_PLUGIN_OWNED_TYPE_PREFIXES = ("ff_",)') == 1


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"  {len(tests)} passed, 0 failed")

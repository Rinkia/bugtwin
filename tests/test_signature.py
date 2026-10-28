import ast

from bugtwin.signature import normalize, signatures_for_sources


def sigs(old, new):
    return signatures_for_sources("m.py", old, new)


def test_literal_fix_gives_call_anchor_and_literal_delta():
    old = 'p = Policy(default=obj.get("default", "allow"))\n'
    new = 'p = Policy(default=obj.get("default", "deny"))\n'
    [s] = sigs(old, new)
    assert s.old_shape == "$1.get('default', 'allow')"
    assert s.new_shape == "$1.get('default', 'deny')"
    assert s.delta == (("allow", "deny"),)
    assert s.call == "get" and s.slots == (("arg", 1),)
    assert s.line == 1


def test_keyword_slot():
    [s] = sigs("f(x, mode='allow')\n", "f(x, mode='deny')\n")
    assert s.call == "f" and s.slots == (("kw", "mode"),)


def test_two_unrelated_statements_in_one_hunk_give_two_signatures():
    old = 'a = d.get("k", "allow")\nb = run(1)\n'
    new = 'a = d.get("k", "deny")\nb = run(2)\n'
    out = sigs(old, new)
    assert [s.old_shape for s in out] == ["$1.get('k', 'allow')", "run(1)"]


def test_non_literal_change_has_no_delta():
    [s] = sigs("x = load(path)\n", "x = load_safe(path)\n")
    assert s.delta == ()
    assert s.old_shape == "load($1)"


def test_additive_fix_gives_no_signature():
    old = "def f(x):\n    return x.y\n"
    new = "def f(x):\n    if x is None:\n        return None\n    return x.y\n"
    assert sigs(old, new) == []


def test_unparseable_side_gives_no_signature():
    assert sigs("x = (\n", "x = 1\n") == []


def test_unchanged_source_gives_nothing():
    assert sigs("x = 1\n", "x = 1\n") == []


def test_normalize_placeholders_names_but_keeps_calls_and_attrs():
    node = ast.parse("foo(a, b.c, a)", mode="eval").body
    assert normalize(node) == "foo($1, $2.c, $1)"


def test_normalize_sorts_keywords_and_reduces_fstrings():
    node = ast.parse("g(z=1, a=f'x{y}z')", mode="eval").body
    assert normalize(node) == "g(a=f'x' $f 'z', z=1)"


def test_normalize_masks_strings_except_keep():
    node = ast.parse("h('ignore all previous', 'allow')", mode="eval").body
    assert normalize(node, mask_strings=True, keep="allow") == "h($s, 'allow')"


def test_normalize_caps_and_sanitizes_identifiers_when_masking():
    node = ast.parse("obj.é" + "x" * 60 + "()", mode="eval").body
    out = normalize(node, mask_strings=True)
    assert out == "$1." + "x" * 40 + "()"


def test_multi_literal_change_keeps_one_slot_per_literal():
    [s] = sigs('d.get("a", "allow")\n', 'd.get("b", "deny")\n')
    assert s.delta == (("a", "b"), ("allow", "deny"))
    assert s.slots == (("arg", 0), ("arg", 1))


def test_removed_statement_gives_tier1_only_signature():
    old = "x = 1\nunsafe_eval(data)\ny = 2\n"
    new = "x = 1\ny = 2\n"
    [s] = sigs(old, new)
    assert s.new_shape is None and s.delta == ()
    assert s.old_shape == "unsafe_eval($1)"

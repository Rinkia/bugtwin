import pytest

from bugtwin.match import (RevTree, WorkTree, find_twins, is_specific, needle_for,
                           rev_signatures, scan_source, worktree_signatures)
from bugtwin.signature import signatures_for_sources

from .conftest import git, write

FIX_OLD = 'p = Policy(default=obj.get("default", "allow"))\n'
FIX_NEW = 'p = Policy(default=obj.get("default", "deny"))\n'


def allow_sig():
    [s] = signatures_for_sources("gate/policy.py", FIX_OLD, FIX_NEW)
    return s


def hits(src, sig=None):
    return sorted((c.line, c.tier) for c in scan_source("x.py", src, sig or allow_sig()))


# --- tiers --------------------------------------------------------------------

def test_tier1_exact_copy_with_other_names():
    assert hits('cfg2 = load(c.get("default", "allow"))\n') == [(1, 1)]


def test_tier2_other_key_same_slot():
    assert hits('m = d.get("mode", "allow")\n') == [(1, 2)]


def test_tier2_or_fallback_form():
    assert hits('m = d.get("mode") or "allow"\n') == [(1, 2)]


def test_tier2_same_file_constant_form():
    src = 'ALLOW = "allow"\n\ndef f(p):\n    return p.get("mode", ALLOW)\n'
    assert hits(src) == [(4, 2)]


def test_non_matches():
    src = ('a = d.get("default", "deny")\n'
           'print("allow")\n'
           'b = d.setdefault("k", "allow")\n'
           'c = d.get("allow")\n'
           'e = d.get("k") or "deny"\n')
    assert hits(src) == []


def test_already_fixed_location_is_not_reported():
    assert hits(FIX_NEW) == []


def test_unparseable_file_yields_nothing():
    assert hits("def (:\n") == []


# --- R6 Tier 2 specificity floor ----------------------------------------------

@pytest.mark.parametrize("value", [None, True, False, 0, 1, -1, "", "ab"])
def test_floor_values_are_not_specific(value):
    assert not is_specific(value)


@pytest.mark.parametrize("value", ["allow", 2, 3.5, b"abc"])
def test_specific_values(value):
    assert is_specific(value)


def test_none_delta_gives_no_tier2_hits():
    [s] = signatures_for_sources("a.py", "x = d.get('k', None)\n", "x = d.get('k', 'safe')\n")
    assert s.delta == ((None, "safe"),)
    assert hits("y = e.get('other', None)\n", s) == []
    assert hits("y = e.get('k', None)\n", s) == [(1, 1)]  # Tier 1 still runs


# --- R5 prefilter needle --------------------------------------------------------

def test_needle_is_longest_plain_run():
    assert needle_for('al"low\tnow plain') == "now plain"
    assert needle_for("ab\nnew line part") == "new line part"


def test_escaped_string_twin_survives_prefilter(make_repo):
    # value has a real tab; the source spells it \t, so grepping the raw value would miss it
    [s] = signatures_for_sources("a.py", "x = f(k, 'one\\ttwo words')\n", "x = f(k, 'safe')\n")
    root = make_repo("r", {"t.py": "y = f(z, 'one\\ttwo words')\n"})
    out = find_twins([s], WorkTree(root), [], top=0)
    assert [(c.path, c.tier) for c in out] == [("t.py", 1)]


def test_needle_none_when_too_short():
    assert needle_for("x") is None
    assert needle_for('a"b') is None


def test_needle_for_bytes_and_ints():
    assert needle_for(b"secret") == "secret"
    assert needle_for(8080) == "8080"
    assert needle_for(7) is None


# --- discovery and grep (R1, R3) -------------------------------------------------

def test_worktree_lists_tracked_and_untracked_py_but_not_ignored(make_repo):
    root = make_repo("r", {"a.py": "", ".gitignore": ".venv/\n", "README.md": ""})
    write(root, {"new.py": "", ".venv/lib/x.py": ""})
    assert sorted(WorkTree(root).files()) == ["a.py", "new.py"]


def test_non_git_tree_prunes_venv_and_dot_dirs(tmp_path):
    root = tmp_path / "plain"
    write(root, {"a.py": "", "pkg/b.py": "", ".venv/c.py": "", "venv/d.py": "",
                 "node_modules/e.py": "", ".hidden/f.py": "", "build/g.py": ""})
    assert sorted(WorkTree(root).files()) == ["a.py", "pkg/b.py"]


@pytest.mark.parametrize("literal", ["-e", ".*", "a[b", "città"])
def test_grep_is_fixed_string(make_repo, literal):
    root = make_repo("r", {"hit.py": f"x = {literal!r}\n", "miss.py": "x = 'abcd'\n"})
    assert WorkTree(root).grep(literal) == {"hit.py"}


def test_grep_on_non_git_tree(tmp_path):
    root = tmp_path / "plain"
    write(root, {"hit.py": "x = '-e'\n", "miss.py": "x = 1\n"})
    assert WorkTree(root).grep("-e") == {"hit.py"}


def test_rev_tree_reads_files_at_revision(make_repo):
    root = make_repo("r", {"a.py": "x = 'allow'\n"})
    rev = git(root, "rev-parse", "HEAD").strip()
    write(root, {"a.py": "x = 'deny'\n", "b.py": "y = 'allow'\n"})
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "fix")
    tree = RevTree(root, rev)
    assert tree.files() == ["a.py"]
    assert tree.grep("allow") == {"a.py"}
    assert tree.read_many(["a.py"]) == {"a.py": "x = 'allow'\n"}


# --- ranking, cap, top-N -----------------------------------------------------------

def test_find_twins_ranks_tier_then_proximity(make_repo):
    gate = make_repo("gate", {
        "gate/policy.py": FIX_NEW,
        "gate/loader.py": 'x = c.get("default", "allow")\n',
        "gate/other.py": 'y = c.get("mode", "allow")\n',
        "tools/far.py": 'z = c.get("mode", "allow")\n',
    })
    mesh = make_repo("mesh", {"m.py": 'w = c.get("default", "allow")\n'})
    out = find_twins([allow_sig()], WorkTree(gate), [WorkTree(mesh)], top=0)
    assert [(c.repo, c.path, c.tier) for c in out] == [
        ("gate", "gate/loader.py", 1),
        ("mesh", "m.py", 1),
        ("gate", "gate/other.py", 2),
        ("gate", "tools/far.py", 2),
    ]


def test_find_twins_top_n_and_cap(make_repo):
    files = {f"f{i:02}.py": 'v = c.get("mode", "allow")\n' for i in range(30)}
    root = make_repo("r", files)
    assert len(find_twins([allow_sig()], WorkTree(root), [], top=5)) == 5
    assert len(find_twins([allow_sig()], WorkTree(root), [], top=0)) == 30


def test_find_twins_dedups_across_signatures(make_repo):
    root = make_repo("r", {"t.py": 'x = c.get("default", "allow")\n'})
    out = find_twins([allow_sig(), allow_sig()], WorkTree(root), [], top=0)
    assert len(out) == 1


# --- diff modes ------------------------------------------------------------------------

def test_worktree_signatures_cover_staged_and_unstaged_py_only(make_repo):
    root = make_repo("r", {"a.py": FIX_OLD, "b.py": "x = run(1)\n", "c.md": "allow\n"})
    write(root, {"a.py": FIX_NEW, "c.md": "deny\n", "new.py": "z = 1\n"})
    git(root, "add", "a.py")
    write(root, {"b.py": "x = run(2)\n"})
    sigs, notes = worktree_signatures(root)
    assert sorted((s.path, s.old_shape) for s in sigs) == [
        ("a.py", "$1.get('default', 'allow')"), ("b.py", "run(1)")]
    assert notes == []


def test_worktree_unparseable_file_is_noted_and_skipped(make_repo):
    root = make_repo("r", {"a.py": FIX_OLD, "b.py": "x = 1\n"})
    write(root, {"a.py": FIX_NEW, "b.py": "x = (\n"})
    sigs, notes = worktree_signatures(root)
    assert [s.path for s in sigs] == ["a.py"]
    assert notes == ["b.py: could not parse, skipped"]


def test_rev_signatures_diff_against_parent(make_repo):
    root = make_repo("r", {"a.py": FIX_OLD})
    write(root, {"a.py": FIX_NEW})
    git(root, "commit", "-qam", "fix")
    sigs, _ = rev_signatures(root, "HEAD")
    assert [s.old_shape for s in sigs] == ["$1.get('default', 'allow')"]


# --- review fixes ---------------------------------------------------------------------

def test_deeply_nested_file_is_skipped_not_fatal(make_repo):
    [s] = signatures_for_sources("a.py", "f(a, 'abc')\n", "f(a, 'abd')\n")
    deep = "f(" + "+".join(["a"] * 20000) + ", 'abc')\n"
    root = make_repo("r", {"deep.py": deep, "ok.py": "f(b, 'abc')\n"})
    out = find_twins([s], WorkTree(root), [], top=0)
    assert [c.path for c in out] == ["ok.py"]


def test_or_form_fix_gets_tier2(make_repo):
    [s] = signatures_for_sources("a.py", "x = d.get(k) or 'allow'\n", "x = d.get(k) or 'deny'\n")
    assert s.call == "get" and s.slots == (("arg", 1),) and s.delta == (("allow", "deny"),)
    assert hits("m = e.get('mode', 'allow')\n", s) == [(1, 2)]
    assert hits("m = e.get(q) or 'allow'\n", s) == [(1, 1)]


def test_rev_starting_with_dash_is_rejected(make_repo):
    root = make_repo("r", {"a.py": ""})
    with pytest.raises(ValueError):
        RevTree(root, "--output=x")
    with pytest.raises(ValueError):
        rev_signatures(root, "--output=x")

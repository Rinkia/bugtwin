import json

from bugtwin import cli, hook

from .conftest import git, write

OLD = 'p = Policy(default=obj.get("default", "allow"))\n'
NEW = 'p = Policy(default=obj.get("default", "deny"))\n'
TWIN = 'x = c.get("default", "allow")\n'


def test_default_command_is_scan_of_worktree(make_repo, capsys, monkeypatch):
    root = make_repo("r", {"a.py": OLD, "b.py": TWIN})
    write(root, {"a.py": NEW})
    monkeypatch.chdir(root)
    assert cli.main([]) == 0
    out = capsys.readouterr().out
    assert "r/b.py:1" in out and "tier 1" in out


def test_scan_rev_json(make_repo, capsys):
    root = make_repo("r", {"a.py": OLD, "b.py": TWIN})
    write(root, {"a.py": NEW})
    git(root, "commit", "-qam", "fix")
    assert cli.main(["HEAD", "-C", str(root), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [(c["path"], c["tier"]) for c in data["candidates"]] == [("b.py", 1)]
    assert data["signatures"][0]["old_shape"] == "$1.get('default', 'allow')"


def test_rev_mode_scans_the_revision_tree_not_disk(make_repo, capsys):
    root = make_repo("r", {"a.py": OLD, "b.py": TWIN})
    write(root, {"a.py": NEW})
    git(root, "commit", "-qam", "fix")
    write(root, {"b.py": 'x = c.get("default", "deny")\n'})  # fixed on disk, not in HEAD
    cli.main(["HEAD", "-C", str(root), "--json"])
    assert [c["path"] for c in json.loads(capsys.readouterr().out)["candidates"]] == ["b.py"]


def test_additive_fix_says_no_signature(make_repo, capsys):
    root = make_repo("r", {"a.py": "def f(x):\n    return x.y\n"})
    write(root, {"a.py": "def f(x):\n    if x is None:\n        return None\n    return x.y\n"})
    assert cli.main(["-C", str(root)]) == 0
    assert "no signature" in capsys.readouterr().out


def test_cli_keeps_full_strings(make_repo, capsys):
    root = make_repo("r", {"a.py": OLD, "b.py": 'y = c.get("mode", "allow", "note")\n'})
    write(root, {"a.py": NEW})
    cli.main(["-C", str(root)])
    assert "'note'" in capsys.readouterr().out


def test_install_hook_prints_settings(capsys):
    assert cli.main(["install-hook"]) == 0
    assert json.loads(capsys.readouterr().out) == hook.settings_snippet()


def test_review_lists_and_marks(make_repo, capsys, bugtwin_home):
    root = make_repo("r", {"a.py": OLD, "b.py": TWIN})
    write(root, {"a.py": NEW})
    hook.stop(__import__("io").StringIO(json.dumps({"cwd": str(root)})), __import__("io").StringIO())
    assert cli.main(["review"]) == 0
    listing = capsys.readouterr().out
    cid = listing.split()[0]
    assert "b.py:1" in listing
    assert cli.main(["review", cid, "wrong"]) == 0
    capsys.readouterr()
    cli.main(["review"])
    out = capsys.readouterr().out
    assert "wrong 1" in out and "wrong/reverted rate 100%" in out


def test_review_rejects_unknown_state(capsys):
    assert cli.main(["review", "abc", "maybe"]) == 2


def test_scan_from_a_subdirectory(make_repo, capsys):
    root = make_repo("r", {"pkg/a.py": OLD, "pkg/b.py": TWIN})
    write(root, {"pkg/a.py": NEW})
    assert cli.main(["-C", str(root / "pkg"), "--json"]) == 0
    assert [c["path"] for c in json.loads(capsys.readouterr().out)["candidates"]] == ["pkg/b.py"]

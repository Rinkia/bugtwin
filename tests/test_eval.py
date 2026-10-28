"""Smoke-checks for the dev-only eval tools (eval/ is not packaged)."""

import importlib.util
import json
import sys
from pathlib import Path

from .conftest import git, write

EVAL = Path(__file__).resolve().parent.parent / "eval"


def load(name):
    spec = importlib.util.spec_from_file_location(f"eval_{name}", EVAL / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(mod)
    return mod


def history(make_repo):
    root = make_repo("r", {"a.py": 'x = d.get("default", "allow")\n',
                           "b.py": 'def f(c):\n    return c.get("default", "allow")\n'})
    write(root, {"a.py": 'x = d.get("default", "deny")\n'})
    git(root, "commit", "-qam", "fix a")
    f_sha = git(root, "rev-parse", "HEAD").strip()
    write(root, {"b.py": 'def f(c):\n    return c.get("default", "deny")\n'})
    git(root, "commit", "-qam", "fix b later")
    return root, f_sha


def test_harvest_proposes_the_later_twin_fix(make_repo):
    root, f_sha = history(make_repo)
    pairs = load("harvest").propose(load("harvest").literal_changes(root))
    [p] = [p for p in pairs if p["f_sha"] == f_sha]
    assert (p["f_path"], p["t_path"]) == ("a.py", "b.py")
    assert p["t_line"] == 'return c.get("default", "allow")'
    assert p["confirmed"] is None and p["label_method"] == "human"


def test_run_scores_a_confirmed_case_as_hit(make_repo, tmp_path, capsys):
    root, f_sha = history(make_repo)
    harvest = load("harvest")
    [p] = [p for p in harvest.propose(harvest.literal_changes(root)) if p["f_sha"] == f_sha]
    cases = tmp_path / "cases.jsonl"
    cases.write_text(json.dumps({**p, "confirmed": True}) + "\n", encoding="utf-8")
    assert load("run").main([str(cases), "--repos", str(root)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["cases"] == 1
    assert report["recall_at_5"] == 1.0 and report["recall_tier1_only"] == 1.0


def test_harvest_ignores_version_bumps(make_repo):
    root = make_repo("v", {"a/__init__.py": '__version__ = "0.1.0"\n', "b/__init__.py": '__version__ = "0.1.0"\n'})
    write(root, {"a/__init__.py": '__version__ = "0.2.0"\n'})
    git(root, "commit", "-qam", "bump a")
    write(root, {"b/__init__.py": '__version__ = "0.2.0"\n'})
    git(root, "commit", "-qam", "bump b")
    harvest = load("harvest")
    assert harvest.propose(harvest.literal_changes(root)) == []

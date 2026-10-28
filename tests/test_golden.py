"""CI golden test (eng review R7): the gate fail-open default and its twins.

Three synthetic repos reproduce the real shapes from the Bastion suite:
a copy-pasted loader (Tier 1), drifted spellings (Tier 2) and near misses that
must stay silent. The run over the real repos lives in eval/run.py.
"""

import shutil
from pathlib import Path

from bugtwin.match import WorkTree, find_twins, worktree_signatures

from .conftest import git

FIXTURES = Path(__file__).parent / "fixtures" / "golden"

EXPECTED = {
    ("gate", "gate/tools.py", 5, 1),
    ("mesh", "mesh/config.py", 7, 2),
    ("mesh", "mesh/config.py", 11, 2),
    ("mesh", "mesh/config.py", 15, 2),
}


def repo(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    shutil.copytree(FIXTURES / name, root)
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


def test_golden_fix_finds_exactly_the_expected_twins(tmp_path):
    gate, mesh, other = (repo(tmp_path, n) for n in ("gate", "mesh", "other"))
    policy = gate / "gate" / "policy.py"
    policy.write_text(policy.read_text(encoding="utf-8").replace(
        'obj.get("default", "allow")', 'obj.get("default", "deny")'), encoding="utf-8", newline="\n")

    sigs, notes = worktree_signatures(gate)
    assert notes == []
    assert [s.old_shape for s in sigs] == ["$1.get('default', 'allow')"]

    out = find_twins(sigs, WorkTree(gate), [WorkTree(mesh), WorkTree(other)], top=0)
    assert {(c.repo, c.path, c.line, c.tier) for c in out} == EXPECTED
    assert [c.tier for c in out] == sorted(c.tier for c in out)  # Tier 1 ranks first

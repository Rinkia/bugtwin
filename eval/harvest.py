"""Propose (fix F, later twin fix T) pairs from git history, for a human to label.

Independent of the engine on purpose (eng review R1-6): pairs come from plain
text, never from bugtwin.signature or bugtwin.match, so recall measured on them
is not circular. A pair is proposed when a later commit T removes and adds the
same string literals as an earlier small fix F, in a different place.

    python eval/harvest.py REPO [REPO ...] > eval/cases_proposed.jsonl

Then set "confirmed": true or false on each line by hand and save the kept
lines as eval/cases.jsonl. Only confirmed pairs are scored by eval/run.py.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

MAX_CHANGED_LINES = 20
_STRING = re.compile(r"""(['"])((?:\\.|(?!\1).)*)\1""")
_VERSION = re.compile(r"^[v=<>~!]*\d+(\.\d+)+([a-z]+\d*)?$")


@dataclass(frozen=True)
class Change:
    repo: str
    sha: str
    ts: int
    path: str
    removed: tuple[str, ...]
    old: frozenset[str]
    new: frozenset[str]


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "core.quotepath=off", *args], cwd=root, check=True,
                          capture_output=True, text=True, encoding="utf-8", errors="replace").stdout


def _literals(lines: list[str]) -> set[str]:
    """String literals of 2+ chars, minus version strings: release bumps are not bug fixes."""
    return {m.group(2) for line in lines for m in _STRING.finditer(line)
            if len(m.group(2)) >= 2 and not _VERSION.match(m.group(2))}


def literal_changes(root: Path) -> list[Change]:
    """Small per-file commits whose removed and added lines differ in string literals."""
    out = []
    log = _git(root, "log", "--no-merges", "--format=%H %ct")
    for entry in log.splitlines():
        sha, ts = entry.split()
        diff = _git(root, "show", "--format=", "-U0", sha, "--", "*.py")
        for path, removed, added in _per_file(diff):
            if len(removed) + len(added) > MAX_CHANGED_LINES or not removed or not added:
                continue
            r, a = _literals(removed), _literals(added)
            old, new = r - a, a - r
            if old and new:
                out.append(Change(root.name, sha, int(ts), path, tuple(removed), frozenset(old), frozenset(new)))
    return out


def _per_file(diff: str) -> list[tuple[str, list[str], list[str]]]:
    files: list[tuple[str, list[str], list[str]]] = []
    for line in diff.splitlines():
        if line.startswith("+++ "):
            files.append((line[6:] if line.startswith("+++ b/") else line[4:], [], []))
        elif files and line.startswith("-") and not line.startswith(("--- a/", "--- /dev/null")):
            files[-1][1].append(line[1:])
        elif files and line.startswith("+"):
            files[-1][2].append(line[1:])
    return [f for f in files if f[0] != "/dev/null"]


def propose(changes: list[Change]) -> list[dict]:
    pairs = []
    for f in changes:
        for t in changes:
            if t.ts < f.ts or (t.repo, t.sha, t.path) == (f.repo, f.sha, f.path):
                continue
            shared_old, shared_new = f.old & t.old, f.new & t.new
            if not (shared_old and shared_new):
                continue
            line = next((l for l in t.removed if any(o in l for o in shared_old)), t.removed[0])
            pairs.append({
                "repo_f": f.repo, "f_sha": f.sha, "f_path": f.path,
                "repo_t": t.repo, "t_sha": t.sha, "t_path": t.path, "t_line": line.strip(),
                "shared_old": sorted(shared_old), "shared_new": sorted(shared_new),
                "label_method": "human", "confirmed": None,
            })
    return pairs


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    changes = [c for r in argv for c in literal_changes(Path(r).resolve())]
    for pair in propose(changes):
        print(json.dumps(pair))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

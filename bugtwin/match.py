"""Hunt for twins of a fix signature across one or more source trees.

Discovery goes through git (`ls-files`, `grep -F`, `cat-file --batch`) because
walking a repo tree that holds a .venv costs far more than parsing it (24 s
cold walk vs 0.26 s parse on the Bastion repos). Non-git paths fall back to an
os.walk that prunes dot-dirs, virtualenvs and build output.

Tier 1: a node whose normalized shape equals the fix's old shape.
Tier 2: the same call (by name) with the old literal in the same slot, the key
relaxed. Recognized forms: `x.get(K, OLD)`, `x.get(K) or OLD`, and a same-file
module constant `NAME = OLD` used in the slot. Tier 2 is skipped for literals
too common to mean anything (None, booleans, -1..1, strings under 3 chars).
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable

from .signature import FixSignature, Slot, normalize, signatures_for_sources

TIER2_CAP = 20  # per signature, before top-N; disabled by top=0
MIN_NEEDLE = 3
_PRUNE = {"venv", "env", "node_modules", "build", "dist", "__pycache__", "site-packages"}
_UNSAFE_NEEDLE = re.compile(r"['\"\\\x00-\x1f\x7f]+")  # quotes, backslash, any control char (written escaped in source)


@dataclass(frozen=True)
class Candidate:
    repo: str
    path: str
    line: int
    end_line: int
    tier: int
    shape: str
    masked: str
    sig_key: str
    distance: int = 3


def is_specific(value: Any) -> bool:
    """False for literals too common for Tier 2 (R6 floor)."""
    if value is None or isinstance(value, bool):
        return False
    if isinstance(value, int) and -1 <= value <= 1:
        return False
    if isinstance(value, (str, bytes)) and len(value) < MIN_NEEDLE:
        return False
    return True


def needle_for(value: Any) -> str | None:
    """Longest run of the value with no quote, backslash or newline (R5), min 3 chars.

    Source code spells a string differently from its value ("a\\nb"), so the
    grep needle must be a piece that reads the same in both.
    """
    if isinstance(value, bytes):
        value = value.decode("latin-1")
    text = value if isinstance(value, str) else repr(value)
    best = max(_UNSAFE_NEEDLE.split(text), key=len, default="")
    return best if len(best) >= MIN_NEEDLE else None


# --- trees ---------------------------------------------------------------------------

def _git(root: Path, *args: str, stdin: bytes | None = None, ok: tuple[int, ...] = (0,)) -> bytes:
    proc = subprocess.run(["git", "-c", "core.quotepath=off", *args], cwd=root, input=stdin,
                          capture_output=True, timeout=30)
    if proc.returncode not in ok:
        raise RuntimeError(f"git {args[0]} failed: {proc.stderr.decode('utf-8', 'replace').strip()}")
    return proc.stdout


def _split_z(out: bytes) -> list[str]:
    return [p.decode("utf-8", "surrogateescape") for p in out.split(b"\0") if p]


def is_git(root: Path) -> bool:
    try:
        return _git(root, "rev-parse", "--is-inside-work-tree").strip() == b"true"
    except (RuntimeError, OSError, subprocess.TimeoutExpired):
        return False


class WorkTree:
    """Files as they are on disk (tracked + untracked, not ignored)."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.name = self.root.resolve().name
        self.git = is_git(self.root)

    def files(self) -> list[str]:
        if self.git:
            return _split_z(_git(self.root, "ls-files", "-z", "-co", "--exclude-standard", "--", "*.py"))
        out = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in _PRUNE]
            rel = Path(dirpath).relative_to(self.root)
            out += [(rel / f).as_posix() for f in filenames if f.endswith(".py")]
        return out

    def grep(self, needle: str) -> set[str]:
        if self.git:
            out = _git(self.root, "grep", "--untracked", "-l", "-F", "-z", "-e", needle, "--", "*.py", ok=(0, 1))
            return set(_split_z(out))
        return {f for f, text in self.read_many(self.files()).items() if needle in text}

    def read_many(self, paths: Iterable[str]) -> dict[str, str]:
        out = {}
        for p in paths:
            try:
                out[p] = (self.root / p).read_text(encoding="utf-8", errors="surrogateescape")
            except OSError:
                continue  # listed by git but deleted on disk
        return out


def check_rev(rev: str) -> str:
    """Refuse a revision that git would read as an option."""
    if not rev or rev.startswith("-"):
        raise ValueError(f"not a revision: {rev!r}")
    return rev


class RevTree:
    """Files at a git revision, read in one pass via `cat-file --batch`."""

    def __init__(self, root: str | Path, rev: str):
        check_rev(rev)
        self.root = Path(root)
        self.rev = rev
        self.name = self.root.resolve().name

    def files(self) -> list[str]:
        out = _git(self.root, "ls-tree", "-r", "-z", "--name-only", self.rev)
        return [p for p in _split_z(out) if p.endswith(".py")]

    def grep(self, needle: str) -> set[str]:
        out = _git(self.root, "grep", "-l", "-F", "-z", "-e", needle, self.rev, "--", "*.py", ok=(0, 1))
        prefix = f"{self.rev}:"
        return {p[len(prefix):] if p.startswith(prefix) else p for p in _split_z(out)}

    def read_many(self, paths: Iterable[str]) -> dict[str, str]:
        paths = [p for p in paths if "\n" not in p]  # a newline would desync the batch protocol
        if not paths:
            return {}
        req = "".join(f"{self.rev}:{p}\n" for p in paths).encode("utf-8", "surrogateescape")
        raw = _git(self.root, "cat-file", "--batch", stdin=req)
        out, pos = {}, 0
        for p in paths:
            nl = raw.index(b"\n", pos)
            header = raw[pos:nl].split()
            pos = nl + 1
            if len(header) < 3 or header[1] != b"blob":
                continue  # missing object
            size = int(header[2])
            out[p] = raw[pos:pos + size].decode("utf-8", "surrogateescape")
            pos += size + 1
        return out


Tree = WorkTree | RevTree


# --- matching -------------------------------------------------------------------------

def _call_name(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Call):
        return None
    f = node.func
    return f.id if isinstance(f, ast.Name) else getattr(f, "attr", None)


def _at_slot(call: ast.Call, slot: Slot) -> ast.AST | None:
    kind, key = slot
    if kind == "arg":
        return call.args[key] if key < len(call.args) else None
    return next((k.value for k in call.keywords if k.arg == key), None)


def _same_literal(a: Any, b: Any) -> bool:
    return type(a) is type(b) and a == b


def _module_constants(tree: ast.Module) -> dict[str, Any]:
    consts = {}
    for stmt in tree.body:
        if (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name) and isinstance(stmt.value, ast.Constant)):
            consts[stmt.targets[0].id] = stmt.value.value
    return consts


def scan_source(path: str, src: str, sig: FixSignature, repo: str = "") -> list[Candidate]:
    """Tier 1 and Tier 2 hits of one signature in one file, one per node (lowest tier wins).

    A file too deeply nested to parse or normalize is skipped, never fatal.
    """
    try:
        return _scan_source(path, ast.parse(src), sig, repo)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return []


def _scan_source(path: str, tree: ast.Module, sig: FixSignature, repo: str) -> list[Candidate]:
    keep = sig.delta[0][0] if sig.delta else None
    found: dict[tuple[int, int, int], Candidate] = {}

    def add(node: ast.AST, tier: int, keep_value: Any) -> None:
        key = (node.lineno, node.col_offset, node.end_lineno or node.lineno)
        if key in found and found[key].tier <= tier:
            return
        found[key] = Candidate(repo, path, node.lineno, node.end_lineno or node.lineno, tier,
                               normalize(node), normalize(node, mask_strings=True, keep=keep_value), sig.key)

    queries = [(old, slot) for (old, _), slot in zip(sig.delta, sig.slots)
               if slot is not None and sig.call and is_specific(old)]
    consts = _module_constants(tree) if queries else {}
    for node in ast.walk(tree):
        if (type(node).__name__ == sig.anchor_type
                and (sig.anchor_type != "Call" or sig.call is None
                     or _call_name(node) == sig.call)  # cheap check before normalize
                and normalize(node) == sig.old_shape):
            add(node, 1, keep)
        for old, slot in queries:
            if _call_name(node) == sig.call:
                value = _at_slot(node, slot)
                if isinstance(value, ast.Constant) and _same_literal(value.value, old):
                    add(node, 2, old)
                elif isinstance(value, ast.Name) and value.id in consts and _same_literal(consts[value.id], old):
                    add(node, 2, old)
            elif (isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or)
                  and _call_name(node.values[0]) == sig.call and _at_slot(node.values[0], slot) is None
                  and isinstance(node.values[-1], ast.Constant) and _same_literal(node.values[-1].value, old)):
                add(node, 2, old)
    return sorted(found.values(), key=lambda c: (c.line, c.tier))


def _needles(sig: FixSignature) -> list[str] | None:
    """Prefilter needles; None means no safe needle exists, so scan every file."""
    if sig.delta:
        needles = [needle_for(old) for old, _ in sig.delta]
    elif sig.call:
        needles = [sig.call if len(sig.call) >= MIN_NEEDLE else None]
    else:
        return None
    return None if any(n is None for n in needles) else needles  # type: ignore[return-value]


def _distance(sig: FixSignature, tree_index: int, path: str) -> int:
    if tree_index > 0:
        return 3
    if path == sig.path:
        return 0
    return 1 if Path(path).parent == Path(sig.path).parent else 2


def _rank_key(c: Candidate) -> tuple:
    return (c.tier, c.distance, c.repo, c.path, c.line)


def twins_for(sig: FixSignature, trees: list[Tree], top: int) -> list[Candidate]:
    needles = _needles(sig)
    out: list[Candidate] = []
    for i, tree in enumerate(trees):
        if needles is None:
            paths = tree.files()
        else:
            paths = sorted(set().union(*(tree.grep(n) for n in needles)))
        for path, src in tree.read_many(paths).items():
            out += [replace(c, distance=_distance(sig, i, path))
                    for c in scan_source(path, src, sig, tree.name)]
    out.sort(key=_rank_key)
    return out[:TIER2_CAP] if top else out


def find_twins(sigs: list[FixSignature], origin: Tree, also: list[Tree], top: int = 5) -> list[Candidate]:
    """Ranked, deduplicated twins of all signatures. top=0 disables the cap and top-N."""
    best: dict[tuple[str, str, int, int], Candidate] = {}
    for sig in sigs:
        for c in twins_for(sig, [origin, *also], top):
            key = (c.repo, c.path, c.line, c.end_line)
            if key not in best or _rank_key(c) < _rank_key(best[key]):
                best[key] = c
    ranked = sorted(best.values(), key=_rank_key)
    return ranked[:top] if top else ranked


# --- diff modes ---------------------------------------------------------------------------

def _signatures(pairs: dict[str, tuple[str | None, str | None]]) -> tuple[list[FixSignature], list[str]]:
    sigs: list[FixSignature] = []
    notes: list[str] = []
    for path, (old, new) in sorted(pairs.items()):
        if old is None or new is None:
            continue  # added file (additive) or deleted file
        try:
            ast.parse(old)
            ast.parse(new)
            sigs += signatures_for_sources(path, old, new)
        except (SyntaxError, ValueError, RecursionError, MemoryError):
            notes.append(f"{path}: could not parse, skipped")
    return sigs, notes


def worktree_signatures(root: str | Path) -> tuple[list[FixSignature], list[str]]:
    """Signatures for HEAD vs the working tree (staged and unstaged), .py files only."""
    root = Path(root)
    changed = _split_z(_git(root, "diff", "--name-only", "-z", "HEAD", "--", "*.py"))
    olds = RevTree(root, "HEAD").read_many(changed)
    news = WorkTree(root).read_many(changed)
    return _signatures({p: (olds.get(p), news.get(p)) for p in changed})


def rev_signatures(root: str | Path, rev: str) -> tuple[list[FixSignature], list[str]]:
    """Signatures for REV^ vs REV, .py files only."""
    check_rev(rev)
    root = Path(root)
    changed = _split_z(_git(root, "diff", "--name-only", "-z", f"{rev}^", rev, "--", "*.py"))
    olds = RevTree(root, f"{rev}^").read_many(changed)
    news = RevTree(root, rev).read_many(changed)
    return _signatures({p: (olds.get(p), news.get(p)) for p in changed})

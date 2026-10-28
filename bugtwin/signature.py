"""Fix signatures: what a fix changed, as a pattern to hunt for.

A fix is a pair of sources (before, after) for one file. Each changed leaf
statement yields one FixSignature:

- old_shape / new_shape: the normalized anchor before and after. The anchor is
  the nearest Call around the change (so `obj.get("default", "allow")`, not the
  bare literal), else the whole changed statement.
- delta: the literal pairs that changed, with the argument slot of each in the
  anchor call. Only literal-to-literal changes get a delta; anything else
  (renamed call, new operator) is Tier 1 only.

Pure insertions (an added guard) have no "before" shape to hunt for, so v0.1
gives no signature for them. A removed statement gives a Tier 1 signature with
new_shape None.
"""

from __future__ import annotations

import ast
import difflib
import hashlib
import re
from dataclasses import dataclass
from typing import Any, Iterator

MAX_IDENT = 40
_SKIP_FIELDS = {"ctx", "type_comment", "kind"}
_UNSAFE_IDENT = re.compile(r"[^A-Za-z0-9_]")

Slot = tuple[str, Any]  # ("arg", index) | ("kw", name)


@dataclass(frozen=True)
class FixSignature:
    path: str
    line: int
    old_shape: str
    new_shape: str | None
    anchor_type: str
    call: str | None = None
    delta: tuple[tuple[Any, Any], ...] = ()
    slots: tuple[Slot | None, ...] = ()

    @property
    def key(self) -> str:
        """Stable id for dedup across hook runs: (old_shape, delta, file)."""
        raw = repr((self.old_shape, self.delta, self.path))
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def normalize(node: ast.AST, *, mask_strings: bool = False, keep: Any = None) -> str:
    """Render an AST node as a shape string.

    Variable and attribute-base names become $1, $2 in order of appearance;
    called function names and attribute names are kept; literals are kept
    verbatim; keyword arguments are sorted; f-strings keep their constant parts
    and show each expression as $f. With mask_strings, every string literal
    except `keep` becomes $s and identifiers are cut to [A-Za-z0-9_] and 40
    chars, so no free text from scanned code survives.
    """
    names: dict[str, str] = {}

    def ident(s: str) -> str:
        return _UNSAFE_IDENT.sub("", s)[:MAX_IDENT] if mask_strings else s

    def const(value: Any) -> str:
        if mask_strings and isinstance(value, (str, bytes)) and value != keep:
            return "$s"
        return repr(value)

    def n(x: Any) -> str:
        if isinstance(x, ast.Name):
            return names.setdefault(x.id, f"${len(names) + 1}")
        if isinstance(x, ast.Constant):
            return const(x.value)
        if isinstance(x, ast.Attribute):
            return f"{n(x.value)}.{ident(x.attr)}"
        if isinstance(x, ast.Call):
            func = ident(x.func.id) if isinstance(x.func, ast.Name) else n(x.func)
            args = [n(a) for a in x.args]
            kws = sorted((k for k in x.keywords if k.arg), key=lambda k: k.arg)
            args += [f"{ident(k.arg)}={n(k.value)}" for k in kws]
            args += [f"**{n(k.value)}" for k in x.keywords if not k.arg]
            return f"{func}({', '.join(args)})"
        if isinstance(x, ast.JoinedStr):
            parts = [const(v.value) if isinstance(v, ast.Constant) else "$f" for v in x.values]
            return "f" + " ".join(parts)
        if isinstance(x, ast.Starred):
            return "*" + n(x.value)
        if isinstance(x, ast.AST):
            fields = [f"{k}={n(v)}" for k, v in ast.iter_fields(x) if k not in _SKIP_FIELDS]
            return f"{type(x).__name__}({', '.join(fields)})"
        if isinstance(x, list):
            return "[" + ", ".join(n(i) for i in x) + "]"
        if isinstance(x, str):
            return repr(ident(x))
        return repr(x)

    return n(node)


def signatures_for_sources(path: str, old: str, new: str) -> list[FixSignature]:
    """One signature per changed leaf statement. Unparseable sides give none."""
    try:
        old_tree, new_tree = ast.parse(old), ast.parse(new)
    except (SyntaxError, ValueError):
        return []
    out: list[FixSignature] = []
    matcher = difflib.SequenceMatcher(None, old.splitlines(), new.splitlines(), autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("equal", "insert"):
            continue  # insert = additive fix: no before-shape to hunt
        olds = _leaf_statements(old_tree, i1 + 1, i2)
        news = _leaf_statements(new_tree, j1 + 1, j2) if tag == "replace" else []
        if tag == "delete" or not news:
            out += [_removed(path, s) for s in olds]
        elif len(olds) == len(news):
            out += [sig for a, b in zip(olds, news) if (sig := _changed(path, a, b))]
        # unequal statement counts on both sides: ambiguous pairing, skipped in v0.1
    return out


def _leaf_statements(tree: ast.AST, first: int, last: int) -> list[ast.stmt]:
    """Statements overlapping lines first..last that contain no other overlapping statement."""
    def overlaps(s: ast.stmt) -> bool:
        return s.lineno <= last and (s.end_lineno or s.lineno) >= first

    hits = [s for s in ast.walk(tree) if isinstance(s, ast.stmt) and overlaps(s)]
    leaves = [s for s in hits
              if not any(c is not s and isinstance(c, ast.stmt) and overlaps(c) for c in ast.walk(s))]
    return sorted(leaves, key=lambda s: (s.lineno, s.col_offset))


def _removed(path: str, stmt: ast.stmt) -> FixSignature:
    anchor = _first_call(stmt) or stmt
    return FixSignature(path, anchor.lineno, normalize(anchor), None, type(anchor).__name__)


def _first_call(stmt: ast.stmt) -> ast.Call | None:
    return next((x for x in ast.walk(stmt) if isinstance(x, ast.Call)), None)


def _changed(path: str, a: ast.stmt, b: ast.stmt) -> FixSignature | None:
    if ast.dump(a) == ast.dump(b):
        return None  # comment or whitespace change only
    steps = _diverge(a, b, [])
    anchor_len = _anchor_depth(a, steps)
    or_call = _or_default_call(a, steps, anchor_len)
    if or_call is not None:
        anchor_len = len(steps) - 1  # anchor on `x.get(K) or OLD`, not the whole statement
    old_anchor, new_anchor = _follow(a, steps[:anchor_len]), _follow(b, steps[:anchor_len])
    old_div, new_div = _follow(a, steps), _follow(b, steps)
    literals = _literal_pairs(old_div, new_div, steps[anchor_len:])
    call_node = or_call if or_call is not None else old_anchor
    call = None
    if isinstance(call_node, ast.Call):
        call = call_node.func.id if isinstance(call_node.func, ast.Name) else getattr(call_node.func, "attr", None)
    delta = tuple((o, w) for o, w, _ in literals) if literals is not None else ()
    if or_call is not None:  # `x.get(K) or OLD`: OLD stands in for the call's next positional slot
        slots = tuple(("arg", len(or_call.args)) for _ in delta)
    else:
        slots = tuple(_slot(old_anchor, rel) for _, _, rel in literals) if literals is not None else ()
    return FixSignature(path, old_anchor.lineno, normalize(old_anchor), normalize(new_anchor),
                        type(old_anchor).__name__, call, delta, slots)


Step = tuple[str, int | None]


def _children(x: ast.AST) -> Iterator[tuple[Step, Any]]:
    for name, value in ast.iter_fields(x):
        if name in _SKIP_FIELDS:
            continue
        if isinstance(value, list):
            for i, v in enumerate(value):
                yield (name, i), v
        else:
            yield (name, None), value


def _diverge(a: Any, b: Any, path: list[Step]) -> list[Step]:
    """Path from the statement to the smallest node where a and b differ."""
    if type(a) is not type(b) or not isinstance(a, ast.AST):
        return path
    ca, cb = list(_children(a)), list(_children(b))
    if [k for k, _ in ca] != [k for k, _ in cb]:
        return path  # list lengths differ: the change is at this node
    diffs = [(k, x, y) for (k, x), (_, y) in zip(ca, cb) if _dump(x) != _dump(y)]
    if len(diffs) == 1 and isinstance(diffs[0][1], ast.AST):
        k, x, y = diffs[0]
        return _diverge(x, y, path + [k])
    return path


def _dump(x: Any) -> str:
    return ast.dump(x) if isinstance(x, ast.AST) else repr(x)


def _follow(node: ast.AST, steps: list[Step]) -> ast.AST:
    for name, i in steps:
        value = getattr(node, name)
        node = value[i] if i is not None else value
    return node


def _or_default_call(stmt: ast.stmt, steps: list[Step], anchor_len: int) -> ast.Call | None:
    """The call in `call(...) or OLD` when the change is to OLD, else None."""
    if not steps or steps[-1][0] != "values" or len(steps) - 1 < anchor_len:
        return None
    parent = _follow(stmt, steps[:-1])
    i = steps[-1][1]
    if (isinstance(parent, ast.BoolOp) and isinstance(parent.op, ast.Or) and i is not None
            and 0 < i == len(parent.values) - 1 and isinstance(parent.values[0], ast.Call)
            and isinstance(parent.values[i], ast.Constant)):
        return parent.values[0]
    return None


def _anchor_depth(stmt: ast.stmt, steps: list[Step]) -> int:
    """Length of the step prefix reaching the nearest Call around the divergence (0 = the statement)."""
    for depth in range(len(steps), 0, -1):
        if isinstance(_follow(stmt, steps[:depth]), ast.Call):
            return depth
    return 0


def _literal_pairs(a: Any, b: Any, rel: list[Step]) -> list[tuple[Any, Any, list[Step]]] | None:
    """Changed (old, new, path-from-anchor) literal pairs, or None if anything non-literal changed."""
    if isinstance(a, ast.Constant) and isinstance(b, ast.Constant):
        return [(a.value, b.value, rel)] if a.value != b.value or type(a.value) is not type(b.value) else []
    if type(a) is not type(b) or not isinstance(a, ast.AST):
        return None if _dump(a) != _dump(b) else []
    ca, cb = list(_children(a)), list(_children(b))
    if [k for k, _ in ca] != [k for k, _ in cb]:
        return None
    out: list[tuple[Any, Any, list[Step]]] = []
    for (k, x), (_, y) in zip(ca, cb):
        sub = _literal_pairs(x, y, rel + [k])
        if sub is None:
            return None
        out += sub
    return out


def _slot(anchor: ast.AST, rel: list[Step]) -> Slot | None:
    """Argument slot of a literal that sits directly in the anchor call, else None."""
    if not isinstance(anchor, ast.Call) or not rel:
        return None
    if len(rel) == 1 and rel[0][0] == "args":
        return ("arg", rel[0][1])
    if len(rel) == 2 and rel[0][0] == "keywords" and rel[1][0] == "value":
        return ("kw", anchor.keywords[rel[0][1]].arg)
    return None

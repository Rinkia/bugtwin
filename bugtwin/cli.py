"""bugtwin command line.

    bugtwin [REV] [-C DIR] [--also PATH ...] [--top N] [--json]   scan (default command)
    bugtwin hook stop                                            Claude Code Stop hook
    bugtwin install-hook                                         print the settings.json block
    bugtwin review [ID STATE]                                    list candidates / mark one

Without REV, scan diffs HEAD against the working tree and scans the working tree.
With REV, it diffs REV^ against REV and scans the tree at REV.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from . import __version__, hook, match

COMMANDS = ("scan", "hook", "install-hook", "review")
STATES = ("fixed", "dismissed", "wrong", "reverted")


def _scan(args: argparse.Namespace) -> int:
    # git reports diff paths from the repo top, so scan from there even when -C is a subdirectory
    root = Path(match._git(Path(args.C), "rev-parse", "--show-toplevel").decode("utf-8").strip()).resolve()
    _mode, cfg_also = hook.load_config(root)
    also = [match.WorkTree(Path(p).resolve()) for p in args.also] + [match.WorkTree(p) for p in cfg_also]
    if args.rev:
        sigs, notes = match.rev_signatures(root, args.rev)
        origin: match.Tree = match.RevTree(root, args.rev)
    else:
        sigs, notes = match.worktree_signatures(root)
        origin = match.WorkTree(root)
    for note in notes:
        print(f"bugtwin: {note}", file=sys.stderr)
    cands = match.find_twins(sigs, origin, also, top=args.top) if sigs else []
    if args.json:
        print(json.dumps({
            "signatures": [asdict(s) for s in sigs],
            "candidates": [{k: v for k, v in asdict(c).items() if k != "masked"} for c in cands],
            "notes": notes,
        }, default=repr, indent=2))
        return 0
    if not sigs:
        print("no signature: no changed statement to hunt (an additive-only fix has no before-shape)")
        return 0
    for s in sigs:
        print(f"fix {s.path}:{s.line}  {s.old_shape}  ->  {s.new_shape}")
    if not cands:
        print("no twins found")
    for c in cands:
        print(f"{c.repo}/{c.path}:{c.line}  tier {c.tier}  {c.shape}")
    return 0


def _review(args: argparse.Namespace) -> int:
    records = hook.read_log()
    cands = {c["id"]: c for r in records for c in r.get("candidates", [])}
    marks = {r["mark"]: r["state"] for r in records if "mark" in r}
    if args.id:
        if args.state not in STATES:
            print(f"bugtwin review: state must be one of {', '.join(STATES)}", file=sys.stderr)
            return 2
        if args.id not in cands:
            print(f"bugtwin review: unknown candidate id {args.id}", file=sys.stderr)
            return 2
        hook.append_log({"mark": args.id, "state": args.state})
        return 0
    for cid, c in cands.items():
        if cid not in marks:
            print(f"{cid} {c['repo']}/{c['path']}:{c['line']} tier {c['tier']} {c['shape']}")
    counts = {s: sum(1 for m in marks.values() if m == s) for s in STATES}
    acted = counts["fixed"] + counts["wrong"] + counts["reverted"]
    rate = f"{100 * (counts['wrong'] + counts['reverted']) // acted}%" if acted else "n/a"
    print(" ".join(f"{s} {n}" for s, n in counts.items())
          + f" unmarked {len(cands) - len(marks)}; wrong/reverted rate {rate}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bugtwin", description="Find the unfixed siblings of the bug you just fixed.")
    p.add_argument("--version", action="version", version=f"bugtwin {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)
    scan = sub.add_parser("scan", help="diff a fix and hunt its twins (default)")
    scan.add_argument("rev", nargs="?", help="commit to diff against its parent (default: working tree vs HEAD)")
    scan.add_argument("-C", default=".", help="repository root (default: current directory)")
    scan.add_argument("--also", action="append", default=[], help="another repo or folder to scan (repeatable)")
    scan.add_argument("--top", type=int, default=5, help="max candidates; 0 = no cap (default 5)")
    scan.add_argument("--json", action="store_true", help="machine-readable output")
    h = sub.add_parser("hook", help="Claude Code hook entry points")
    h.add_argument("event", choices=["stop"])
    sub.add_parser("install-hook", help="print the settings.json block that registers the Stop hook")
    r = sub.add_parser("review", help="list logged candidates, or mark one")
    r.add_argument("id", nargs="?")
    r.add_argument("state", nargs="?")
    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or (argv[0] not in COMMANDS and argv[0] not in ("-h", "--help", "--version")):
        argv = ["scan", *argv]
    args = build_parser().parse_args(argv)
    if args.cmd == "scan":
        return _scan(args)
    if args.cmd == "hook":
        return hook.main_stop()
    if args.cmd == "install-hook":
        print(json.dumps(hook.settings_snippet(), indent=2))
        return 0
    return _review(args)


if __name__ == "__main__":
    sys.exit(main())

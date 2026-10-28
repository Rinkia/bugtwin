"""Score the engine on human-confirmed (fix F, twin T) pairs. Required before release.

    python eval/run.py eval/cases.jsonl --repos DIR [DIR ...]

For each confirmed case, bugtwin diffs F against its parent and scans F's tree
(and, for a twin in another repo, that repo at its last commit made at or
before F's commit time, so twins are pinned in time). A case is a hit when a
candidate in T's file covers a line whose text equals the line T later fixed.
Line numbers are not compared, so commits between F and T cannot shift them.

Reports recall@5, recall with no cap, Tier 1 alone (premise #3), and the mean
number of candidates per signature before top-N (noise ceiling: 8 or fewer).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bugtwin.match import RevTree, find_twins, rev_signatures  # noqa: E402


def _rev_at(root: Path, ts: str) -> str:
    out = subprocess.run(["git", "rev-list", "-1", f"--before={ts}", "HEAD"], cwd=root,
                         check=True, capture_output=True, text=True).stdout.strip()
    return out or "HEAD"


def _commit_time(root: Path, sha: str) -> str:
    return subprocess.run(["git", "show", "-s", "--format=%ct", sha], cwd=root,
                          check=True, capture_output=True, text=True).stdout.strip()


def score_case(case: dict, repos: dict[str, Path]) -> dict:
    root_f, root_t = repos[case["repo_f"]], repos[case["repo_t"]]
    sigs, _ = rev_signatures(root_f, case["f_sha"])
    origin = RevTree(root_f, case["f_sha"])
    if case["repo_t"] == case["repo_f"]:
        also, target = [], origin
    else:
        target = RevTree(root_t, _rev_at(root_t, "@" + _commit_time(root_f, case["f_sha"])))
        also = [target]
    every = find_twins(sigs, origin, also, top=0) if sigs else []
    top5 = find_twins(sigs, origin, also, top=5) if sigs else []
    src = target.read_many([case["t_path"]]).get(case["t_path"], "")
    lines = src.splitlines()

    def hit(cands: list) -> bool:
        return any(c.repo == target.name and c.path == case["t_path"]
                   and any(l.strip() == case["t_line"] for l in lines[c.line - 1:c.end_line])
                   for c in cands)

    return {"sigs": len(sigs), "all": len(every), "hit5": hit(top5), "hit_all": hit(every),
            "hit_t1": hit([c for c in every if c.tier == 1])}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cases")
    ap.add_argument("--repos", nargs="+", required=True, help="repo dirs; matched to cases by folder name")
    args = ap.parse_args(argv)
    repos = {Path(r).resolve().name: Path(r).resolve() for r in args.repos}
    cases = [json.loads(l) for l in Path(args.cases).read_text(encoding="utf-8").splitlines() if l.strip()]
    cases = [c for c in cases if c.get("confirmed") is True]
    if not cases:
        print("no confirmed cases (set \"confirmed\": true on reviewed lines)")
        return 1
    results = [score_case(c, repos) for c in cases]
    n = len(results)
    sig_total = sum(r["sigs"] for r in results) or 1
    print(json.dumps({
        "cases": n,
        "recall_at_5": round(sum(r["hit5"] for r in results) / n, 3),
        "recall_all": round(sum(r["hit_all"] for r in results) / n, 3),
        "recall_tier1_only": round(sum(r["hit_t1"] for r in results) / n, 3),
        "mean_candidates_per_signature": round(sum(r["all"] for r in results) / sig_total, 2),
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

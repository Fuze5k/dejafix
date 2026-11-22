"""Pick the scored ticket stream from the cache and write data/stream.json (issue numbers and labels only).

Roles: `first` (a canonical issue, the first time a problem shows up), `repeat` (a later issue a
maintainer closed as a duplicate of it, with `expect` = the canonical number) and `new` (an unrelated
issue, closed as completed: the agents must not claim it is a known one). Ordered by creation time.

Usage: uv run python -m dejafix.build_stream --clusters 12 --max-repeats 3 --distractors 8
"""

from __future__ import annotations

import argparse
import json
import random
from collections import defaultdict
from typing import Any

from dejafix.fetch_issues import CACHE, MAINTAINER, fetch_by_number, search_all

DATA = CACHE.parent


def has_answer(issue: dict[str, Any], min_chars: int = 150) -> bool:
    """A maintainer said something substantial on it: the resolution a later repeat can reuse."""
    return any(
        c["authorAssociation"] in MAINTAINER and len(c["body"] or "") >= min_chars
        for c in issue["comments"]["nodes"]
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default="astral-sh/uv")
    parser.add_argument("--clusters", type=int, default=12)
    parser.add_argument("--max-repeats", type=int, default=3)
    parser.add_argument("--distractors", type=int, default=8)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    owner, name = args.repo.split("/")
    rng = random.Random(args.seed)

    cache = json.loads((CACHE / f"{owner}__{name}.json").read_text())
    canonicals = {int(k): v for k, v in cache["canonicals"].items()}
    duplicates = {d["number"]: d for d in cache["duplicates"]}
    clusters: dict[int, list[int]] = defaultdict(list)
    for dup, canon in cache["links"].items():
        dup_n = int(dup)
        if canon in canonicals and canonicals[canon]["createdAt"] < duplicates[dup_n]["createdAt"]:
            clusters[canon].append(dup_n)

    usable = sorted(c for c in clusters if has_answer(canonicals[c]))
    big = [c for c in usable if len(clusters[c]) >= 3]
    small = [c for c in usable if len(clusters[c]) < 3]
    n_big = min(len(big), args.clusters // 3)
    chosen = rng.sample(big, n_big) + rng.sample(small, args.clusters - n_big)

    tickets: list[dict[str, Any]] = []
    for canon in chosen:
        tickets.append({"number": canon, "role": "first", "created_at": canonicals[canon]["createdAt"]})
        repeats = sorted(clusters[canon], key=lambda n: duplicates[n]["createdAt"])[: args.max_repeats]
        tickets += [
            {"number": n, "role": "repeat", "expect": canon, "created_at": duplicates[n]["createdAt"]}
            for n in repeats
        ]

    known = set(canonicals) | set(duplicates)
    windows = [
        "2024-03-01..2024-12-31",
        "2025-01-01..2025-06-30",
        "2025-07-01..2025-12-31",
        "2026-01-01..2026-10-01",
    ]
    distractors: list[dict[str, Any]] = []
    for i, window in enumerate(windows):
        query = f"repo:{args.repo} is:issue is:closed reason:completed comments:>2 created:{window}"
        pool = [
            issue
            for issue in search_all(query, 60)
            if issue["number"] not in known
            and has_answer(issue)
            and issue["authorAssociation"] not in MAINTAINER
        ]
        share = args.distractors // len(windows) + (1 if i < args.distractors % len(windows) else 0)
        distractors += rng.sample(pool, min(share, len(pool)))
    tickets += [{"number": i["number"], "role": "new", "created_at": i["createdAt"]} for i in distractors]
    tickets.sort(key=lambda t: t["created_at"])

    # Keep the distractors' text in the cache too, so the run never reads GitHub.
    extra = fetch_by_number(owner, name, [i["number"] for i in distractors])
    cache["distractors"] = {str(k): v for k, v in extra.items()}
    (CACHE / f"{owner}__{name}.json").write_text(json.dumps(cache, indent=1))

    out = DATA / "stream.json"
    out.write_text(json.dumps({"repo": args.repo, "seed": args.seed, "tickets": tickets}, indent=1))
    roles = defaultdict(int)
    for t in tickets:
        roles[t["role"]] += 1
    print(f"{len(tickets)} tickets {dict(roles)} -> {out}")


if __name__ == "__main__":
    main()

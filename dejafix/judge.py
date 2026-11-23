"""Grade the replies of the cold crew, the warm crew and the baseline against the uv maintainers' reply.

One Haiku call per ticket sees the three drafts blind (labelled A, B, C in a per-ticket random order)
with the maintainers' replies on the issue and, for a repeat, on the issue it duplicates.

Usage: uv run python -m dejafix.judge --cold runs/cold-41.jsonl --warm runs/<warm>.jsonl \\
    --baseline runs/<baseline>.jsonl --max-tokens 540000
"""

from __future__ import annotations

import argparse
import json
import random
from collections import Counter
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dejafix.agents import RUNS, run_agent
from dejafix.replay import MAINTAINER_CHARS
from dejafix.tickets import load_pool, maintainer_replies, ticket_text

SYSTEMS = ("cold", "warm", "baseline")
LABELS = ("A", "B", "C")


def load_records(path: Path) -> dict[int, dict[str, Any]]:
    return {r["ticket"]: r for r in map(json.loads, path.read_text().splitlines())}


def ground_truth(pool: dict[int, Any], number: int, canonical: int | None) -> str:
    """The maintainers' replies on the issue, and on its canonical issue for a repeat."""
    own = "\n\n---\n\n".join(maintainer_replies(pool[number]))[:MAINTAINER_CHARS]
    parts = [f"On this issue:\n{own}"]
    if canonical is not None and canonical in pool:
        replies = "\n\n---\n\n".join(maintainer_replies(pool[canonical]))[:MAINTAINER_CHARS]
        parts.append(f"It was closed as a duplicate of uv#{canonical}. On uv#{canonical}:\n{replies}")
    return "\n\n".join(parts)


def grade(number: int, task: str, order: list[str]) -> tuple[dict[str, dict[str, Any]], Any]:
    run = run_agent("judge", task, {}, [], label=f"{number}-judge")
    by_label = {g["label"]: g for g in run.answer["grades"]}
    if sorted(by_label) != list(LABELS):
        raise TypeError(f"uv#{number}: the judge graded {sorted(by_label)}, not A, B and C")
    return {system: by_label[label] for label, system in zip(LABELS, order, strict=True)}, run


def summary(records: list[dict[str, Any]]) -> str:
    lines = []
    for role in ("repeat", "first", "new", "all"):
        rows = [r for r in records if role in ("all", r["stream_role"])]
        if not rows:
            continue
        lines.append(f"{role} ({len(rows)} tickets):")
        for system in SYSTEMS:
            res = Counter(r["grades"][system]["resolution"] for r in rows)
            wrong = sum(r["grades"][system]["wrong_claim"] for r in rows)
            lines.append(
                f"  {system:8} match {res['match']}, partial {res['partial']}, miss {res['miss']}; "
                f"wrong claims {wrong}"
            )
    runs = [run for r in records for run in r["runs"]]
    tokens = sum(run["tokens"] for run in runs)
    cost = sum(run["cost_usd"] for run in runs)
    lines.append(f"judge: {len(runs)} runs, {tokens / 1000:.1f}k tokens, ${cost:.4f}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    for system in SYSTEMS:
        parser.add_argument(f"--{system}", type=Path, required=True, help=f"the {system} run's records")
    parser.add_argument(
        "--max-tokens", type=int, required=True, help="stop once the judge has spent this many"
    )
    args = parser.parse_args()
    sources = {system: load_records(getattr(args, system)) for system in SYSTEMS}
    pool = load_pool()
    common = [n for n in sources["cold"] if all(n in sources[s] for s in SYSTEMS)]
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = RUNS / f"judge-{stamp}.jsonl"
    records: list[dict[str, Any]] = []
    failed: list[int] = []
    for number in common:
        base = sources["cold"][number]
        if not maintainer_replies(pool[number]):
            print(f"uv#{number}: no maintainer reply, skipped")
            continue
        order = list(SYSTEMS)
        random.Random(number).shuffle(order)
        drafts = "\n\n".join(
            f"Draft {label}:\n{sources[system][number].get('reply') or '(no reply)'}"
            for label, system in zip(LABELS, order, strict=True)
        )
        truth = ground_truth(pool, number, base.get("expect"))
        task = f"The issue:\n\n{ticket_text(pool[number])}\n\n===\n\nThe maintainers' replies:\n\n{truth}\n\n===\n\n{drafts}"
        try:
            grades, run = grade(number, task, order)
        except (RuntimeError, TypeError) as error:
            failed.append(number)
            print(f"ERROR uv#{number}: {error}")
            continue
        record = {
            "ticket": number,
            "stream_role": base["stream_role"],
            "order": order,
            "grades": grades,
            "runs": [asdict(run) | {"tokens": run.tokens}],
        }
        records.append(record)
        with out.open("a") as f:
            f.write(json.dumps(record) + "\n")
        print(
            f"uv#{number} {base['stream_role']}: "
            + ", ".join(f"{s} {grades[s]['resolution']}" for s in SYSTEMS)
        )
        spent = sum(r["tokens"] for rec in records for r in rec["runs"])
        if spent > args.max_tokens:
            print(f"STOPPED: {spent} tokens after uv#{number}, over the limit {args.max_tokens}")
            break
    print(summary(records))
    if failed:
        print(f"failed: {failed}")
    print(f"{len(records)} tickets -> {out}")


if __name__ == "__main__":
    main()

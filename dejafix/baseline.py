"""The context-stuffing baseline: the same tickets without Meko, one call each with everything in the prompt.

The prompt holds all the uv docs (the commit the datapacks hold) and every earlier ticket of the stream
with the uv maintainers' reply on it: what a crew without shared memory has to carry to reuse past fixes.
Scored like the replay.

Usage: uv run python -m dejafix.baseline --tickets 6794,11027 --max-runs 10
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from dejafix.agents import RUNS, run_agent
from dejafix.load_docs import CACHE, DOCS_REF
from dejafix.replay import issue_number, summary
from dejafix.tickets import load_links, load_pool, load_stream, maintainer_replies, ticket_text

DOCS_ROOT = CACHE / "astral-sh__uv-docs" / DOCS_REF
HISTORY_BODY_CHARS = 400
HISTORY_REPLY_CHARS = 1200


def docs_text() -> str:
    files = sorted(DOCS_ROOT.rglob("*.md"))
    if not files:
        raise FileNotFoundError(f"no docs under {DOCS_ROOT}: run dejafix.load_docs first")
    docs = (f'<doc path="{f.relative_to(DOCS_ROOT)}">\n{f.read_text().strip()}\n</doc>' for f in files)
    return "\n\n<uv-docs>\n" + "\n\n".join(docs) + "\n</uv-docs>"


def history_entry(issue: dict[str, Any]) -> str:
    """An earlier ticket as the baseline sees it: the start of the report and what the maintainers said."""
    body = (issue["body"] or "").strip()[:HISTORY_BODY_CHARS]
    replies = "\n\n".join(maintainer_replies(issue))[:HISTORY_REPLY_CHARS]
    return (
        f"uv issue #{issue['number']} ({issue['createdAt'][:10]}): {issue['title']}\n{body}\n"
        f"Maintainers' reply: {replies}"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickets", required=True, help="issue numbers, comma-separated")
    parser.add_argument("--max-runs", type=int, default=10)
    parser.add_argument("--max-tokens", type=int, help="stop after the ticket that takes the batch past this")
    args = parser.parse_args()
    wanted = [int(n) for n in args.tickets.split(",")]
    stream = load_stream()
    order = [t["number"] for t in stream]
    by_number = {t["number"]: t for t in stream}
    pool = load_pool()
    links = load_links()
    docs = docs_text()
    if len(wanted) > args.max_runs:
        raise SystemExit(f"{len(wanted)} tickets is more than --max-runs {args.max_runs}")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = RUNS / f"baseline-{stamp}.jsonl"
    records: list[dict[str, Any]] = []
    for number in wanted:
        ticket = by_number[number]
        earlier = [pool[n] for n in order[: order.index(number)]]
        history = "\n\n---\n\n".join(history_entry(issue) for issue in earlier) or "(none yet)"
        task = f"Earlier issues:\n\n{history}\n\n===\n\nThe new issue:\n\n{ticket_text(pool[number])}"
        run = run_agent("baseline", task, {}, [], label=f"{number}-baseline", context=docs)
        named = issue_number(run.answer.get("known_issue"))
        known = run.answer.get("verdict") == "known" and named is not None
        record: dict[str, Any] = {
            "ticket": number,
            "stream_role": ticket["role"],
            "expect": ticket.get("expect"),
            "baseline": {"verdict": run.answer.get("verdict"), "known_issue": named},
            "history_tickets": len(earlier),
        }
        if ticket["role"] == "repeat":
            record["correct"] = known and links.get(named, named) == ticket["expect"]
            record["exact"] = known and named == ticket["expect"]
        else:
            record["correct"] = not known
        record["reply"] = run.answer.get("reply")
        record["runs"] = [asdict(run) | {"tokens": run.tokens}]
        records.append(record)
        with out.open("a") as f:
            f.write(json.dumps(record) + "\n")
        print(
            f"uv#{number} {ticket['role']}: baseline {record['baseline']} correct={record['correct']} "
            f"| {run.tokens} tokens ${run.cost_usd:.4f}"
        )
        spent = sum(r["tokens"] for rec in records for r in rec["runs"])
        if args.max_tokens and spent > args.max_tokens:
            print(f"STOPPED: {spent} tokens after uv#{number}, over the batch limit {args.max_tokens}")
            break
    print(summary(records))
    print(f"{len(records)} tickets -> {out}")


if __name__ == "__main__":
    main()

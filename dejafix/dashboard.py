"""Build the static dashboard of the recorded runs: one HTML file with the data embedded.

It compares the cold crew, the warm crew and the context-stuffing baseline on the same tickets, shows
every ticket's trace (each agent's Meko calls, reasoning and plan) and which learning a repeat reused.

Usage: uv run python -m dejafix.dashboard --cold results/cold.jsonl --warm results/warm.jsonl \\
    --baseline results/baseline.jsonl --judge results/judge.jsonl --out docs/index.html
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from dejafix.judge import SYSTEMS, load_records
from dejafix.load_docs import DOCS_REF
from dejafix.tickets import load_pool, load_stream

TEMPLATE = Path(__file__).with_name("dashboard.html")
LEARNING_OF = re.compile(r"^Known issue uv#(\d+):")
ANSWER_KEYS = ("verdict", "known_issue", "evidence", "reasoning", "plan", "approved", "problems")


def run_view(run: dict[str, Any]) -> dict[str, Any]:
    """One agent run as the trace shows it: its Meko calls and what it said, without the raw reply."""
    answer = run.get("answer") or {}
    return {
        "role": run["role"],
        "tokens": run["tokens"],
        "seconds": run["seconds"],
        "cost": run["cost_usd"],
        "calls": [
            {"tool": c["tool"], "query": c.get("query"), "source": c.get("force_source")}
            for c in run.get("calls", [])
        ],
        "answer": {k: answer[k] for k in ANSWER_KEYS if k in answer},
    }


def system_view(record: dict[str, Any] | None, system: str) -> dict[str, Any] | None:
    if record is None:
        return None
    runs = record["runs"]
    answer = record.get("baseline") if system == "baseline" else record.get("triage")
    view = {
        "verdict": (answer or {}).get("verdict"),
        "known_issue": (answer or {}).get("known_issue"),
        "correct": bool(record.get("correct")),
        "exact": bool(record.get("exact")),
        "tokens": sum(r["tokens"] for r in runs),
        "cost": sum(r["cost_usd"] for r in runs),
        "seconds": sum(r["seconds"] for r in runs),
        "meko_calls": sum(r["tool_calls"] for r in runs),
        "reply": record.get("reply"),
        "runs": [run_view(r) for r in runs],
    }
    if record.get("learning"):
        view["learning"] = record["learning"]
    if record.get("shortfall"):
        view["shortfall"] = record["shortfall"]
    return view


def learned_from(warm: dict[int, dict[str, Any]]) -> dict[int, int]:
    """The issue each saved learning is filed under -> the ticket whose review saved it."""
    sources: dict[int, int] = {}
    for number, record in warm.items():
        filed = record.get("learning_filed_under")
        if filed is None and record.get("learning"):
            match = LEARNING_OF.match(record["learning"])
            filed = int(match.group(1)) if match else number
        if filed is not None:
            sources.setdefault(filed, number)
    return sources


def build(paths: dict[str, Path], meko: str, note: str) -> dict[str, Any]:
    records = {system: load_records(paths[system]) for system in SYSTEMS}
    judged = load_records(paths["judge"]) if paths.get("judge") else {}
    pool = load_pool()
    sources = learned_from(records["warm"])
    tickets = []
    for ticket in load_stream():
        number = ticket["number"]
        issue = pool[number]
        row: dict[str, Any] = {
            "number": number,
            "date": issue["createdAt"][:10],
            "title": issue["title"],
            "url": f"https://github.com/astral-sh/uv/issues/{number}",
            "role": ticket["role"],
            "expect": ticket.get("expect"),
            "systems": {s: system_view(records[s].get(number), s) for s in SYSTEMS},
            "grades": (judged.get(number) or {}).get("grades"),
        }
        warm = row["systems"]["warm"]
        if warm and warm["verdict"] == "known" and warm["known_issue"] in sources:
            row["reused_from"] = sources[warm["known_issue"]]
        tickets.append(row)
    return {
        "repo": "astral-sh/uv",
        "docs_ref": DOCS_REF,
        "model": "Claude Haiku 5.5 (headless Claude Code)",
        "meko": meko,
        "note": note,
        "systems": list(SYSTEMS),
        "tickets": tickets,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    for name in (*SYSTEMS, "judge"):
        parser.add_argument(f"--{name}", type=Path, required=name != "judge")
    parser.add_argument("--out", type=Path, default=Path("docs/index.html"))
    parser.add_argument(
        "--meko", default="Meko 1.20261116.0, local", help="where the runs' Meko ran, as shown"
    )
    parser.add_argument(
        "--note", default="", help="a sentence under the chart, e.g. where each run's Meko ran"
    )
    args = parser.parse_args()
    data = build({name: getattr(args, name) for name in (*SYSTEMS, "judge")}, args.meko, args.note)
    # Inside <script>: "</" would end the element early; issue titles and replies are untrusted text.
    embedded = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(TEMPLATE.read_text().replace("__DATA__", embedded))
    print(f"{len(data['tickets'])} tickets -> {args.out} ({args.out.stat().st_size / 1000:.0f} kB)")


if __name__ == "__main__":
    main()

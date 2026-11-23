"""Replay tickets through the crew on one Meko datapack and record what each agent cost.

Per ticket: triage searches Meko; the solver drafts a reply (reusing a known fix, or from the docs);
the reviewer checks it. A ticket that was not a known issue goes to the maintainer step: the uv
maintainers' real reply is the human decision, the reviewer writes the learning from it, and only
then is it saved (memory_add) and promoted into the datapack's collective memory (memory_promote).
Agents only read Meko; every write is the pipeline's, after review.

Memory off (the cold run): no learning is looked up or saved, so every ticket takes the full path.

Usage: uv run python -m dejafix.replay --datapack dejafix-uv --tickets 6794:full,11300:triage --max-runs 10
"""

from __future__ import annotations

import argparse
import base64
import json
import re
from collections import defaultdict
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from dejafix.agents import RUNS, AgentRun, run_agent
from dejafix.load_docs import loaded_datapack
from dejafix.meko import Meko
from dejafix.tickets import load_links, load_pool, load_stream, maintainer_replies, ticket_text

LEARNING_PREFIX = "Known issue uv#{}:"
KNOWN_PREFIX = re.compile(r"^Known issue uv#\d+:\s*")

MAINTAINER_CHARS = 3000


def issue_number(value: Any) -> int | None:
    """An agent's issue reference (6794, "6794", "uv#6794") as a number; None when it names none."""
    digits = re.sub(r"\D", "", str(value)) if value is not None else ""
    return int(digits) if digits else None


class Crew:
    def __init__(
        self, meko: Meko, datapack_id: str, max_runs: int, links: dict[int, int], *, memory: bool
    ) -> None:
        self.meko = meko
        self.datapack_id = datapack_id
        self.runs_left = max_runs
        self.links = links
        self.memory = memory
        self.tools = [t["name"] for t in meko.tools()]

    def cluster(self, number: int | None) -> int | None:
        """The canonical issue a maintainer closed this one as a duplicate of (itself when none)."""
        return None if number is None else self.links.get(number, number)

    def learning_of(self, issue: int) -> str | None:
        """The approved learning saved for `issue`, from the datapack's collective memory."""
        ids = {"datapack_id": self.datapack_id, "agent_id": "dejafix_harness"}
        ids["conversation_id"] = self._conversation("dejafix_harness", 0, "Replay reports")
        promoted = self.meko.call("memory_get_all", **ids, promoted=True)["promoted_memories"]
        prefix = LEARNING_PREFIX.format(issue)
        return next((m["data"] for m in promoted if m["data"].startswith(prefix)), None)

    def _conversation(self, agent_id: str, number: int, title: str) -> str:
        return self.meko.call(
            "conversation_create",
            agent_id=agent_id,
            datapack_id=self.datapack_id,
            session_id=f"{self.datapack_id}-{number}-{agent_id}",
            title=title,
        )["id"]

    def _run(
        self, role: str, number: int, task: str, tools: list[str] | None = None
    ) -> tuple[AgentRun, dict[str, str]]:
        if self.runs_left <= 0:
            raise RuntimeError("run budget spent")
        self.runs_left -= 1
        agent_id = f"{role}_agent"
        ids = {
            "datapack_id": self.datapack_id,
            "agent_id": agent_id,
            "conversation_id": self._conversation(agent_id, number, f"uv#{number} {role}"),
        }
        run = run_agent(role, task, ids, self.tools, label=f"{number}-{role}", tools=tools)
        self.meko.call(
            "conversation_add_message",
            **ids,
            input=task,
            output=json.dumps(run.answer),
            reasoning=str(run.answer.get("reasoning", "")),
            plan=str(run.answer.get("plan", "")),
            metadata={
                "model": run.models,
                "usage": run.usage,
                "tool_calls": run.tool_calls,
                "seconds": run.seconds,
            },
            seed=f"{number}-{role}",
        )
        return run, ids

    def handle(self, ticket: dict[str, Any], issue: dict[str, Any], steps: str) -> dict[str, Any]:
        number = issue["number"]
        text = ticket_text(issue)
        runs: list[AgentRun] = []
        triage, _ = self._run("triage", number, text)
        runs.append(triage)
        named = issue_number(triage.answer.get("known_issue"))
        claims_known = triage.answer.get("verdict") == "known" and named is not None
        learning = self.learning_of(named) if self.memory and claims_known else None
        known = learning is not None
        record: dict[str, Any] = {
            "ticket": number,
            "memory": self.memory,
            "stream_role": ticket["role"],
            "expect": ticket.get("expect"),
        }
        record["triage"] = {k: triage.answer.get(k) for k in ("verdict", "known_issue")}
        record["triage"]["learning_found"] = known
        if ticket["role"] == "repeat":
            # Right when the named issue is in the expected cluster; exact when it is the canonical itself.
            record["correct"] = known and self.cluster(named) == ticket["expect"]
            record["exact"] = known and named == ticket["expect"]
        else:
            record["correct"] = not known

        if steps == "full":
            if known:
                solver_task = f"{text}\n\nKnown issue's learning:\n{learning}"
                solver, _ = self._run("solver", number, solver_task, tools=[])
            else:
                solver_task = f"{text}\n\nTriage result:\n{json.dumps(triage.answer)}"
                solver, _ = self._run("solver", number, solver_task)
            runs.append(solver)
            review_task = f"{text}\n\nSolver's draft:\n{json.dumps(solver.answer)}"
            if known:
                review_task += f"\n\nThe approved learning it reuses:\n{learning}"
            else:
                replies = "\n\n---\n\n".join(maintainer_replies(issue))[:MAINTAINER_CHARS]
                review_task += f"\n\nThe uv maintainers' reply on this issue (the final word):\n{replies}"
            reviewer, reviewer_ids = self._run("reviewer", number, review_task)
            runs.append(reviewer)
            record["reply"] = solver.answer.get("reply")
            record["approved"] = reviewer.answer.get("approved")
            new_learning = reviewer.answer.get("learning")
            if self.memory and not known and new_learning:
                record["learning"] = new_learning
                record["learning_id"] = self._save_learning(number, new_learning, reviewer_ids)
            record["maintainer_needed"] = not known
        record["runs"] = [asdict(r) | {"tokens": r.tokens} for r in runs]
        return record

    def _save_learning(self, number: int, learning: str, ids: dict[str, str]) -> str:
        """The maintainer's reply is the approval: save it as a learning, promote it into collective memory.

        The learning is filed under its own issue (GitHub's duplicate links point at it); the pipeline sets
        the number, not the model. A tracking issue the maintainer named stays in the text."""
        learning = f"{LEARNING_PREFIX.format(number)} {KNOWN_PREFIX.sub('', learning.strip(), count=1)}"
        added = self.meko.call(
            "memory_add",
            **ids,
            text=learning,
            metadata={"issue": number, "kind": "known_issue", "approved_by": "maintainer"},
        )
        memory_id = added["results"][0]["id"]
        promoted = self.meko.call(
            "memory_promote", **(ids | {"agent_id": "maintainer"}), memory_ids=[memory_id]
        )
        if memory_id not in promoted.get("inserted_ids", []) + promoted.get("updated_ids", []):
            raise RuntimeError(f"uv#{number}: promotion of {memory_id} failed: {promoted}")
        return memory_id

    def save_report(self, records: list[dict[str, Any]], name: str) -> dict[str, Any]:
        conversation_id = self._conversation("dejafix_harness", 0, "Replay reports")
        payload = json.dumps(records, indent=1).encode()
        return self.meko.call(
            "artifact_put",
            datapack_id=self.datapack_id,
            conversation_id=conversation_id,
            agent_id="dejafix_harness",
            filename=name,
            content_type="application/json",
            content_base64=base64.b64encode(payload).decode(),
        )


def summary(records: list[dict[str, Any]]) -> str:
    """Tokens, cost and runs per role, and how many tickets triage got right, per stream role."""
    per_role: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        for run in record["runs"]:
            per_role[run["role"]].append(run)
    lines = []
    for role, runs in per_role.items():
        tokens = sum(r["tokens"] for r in runs)
        cost = sum(r["cost_usd"] for r in runs)
        lines.append(
            f"{role}: {len(runs)} runs, {tokens / 1000:.1f}k tokens (avg {tokens / len(runs) / 1000:.1f}k), ${cost:.4f}"
        )
    for kind in ("first", "repeat", "new"):
        group = [r for r in records if r["stream_role"] == kind]
        if group:
            right = sum(bool(r["correct"]) for r in group)
            exact = f", exact {sum(bool(r.get('exact')) for r in group)}" if kind == "repeat" else ""
            lines.append(f"{kind}: {right}/{len(group)} right{exact}")
    calls = [c for runs in per_role.values() for r in runs for c in r["calls"]]
    loose = sum(c.get("force_source") != "knowledge_base" for c in calls if c["tool"] == "context_search")
    lines.append(f"Meko calls: {len(calls)}, context_search without force_source=knowledge_base: {loose}")
    total = sum(r["tokens"] for runs in per_role.values() for r in runs)
    cost = sum(r["cost_usd"] for runs in per_role.values() for r in runs)
    lines.append(
        f"total: {sum(len(v) for v in per_role.values())} runs, {total / 1000:.1f}k tokens, ${cost:.4f}"
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--datapack", default="dejafix-uv")
    parser.add_argument("--memory", choices=("on", "off"), default="on")
    parser.add_argument("--tickets", required=True, help="number:full|triage, comma-separated, in order")
    parser.add_argument("--max-runs", type=int, default=10)
    parser.add_argument("--max-tokens", type=int, help="stop after the ticket that takes the batch past this")
    args = parser.parse_args()
    plan = [(int(n), steps) for n, steps in (item.split(":") for item in args.tickets.split(","))]
    stream = {t["number"]: t for t in load_stream()}
    pool = load_pool()
    links = load_links()
    datapack_id = loaded_datapack(args.datapack)["datapack_id"]
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = RUNS / f"replay-{args.datapack}-{args.memory}-{stamp}.jsonl"
    records = []
    with Meko() as meko:
        crew = Crew(meko, datapack_id, args.max_runs, links, memory=args.memory == "on")
        failures = 0
        for number, steps in plan:
            try:
                record = crew.handle(stream[number], pool[number], steps)
            except (RuntimeError, TypeError) as exc:
                # One agent's failure costs its ticket, not the batch; two in a row stop it.
                failures += 1
                print(f"ERROR uv#{number}: {exc}")
                if failures >= 2:
                    print("STOPPED: two tickets in a row failed")
                    break
                continue
            failures = 0
            records.append(record)
            with out.open("a") as f:
                f.write(json.dumps(record) + "\n")
            tokens = [f"{r['role']} {r['tokens']}" for r in record["runs"]]
            print(
                f"uv#{number} {record['stream_role']}: triage {record['triage']} correct={record['correct']} | {tokens}"
            )
            spent = sum(r["tokens"] for rec in records for r in rec["runs"])
            if args.max_tokens and spent > args.max_tokens:
                print(f"STOPPED: {spent} tokens after uv#{number}, over the batch limit {args.max_tokens}")
                break
        artifact = crew.save_report(records, out.with_suffix(".json").name)
    print(summary(records))
    print(f"{len(records)} tickets -> {out}; artifact {artifact['content_hash'][:12]}")


if __name__ == "__main__":
    main()

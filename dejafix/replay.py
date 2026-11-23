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
import tempfile
import time
from collections import defaultdict
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dejafix.agents import RUNS, AgentRun, run_agent
from dejafix.load_docs import document_state, loaded_datapack
from dejafix.meko import Meko
from dejafix.tickets import load_links, load_pool, load_stream, maintainer_replies, ticket_text

LEARNING_PREFIX = "Known issue uv#{}:"
KNOWN_PREFIX = re.compile(r"^Known issue uv#\d+:\s*")

MAINTAINER_CHARS = 3000
LEARNING_INDEX_TIMEOUT = 600  # seconds for Meko Cloud to index a learning filed as a document


def issue_number(value: Any) -> int | None:
    """An agent's issue reference (6794, "6794", "uv#6794") as a number; None when it names none."""
    digits = re.sub(r"\D", "", str(value)) if value is not None else ""
    return int(digits) if digits else None


class Crew:
    def __init__(
        self, meko: Meko, datapack_id: str, max_runs: int, links: dict[int, int], *, memory: bool, cloud: bool
    ) -> None:
        self.meko = meko
        self.datapack_id = datapack_id
        self.cloud = cloud
        self.runs_left = max_runs
        self.links = links
        self.memory = memory
        self.tools = [t["name"] for t in meko.tools()]

    def _metadata(self, metadata: dict[str, Any]) -> dict[str, Any] | str:
        """Meko Cloud takes metadata as a JSON string; the local Meko takes the object."""
        return json.dumps(metadata) if self.cloud else metadata

    def cluster(self, number: int | None) -> int | None:
        """The canonical issue a maintainer closed this one as a duplicate of (itself when none)."""
        return None if number is None else self.links.get(number, number)

    def _harness_ids(self) -> dict[str, str]:
        ids = {"datapack_id": self.datapack_id, "agent_id": "dejafix_harness"}
        ids["conversation_id"] = self._conversation("dejafix_harness", 0, "Replay reports")
        return ids

    def learning_of(self, issue: int) -> str | None:
        """The approved learnings saved for `issue` (the first one, then other reports of it), from the
        datapack's collective memory."""
        ids, by_id, page, pages = self._harness_ids(), {}, 1, 1
        # Meko Cloud pages the list (page_size up to 100). The local Meko, while linked to Meko Cloud, answers
        # page 1 of 20 whatever page is asked (checked 2026-11-23): a page with nothing new ends the loop.
        while page <= pages:
            reply = self.meko.call("memory_get_all", **ids, promoted=True, page=page, page_size=100)
            new = {m["id"]: m for m in reply["promoted_memories"] if m["id"] not in by_id}
            if not new:
                break
            by_id |= new
            page, pages = page + 1, reply.get("total_pages", 1)
        promoted = list(by_id.values())
        prefix = LEARNING_PREFIX.format(issue)
        texts = [m["data"] for m in sorted(promoted, key=lambda m: m.get("created_at") or "")]
        found = [t for t in texts if t.startswith(prefix)]
        return "\n\n".join(found) or None

    def learning_ranks(self, issue: int, queries: list[str]) -> list[int | None]:
        """Where a learning of `issue` ranks in the knowledge-base results of each query (None: absent)."""
        prefix = LEARNING_PREFIX.format(issue)
        ranks: list[int | None] = []
        for query in queries:
            found = (
                self.meko.call(
                    "context_search", **self._harness_ids(), query=query, force_source="knowledge_base"
                )["results"]["knowledge_base"]
                or []
            )
            ranks.append(
                next((i for i, hit in enumerate(found, 1) if hit["chunk_text"].startswith(prefix)), None)
            )
        return ranks

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
        metadata = {
            "model": run.models,
            "usage": run.usage,
            "tool_calls": run.tool_calls,
            "seconds": run.seconds,
        }
        plan = str(run.answer.get("plan") or "")
        message = self.meko.call(
            "conversation_add_message",
            **ids,
            input=task,
            output=json.dumps(run.answer),
            reasoning=str(run.answer.get("reasoning", "")),
            # Meko Cloud takes the plan as a list of sentences; the local Meko takes a string.
            plan=([plan] if plan else None) if self.cloud else plan,
            metadata=self._metadata(metadata),
            seed=f"{number}-{role}",
        )
        if self.cloud:
            # Meko Cloud keeps a trace per conversation: the agent's model spend goes on it, per turn.
            self.meko.call(
                "track_token_usage",
                **{k: ids[k] for k in ("datapack_id", "conversation_id")},
                name=f"{role} LLM Response",
                model=",".join(run.models) or run.model,
                input_tokens=run.usage["input_tokens"]
                + run.usage["cache_creation_input_tokens"]
                + run.usage["cache_read_input_tokens"],
                output_tokens=run.usage["output_tokens"],
                message_id=message["message_id"],
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
                # The maintainer's duplicate link comes after the crew answered: it is the human correction.
                # A miss of a known issue (a shortfall) is filed under that issue, so the next report
                # worded like this one finds it; the triage's own queries, run again, show the gain.
                canonical = self.links.get(number)
                shortfall = canonical is not None and self.learning_of(canonical) is not None
                queries = [c["query"] for c in triage.calls if c["tool"] == "context_search"]
                before = self.learning_ranks(canonical, queries) if shortfall else None
                filed_under = canonical if canonical is not None else number
                record["learning"] = new_learning
                record["learning_filed_under"] = filed_under
                record["learning_id"] = self._save_learning(
                    filed_under, new_learning, reviewer_ids, reported_as=number
                )
                if shortfall:
                    record["shortfall"] = {
                        "canonical": canonical,
                        "queries": queries,
                        "rank_before": before,
                        "rank_after": self.learning_ranks(canonical, queries),
                    }
                    if self.cloud:
                        record["shortfall"]["selflearning"] = self.verify_searches(queries)
            record["maintainer_needed"] = not known
        record["runs"] = [asdict(r) | {"tokens": r.tokens} for r in runs]
        return record

    def _save_learning(self, number: int, learning: str, ids: dict[str, str], reported_as: int) -> str:
        """The maintainer's reply is the approval: save it as a learning, promote it into collective memory.

        The learning is filed under the issue GitHub's duplicate links point at: its own, or the one the
        maintainer closed it against (then it is another report of that issue). The pipeline sets the
        number, not the model; a tracking issue the maintainer only named stays in the text."""
        body = KNOWN_PREFIX.sub("", learning.strip(), count=1)
        alias = reported_as != number
        if alias:
            body = f"(also reported as uv#{reported_as}) {body}"
        added = self.meko.call(
            "memory_add",
            **ids,
            text=f"{LEARNING_PREFIX.format(number)} {body}",
            metadata=self._metadata(
                {
                    "issue": number,
                    "reported_as": reported_as,
                    "kind": "known_issue_report" if alias else "known_issue",
                    "approved_by": "maintainer",
                }
            ),
        )
        memory_id = added["results"][0]["id"]
        promoted = self.meko.call(
            "memory_promote", **(ids | {"agent_id": "maintainer"}), memory_ids=[memory_id]
        )
        if memory_id not in promoted.get("inserted_ids", []) + promoted.get("updated_ids", []):
            raise RuntimeError(f"uv#{number}: promotion of {memory_id} failed: {promoted}")
        if self.cloud:
            self._file_as_document(f"{LEARNING_PREFIX.format(number)} {body}", memory_id, ids)
        return memory_id

    def _file_as_document(self, text: str, memory_id: str, ids: dict[str, str]) -> None:
        """Meko Cloud puts a promoted memory into the knowledge base without a usable vector (its distance to
        any query, even its own text, is ~0.99; checked 2026-11-23), so context_search's knowledge-base cutoff
        drops it and only a keyword match finds it. The same text filed as a knowledge-base document is
        embedded like the docs. Plain text: Meko Cloud makes no chunk from a Markdown file without a heading,
        and a .txt file is one chunk with the text as it is. The next ticket starts once it is indexed."""
        with tempfile.TemporaryDirectory(prefix="dejafix-learning-") as tmp:
            path = Path(tmp) / f"learning-{memory_id}.txt"
            path.write_text(text + "\n")
            key = self.meko.call("knowledgebase_upload_file", **ids, path=str(path))["document_key"]
            deadline = time.monotonic() + LEARNING_INDEX_TIMEOUT
            while True:
                status = self.meko.call("knowledgebase_document_status", **ids, document_key=key)
                state, chunks = document_state(status)
                if state == "COMPLETED" and chunks:
                    return
                if state in ("COMPLETED", "FAILED") or time.monotonic() > deadline:
                    raise RuntimeError(f"learning {memory_id}: document {key} {state}, {chunks} chunks")
                time.sleep(5)

    def verify_searches(self, queries: list[str]) -> list[dict[str, Any]]:
        """Meko Cloud's own replay of the triage's searches now that the learning is filed: its band before
        (from the search log) and after (selflearning_verify)."""
        ids = self._harness_ids()
        base = {"datapack_id": ids["datapack_id"], "conversation_id": ids["conversation_id"]}
        rows = self.meko.call("selflearning_query_log", **base, band="all", last="1d")["rows"]
        checks: list[dict[str, Any]] = []
        for query in queries:
            row = next((r for r in rows if r["query"] == query and r["agent_id"] == "triage_agent"), None)
            if row is None:
                checks.append({"query": query, "logged": False})
                continue
            replay = self.meko.call("selflearning_verify", **base, log_id=row["id"])
            checks.append({"query": query, "logged": True, "band_before": row["band"], "verify": replay})
        return checks

    def selflearning_status(self) -> dict[str, Any]:
        """Meko Cloud's read of the datapack's context quality: strong and weak searches, and its to-dos."""
        ids = self._harness_ids()
        return self.meko.call(
            "selflearning_status", datapack_id=ids["datapack_id"], conversation_id=ids["conversation_id"]
        )

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
    datapack = loaded_datapack(args.datapack)
    datapack_id = datapack["datapack_id"]
    cloud = datapack.get("where") == "cloud"
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = RUNS / f"replay-{args.datapack}-{args.memory}-{stamp}.jsonl"
    records = []
    with Meko() as meko:
        crew = Crew(meko, datapack_id, args.max_runs, links, memory=args.memory == "on", cloud=cloud)
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
        if cloud:
            status = crew.selflearning_status()
            out.with_suffix(".selflearning.json").write_text(json.dumps(status, indent=1))
            print(f"self-learning: quality {status.get('quality')}, open fixes {status.get('open_fixes')}")
        print(
            f"Meko calls by the harness: {meko.calls}, rate-limited {meko.rate_limited}, retried {meko.retried}"
        )
    print(summary(records))
    print(f"{len(records)} tickets -> {out}; artifact {artifact['content_hash'][:12]}")


if __name__ == "__main__":
    main()

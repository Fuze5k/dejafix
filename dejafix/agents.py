"""The crew's agents: headless Claude Code runs with Meko as their only MCP server.

Each run starts in an empty temporary folder with no settings sources but the (empty) project, no
built-in tools and only its role's Meko tools, so its token count is the agent's own work, not the
machine's global context. The Meko client token reaches the MCP config through the environment.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dejafix.meko import DEFAULT_URL, client_token

RUNS = Path(__file__).resolve().parent.parent / "runs"

COMMON = """You are part of DejaFix, a support crew for the issue tracker of uv, the Python package manager.
Every agent of the crew shares one Meko datapack. Its knowledge base holds the uv docs and the crew's
learnings: resolutions of past issues that a uv maintainer approved, written as "Known issue uv#N: ...".
On every Meko tool call pass datapack_id="{datapack_id}", conversation_id="{conversation_id}",
agent_id="{agent_id}" and force_source="knowledge_base". Keep searches short (a few words or an error
message). Answer with one JSON object and nothing else."""


def _schema(**properties: Any) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


TEXT = {"type": "string"}
TEXTS = {"type": "array", "items": TEXT}
ISSUE = {"type": ["integer", "null"]}
VERDICT = {"type": "string", "enum": ["known", "docs", "new"]}

ROLES: dict[str, dict[str, Any]] = {
    "triage": {
        "model": "haiku",
        "tools": ["context_search"],
        "schema": _schema(verdict=VERDICT, known_issue=ISSUE, evidence=TEXTS, reasoning=TEXT, plan=TEXT),
        "prompt": """You are the triage agent. Decide what the new issue is, from what Meko returns:
- "known": a learning covers this issue: the same root cause, or the same missing feature, even when
  this report asks for a narrower case of it (one package instead of all) or words it differently.
  The test: a uv maintainer would close this issue as a duplicate of that learning's issue.
- "docs": no learning covers it, but the uv docs answer the question.
- "new": neither; it needs a maintainer.
Call context_search 1 to 3 times: first with the issue's title exactly as written, then, if needed,
with an error message or the problem in other words. Name only an issue number that a learning gives;
a learning that only shares the topic (both about dependencies) is not a match, and neither is one
that names your issue's request only in passing: its main problem must be your issue's main problem.
JSON: {"verdict": "known"|"docs"|"new", "known_issue": <N or null>, "evidence": [<short quotes>],
"reasoning": "<two sentences>", "plan": "<one sentence for the solver>"}""",
    },
    "solver": {
        "model": "haiku",
        "tools": ["context_search", "knowledgebase_search"],
        "schema": _schema(
            reply=TEXT, sources=TEXTS, needs_maintainer={"type": "boolean"}, reasoning=TEXT, plan=TEXT
        ),
        "prompt": """You are the solver. Draft the reply a uv maintainer would post on the issue.
When you are given a known issue's learning, reuse it: say this is tracked in uv#N and give its
resolution, status or workaround, adapted to this report; you have no tools then. Otherwise search
the docs (at most 3 searches) and answer only what the docs support; if they do not settle it, say
what a maintainer must decide. Write to the issue's author; do not mention triage or the crew.
JSON: {"reply": "<markdown, under 150 words>", "sources": [<doc sections or uv#N>],
"needs_maintainer": true|false, "reasoning": "<two sentences>", "plan": "<what you did>"}""",
    },
    "reviewer": {
        "model": "haiku",
        "tools": [],
        "schema": _schema(
            approved={"type": "boolean"},
            problems=TEXTS,
            learning={"type": ["string", "null"]},
            reasoning=TEXT,
            plan=TEXT,
        ),
        "prompt": """You are the reviewer. Check the solver's draft against the evidence you are given.
Approve only if every claim is supported and the draft answers the issue. When a maintainer's own
reply is given, it is the final word: judge the draft against it and write the learning from it,
so the crew reuses the maintainer's resolution next time. A learning starts "Known issue uv#N:",
names the issue's main problem in general terms first (the root cause or the missing feature that the
maintainer's reply resolves), then the ways users report that problem (symptoms, error text, narrower
requests, what they tried), then the resolution, status or workaround in the maintainer's terms; under
120 words. Leave out other requests the report makes in passing (other commands or features it also
asks for): they are other issues, and naming them makes the crew match those issues to this one. If the maintainer says it is tracked in
another issue, start "Known issue uv#<that issue>:" instead. No maintainer reply: learning null.
JSON: {"approved": true|false, "problems": [<short>], "learning": "<text or null>",
"reasoning": "<two sentences>", "plan": "<one sentence>"}""",
    },
    # The context-stuffing baseline: no Meko, one call with the docs and the issue history in the prompt.
    "baseline": {
        "model": "haiku",
        "tools": [],
        "mcp": False,
        "schema": _schema(verdict=VERDICT, known_issue=ISSUE, reply=TEXT, sources=TEXTS, reasoning=TEXT),
        "prompt": """You are the support agent for the issue tracker of uv, the Python package manager.
Below are the uv docs. The user message holds earlier uv issues with the uv maintainers' replies, then
a new issue. Decide what the new issue is:
- "known": it repeats an earlier issue (the same root cause or the same missing feature, even when it
  asks for a narrower case or words it differently): a uv maintainer would close it as a duplicate.
- "docs": no earlier issue covers it, but the uv docs answer the question.
- "new": neither; it needs a maintainer.
Then draft the reply a uv maintainer would post: reuse the earlier resolution for a known issue
("tracked in uv#N"), otherwise answer only what the docs support. Write to the issue's author.
Answer with one JSON object and nothing else:
{"verdict": "known"|"docs"|"new", "known_issue": <N or null>, "reply": "<markdown, under 150 words>",
"sources": [<doc sections or uv#N>], "reasoning": "<two sentences>"}""",
    },
}


@dataclass
class AgentRun:
    role: str
    model: str
    answer: dict[str, Any]
    seconds: float
    turns: int
    tool_calls: int
    usage: dict[str, int]
    cost_usd: float
    models: list[str] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    attempts: int = 1

    @property
    def tokens(self) -> int:
        return sum(self.usage.values())


def _mcp_config(path: Path) -> Path:
    config = {
        "mcpServers": {
            "meko": {"type": "http", "url": DEFAULT_URL, "headers": {"Authorization": "Bearer ${MEKO_TOKEN}"}}
        }
    }
    path.write_text(json.dumps(config))
    return path


def _answer(result: dict[str, Any]) -> dict[str, Any]:
    """The agent's final answer, validated by the CLI against the role's schema."""
    answer = result.get("structured_output")
    if not isinstance(answer, dict):
        raise TypeError(f"no structured answer: {str(result.get('result'))[:400]!r}")
    return answer


USAGE_KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
# Haiku now and then garbles the StructuredOutput call (one field swallows the next) and repeats the same
# mistake on the CLI's own retries; a fresh run does not carry the bad attempt, so it gets one more.
SCHEMA_FAILED = "error_max_structured_output_retries"


def run_agent(
    role: str,
    task: str,
    ids: dict[str, str],
    all_tools: list[str],
    label: str,
    tools: list[str] | None = None,
    context: str = "",
) -> AgentRun:
    """One headless run; `tools` narrows the role's Meko tools (an empty list: none); `context` is
    appended to the system prompt (the baseline's docs). The usage of a failed attempt counts too."""
    spec = ROLES[role]
    granted = spec["tools"] if tools is None else [t for t in tools if t in spec["tools"]]
    allowed = [f"mcp__meko__{t}" for t in granted]
    blocked = [f"mcp__meko__{t}" for t in all_tools if t not in granted]
    use_meko = spec.get("mcp", True)
    system = (COMMON.format(**ids) + "\n\n" if use_meko else "") + spec["prompt"] + context
    RUNS.mkdir(exist_ok=True)
    usage = dict.fromkeys(USAGE_KEYS, 0)
    cost = seconds = 0.0
    for attempt in (1, 2):
        with tempfile.TemporaryDirectory(prefix="dejafix-agent-") as cwd:
            # A file, not an argument: the baseline's prompt (all the docs) is larger than the argument limit.
            system_file = Path(cwd) / "system.md"
            system_file.write_text(system)
            args = [
                "claude", "-p", "--model", spec["model"], "--output-format", "stream-json", "--verbose",
                "--system-prompt-file", str(system_file), "--tools", "", "--strict-mcp-config",
                "--setting-sources", "project", "--no-session-persistence", "--disable-slash-commands",
                "--json-schema", json.dumps(spec["schema"]),
            ]  # fmt: skip
            if use_meko:
                args += ["--mcp-config", str(_mcp_config(Path(cwd) / "mcp.json"))]
            if allowed:
                args += ["--allowedTools", ",".join(allowed)]
            if blocked:
                args += ["--disallowedTools", ",".join(blocked)]
            env = {**os.environ, "ENABLE_TOOL_SEARCH": "false"}
            if use_meko:
                env["MEKO_TOKEN"] = client_token()
            started = time.monotonic()
            proc = subprocess.run(
                args, input=task, cwd=cwd, env=env, check=False, capture_output=True, text=True, timeout=600
            )
            seconds += time.monotonic() - started
        events = [json.loads(line) for line in proc.stdout.splitlines() if line.startswith("{")]
        result = next((e for e in reversed(events) if e.get("type") == "result"), None)
        for key in USAGE_KEYS:
            usage[key] += (result or {}).get("usage", {}).get(key, 0)
        cost += (result or {}).get("total_cost_usd", 0.0)
        if attempt == 1 and result is not None and result.get("subtype") == SCHEMA_FAILED:
            (RUNS / f"{label}-failed.jsonl").write_text(proc.stdout)
            continue
        break
    (RUNS / f"{label}.jsonl").write_text(proc.stdout)
    if proc.returncode != 0 or result is None or result.get("is_error"):
        raise RuntimeError(
            f"{label}: claude exit {proc.returncode}: {proc.stderr[-500:]} {str(result)[:500]}"
        )
    # Meko calls only: the CLI returns the structured answer through its own StructuredOutput tool.
    uses = [
        block
        for e in events
        if e.get("type") == "assistant"
        for block in e["message"].get("content", [])
        if block.get("type") == "tool_use" and block.get("name", "").startswith("mcp__meko__")
    ]
    # What each Meko call asked for, without the ids every call repeats.
    calls = [
        {"tool": u["name"].removeprefix("mcp__meko__")}
        | {
            k: v
            for k, v in u.get("input", {}).items()
            if k not in ("datapack_id", "conversation_id", "agent_id")
        }
        for u in uses
    ]
    return AgentRun(
        role=role,
        model=spec["model"],
        answer=_answer(result),
        seconds=round(seconds, 1),
        turns=result.get("num_turns", 0),
        tool_calls=len(uses),
        usage=usage,
        cost_usd=cost,
        models=sorted(result.get("modelUsage", {})),
        calls=calls,
        attempts=attempt,
    )

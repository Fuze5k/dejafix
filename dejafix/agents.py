"""The crew's agents: headless Claude Code runs with Meko as their only MCP server.

Each run starts in an empty temporary folder with no settings sources but the (empty) project, no
built-in tools and only its role's Meko tools, so its token count is the agent's own work, not the
machine's global context. The Meko client token reaches the MCP config through the environment.
"""

from __future__ import annotations

import json
import os
import re
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


ROLES: dict[str, dict[str, Any]] = {
    "triage": {
        "model": "haiku",
        "tools": ["context_search"],
        "prompt": """You are the triage agent. Decide what the new issue is, from what Meko returns:
- "known": a learning covers this issue: the same root cause, or the same missing feature, even when
  this report asks for a narrower case of it (one package instead of all) or words it differently.
  The test: a uv maintainer would close this issue as a duplicate of that learning's issue.
- "docs": no learning covers it, but the uv docs answer the question.
- "new": neither; it needs a maintainer.
Call context_search 1 to 3 times: first with the issue's title exactly as written, then, if needed,
with an error message or the problem in other words. Name only an issue number that a learning gives;
a learning that only shares the topic (both about dependencies) is not a match.
JSON: {"verdict": "known"|"docs"|"new", "known_issue": <N or null>, "evidence": [<short quotes>],
"reasoning": "<two sentences>", "plan": "<one sentence for the solver>"}""",
    },
    "solver": {
        "model": "haiku",
        "tools": ["context_search", "knowledgebase_search"],
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
        "prompt": """You are the reviewer. Check the solver's draft against the evidence you are given.
Approve only if every claim is supported and the draft answers the issue. When a maintainer's own
reply is given, it is the final word: judge the draft against it and write the learning from it,
so the crew reuses the maintainer's resolution next time. A learning starts "Known issue uv#N:",
names the problem in general terms first (the root cause or the missing feature), then the ways users
report it (symptoms, error text, narrower requests, what they tried), then the resolution, status or
workaround in the maintainer's terms; under 120 words. If the maintainer says it is tracked in
another issue, start "Known issue uv#<that issue>:" instead. No maintainer reply: learning null.
JSON: {"approved": true|false, "problems": [<short>], "learning": "<text or null>",
"reasoning": "<two sentences>", "plan": "<one sentence>"}""",
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


def _answer(text: str) -> dict[str, Any]:
    """The last JSON object in the agent's final message."""
    fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fenced[-1] if fenced else text[text.find("{") : text.rfind("}") + 1]
    try:
        answer = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"agent reply is not JSON: {text[:400]!r}") from exc
    if not isinstance(answer, dict):
        raise TypeError(f"agent reply is not a JSON object: {text[:400]!r}")
    return answer


def run_agent(
    role: str,
    task: str,
    ids: dict[str, str],
    all_tools: list[str],
    label: str,
    tools: list[str] | None = None,
) -> AgentRun:
    """One headless run; `tools` narrows the role's Meko tools (an empty list: none)."""
    spec = ROLES[role]
    granted = spec["tools"] if tools is None else [t for t in tools if t in spec["tools"]]
    allowed = [f"mcp__meko__{t}" for t in granted]
    blocked = [f"mcp__meko__{t}" for t in all_tools if t not in granted]
    system = COMMON.format(**ids) + "\n\n" + spec["prompt"]
    RUNS.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="dejafix-agent-") as cwd:
        args = [
            "claude", "-p", "--model", spec["model"], "--output-format", "stream-json", "--verbose",
            "--system-prompt", system, "--tools", "", "--strict-mcp-config",
            "--mcp-config", str(_mcp_config(Path(cwd) / "mcp.json")),
            "--setting-sources", "project", "--no-session-persistence", "--disable-slash-commands",
        ]  # fmt: skip
        if allowed:
            args += ["--allowedTools", ",".join(allowed)]
        if blocked:
            args += ["--disallowedTools", ",".join(blocked)]
        env = {**os.environ, "MEKO_TOKEN": client_token(), "ENABLE_TOOL_SEARCH": "false"}
        started = time.monotonic()
        proc = subprocess.run(
            args, input=task, cwd=cwd, env=env, check=False, capture_output=True, text=True, timeout=600
        )
        seconds = time.monotonic() - started
    events = [json.loads(line) for line in proc.stdout.splitlines() if line.startswith("{")]
    (RUNS / f"{label}.jsonl").write_text(proc.stdout)
    result = next((e for e in reversed(events) if e.get("type") == "result"), None)
    if proc.returncode != 0 or result is None or result.get("is_error"):
        raise RuntimeError(
            f"{label}: claude exit {proc.returncode}: {proc.stderr[-500:]} {str(result)[:500]}"
        )
    tool_calls = sum(
        1
        for e in events
        if e.get("type") == "assistant"
        for block in e["message"].get("content", [])
        if block.get("type") == "tool_use"
    )
    usage = result.get("usage", {})
    return AgentRun(
        role=role,
        model=spec["model"],
        answer=_answer(result["result"]),
        seconds=round(seconds, 1),
        turns=result.get("num_turns", 0),
        tool_calls=tool_calls,
        usage={
            k: usage.get(k, 0)
            for k in (
                "input_tokens",
                "cache_creation_input_tokens",
                "cache_read_input_tokens",
                "output_tokens",
            )
        },
        cost_usd=result.get("total_cost_usd", 0.0),
        models=sorted(result.get("modelUsage", {})),
    )

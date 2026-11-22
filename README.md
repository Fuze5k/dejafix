# DejaFix

Support agents that never solve the same issue twice. A triage crew for open-source maintainers that shares one
[Meko](https://mekodata.ai) datapack: every issue a maintainer resolves becomes a fix the next agent finds, so a
repeat question gets the maintainer's own answer in seconds instead of another round of their time.

Built for ContextCon: Remember Everything (YugabyteDB x HackerEarth), theme 2 "Never Solve It Twice". Work in
progress.

## The data

DejaFix is measured on the real issue history of [astral-sh/uv](https://github.com/astral-sh/uv), replayed in date
order. GitHub gives the ground truth: issues the uv maintainers closed as duplicates, and the issue they pointed to.
DejaFix never posts to GitHub and never contacts the uv maintainers; it only reads public issues.

```sh
uv sync
uv run python -m dejafix.fetch_issues --repo astral-sh/uv   # needs the gh CLI, logged in; writes data/cache/
uv run python -m dejafix.build_stream                        # picks the scored stream into data/stream.json
```

`data/stream.json` holds issue numbers and labels only: `first` (the first time a problem shows up), `repeat` (a
later issue a maintainer closed as a duplicate of it) and `new` (an unrelated issue the agents must not call a known
one). The current stream: 41 tickets, 12 first, 21 repeat, 8 new, from 2024-02 to 2026-08.

## Running the crew

DejaFix runs on a local [Meko](https://github.com/yugabyte/meko) server (no account needed) and headless
[Claude Code](https://docs.claude.com/en/docs/claude-code) agents.

```sh
curl -fsSL https://mekodata.ai/install.sh | sh -s -- --no-service --non-interactive
scripts/meko-start.sh                         # starts Meko with usage stats off (Meko's and ONNX Runtime's)
uv run python -m dejafix.load_docs            # uv's docs, pinned to one commit, into the datapack's knowledge base
uv run python -m dejafix.replay --tickets 6794:full,11027:full --max-runs 6
```

Each ticket goes through three agents, each its own `agent_id` on one datapack:

- **triage** searches Meko (`context_search`: knowledge base, learnings, the crew's past conversations) and says
  whether the issue is a known one, answered by the docs, or new;
- **solver** drafts the reply: it reuses a known fix, or researches the docs;
- **reviewer** checks the draft against the evidence. When the issue was not a known one, the uv maintainers' real
  reply on it is the human decision: the reviewer writes the learning from it, and only then the pipeline saves it
  (`memory_add`) and promotes it into the datapack's collective memory (`memory_promote`).

Agents only read Meko; every write (conversations with each turn's reasoning and plan, learnings, the run report
as an artifact) is the pipeline's. Each agent is a `claude -p` run in an empty folder, with no built-in tools and only
its role's Meko tools, so its token count is the agent's own work. Raw run logs go to `runs/` (not committed).

The docs are today's (one pinned commit), not the docs as they were when each issue was opened: both cold and warm
runs get the same docs, so the comparison stays fair, but a docs answer can be newer than the issue.

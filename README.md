# DejaFix

Support agents that never solve the same issue twice. A triage crew for open-source maintainers that shares one
[Meko](https://mekodata.ai) datapack: every issue a maintainer resolves becomes a fix the next agent finds, so a
repeat question gets the maintainer's own answer in seconds instead of another round of their time.

Built for ContextCon: Remember Everything (YugabyteDB x HackerEarth), theme 2 "Never Solve It Twice". Work in
progress.

## Results

The same 36 uv tickets (20 repeats, 12 first reports, 4 unrelated), Claude Haiku 5.5 for every agent, a local Meko:

| | Right (repeats named exactly) | Tokens per ticket | Cost, 36 tickets | Time per ticket | Replies matching the maintainers |
|---|---|---|---|---|---|
| Cold crew (no shared memory) | 16/36 (0/20) | 47.1k | $0.30 | 26 s | 4/36 (repeats 0/20) |
| **Warm crew (Meko memory)** | **35/36 (19/20)** | **39.4k** | **$0.27** | 25 s | **23/36 (repeats 20/20)** |
| Stuffed prompt (all docs and history, one call) | 36/36 (19/20) | 250.4k | $1.05 | 9 s | 22/36 (repeats 17/20) |

- On repeats the warm crew spends 35% fewer tokens than the cold one (47.2k to 30.6k) with half the Meko calls,
  and its reply matched what the maintainers said on all 20.
- Stuffing everything into one prompt is as accurate on a history this small, and faster, but it costs 6.4 times
  the tokens and grows by about 330 tokens with every earlier ticket: on uv's real backlog it passes a 1M-token
  context window at about 2,400 earlier tickets. The crew's prompt stays flat.
- "Right": a repeat named an issue of the right duplicate cluster, a first report or an unrelated issue was not
  called a known one. The replies were graded blind (shuffled per ticket) by the same model against the
  maintainers' replies. Costs are API prices, prompt caching included.

The dashboard shows every ticket with each agent's Meko calls, reasoning, plan and reply, and which learning a
repeat reused: <https://fuze5k.github.io/dejafix/> (the same page is `docs/index.html`).

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

What this repository holds, and what it leaves on GitHub:

- `data/stream.json`: issue numbers, labels and dates.
- `results/`: one JSON line per ticket for each system (`cold.jsonl`, `warm.jsonl`, `baseline.jsonl`), with the
  agents' answers (verdict, evidence, reasoning, plan, reply), their Meko calls and token counts; `judge.jsonl`
  with the blind grades; `shortfall-check-20907.json`, the before/after of the learning loop below. The agents'
  text may quote a line of an issue or of the uv docs (the longest verbatim issue quote is 225 characters).
- Not here: issue bodies, comments and maintainer replies (`fetch_issues` reads them into `data/cache/` with your
  own `gh` login), the uv docs (`load_docs` fetches them at one pinned commit), and the raw agent logs (`runs/`),
  which contain both. Every ticket links to its issue on GitHub. uv's issues belong to their authors; uv's docs
  are under MIT or Apache-2.0 ([astral-sh/uv](https://github.com/astral-sh/uv)).

## Running the crew

DejaFix runs on a local [Meko](https://github.com/yugabyte/meko) server (no account needed) and headless
[Claude Code](https://docs.claude.com/en/docs/claude-code) agents.

```sh
curl -fsSL https://mekodata.ai/install.sh | sh -s -- --no-service --non-interactive
scripts/meko-start.sh                         # starts Meko with usage stats off (Meko's and ONNX Runtime's)
uv run python -m dejafix.load_docs --datapack dejafix-uv-warm   # uv's docs, pinned to one commit
uv run python -m dejafix.replay --datapack dejafix-uv-warm --tickets 6794:full,11027:full --max-runs 6
```

`--memory off` runs the same crew without shared memory (no learning is looked up or saved: the cold run);
`--max-tokens` stops a batch once it has spent that many. `dejafix.baseline` runs the same tickets without
Meko, one call each with all the docs and every earlier ticket's maintainer reply in the prompt.

Grading and the dashboard:

```sh
uv run python -m dejafix.judge --cold results/cold.jsonl --warm results/warm.jsonl \
    --baseline results/baseline.jsonl --max-tokens 400000   # one blind call per ticket grades the three replies
uv run python -m dejafix.dashboard --cold results/cold.jsonl --warm results/warm.jsonl \
    --baseline results/baseline.jsonl --judge results/judge.jsonl  # writes docs/index.html, data inside
```

Each ticket goes through three agents, each its own `agent_id` on one datapack:

- **triage** searches Meko (`context_search`: knowledge base, learnings, the crew's past conversations) and says
  whether the issue is a known one, answered by the docs, or new;
- **solver** drafts the reply: it reuses a known fix, or researches the docs;
- **reviewer** checks the draft against the evidence. When the issue was not a known one, the uv maintainers' real
  reply on it is the human decision: the reviewer writes the learning from it, and only then the pipeline saves it
  (`memory_add`) and promotes it into the datapack's collective memory (`memory_promote`).

**Learning from the maintainer's correction.** When the crew misses a repeat and the maintainer then closes
the issue as a duplicate of one the crew already had a learning for (a shortfall), the new learning is filed under
that issue as another report of it, so the next report worded like this one finds it. The triage's own searches
are run again before and after, and the record keeps where the learning ranked: the before/after of the loop.

Agents only read Meko; every write (conversations with each turn's reasoning and plan, learnings, the run report
as an artifact) is the pipeline's. Each agent is a `claude -p` run in an empty folder, with no built-in tools and only
its role's Meko tools, so its token count is the agent's own work. A run writes its records and the raw agent
logs to `runs/` (not committed); `results/` holds the records of the runs above.

The docs are today's (one pinned commit), not the docs as they were when each issue was opened: both cold and warm
runs get the same docs, so the comparison stays fair, but a docs answer can be newer than the issue.

## Licence

MIT, see `LICENSE`. The uv issues and docs the runs read are not part of this repository.

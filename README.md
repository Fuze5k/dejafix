# DejaFix

Support agents that never solve the same issue twice. A triage crew for open-source maintainers that shares one
[Meko](https://mekodata.ai) datapack: every issue a maintainer resolves becomes a fix the next agent finds, so a
repeat question gets the maintainer's own answer in seconds instead of another round of their time.

Built for ContextCon: Remember Everything (YugabyteDB x HackerEarth), theme 2 "Never Solve It Twice".

## Results

The same 35 uv tickets (20 repeats, 11 first reports, 4 unrelated), Claude Haiku 5.5 for every agent:

| | Right (repeats named exactly) | Tokens per ticket | Cost, 35 tickets | Time per ticket | Replies matching the maintainers |
|---|---|---|---|---|---|
| Cold crew (no shared memory), local Meko | 15/35 (0/20) | 47.2k | $0.30 | 26 s | 3/35 (repeats 0/20) |
| **Warm crew (Meko memory), local Meko** | **34/35 (19/20)** | **39.2k** | **$0.26** | 24 s | **23/35 (repeats 20/20)** |
| Warm crew, final run on Meko Cloud | 33/35 (18/20) | 51.1k | $0.29 | 29 s | 21/35 (repeats 19/20) |
| Stuffed prompt (all docs and history, one call) | 35/35 (19/20) | 251.5k | $1.03 | 8 s | 19/35 (repeats 18/20) |

- Like for like on the local Meko, the warm crew spends 35% fewer tokens than the cold one on repeats (47.2k to
  30.6k) with half the Meko calls, and its reply matched what the maintainers said on all 20.
- The final run on Meko Cloud has every later fix and Cloud's traces (below). Its replies matched the maintainers
  on 21 tickets against the cold crew's 3. Its tokens are not like for like with the cold run: Cloud's searches
  return different chunks (0.6 to 2.3 times the local text in a spot check). It missed two repeats: uv#10281,
  because a passing Cloud error ("connection reset") lost the learning of uv#4422 (the client now retries what
  Cloud marks retryable), and uv#12581, whose triage searches did not reach the learning. The maintainers'
  duplicate links then filed both as new reports of their issues (the correction loop below).
- Stuffing everything into one prompt is as accurate on a history this small, and faster, but it costs 6.4 times
  the tokens of the local warm crew and grows by about 330 tokens with every earlier ticket: on uv's real backlog it
  passes a 1M-token context window at about 2,400 earlier tickets. The crew's prompt stays flat.
- "Right": a repeat named an issue of the right duplicate cluster, a first report or an unrelated issue was not
  called a known one. The replies were graded blind (shuffled per ticket) by the same model against the
  maintainers' replies; the local warm run was graded in an earlier pass, which gave the cold crew 4 and the
  stuffed prompt 22 (3 and 19 in the final pass). Costs are API prices, prompt caching included. uv#4422 is left
  out of every system: the Cloud run has no record of it.

The dashboard shows every ticket with each agent's Meko calls, reasoning, plan and reply, and which learning a
repeat reused: <https://fuze5k.github.io/dejafix/> (the same page is `docs/index.html`). The 22 learnings the final
run saved can be queried in Meko Cloud: <https://cloud.mekodata.ai/share/o3dxj4ex9JQNVn-Kp1Dj7w> (a free Meko Cloud account opens it).

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
- `results/`: one JSON line per ticket for each system (`cold.jsonl`; `warm.jsonl`, the final run on Meko Cloud;
  `warm-local.jsonl`, the warm run on the local Meko; `baseline.jsonl`), with the agents' answers (verdict,
  evidence, reasoning, plan, reply), their Meko calls and token counts; `judge.jsonl` and `judge-local.jsonl` with
  the blind grades; `warm.selflearning.json`, Meko Cloud's read of the final datapack; `amp-queries.json`, the
  Postgres queries below and their answers; `shortfall-check-20907.json`, the before/after of the learning loop. The agents'
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
    --baseline results/baseline.jsonl --max-tokens 285000   # one blind call per ticket grades the three replies
uv run python -m dejafix.dashboard --cold results/cold.jsonl --warm results/warm.jsonl \
    --baseline results/baseline.jsonl --judge results/judge.jsonl  # writes docs/index.html, data inside
# --meko and --note say on the page where each run's Meko ran (the published page: cold local, warm on Meko Cloud)
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

## On Meko Cloud

The final warm run ran on Meko Cloud (Free plan) through the local Meko linked to it (Settings › Meko Cloud in
Meko's web UI): the same crew and tools, plus what only Meko Cloud has.

- **Traces with their cost.** Each agent turn's tokens go onto its conversation's trace (`track_token_usage`),
  next to that turn's reasoning and plan, so Meko's Observe view shows every ticket's decision trace with its
  spend.
- **Self-learning.** When the crew misses a repeat and the maintainer's duplicate link files the learning (above),
  Meko Cloud replays the triage's logged searches against it (`selflearning_verify`); after the run
  `selflearning_status` gives the datapack's search quality and open to-dos.
- **Postgres inside the datapack.** `dejafix.amp` builds a Postgres cluster in the datapack (AMP) and loads the
  tickets, GitHub's duplicate links, every system's outcomes and agent runs, the learnings and the judge's grades,
  then runs the queries behind the numbers above (`results/amp-queries.json`), all four runs side by side.
- **A Cloud gap and its workaround.** On Meko Cloud a promoted memory reaches the knowledge base without a usable
  vector (its distance to any query, even its own text, is about 0.99), so `context_search` never returns it. The
  pipeline also files every approved learning as a plain-text knowledge-base document, which is embedded like the
  docs. Replaying the recorded triage searches, Cloud then found the expected learning for 14 of 15 repeats, the
  same as the local Meko (0 of 21 before). Reported to the Meko team as
  [yugabyte/meko#548](https://github.com/yugabyte/meko/issues/548).

```sh
uv run python -m dejafix.load_docs --datapack dejafix-uv-final   # while linked, a new datapack is made in Meko Cloud
uv run python -m dejafix.replay --datapack dejafix-uv-final --memory on --tickets 6794:full,11027:full --max-runs 6
uv run python -m dejafix.amp --datapack dejafix-uv-final --replay cold=results/cold.jsonl \
    --replay warm=results/warm.jsonl --replay warm_local=results/warm-local.jsonl \
    --replay stuffed=results/baseline.jsonl --judge results/judge.jsonl
```

**A pack to query.** `dejafix.learnings_pack` copies the final run's 22 approved learnings into a datapack of
their own, `dejafix-uv-learnings`: one plain-text document per learning, with a link to its issue, and one saying
what the pack is. The crew's conversations, which quote the issues, stay out; the pack holds no conversations and
no memories. It is shared read-only at <https://cloud.mekodata.ai/share/o3dxj4ex9JQNVn-Kp1Dj7w>: sign in to a free Meko Cloud account to open it, then query it
from Meko's web UI or any MCP client.

```sh
uv run python -m dejafix.learnings_pack --datapack dejafix-uv-learnings --replay results/warm.jsonl
```

The writer endpoint of the Postgres cluster accepts only the address it was built from, and its password lives
in that one process: a later run rebuilds the cluster.

## Licence

MIT, see `LICENSE`. The uv issues and docs the runs read are not part of this repository.

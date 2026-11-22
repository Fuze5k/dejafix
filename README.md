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

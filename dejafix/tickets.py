"""The replayed tickets: issue text from the cache, and the maintainers' real replies."""

from __future__ import annotations

import json
from typing import Any

from dejafix.fetch_issues import CACHE, MAINTAINER

STREAM = CACHE.parent / "stream.json"
BODY_CHARS = 4000


def load_pool(repo: str = "astral-sh/uv") -> dict[int, dict[str, Any]]:
    """Every cached issue by number: duplicates, their canonical issues and the distractors."""
    cache = json.loads((CACHE / f"{repo.replace('/', '__')}.json").read_text())
    pool = {issue["number"]: issue for issue in cache["duplicates"]}
    for source in ("canonicals", "distractors"):
        pool.update({int(number): issue for number, issue in cache[source].items()})
    return pool


def load_links(repo: str = "astral-sh/uv") -> dict[int, int]:
    """Each duplicate-closed issue's canonical issue, as its maintainer named it."""
    cache = json.loads((CACHE / f"{repo.replace('/', '__')}.json").read_text())
    return {int(dup): canon for dup, canon in cache["links"].items() if canon}


def load_stream() -> list[dict[str, Any]]:
    return json.loads(STREAM.read_text())["tickets"]


def ticket_text(issue: dict[str, Any]) -> str:
    """The issue as its author opened it: what an agent sees (no comments, no labels)."""
    body = (issue["body"] or "").strip()
    if len(body) > BODY_CHARS:
        body = body[:BODY_CHARS] + "\n[...]"
    return f"uv issue #{issue['number']} ({issue['createdAt'][:10]}): {issue['title']}\n\n{body}"


def maintainer_replies(issue: dict[str, Any]) -> list[str]:
    """What the uv maintainers said on the issue, in order: the human decision the replay reuses."""
    return [
        (c["body"] or "").strip() for c in issue["comments"]["nodes"] if c["authorAssociation"] in MAINTAINER
    ]

"""Fetch a repository's duplicate-closed issues and their canonical issues into data/cache.

Ground truth comes from the maintainers themselves: the issue number a maintainer named in a
"Duplicate of #N" style comment, or GitHub's marked-as-duplicate event when it points elsewhere.

Usage: uv run python -m dejafix.fetch_issues --repo astral-sh/uv
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from dejafix.github import graphql

MAINTAINER = {"OWNER", "MEMBER", "COLLABORATOR"}
CACHE = Path(__file__).resolve().parent.parent / "data" / "cache"

ISSUE_FIELDS = """
  number title body url createdAt closedAt state stateReason
  author { login }
  authorAssociation
  labels(first: 10) { nodes { name } }
  comments(first: 30) { nodes { authorAssociation author { login } body createdAt } }
  timelineItems(itemTypes: [MARKED_AS_DUPLICATE_EVENT], first: 3) {
    nodes { ... on MarkedAsDuplicateEvent { canonical { ... on Issue { number } } } }
  }
  closedByPullRequestsReferences(first: 3, includeClosedPrs: false) { nodes { number title mergedAt } }
"""

SEARCH = f"""
query($q: String!, $after: String) {{
  search(query: $q, type: ISSUE, first: 25, after: $after) {{
    pageInfo {{ hasNextPage endCursor }}
    nodes {{ ... on Issue {{ {ISSUE_FIELDS} }} }}
  }}
}}"""


def search_all(query: str, limit: int) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    after = ""
    while len(issues) < limit:
        variables: dict[str, Any] = {"q": query}
        if after:
            variables["after"] = after
        page = graphql(SEARCH, **variables)["search"]
        issues += [n for n in page["nodes"] if n]
        if not page["pageInfo"]["hasNextPage"]:
            break
        after = page["pageInfo"]["endCursor"]
    return issues[:limit]


def fetch_by_number(owner: str, name: str, numbers: list[int]) -> dict[int, dict[str, Any]]:
    """Fetch issues by number in batches; pull requests and missing numbers come back absent."""
    found: dict[int, dict[str, Any]] = {}
    for start in range(0, len(numbers), 20):
        batch = numbers[start : start + 20]
        aliases = "\n".join(
            f"i{n}: issueOrPullRequest(number: {n}) {{ __typename ... on Issue {{ {ISSUE_FIELDS} }} }}"
            for n in batch
        )
        query = f'query {{ repository(owner: "{owner}", name: "{name}") {{ {aliases} }} }}'
        repo = graphql(query)["repository"]
        for n in batch:
            node = repo.get(f"i{n}")
            if node and node.get("__typename") == "Issue":
                found[n] = node
    return found


def canonical_of(issue: dict[str, Any], repo: str) -> int | None:
    """The issue a maintainer said this one duplicates, or None when nobody said so clearly."""
    ref = rf"(?:https://github\.com/{re.escape(repo)}/issues/|#)(\d+)"
    explicit = re.compile(rf"duplicate\s+of:?\s+{ref}", re.IGNORECASE)
    pointer = re.compile(
        rf"(?:duplicate|dupe|tracked|tracking|covered|same as|same issue|see|similar to)[^\n]{{0,40}}?{ref}",
        re.IGNORECASE,
    )
    own = issue["number"]
    comments = [c for c in issue["comments"]["nodes"] if c["authorAssociation"] in MAINTAINER]
    for pattern in (explicit, pointer):
        for comment in comments:
            for match in pattern.finditer(comment["body"] or ""):
                number = int(match.group(1))
                if number != own:
                    return number
    for event in issue["timelineItems"]["nodes"]:
        number = (event.get("canonical") or {}).get("number")
        if number and number != own:
            return number
    return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default="astral-sh/uv")
    parser.add_argument("--limit", type=int, default=400)
    args = parser.parse_args()
    owner, name = args.repo.split("/")

    duplicates = search_all(f"repo:{args.repo} is:issue is:closed reason:duplicate", args.limit)
    links = {d["number"]: canonical_of(d, args.repo) for d in duplicates}
    wanted = sorted({c for c in links.values() if c} - {d["number"] for d in duplicates})
    canonicals = fetch_by_number(owner, name, wanted)
    # A canonical may itself be a duplicate-closed issue we already have.
    canonicals.update({d["number"]: d for d in duplicates if d["number"] in set(links.values())})

    CACHE.mkdir(parents=True, exist_ok=True)
    out = CACHE / f"{owner}__{name}.json"
    out.write_text(
        json.dumps(
            {"repo": args.repo, "duplicates": duplicates, "links": links, "canonicals": canonicals},
            indent=1,
        )
    )
    resolved = sum(1 for c in links.values() if c and c in canonicals)
    print(f"{len(duplicates)} duplicates, {resolved} with a canonical issue fetched -> {out}")


if __name__ == "__main__":
    main()

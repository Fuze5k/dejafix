"""Read-only GitHub access through the `gh` CLI, so the token never passes through this code."""

from __future__ import annotations

import json
import subprocess
import time
from typing import Any


class GitHubError(RuntimeError):
    pass


def graphql(query: str, **variables: Any) -> dict[str, Any]:
    """Run one GraphQL query with `gh api graphql` and return its `data`, retrying rate limits."""
    args = ["gh", "api", "graphql", "-f", f"query={query}"]
    for name, value in variables.items():
        flag = "-F" if isinstance(value, int) else "-f"
        args += [flag, f"{name}={value}"]
    for attempt in range(4):
        proc = subprocess.run(args, check=False, capture_output=True, text=True)
        if proc.returncode == 0:
            payload = json.loads(proc.stdout)
            if payload.get("errors"):
                raise GitHubError(f"GraphQL errors: {payload['errors']}")
            return payload["data"]
        if "rate limit" in proc.stderr.lower() and attempt < 3:
            time.sleep(30 * (attempt + 1))
            continue
        raise GitHubError(f"gh api graphql failed: {proc.stderr.strip()[:500]}")
    raise GitHubError("gh api graphql: retries exhausted")


def rest(path: str, *, raw: bool = False) -> Any:
    """GET one REST path with `gh api`: parsed JSON, or the body as text with `raw`."""
    args = ["gh", "api", path]
    if raw:
        args += ["-H", "Accept: application/vnd.github.raw"]
    proc = subprocess.run(args, check=False, capture_output=True, text=True)
    if proc.returncode != 0:
        raise GitHubError(f"gh api {path} failed: {proc.stderr.strip()[:500]}")
    return proc.stdout if raw else json.loads(proc.stdout)

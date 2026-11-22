"""A small client for a Meko server's MCP endpoint (stateless JSON-RPC over streamable HTTP).

The client token comes from `meko token --json` (Meko keeps it in the macOS Keychain) and is only
ever sent in the Authorization header or handed to a child process's environment.
"""

from __future__ import annotations

import functools
import itertools
import json
import subprocess
from typing import Any, Self

import httpx

DEFAULT_URL = "http://127.0.0.1:7777/mcp"


class MekoError(RuntimeError):
    """A tool reply with an error code, a JSON-RPC error, or an HTTP failure."""


@functools.cache
def client_token() -> str:
    done = subprocess.run(["meko", "token", "--json"], check=False, capture_output=True, text=True)
    if done.returncode != 0:
        raise MekoError(f"meko token failed (exit {done.returncode}): is Meko installed?")
    token = json.loads(done.stdout).get("token", "")
    if not token:
        raise MekoError("meko token gave no token: start Meko once (scripts/meko-start.sh)")
    return token


class Meko:
    def __init__(self, url: str = DEFAULT_URL, timeout: float = 120.0) -> None:
        self.url = url
        self._ids = itertools.count(1)
        self._http = httpx.Client(
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {client_token()}",
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
        )

    def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        body = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params}
        response = self._http.post(self.url, json=body)
        if response.status_code != 200:
            raise MekoError(f"{method}: HTTP {response.status_code}: {response.text[:300]}")
        reply = response.json()
        if "error" in reply:
            raise MekoError(f"{method}: {reply['error']}")
        return reply["result"]

    def tools(self) -> list[dict[str, Any]]:
        return self._rpc("tools/list", {})["tools"]

    def call(self, tool: str, **arguments: Any) -> Any:
        """Call a tool and return its parsed JSON reply; an in-band error code raises MekoError."""
        result = self._rpc("tools/call", {"name": tool, "arguments": arguments})
        text = "".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
        try:
            reply = json.loads(text)
        except json.JSONDecodeError as exc:
            raise MekoError(f"{tool}: not JSON: {text[:300]}") from exc
        if result.get("isError") or (isinstance(reply, dict) and "error" in reply):
            raise MekoError(f"{tool}: {text[:500]}")
        return reply

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

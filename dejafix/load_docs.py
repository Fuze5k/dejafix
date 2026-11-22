"""Load a repository's Markdown docs, pinned to one commit, into a Meko datapack's knowledge base.

Usage: uv run python -m dejafix.load_docs --datapack dejafix-uv

Every datapack gets the same docs (one pinned commit, DOCS_REF), so runs on different datapacks compare.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from dejafix.github import rest
from dejafix.meko import Meko, MekoError

CACHE = Path(__file__).resolve().parent.parent / "data" / "cache"
STATE = Path(__file__).resolve().parent.parent / "data" / "datapacks.json"
DOCS_REF = "264d8b4a5e10d195d2cc2a34dffa807cfbe6298c"  # astral-sh/uv main on 2026-11-22


def loaded_datapack(name: str) -> dict[str, str]:
    """The datapack `name` as load_docs recorded it: its id and the docs it holds."""
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    if name not in state:
        raise KeyError(f"datapack {name!r} has no docs loaded: run dejafix.load_docs --datapack {name}")
    return state[name]


def fetch_docs(repo: str, ref: str, folder: str) -> tuple[str, list[Path]]:
    """Download every Markdown file under `folder` at `ref`; returns the commit SHA and the files."""
    sha = rest(f"repos/{repo}/commits/{ref}")["sha"]
    tree = rest(f"repos/{repo}/git/trees/{sha}?recursive=1")
    if tree.get("truncated"):
        raise RuntimeError(f"{repo}@{sha}: the tree listing is truncated")
    wanted = [
        e["path"] for e in tree["tree"] if e["path"].startswith(f"{folder}/") and e["path"].endswith(".md")
    ]
    root = CACHE / f"{repo.replace('/', '__')}-docs" / sha
    files = []
    for path in wanted:
        target = root / path
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(rest(f"repos/{repo}/contents/{path}?ref={sha}", raw=True))
        files.append(target)
    return sha, files


AGENT = "dejafix_loader"


def conversation(meko: Meko, session: str, title: str, datapack_id: str = "") -> str:
    """The loader's conversation for `session` (Meko needs one on most calls); the same session, the same one."""
    return meko.call(
        "conversation_create", agent_id=AGENT, datapack_id=datapack_id, title=title, session_id=session
    )["id"]


def ensure_datapack(meko: Meko, name: str) -> str:
    conversation_id = conversation(meko, "dejafix-setup", "DejaFix setup")
    for pack in meko.call("datapack_list", conversation_id=conversation_id, query=name):
        if pack["datapack_name"] == name:
            return pack["datapack_id"]
    return meko.call("datapack_create", conversation_id=conversation_id, name=name)["datapack_id"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default="astral-sh/uv")
    parser.add_argument("--ref", default=DOCS_REF)
    parser.add_argument("--folder", default="docs")
    parser.add_argument("--datapack", default="dejafix-uv")
    args = parser.parse_args()

    sha, files = fetch_docs(args.repo, args.ref, args.folder)
    print(f"{len(files)} docs from {args.repo}@{sha[:12]}")
    with Meko() as meko:
        datapack_id = ensure_datapack(meko, args.datapack)
        ids = {"datapack_id": datapack_id, "agent_id": AGENT}
        ids["conversation_id"] = conversation(
            meko, f"dejafix-load-docs-{args.datapack}", "Load docs", datapack_id
        )
        pending = {}
        for path in files:
            reply = meko.call("knowledgebase_upload_file", path=str(path.resolve()), **ids)
            pending[str(path)] = reply["document_key"]
        failed: dict[str, str] = {}
        deadline = time.monotonic() + 600
        while pending and time.monotonic() < deadline:
            for name, key in list(pending.items()):
                status = meko.call("knowledgebase_document_status", document_key=key, **ids)
                state = status["document"]["status"]
                if state == "COMPLETED":
                    del pending[name]
                elif state == "FAILED":
                    failed[name] = status.get("message") or "failed"
                    del pending[name]
            if pending:
                time.sleep(3)
        info = meko.call("datapack_describe", datapack_id=datapack_id, conversation_id=ids["conversation_id"])
    if pending or failed:
        raise MekoError(f"not indexed: {len(pending)} pending, failed: {failed}")
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    state[args.datapack] = {"datapack_id": datapack_id, "docs": f"{args.repo}@{sha}"}
    STATE.write_text(json.dumps(state, indent=1))
    print(
        f"datapack {args.datapack}: {info.get('knowledge_chunk_count')} chunks from {len(files)} files -> {STATE}"
    )


if __name__ == "__main__":
    main()

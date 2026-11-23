"""Build the datapack a judge can query: the crew's approved learnings only, on Meko Cloud.

The replay's own datapack holds the crew's conversations, which quote whole issues; those are not ours to share.
This pack holds one plain-text knowledge-base document per approved learning of a recorded replay (the reviewer's
summary of the uv maintainers' public reply, with a link to the issue) and one document saying what the pack is.
No conversation is made in it: the loader's conversation, which Meko needs on every call, lives in the account's
default datapack, and the run checks that the pack holds no conversations and no memories.

Usage: uv run python -m dejafix.learnings_pack --datapack dejafix-uv-learnings --replay results/warm.jsonl
"""

from __future__ import annotations

import argparse
import json
import tempfile
import time
from pathlib import Path
from typing import Any

from dejafix.load_docs import STATE, conversation, document_state, ensure_datapack
from dejafix.meko import Meko, MekoError, on_cloud
from dejafix.replay import KNOWN_PREFIX, LEARNING_PREFIX

AGENT = "dejafix_publisher"
REPO_URL = "https://github.com/Fuze5k/dejafix"
ISSUE_URL = "https://github.com/astral-sh/uv/issues/{}"
INDEX_TIMEOUT = 600
DESCRIPTION = (
    "Known issues of astral-sh/uv that the DejaFix crew learned while replaying uv's issue history: one approved "
    "learning per document, written from the uv maintainers' public replies. " + REPO_URL
)
# A probe per kind of learning: the search must return the learning filed under that issue first.
PROBES = {
    6794: "How do I upgrade all dependencies in pyproject.toml to their latest versions?",
    1495: "Can uv keep virtual environments outside the project folder, in a central location?",
}


def learning_documents(records: list[dict[str, Any]]) -> dict[str, str]:
    """File name -> text for each approved learning, worded as the replay filed it, with its source in the same
    paragraph: Meko Cloud splits a document at blank lines, and a source line alone would be a chunk of its own."""
    documents: dict[str, str] = {}
    for record in records:
        if not record.get("learning"):
            continue
        filed_under, reported_as = record["learning_filed_under"], record["ticket"]
        body = KNOWN_PREFIX.sub("", record["learning"].strip(), count=1)
        if reported_as != filed_under:
            body = f"(also reported as uv#{reported_as}) {body}"
        name = f"uv-{filed_under}-learning-from-uv-{reported_as}.txt"
        if name in documents:
            raise ValueError(f"two learnings would share {name}")
        documents[name] = (
            f"{LEARNING_PREFIX.format(filed_under)} {body} "
            f"Learned from the uv maintainers' reply on {ISSUE_URL.format(reported_as)}"
            + (f" (a report of {ISSUE_URL.format(filed_under)})" if reported_as != filed_under else "")
            + ". Written by the DejaFix reviewer agent and saved only after that reply approved it.\n"
        )
    return documents


def about_document(count: int, source: str) -> str:
    return (
        "About this datapack: DejaFix known issues for astral-sh/uv\n\n"
        f"This datapack holds {count} approved learnings about astral-sh/uv (the Python package manager), one per "
        "document. Each one starts with 'Known issue uv#N:' and says what users ran into, what the uv maintainers "
        "answered, and where it stands, with a link to the GitHub issue.\n\n"
        "They come from DejaFix, a crew of support agents (triage, solver, reviewer) that shares one Meko datapack "
        "and was replayed on uv's real issue history in date order. When an issue was new to the crew, the "
        "reviewer wrote a learning from the maintainers' public reply on GitHub; the next agent that met the same "
        "problem found it here instead of researching it again. Repeats are filed under the issue the maintainers "
        "linked them to.\n\n"
        "Ask it like a uv support desk, for example: 'How do I upgrade all dependencies in pyproject.toml?' The "
        "learnings are as of the replay and can be out of date; the linked issue is the source.\n\n"
        f"Source: {source} in {REPO_URL} (MIT). Issue texts and conversations are not included: uv's issues belong "
        "to their authors and are linked, not copied.\n"
    )


def upload(meko: Meko, ids: dict[str, str], documents: dict[str, str]) -> None:
    pending: dict[str, str] = {}
    with tempfile.TemporaryDirectory(prefix="dejafix-pack-") as tmp:
        for name, text in documents.items():
            path = Path(tmp) / name
            path.write_text(text)
            pending[name] = meko.call("knowledgebase_upload_file", **ids, path=str(path))["document_key"]
        failed: dict[str, str] = {}
        deadline = time.monotonic() + INDEX_TIMEOUT
        while pending and time.monotonic() < deadline:
            for name, key in list(pending.items()):
                status = meko.call("knowledgebase_document_status", **ids, document_key=key)
                state, chunks = document_state(status)
                if state == "COMPLETED" and chunks:
                    del pending[name]
                elif state in ("COMPLETED", "FAILED"):
                    failed[name] = f"{state}, {chunks} chunks"
                    del pending[name]
            if pending:
                time.sleep(10)
    if pending or failed:
        raise MekoError(f"not indexed: {sorted(pending)} pending, failed: {failed}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--datapack", default="dejafix-uv-learnings")
    parser.add_argument("--replay", type=Path, default=Path("results/warm.jsonl"))
    args = parser.parse_args()

    records = [json.loads(line) for line in args.replay.read_text().splitlines()]
    documents = learning_documents(records)
    documents["about-this-datapack.txt"] = about_document(len(documents), args.replay.as_posix())
    with Meko() as meko:
        datapack_id = ensure_datapack(meko, args.datapack)
        conversation_id = conversation(meko, "dejafix-setup", "DejaFix setup")
        ids = {"datapack_id": datapack_id, "agent_id": AGENT, "conversation_id": conversation_id}
        if not on_cloud(meko, datapack_id, conversation_id):
            raise MekoError(f"{args.datapack} is not in Meko Cloud: link the local Meko first")
        meko.call(
            "datapack_update",
            datapack_id=datapack_id,
            conversation_id=conversation_id,
            description=DESCRIPTION,
        )
        upload(meko, ids, documents)
        probes = {}
        for issue, query in PROBES.items():
            hits = meko.call("context_search", **ids, query=query)
            text = json.dumps(hits)
            probes[issue] = text.find(LEARNING_PREFIX.format(issue)) >= 0
        left = meko.call("conversation_list", **ids, limit=5)
        memories = meko.call("memory_get_all", **ids)
        info = meko.call(
            "datapack_describe", datapack_id=datapack_id, conversation_id=conversation_id, include_status=True
        )
    conversations = left.get("conversations", left) if isinstance(left, dict) else left
    memories = memories["memories"]
    print(f"datapack {args.datapack} ({datapack_id}): {len(documents)} documents uploaded")
    print("probes found their learning:", probes)
    print("conversations:", len(conversations), "memories:", len(memories))
    print("describe:", {k: v for k, v in info.items() if "count" in k or k in ("name", "description")})
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    state[args.datapack] = {
        "datapack_id": datapack_id,
        "learnings_from": args.replay.as_posix(),
        "where": "cloud",
    }
    STATE.write_text(json.dumps(state, indent=1) + "\n")
    if conversations or memories or not all(probes.values()):
        raise SystemExit("check failed: see above")


if __name__ == "__main__":
    main()

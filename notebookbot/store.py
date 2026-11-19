"""Saves which Discord channel is linked to which notebook, and the
NotebookLM conversation each channel/thread is in, to a small JSON file."""

import json
import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class Link:
    notebook_id: str
    title: str


class Store:
    def __init__(self, data_dir: Path) -> None:
        self._path = data_dir / "links.json"
        self._links: dict[str, Link] = {}
        # place (channel or thread id) -> {"notebook_id": ..., "conversation_id": ...}
        self._conversations: dict[str, dict[str, str]] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text())
        except (OSError, ValueError):
            log.exception("Couldn't read %s; starting with no links", self._path)
            return
        self._links = {k: Link(**v) for k, v in data.get("links", {}).items()}
        self._conversations = data.get("conversations", {})

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        data = {
            "links": {k: asdict(v) for k, v in self._links.items()},
            "conversations": self._conversations,
        }
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, self._path)

    # Links ------------------------------------------------------------

    def get_link(self, channel_id: int) -> Link | None:
        return self._links.get(str(channel_id))

    def set_link(self, channel_id: int, link: Link) -> None:
        self._links[str(channel_id)] = link
        self._save()

    def remove_link(self, channel_id: int) -> Link | None:
        link = self._links.pop(str(channel_id), None)
        if link:
            self._save()
        return link

    # Conversations ----------------------------------------------------

    def get_conversation(self, place_id: int, notebook_id: str) -> str | None:
        entry = self._conversations.get(str(place_id))
        if entry and entry.get("notebook_id") == notebook_id:
            return entry.get("conversation_id")
        return None

    def set_conversation(self, place_id: int, notebook_id: str, conversation_id: str | None) -> None:
        if conversation_id:
            self._conversations[str(place_id)] = {
                "notebook_id": notebook_id,
                "conversation_id": conversation_id,
            }
        else:
            self._conversations.pop(str(place_id), None)
        self._save()

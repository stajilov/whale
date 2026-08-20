from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from logger import logger

DEFAULT_ROOT = Path.home() / ".whale"
MEMORY_DIR_NAME = "memory"
ENTRIES_FILE = "entries.json"


@dataclass
class MemoryEntry:
    id: str
    kind: str
    content: dict[str, Any]
    created_at: str
    tags: list[str] = field(default_factory=list)
    source: str | None = None


class MemoryEngine:
    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or DEFAULT_ROOT).expanduser()
        self.memory_dir = self.root / MEMORY_DIR_NAME
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.memory_dir / ENTRIES_FILE
        if not self.path.exists():
            self.path.write_text("[]", encoding="utf-8")

    def _now(self) -> str:
        return (
            datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )

    def _read(self) -> list[dict[str, Any]]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []

    def _write(self, entries: list[dict[str, Any]]) -> None:
        self.path.write_text(
            json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def record(
        self,
        kind: str,
        content: dict[str, Any],
        tags: list[str] | None = None,
        source: str | None = None,
    ) -> MemoryEntry:
        entry = MemoryEntry(
            id=uuid.uuid4().hex,
            kind=kind,
            content=content,
            created_at=self._now(),
            tags=list(tags or []),
            source=source,
        )
        entries = self._read()
        entries.append(entry.__dict__)
        self._write(entries)
        logger.info(
            "memory.record",
            kind=kind,
            entry_id=entry.id,
            source=source,
        )
        return entry

    def retrieve(
        self, kind: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        entries = self._read()
        if kind is not None:
            entries = [e for e in entries if e.get("kind") == kind]
        entries.sort(key=lambda e: e.get("created_at") or "", reverse=True)
        return entries[:limit]


memoryEngine = MemoryEngine()


def _hook_record(record: dict[str, Any] | str) -> MemoryEntry | None:
    if isinstance(record, str):
        payload: dict[str, Any] = {"text": record}
    else:
        payload = record
    kind = payload.pop("kind", "note")
    tags = payload.pop("tags", None)
    source = payload.pop("source", None)
    return memoryEngine.record(kind=kind, content=payload, tags=tags, source=source)


def _hook_retrieve(kind: str | None = None, limit: int = 20) -> list[str]:
    return [
        json.dumps(entry, ensure_ascii=False)
        for entry in memoryEngine.retrieve(kind=kind, limit=limit)
    ]

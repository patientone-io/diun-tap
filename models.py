"""Pydantic models, image parsing and opaque Telegram action store.

Telegram `callback_data` is limited to 64 bytes, so the legacy format
`upd:loc:<stack>:<tag>` forced dangerous truncation (`tag[:14]` broke tags
like `v1.2.3-alpine`). This module provides an opaque action store instead:
buttons carry `upd:<action_id>` / `rb:<action_id>` / `ccl:<action_id>` and the
server resolves the full parameters from SQLite. Actions persist across restarts
when a database path is supplied. Legacy callbacks still use strict validation.
"""

from __future__ import annotations

import re
import os
import json
import sqlite3
import weakref
import threading
from pathlib import Path
import secrets
import time
from dataclasses import asdict, dataclass
from typing import Any, Dict, Literal, Optional, Tuple

from pydantic import BaseModel, Field, field_validator

IMAGE_RE = re.compile(r"^[\w][\w./:@+\-]{0,511}$")
REPO_RE = re.compile(r"^[\w.\-]{1,128}/[\w.\-]{1,128}$")


class DiunWebhookPayload(BaseModel):
    diun_version: Optional[str] = Field(default=None, max_length=64)
    image: str = Field(..., min_length=1, max_length=512)
    status: Optional[str] = Field(default="update", max_length=32)
    created: Optional[str] = Field(default=None, max_length=64)
    digest: Optional[str] = Field(default=None, max_length=256)
    hub_link: Optional[str] = Field(default=None, max_length=1024)
    mime_type: Optional[str] = Field(default=None, max_length=128)
    platform: Optional[str] = Field(default=None, max_length=64)
    extra: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("image")
    @classmethod
    def _validate_image(cls, v: str) -> str:
        v = v.strip()
        if not v or len(v) > 512:
            raise ValueError("image must be 1..512 chars")
        if not IMAGE_RE.match(v):
            raise ValueError("image contains illegal characters")
        return v

    @field_validator("hub_link")
    @classmethod
    def _validate_hub_link(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip()
        if v and not v.startswith(("https://", "http://")):
            raise ValueError("hub_link must be http(s) URL")
        return v or None


def parse_image_and_tag(image_str: str) -> Tuple[str, str]:
    """Parses e.g. 'docker.io/deluan/navidrome:v0.55.0' -> ('deluan/navidrome', 'v0.55.0').

    Full tags are preserved (no truncation). Digest suffix @sha256:... is stripped.
    """
    clean_img = image_str.strip()
    for prefix in ("docker.io/", "library/"):
        if clean_img.startswith(prefix):
            clean_img = clean_img[len(prefix) :]

    if "@" in clean_img:
        clean_img = clean_img.split("@")[0]

    last_slash = clean_img.rfind("/")
    last_colon = clean_img.rfind(":")

    if last_colon > last_slash:
        tag = clean_img[last_colon + 1 :] or "latest"
        clean_img = clean_img[:last_colon]
    else:
        tag = "latest"

    return clean_img, tag


def container_health(state: str, health: str) -> str:
    """Classify a sample as healthy, failed, or pending (including unknown)."""
    state, health = state.lower(), health.lower()
    if state in ("exited", "dead", "restarting") or health == "unhealthy":
        return "failed"
    running = state == "running" or state.startswith("up ")
    if running and health in ("", "none", "healthy"):
        return "healthy"
    return "pending"


ActionKind = Literal["upd", "rb", "ccl"]
ActionNode = Literal["loc", "rem", "all"]


@dataclass
class PendingAction:
    kind: ActionKind
    node: ActionNode
    target: str  # full stack name or remote service name (validated)
    tag: str  # full tag (never truncated)
    header: str  # plain-text header snapshot (already escaped at render time)
    created: float
    image: str = ""


class ActionStore:
    """Bounded action registry and restore points, optionally persisted in SQLite."""

    def __init__(
        self,
        ttl_seconds: int = 7 * 24 * 3600,
        max_items: int = 4096,
        db_path: Optional[Path] = None,
    ) -> None:
        self._ttl = ttl_seconds
        self._max = max_items
        if db_path is not None:
            db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(
            str(db_path) if db_path else ":memory:", timeout=5.0, check_same_thread=False
        )
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS actions (id TEXT PRIMARY KEY, created REAL, payload TEXT)"
        )
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS restores (node TEXT, target TEXT, payload TEXT, PRIMARY KEY(node, target))"
        )
        self._db.commit()
        self._finalizer = weakref.finalize(self, self._db.close)

    def register(
        self,
        kind: ActionKind,
        node: ActionNode,
        target: str,
        tag: str,
        header: str,
        image: str = "",
    ) -> str:
        self.evict()
        action_id = secrets.token_urlsafe(16)
        action = PendingAction(kind, node, target, tag, header, time.time(), image)
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO actions VALUES (?, ?, ?)",
                (action_id, action.created, json.dumps(asdict(action))),
            )
            self._db.execute(
                "DELETE FROM actions WHERE id IN (SELECT id FROM actions ORDER BY created DESC LIMIT -1 OFFSET ?)",
                (self._max,),
            )
        return action_id

    def resolve(self, action_id: str) -> Optional[PendingAction]:
        # Clicking a button is a read, not a cleanup transaction.
        with self._lock:
            row = self._db.execute(
                "SELECT payload FROM actions WHERE id = ? AND created >= ?",
                (action_id, time.time() - self._ttl),
            ).fetchone()
        return PendingAction(**json.loads(row[0])) if row else None

    def evict(self) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM actions WHERE created < ?", (time.time() - self._ttl,))

    def save_restore_point(
        self, node: str, target: str, backups: Dict[str, str], services: Optional[list[str]] = None
    ) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO restores VALUES (?, ?, ?)",
                (node, target, json.dumps({"backups": backups, "services": services or []})),
            )

    def restore_point(self, node: str, target: str) -> Optional[dict]:
        with self._lock:
            row = self._db.execute(
                "SELECT payload FROM restores WHERE node = ? AND target = ?", (node, target)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def close(self) -> None:
        with self._lock:
            self._finalizer()


action_store = ActionStore(db_path=Path(os.getenv("DIUNTAP_STATE_DIR", "data")) / "actions.sqlite3")


def parse_legacy_callback(data: str) -> Optional[Tuple[str, str, str, str]]:
    """Parse legacy `upd:loc:<stack>:<tag>` style callbacks.

    Returns (kind, node, target, tag) or None when the format is unknown.
    Values are returned raw; callers MUST sanitise them via security.py.
    """
    parts = data.split(":")
    if not parts or parts[0] not in ("upd", "rb", "ccl"):
        return None
    kind = parts[0]
    if kind == "ccl":
        node = parts[1] if len(parts) > 1 else "loc"
        target = parts[2] if len(parts) > 2 else (parts[1] if len(parts) > 1 else "")
        return (kind, node, target, "latest")
    if len(parts) >= 4 and parts[1] in ("loc", "rem"):
        return (kind, parts[1], parts[2], parts[3] if len(parts) > 3 else "latest")
    if len(parts) >= 3:
        # very old format upd:<stack>:<tag>
        return (kind, "loc", parts[1], parts[2] if len(parts) > 2 else "latest")
    return None

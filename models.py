"""Pydantic models, image parsing and opaque Telegram action store.

Telegram `callback_data` is limited to 64 bytes, so the legacy format
`upd:loc:<stack>:<tag>` forced dangerous truncation (`tag[:14]` broke tags
like `v1.2.3-alpine`). This module provides an opaque action store instead:
buttons carry `upd:<action_id>` / `rb:<action_id>` / `ccl:<action_id>` and the
server resolves the full, validated parameters from memory. Legacy callbacks
are still parsed (best effort) so messages sent before a restart keep working.
"""

from __future__ import annotations

import re
import secrets
import time
from dataclasses import dataclass
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
            clean_img = clean_img[len(prefix):]

    if "@" in clean_img:
        clean_img = clean_img.split("@")[0]

    last_slash = clean_img.rfind("/")
    last_colon = clean_img.rfind(":")

    if last_colon > last_slash:
        tag = clean_img[last_colon + 1:] or "latest"
        clean_img = clean_img[:last_colon]
    else:
        tag = "latest"

    return clean_img, tag


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


class ActionStore:
    """In-memory opaque action registry with TTL eviction."""

    def __init__(self, ttl_seconds: int = 7 * 24 * 3600, max_items: int = 4096) -> None:
        self._ttl = ttl_seconds
        self._max = max_items
        self._items: Dict[str, PendingAction] = {}

    def register(self, kind: ActionKind, node: ActionNode, target: str, tag: str, header: str) -> str:
        self.evict()
        if len(self._items) >= self._max:
            oldest = min(self._items, key=lambda k: self._items[k].created)
            del self._items[oldest]
        action_id = secrets.token_urlsafe(16)[:22]
        self._items[action_id] = PendingAction(
            kind=kind, node=node, target=target, tag=tag, header=header, created=time.time()
        )
        return action_id

    def resolve(self, action_id: str) -> Optional[PendingAction]:
        action = self._items.get(action_id)
        if action is None:
            return None
        if time.time() - action.created > self._ttl:
            del self._items[action_id]
            return None
        return action

    def evict(self) -> None:
        now = time.time()
        expired = [k for k, v in self._items.items() if now - v.created > self._ttl]
        for k in expired:
            del self._items[k]


action_store = ActionStore()


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

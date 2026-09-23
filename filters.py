"""Pin / exclude filters for DIUN notifications.

Two independent env-driven lists (see config.py):

  - EXCLUDE_IMAGES: CSV patterns matched against the image reference.
    Supports `*` wildcards (fnmatch), case-insensitive. A pattern matches when
    it hits the full image ref, the parsed base name, or the short name, e.g.
    `vaultwarden/server`, `*:nightly`, `ghcr.io/internal/*`.
  - PINNED_STACKS: CSV names of local stacks / remote services to freeze.
    Exact match (case-insensitive) against the resolved local stack and/or
    remote service name. Pinned targets never produce Telegram messages.

Both filters are fail-closed on garbage input: empty/blank entries are
dropped, and matching never raises.
"""

from __future__ import annotations

import fnmatch
import logging
from typing import List, Optional

logger = logging.getLogger("diuntap")


def normalize_patterns(raw: List[str]) -> List[str]:
    patterns: List[str] = []
    for entry in raw:
        entry = (entry or "").strip().lower()
        if entry:
            patterns.append(entry)
    return patterns


def _matches(value: str, patterns: List[str]) -> Optional[str]:
    candidate = (value or "").strip().lower()
    if not candidate:
        return None
    for pattern in patterns:
        if fnmatch.fnmatchcase(candidate, pattern):
            return pattern
    return None


def match_excluded_image(image_ref: str, image_base: str, patterns: List[str]) -> Optional[str]:
    """Return the matching pattern when the image is excluded, else None."""
    if not patterns:
        return None
    short = image_base.split("/")[-1]
    for candidate in (image_ref, image_base, short):
        hit = _matches(candidate, patterns)
        if hit:
            return hit
    return None


def match_pinned_target(
    local_stack: Optional[str], remote_service: Optional[str], patterns: List[str]
) -> Optional[str]:
    """Return the pinned target name when a resolved target is frozen, else None."""
    if not patterns:
        return None
    lowered = {p.lower() for p in patterns}
    for target in (local_stack, remote_service):
        if target and target.strip().lower() in lowered:
            return target
    return None

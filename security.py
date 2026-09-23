"""Input validation and authentication helpers.

Centralises every security boundary so handlers never touch raw user input:
  - strict allow-lists for stack / service / tag names (blocks `;`, `$()`,
    backticks, `/`, `..`, whitespace),
  - safe resolution of DOCKER_STACKS_DIR paths (blocks traversal + symlinks),
  - Telegram sender verification (from_user.id ONLY, never chat.id),
  - DIUN webhook secret verification with hmac.compare_digest,
  - GitHub repo name validation (blocks SSRF-ish values).
"""

from __future__ import annotations

import hmac
import re
from pathlib import Path
from typing import List

from errors import AuthDenied

STACK_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.\-]{0,63}$")
SERVICE_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_@.\-:]{0,127}$")
TAG_RE = re.compile(r"^[\w][\w.\-+]{0,127}$")
REPO_RE = re.compile(r"^[\w.\-]{1,128}/[\w.\-]{1,128}$")

MAX_STACK_LEN = 64
MAX_TAG_LEN = 128


def sanitize_stack_name(raw: str) -> str:
    name = (raw or "").strip()
    if not name or len(name) > MAX_STACK_LEN or not STACK_RE.match(name):
        raise AuthDenied(f"Invalid stack name: {raw!r}")
    if name in (".", "..") or "/" in name or "\\" in name:
        raise AuthDenied(f"Invalid stack name: {raw!r}")
    return name


def sanitize_service_name(raw: str) -> str:
    name = (raw or "").strip()
    if not name or not SERVICE_RE.match(name):
        raise AuthDenied(f"Invalid service name: {raw!r}")
    if ".." in name or "/" in name or ";" in name or "&" in name or "|" in name:
        raise AuthDenied(f"Invalid service name: {raw!r}")
    if "$" in name or "`" in name or "\n" in name or "\r" in name:
        raise AuthDenied(f"Invalid service name: {raw!r}")
    return name


def sanitize_tag(raw: str) -> str:
    tag = (raw or "").strip() or "latest"
    if len(tag) > MAX_TAG_LEN or not TAG_RE.match(tag):
        raise AuthDenied(f"Invalid tag: {raw!r}")
    return tag


def validate_github_repo(repo: str) -> str:
    repo = (repo or "").strip()
    if not REPO_RE.match(repo):
        raise AuthDenied(f"Invalid GitHub repo: {repo!r}")
    return repo


def guard_stack_path(stacks_dir: Path, stack_name: str) -> Path:
    """Resolve <stacks_dir>/<stack> and ensure it stays inside stacks_dir."""
    safe = sanitize_stack_name(stack_name)
    base = stacks_dir.resolve()
    candidate = (base / safe).resolve()
    if candidate != base and base not in candidate.parents:
        raise AuthDenied(f"Stack path escapes base dir: {stack_name!r}")
    return candidate


def is_telegram_user_allowed(from_user_id: object, allowed_ids: List[str]) -> bool:
    """Authoritative check: ONLY from_user.id matters (De Morgan fix).

    The old code used `from != allowed AND chat != allowed` which accepted a
    request when EITHER matched. Now a sender must be in the allow-list,
    regardless of chat.
    """
    if not allowed_ids:
        return False
    return str(from_user_id) in {str(a) for a in allowed_ids}


def verify_webhook_secret(provided: str, expected: str) -> None:
    """Raise AuthDenied when the webhook secret does not match."""
    if not expected:
        return  # legacy mode handled by caller (warning already logged)
    if not provided or not hmac.compare_digest(provided, expected):
        raise AuthDenied("Invalid webhook secret")

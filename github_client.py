"""GitHub release-notes client with validation, cache and rate-limit backoff."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, Optional, Tuple

import httpx

from security import validate_github_repo

logger = logging.getLogger("diuntap")

_CACHE: Dict[str, Tuple[float, Optional[dict]]] = {}
_CACHE_TTL = 3600.0
_cache_lock = asyncio.Lock()

_REPO_RE_CACHE: Dict[str, str] = {}


async def _cached_get(key: str) -> Optional[dict]:
    async with _cache_lock:
        item = _CACHE.get(key)
        if item and (time.time() - item[0]) < _CACHE_TTL:
            return item[1]
    return None


async def _cached_set(key: str, value: Optional[dict]) -> None:
    async with _cache_lock:
        if len(_CACHE) > 1024:
            oldest = min(_CACHE, key=lambda k: _CACHE[k][0])
            del _CACHE[oldest]
        _CACHE[key] = (time.time(), value)


async def get_github_release_notes(repo: str, tag: str, github_token: str) -> Optional[dict]:
    """Fetch release notes for tag (with v-prefix variants) or latest fallback."""
    try:
        repo = validate_github_repo(repo)
    except Exception:
        logger.warning("Refusing GitHub lookup for invalid repo %r", repo)
        return None

    tag = (tag or "latest").strip()[:128]
    cache_key = f"{repo}@{tag}"
    cached = await _cached_get(cache_key)
    if cached is not None or cache_key in _CACHE:
        return cached

    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "DiunTap/1.1.0",
    }
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"

    candidates = [tag]
    if tag.startswith("v"):
        candidates.append(tag[1:])
    else:
        candidates.append(f"v{tag}")

    async with httpx.AsyncClient(timeout=10.0, headers=headers) as client:
        for attempt, candidate in enumerate(candidates):
            try:
                url = f"https://api.github.com/repos/{repo}/releases/tags/{candidate}"
                resp = await client.get(url)
                if resp.status_code == 200:
                    data = resp.json()
                    await _cached_set(cache_key, data)
                    return data
                if resp.status_code in (403, 429):
                    retry_after = resp.headers.get("Retry-After")
                    wait = float(retry_after) if retry_after and retry_after.isdigit() else 2.0 * (attempt + 1)
                    logger.warning("GitHub rate limited for %s, backing off %.0fs", repo, wait)
                    await asyncio.sleep(min(wait, 30.0))
                    continue
                if resp.status_code == 404:
                    continue
                logger.debug("GitHub lookup %s -> %s", url, resp.status_code)
            except Exception as exc:
                logger.debug("GitHub request failed for %s: %s", repo, exc)
                await asyncio.sleep(1.0)

        try:
            url = f"https://api.github.com/repos/{repo}/releases/latest"
            resp = await client.get(url)
            if resp.status_code == 200:
                data = resp.json()
                await _cached_set(cache_key, data)
                return data
            if resp.status_code in (403, 429):
                logger.warning("GitHub rate limited on latest for %s", repo)
        except Exception as exc:
            logger.debug("GitHub latest lookup failed for %s: %s", repo, exc)

    await _cached_set(cache_key, None)
    return None

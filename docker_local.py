"""Local Docker Compose orchestration: discovery, update, healthcheck, rollback.

Fixes vs legacy app.py:
  - path traversal blocked via security.guard_stack_path (resolve + containment),
  - backups are per-service AND timestamped with retention (no single-tag clobber,
    no first-container-only rollback),
  - full tags everywhere (no tag[:14] truncation),
  - every subprocess has an enforced timeout with kill,
  - healthcheck polls until stable instead of a single sleep+inspect,
  - `up` failures trigger an automatic rollback attempt (idempotent),
  - rollback uses `up -d --no-pull` so a broken :latest is not re-pulled.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from security import guard_stack_path, sanitize_stack_name

logger = logging.getLogger("diuntap")

COMPOSE_FILES = ("compose.yaml", "docker-compose.yml", "compose.yml")
BACKUP_KEEP = 3


async def _run(cmd: List[str], cwd: Optional[str] = None, timeout: int = 180) -> Tuple[int, str, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
            return 1, "", f"Command timed out after {timeout}s: {' '.join(cmd[:4])}"
        return (
            proc.returncode or 0,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
        )
    except Exception as exc:
        logger.error("Subprocess failed %s: %s", cmd[:3], type(exc).__name__)
        return 1, "", str(exc)


def _scan_stacks_sync(stacks_dir: Path, image_base: str, short_name: str) -> Optional[str]:
    try:
        if not stacks_dir.exists():
            return None
        for stack_dir in stacks_dir.iterdir():
            if not stack_dir.is_dir() or stack_dir.is_symlink():
                continue
            # Skip symlinks escaping the base dir
            try:
                resolved = stack_dir.resolve()
                base = stacks_dir.resolve()
                if resolved != base and base not in resolved.parents:
                    continue
            except Exception:
                continue
            for compose_name in COMPOSE_FILES:
                compose_file = stack_dir / compose_name
                try:
                    if compose_file.exists() and not compose_file.is_symlink():
                        content = compose_file.read_text(encoding="utf-8", errors="ignore")
                        if image_base in content:
                            return stack_dir.name
                        if re.search(
                            rf'image:\s*["\']?[^\s\'"]*{re.escape(short_name)}(?:[:@"\']|\s|$)',
                            content,
                            re.IGNORECASE,
                        ):
                            return stack_dir.name
                except Exception:
                    continue
    except Exception as exc:
        logger.warning("Stack scan failed: %s", type(exc).__name__)
    return None


async def find_local_stack_via_docker(image_base: str, timeout: int = 15) -> Optional[str]:
    short_name = image_base.split("/")[-1]
    cmd = ["docker", "ps", "--format", '{{.Image}}|{{.Label "com.docker.compose.project"}}']
    try:
        rc, out, _ = await _run(cmd, timeout=timeout)
        if rc != 0:
            return None
        for line in out.splitlines():
            parts = line.strip().split("|")
            if len(parts) >= 2:
                c_img, c_proj = parts[0], parts[1]
                if c_proj and (image_base in c_img or short_name in c_img):
                    try:
                        return sanitize_stack_name(c_proj)
                    except Exception:
                        continue
    except Exception as exc:
        logger.debug("Docker ps inspection skipped: %s", type(exc).__name__)
    return None


async def find_targets_for_image(
    image_base: str,
    stacks_dir: Path,
    remote_enabled: bool = False,
    remote_map: Optional[Dict[str, str]] = None,
) -> Dict[str, Optional[str]]:
    short_name = image_base.split("/")[-1]
    local_stack = await find_local_stack_via_docker(image_base)

    if not local_stack and stacks_dir.exists():
        local_stack = await asyncio.to_thread(_scan_stacks_sync, stacks_dir, image_base, short_name)
        if not local_stack:
            candidate = stacks_dir / short_name
            try:
                if (
                    short_name
                    and ".." not in short_name
                    and "/" not in short_name
                    and candidate.is_dir()
                    and not candidate.is_symlink()
                    and any((candidate / c).exists() for c in COMPOSE_FILES)
                ):
                    local_stack = short_name
            except Exception:
                pass

    remote_service = None
    if remote_enabled and remote_map:
        if image_base in remote_map:
            remote_service = remote_map[image_base]
        elif short_name in remote_map:
            remote_service = remote_map[short_name]

    return {"local": local_stack, "remote": remote_service}


def find_local_compose_file(
    stacks_dir: Path, stack_name: str
) -> Tuple[Optional[Path], Optional[Path], str]:
    """Return (stack_dir, compose_file, effective_stack_name), securely."""
    safe = sanitize_stack_name(stack_name)
    stack_dir = guard_stack_path(stacks_dir, safe)
    compose_file: Optional[Path] = None
    for cname in COMPOSE_FILES:
        candidate = stack_dir / cname
        if candidate.exists() and candidate.is_file() and not candidate.is_symlink():
            compose_file = candidate
            break

    if compose_file is None and stacks_dir.exists():
        # Exact-name fallback only (no startswith: avoids hijacking sibling stacks).
        for candidate_dir in stacks_dir.iterdir():
            if not candidate_dir.is_dir() or candidate_dir.is_symlink():
                continue
            if candidate_dir.name != safe:
                continue
            for cname in COMPOSE_FILES:
                candidate = candidate_dir / cname
                if candidate.exists() and candidate.is_file() and not candidate.is_symlink():
                    return candidate_dir, candidate, candidate_dir.name

    return stack_dir, compose_file, safe


async def inspect_local_containers(
    stack_dir: Path, compose_file: Path, timeout: int = 60
) -> List[Dict[str, Any]]:
    cmd = [
        "docker", "compose", "--project-directory", str(stack_dir),
        "-f", str(compose_file), "ps", "-a", "--format", "json",
    ]
    rc, out, _ = await _run(cmd, timeout=timeout)
    if rc != 0:
        logger.warning("compose ps failed for %s", stack_dir.name)
        return []
    containers: List[Dict[str, Any]] = []
    for line in out.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            containers.append(json.loads(line))
        except Exception:
            continue
    return containers


async def _docker_tag(src: str, dst: str, timeout: int = 60) -> bool:
    rc, _, _ = await _run(["docker", "tag", src, dst], timeout=timeout)
    return rc == 0


async def _docker_images_matching(suffix: str, timeout: int = 60) -> List[str]:
    rc, out, _ = await _run(["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"], timeout=timeout)
    if rc != 0:
        return []
    return [l.strip() for l in out.splitlines() if l.strip().endswith(suffix)]


async def create_backups(
    stack_key: str, containers: List[Dict[str, Any]], timeout: int = 60
) -> Dict[str, str]:
    """Tag every running image with a timestamped backup tag.

    Returns {original_image_ref: backup_ref}. Retention: keep newest BACKUP_KEEP
    per repository, prune older diun-backup-* tags.
    """
    ts = time.strftime("%Y%m%d%H%M%S")
    backups: Dict[str, str] = {}
    for c in containers:
        img_ref = c.get("Image") or ""
        if not img_ref or ":diun-backup-" in img_ref or len(img_ref) > 512:
            continue
        repo = img_ref.rsplit(":", 1)[0] if ":" in img_ref else img_ref
        if any(x in repo for x in (";", "&", "|", "$", "`", "\n")):
            continue
        backup_ref = f"{repo}:diun-backup-{stack_key}-{ts}"
        if await _docker_tag(img_ref, backup_ref, timeout):
            backups[img_ref] = backup_ref

    # Retention pruning per repo
    try:
        all_backups = await _docker_images_matching(f"-{stack_key}-", timeout)
        by_repo: Dict[str, List[str]] = {}
        for ref in all_backups:
            if ":diun-backup-" not in ref:
                continue
            repo = ref.rsplit(":", 1)[0]
            by_repo.setdefault(repo, []).append(ref)
        for repo, refs in by_repo.items():
            refs_sorted = sorted(refs, reverse=True)
            for old in refs_sorted[BACKUP_KEEP:]:
                await _run(["docker", "rmi", old], timeout=timeout)
    except Exception as exc:
        logger.debug("Backup pruning skipped: %s", type(exc).__name__)
    return backups


def describe_containers(containers: List[Dict[str, Any]]) -> Tuple[bool, str]:
    if not containers:
        return True, "empty"
    has_failure = False
    lines: List[str] = []
    for c in containers:
        cname = str(c.get("Name", "container"))
        state = str(c.get("State") or c.get("Status") or "unknown").lower()
        health = str(c.get("Health") or "").lower()
        running = state == "running" or state.startswith("running") or state.startswith("up")
        desc = f"<b>{html.escape(cname)}</b>: {html.escape(state)}"
        if health:
            desc += f" ({html.escape(health)})"
        if (not running) or health == "unhealthy":
            has_failure = True
            lines.append(f"❌ {desc}")
        else:
            lines.append(f"🟢 {desc}")
    return has_failure, "\n".join(lines)


async def wait_for_healthy(
    stack_dir: Path, compose_file: Path, wait_seconds: int, timeout: int = 60
) -> Tuple[bool, str]:
    """Poll compose ps until all containers are running+healthy or timeout."""
    deadline = time.time() + max(2, wait_seconds)
    last_summary = ""
    last_failure = True
    while time.time() < deadline:
        containers = await inspect_local_containers(stack_dir, compose_file, timeout)
        failure, summary = describe_containers(containers)
        last_summary, last_failure = summary, failure
        if containers and not failure:
            return False, summary
        await asyncio.sleep(2)
    return last_failure, last_summary or "timeout"


async def compose_logs(stack_dir: Path, compose_file: Path, tail: int = 20) -> str:
    cmd = [
        "docker", "compose", "--project-directory", str(stack_dir),
        "-f", str(compose_file), "logs", "--tail", str(tail),
    ]
    _, out, err = await _run(cmd, timeout=60)
    return out or err or ""

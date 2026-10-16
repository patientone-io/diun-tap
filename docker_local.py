"""Local Docker Compose orchestration: discovery, update, healthcheck, rollback.

Fixes vs legacy app.py:
  - path traversal blocked via security.guard_stack_path (resolve + containment),
  - backups are per-service AND timestamped with retention (no single-tag clobber,
    no first-container-only rollback),
  - full tags everywhere (no tag[:14] truncation),
  - every subprocess has an enforced timeout with kill,
  - healthcheck polls until stable instead of a single sleep+inspect,
  - `up` failures trigger an automatic rollback attempt (idempotent),
  - rollback uses `up -d --pull never` so a broken :latest is not re-pulled.
"""

from __future__ import annotations

import asyncio
import html
import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from models import container_health, parse_image_and_tag
from errors import ImageTagMismatch
from security import guard_stack_path, sanitize_stack_name

logger = logging.getLogger("diuntap")

COMPOSE_FILES = ("compose.yaml", "docker-compose.yml", "compose.yml", "docker-compose.yaml")
BACKUP_KEEP = 3


async def _run(
    cmd: List[str], cwd: Optional[str] = None, timeout: int = 180
) -> Tuple[int, str, str]:
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


def same_repository(left: str, right: str) -> bool:
    return parse_image_and_tag(left)[0].lower() == parse_image_and_tag(right)[0].lower()


def _scan_stacks_sync(stacks_dir: Path, image_base: str, short_name: str) -> Optional[str]:
    matches = set()
    if not stacks_dir.exists():
        return None
    for stack_dir in stacks_dir.iterdir():
        if not stack_dir.is_dir() or stack_dir.is_symlink():
            continue
        for compose_name in COMPOSE_FILES:
            compose_file = stack_dir / compose_name
            try:
                if compose_file.is_symlink() or not compose_file.is_file():
                    continue
                data = yaml.safe_load(compose_file.read_text()) or {}
                for service in data.get("services", {}).values():
                    image = service.get("image", "")
                    # Variable interpolation is resolved by Compose at execution time.
                    if (
                        isinstance(image, str)
                        and "$" not in image
                        and same_repository(image, image_base)
                    ):
                        matches.add(sanitize_stack_name(stack_dir.name))
            except Exception:
                continue
    return next(iter(matches)) if len(matches) == 1 else None


async def find_local_stack_via_docker(image_base: str, timeout: int = 15) -> Optional[str]:
    cmd = ["docker", "ps", "--format", '{{.Image}}|{{.Label "com.docker.compose.project"}}']
    try:
        rc, out, _ = await _run(cmd, timeout=timeout)
        if rc != 0:
            return None
        matches = set()
        for line in out.splitlines():
            parts = line.strip().split("|")
            if len(parts) >= 2:
                c_img, c_proj = parts[0], parts[1]
                if c_proj and same_repository(c_img, image_base):
                    try:
                        matches.add(sanitize_stack_name(c_proj))
                    except Exception:
                        continue
        return next(iter(matches)) if len(matches) == 1 else None
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

    remote_service = None
    if remote_enabled and remote_map:
        if image_base in remote_map:
            remote_service = remote_map[image_base]

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
        "docker",
        "compose",
        "--project-directory",
        str(stack_dir),
        "-f",
        str(compose_file),
        "ps",
        "-a",
        "--format",
        "json",
    ]
    rc, out, _ = await _run(cmd, timeout=timeout)
    if rc != 0:
        logger.warning("compose ps failed for %s", stack_dir.name)
        return []
    containers: List[Dict[str, Any]] = []
    try:
        parsed = json.loads(out)
        containers = parsed if isinstance(parsed, list) else [parsed]
    except ValueError:
        for line in out.strip().splitlines():
            try:
                containers.append(json.loads(line))
            except ValueError:
                continue
    return [c for c in containers if isinstance(c, dict)]


async def update_services(
    stack_dir: Path, compose_file: Path, tag: str, image: str = ""
) -> Dict[str, str]:
    """Resolve Compose interpolation and accept only the configured image/tag.

    Updating a floating tag is supported; changing a pinned version requires
    editing Compose first. No unrelated service is pulled or recreated.
    """
    rc, out, err = await _run(
        [
            "docker",
            "compose",
            "--project-directory",
            str(stack_dir),
            "-f",
            str(compose_file),
            "config",
            "--format",
            "json",
        ]
    )
    if rc:
        raise RuntimeError("Cannot resolve Compose configuration")
    config = json.loads(out)
    selected = {}
    configured = []
    for name, service in config.get("services", {}).items():
        ref = service.get("image", "")
        if ref and (not image or same_repository(ref, image)):
            configured.append(ref)
        if ref and "@" not in ref and parse_image_and_tag(ref)[1] == tag:
            if not image or same_repository(ref, image):
                selected[name] = ref
    if not selected and configured:
        raise ImageTagMismatch(configured)
    if not selected:
        raise ValueError("notification does not match configured image/tag")
    return selected


async def _docker_tag(src: str, dst: str, timeout: int = 60) -> bool:
    rc, _, _ = await _run(["docker", "tag", src, dst], timeout=timeout)
    return rc == 0


async def _docker_images_matching(prefix: str, timeout: int = 60) -> List[str]:
    rc, out, _ = await _run(
        ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"], timeout=timeout
    )
    if rc != 0:
        return []
    return [
        l.strip()
        for l in out.splitlines()
        if re.fullmatch(
            re.escape(prefix) + r"[0-9]{14}(?:-[a-f0-9]{10})?", l.strip().rsplit(":", 1)[-1]
        )
    ]


async def create_backups(
    stack_key: str, containers: List[Dict[str, Any]], timeout: int = 60
) -> Dict[str, str]:
    """Tag every running image with a timestamped backup tag.

    Returns {original_image_ref: backup_ref}. Retention: keep newest BACKUP_KEEP
    per original reference, prune older diun-backup-* tags.
    """
    ts = time.strftime("%Y%m%d%H%M%S")
    backups: Dict[str, str] = {}
    running_ids: Dict[str, str] = {}
    for c in containers:
        img_ref = c.get("Image") or ""
        if not img_ref or ":diun-backup-" in img_ref or len(img_ref) > 512:
            raise ValueError("Invalid image reference for restore point")
        repo = parse_image_and_tag(img_ref)[0]
        if any(x in repo for x in (";", "&", "|", "$", "`", "\n")):
            raise ValueError("Invalid image repository")
        suffix = hashlib.sha256(img_ref.encode()).hexdigest()[:10]
        backup_ref = f"{repo}:diun-backup-{stack_key}-{ts}-{suffix}"
        container_id = c.get("ID")
        if not container_id:
            raise RuntimeError("Container ID missing; cannot back up the running image")
        rc, image_id, _ = await _run(
            ["docker", "inspect", str(container_id), "--format", "{{.Image}}"], timeout=timeout
        )
        if rc or not image_id.strip():
            raise RuntimeError("Cannot inspect running image")
        if img_ref in running_ids:
            if running_ids[img_ref] != image_id.strip():
                raise RuntimeError("Containers sharing an image ref use different image IDs")
            continue
        running_ids[img_ref] = image_id.strip()
        if not await _docker_tag(image_id.strip(), backup_ref, timeout):
            raise RuntimeError("Cannot create restore point")
        backups[img_ref] = backup_ref

    # Retention pruning per original reference
    try:
        all_backups = await _docker_images_matching(f"diun-backup-{stack_key}-", timeout)
        by_repo: Dict[str, List[str]] = {}
        for ref in all_backups:
            if ":diun-backup-" not in ref:
                continue
            repo = ref.rsplit(":", 1)[0]
            suffix = ref.rsplit("-", 1)[-1]
            group = suffix if len(suffix) == 10 else "legacy"
            by_repo.setdefault(repo + ":" + group, []).append(ref)
        for repo, refs in by_repo.items():
            refs_sorted = sorted(refs, reverse=True)
            for old in refs_sorted[BACKUP_KEEP:]:
                await _run(["docker", "rmi", old], timeout=timeout)
    except Exception as exc:
        logger.debug("Backup pruning skipped: %s", type(exc).__name__)
    return backups


def describe_containers(containers: List[Dict[str, Any]]) -> Tuple[bool, str]:
    if not containers:
        return False, "Awaiting container state"
    has_failure = False
    lines: List[str] = []
    for c in containers:
        cname = str(c.get("Name", "container"))
        state = str(c.get("State") or c.get("Status") or "unknown").lower()
        health = str(c.get("Health") or "").lower()
        result = container_health(state, health)
        desc = f"<b>{html.escape(cname)}</b>: {html.escape(state)}"
        if health:
            desc += f" ({html.escape(health)})"
        has_failure |= result == "failed"
        icon = {"failed": "❌", "healthy": "🟢", "pending": "⏳"}[result]
        lines.append(f"{icon} {desc}")
    return has_failure, "\n".join(lines)


async def wait_for_healthy(
    stack_dir: Path,
    compose_file: Path,
    wait_seconds: int,
    timeout: int = 60,
    services: Optional[List[str]] = None,
) -> Tuple[Optional[bool], str]:
    """Return (False=healthy, True=failed, None=pending/timeout, summary).

    A healthcheck gets at least 60 seconds even for legacy wait_seconds=6.
    Expiring that budget is inconclusive, never a reason to roll back.
    """
    started = time.monotonic()
    deadline = started + max(2, wait_seconds)
    last_summary = "Awaiting container state"
    stable_samples = 0
    while time.monotonic() < deadline:
        containers = await inspect_local_containers(stack_dir, compose_file, timeout)
        if services is not None:
            containers = [c for c in containers if c.get("Service") in services]
        failure, summary = describe_containers(containers)
        last_summary = summary
        if failure:
            return True, summary
        if any(c.get("Health") not in (None, "", "none") for c in containers):
            deadline = max(deadline, started + 60)
        complete = bool(containers) and (
            not services or {c.get("Service") for c in containers} == set(services)
        )
        healthy = complete and all(
            container_health(
                str(c.get("State") or c.get("Status") or "unknown"), str(c.get("Health") or "")
            )
            == "healthy"
            for c in containers
        )
        stable_samples = stable_samples + 1 if healthy else 0
        if stable_samples >= 2:
            return False, summary
        await asyncio.sleep(1)
    return None, last_summary


async def compose_logs(stack_dir: Path, compose_file: Path, tail: int = 20) -> str:
    cmd = [
        "docker",
        "compose",
        "--project-directory",
        str(stack_dir),
        "-f",
        str(compose_file),
        "logs",
        "--tail",
        str(tail),
    ]
    _, out, err = await _run(cmd, timeout=60)
    return out or err or ""

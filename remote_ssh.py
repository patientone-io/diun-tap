"""Remote node execution over SSH with strict input validation.

Security model (fix for Critical-2):
  - service / image / tag values MUST pass security.py allow-lists first,
  - every dynamic token is additionally wrapped with shlex.quote(),
  - NO caller may pass a raw shell string: helpers build the remote command
    from validated atoms, joined with a single operator at most,
  - every SSH call has an enforced asyncio timeout (default 30s) with process
    kill on expiry.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from typing import Tuple

from security import sanitize_service_name, sanitize_tag

logger = logging.getLogger("diuntap")


async def run_remote_ssh(ssh_host: str, remote_command: str, timeout: int = 30) -> Tuple[int, str, str]:
    """Low-level SSH runner. remote_command must already be built from quoted atoms."""
    if not ssh_host:
        return 1, "", "No REMOTE_SSH_HOST configured."
    if "\n" in remote_command or "\r" in remote_command:
        return 1, "", "Refusing multi-line remote command."

    cmd = [
        "ssh",
        "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=yes",
        "-o", "BatchMode=yes",
        "-o", "ServerAliveInterval=15",
        "-o", f"ConnectTimeout=10",
        ssh_host,
        remote_command,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
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
            return 1, "", f"SSH command timed out after {timeout}s"
        return (
            proc.returncode or 0,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
        )
    except Exception as exc:
        logger.error("SSH exception: %s", type(exc).__name__)
        return 1, "", str(exc)


def _q(value: str) -> str:
    return shlex.quote(value)


async def remote_inspect_image(ssh_host: str, service: str, timeout: int = 30) -> str:
    svc = sanitize_service_name(service)
    cmd = (
        f"podman inspect {_q(svc)} --format '{{{{.ImageName}}}}' 2>/dev/null "
        f"|| podman inspect {_q(svc)} --format '{{{{.Image}}}}'"
    )
    rc, out, _ = await run_remote_ssh(ssh_host, cmd, timeout)
    return out.strip() if rc == 0 and out.strip() else ""


async def remote_tag_image(ssh_host: str, src: str, dst: str, timeout: int = 30) -> bool:
    if not src or not dst or len(src) > 512 or len(dst) > 512:
        return False
    if any(c in src + dst for c in (";", "&", "|", "$", "`", "\n", "\r")):
        return False
    rc, _, _ = await run_remote_ssh(ssh_host, f"podman tag {_q(src)} {_q(dst)}", timeout)
    return rc == 0


async def remote_pull_image(ssh_host: str, image: str, timeout: int = 120) -> Tuple[int, str, str]:
    if not image or len(image) > 512:
        return 1, "", "Invalid image ref"
    if any(c in image for c in (";", "&", "|", "$", "`", "\n", "\r")):
        return 1, "", "Invalid image ref"
    return await run_remote_ssh(ssh_host, f"podman pull {_q(image)}", timeout)


async def remote_restart_service(ssh_host: str, service: str, timeout: int = 60) -> Tuple[int, str, str]:
    svc = sanitize_service_name(service)
    return await run_remote_ssh(ssh_host, f"systemctl --user restart {_q(svc + '.service')}", timeout)


async def remote_service_active(ssh_host: str, service: str, timeout: int = 30) -> str:
    svc = sanitize_service_name(service)
    rc, out, _ = await run_remote_ssh(
        ssh_host, f"systemctl --user is-active {_q(svc + '.service')}", timeout
    )
    return out.strip()


async def remote_container_state(ssh_host: str, service: str, timeout: int = 30) -> Tuple[str, str]:
    svc = sanitize_service_name(service)
    cmd = (
        f"podman inspect {_q(svc)} --format "
        "'{{{{.State.Status}}}}|{{{{if .State.Health}}}}"
        "{{{{.State.Health.Status}}}}{{{{else}}}}none{{{{end}}}}' "
        "2>/dev/null || echo 'not_found'"
    )
    _, out, _ = await run_remote_ssh(ssh_host, cmd, timeout)
    parts = out.strip().split("|")
    state = parts[0] if parts and parts[0] else "unknown"
    health = parts[1] if len(parts) > 1 else "none"
    return state, health


async def remote_logs(ssh_host: str, service: str, timeout: int = 30) -> str:
    svc = sanitize_service_name(service)
    _, out, _ = await run_remote_ssh(
        ssh_host,
        f"podman logs --tail 20 {_q(svc)} 2>&1 || journalctl --user -u {_q(svc + '.service')} -n 20 --no-pager",
        timeout,
    )
    return out


def backup_name_for_repo(repo: str, service: str, timestamp: str) -> str:
    svc = sanitize_service_name(service)
    return f"{repo}:diun-backup-{svc}-{timestamp}"


def validated_tag(raw: str) -> str:
    return sanitize_tag(raw)

import os
import re
import json
import html
import asyncio
import logging
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple
from contextlib import asynccontextmanager
from dotenv import load_dotenv
import time
import httpx
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from pydantic import BaseModel, Field

from mappings import load_custom_mappings, resolve_github_repo
from formatters import format_markdown_for_telegram
from i18n import t

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("diuntap")

# Notification deduplication cache
_recent_notifications: Dict[str, float] = {}
DEDUP_WINDOW_SECONDS = 30

# Core Credentials
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")

# Node Configuration
LOCAL_NODE_NAME = os.getenv("LOCAL_NODE_NAME", "Local")
DOCKER_STACKS_DIR = Path(os.getenv("DOCKER_STACKS_DIR", "/srv/docker"))


# Remote Node Configuration (Optional)
REMOTE_SSH_HOST = os.getenv("REMOTE_SSH_HOST", "")
REMOTE_NODE_NAME = os.getenv("REMOTE_NODE_NAME", "Remote")
REMOTE_CONFIGS_DIR = Path(os.getenv("REMOTE_CONFIGS_DIR", "/remote-configs"))

HEALTHCHECK_WAIT_SECONDS = int(os.getenv("HEALTHCHECK_WAIT_SECONDS", "6"))

# Load mappings
IMAGE_MAPPINGS = load_custom_mappings()

# Polling loop state
is_polling_active = True


class DiunWebhookPayload(BaseModel):
    diun_version: Optional[str] = None
    image: str
    status: Optional[str] = "update"
    created: Optional[str] = None
    digest: Optional[str] = None
    hub_link: Optional[str] = None
    mime_type: Optional[str] = None
    platform: Optional[str] = None
    extra: Dict[str, Any] = Field(default_factory=dict)


def parse_image_and_tag(image_str: str) -> tuple[str, str]:
    """Parses e.g. 'docker.io/deluan/navidrome:v0.55.0' -> ('deluan/navidrome', 'v0.55.0')"""
    clean_img = image_str.strip()
    for prefix in ("docker.io/", "library/"):
        if clean_img.startswith(prefix):
            clean_img = clean_img[len(prefix):]

    # Strip digest @sha256:...
    if "@" in clean_img:
        clean_img = clean_img.split("@")[0]

    # Tag is after the last colon, provided the colon occurs after the last slash
    last_slash = clean_img.rfind("/")
    last_colon = clean_img.rfind(":")

    if last_colon > last_slash:
        tag = clean_img[last_colon + 1:]
        clean_img = clean_img[:last_colon]
    else:
        tag = "latest"

    return clean_img, tag


def load_remote_quadlets() -> Dict[str, str]:
    """Reads image -> service mappings from remote configs directory (*.container)."""
    services: Dict[str, str] = {}
    if not REMOTE_CONFIGS_DIR.exists():
        return services

    try:
        for cfile in REMOTE_CONFIGS_DIR.glob("*.container"):
            service_name = cfile.stem
            try:
                text = cfile.read_text(encoding="utf-8", errors="ignore")
                img_match = re.search(r"^Image=(.+)$", text, re.MULTILINE)
                if img_match:
                    raw_img = img_match.group(1).strip()
                    clean_img, _ = parse_image_and_tag(raw_img)
                    services[clean_img] = service_name
                    short = clean_img.split("/")[-1]
                    if short not in services:
                        services[short] = service_name
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"Error reading REMOTE_CONFIGS_DIR ({REMOTE_CONFIGS_DIR}): {e}")
    return services


async def find_local_stack_via_docker(image_base: str) -> Optional[str]:
    """Queries running Docker containers for exact Compose project label."""
    short_name = image_base.split("/")[-1]
    cmd = [
        "docker", "ps", "--format",
        "{{.Image}}|{{.Label \"com.docker.compose.project\"}}"
    ]
    try:
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, _ = await proc.communicate()
        for line in stdout.decode("utf-8", errors="replace").splitlines():
            parts = line.strip().split("|")
            if len(parts) >= 2:
                c_img, c_proj = parts[0], parts[1]
                if c_proj and (image_base in c_img or short_name in c_img):
                    return c_proj
    except Exception as e:
        logger.debug(f"Docker ps container inspection skipped: {e}")
    return None


async def find_targets_for_image(image_base: str) -> Dict[str, Optional[str]]:
    """
    Checks where the image is deployed:
    1. Inspects running Docker containers via labels (runtime).
    2. Falls back to static filesystem scan of DOCKER_STACKS_DIR.
    3. Checks Remote Services (if configured).
    Returns dict: {"local": stack_name or None, "remote": service_name or None}
    """
    short_name = image_base.split("/")[-1]
    local_stack = None
    remote_service = None

    # 1. Try Docker runtime inspection first
    local_stack = await find_local_stack_via_docker(image_base)

    # 2. Fallback: Check Local Docker Stacks Directory
    if not local_stack and DOCKER_STACKS_DIR.exists():
        for stack_dir in DOCKER_STACKS_DIR.iterdir():
            if not stack_dir.is_dir():
                continue
            for compose_name in ("compose.yaml", "docker-compose.yml", "compose.yml"):
                compose_file = stack_dir / compose_name
                if compose_file.exists():
                    try:
                        content = compose_file.read_text(encoding="utf-8", errors="ignore")
                        # Match full image_base or image line ending with short_name
                        if image_base in content:
                            local_stack = stack_dir.name
                            break
                        if re.search(rf'image:\s*["\']?[^\s\'"]*{re.escape(short_name)}(?:[:@"\']|\s|$)', content, re.IGNORECASE):
                            local_stack = stack_dir.name
                            break
                    except Exception:
                        pass
            if local_stack:
                break

        if not local_stack:
            candidate = DOCKER_STACKS_DIR / short_name
            if candidate.is_dir() and ((candidate / "compose.yaml").exists() or (candidate / "docker-compose.yml").exists() or (candidate / "compose.yml").exists()):
                local_stack = short_name

    # 3. Check Remote Services (if configured)
    if REMOTE_SSH_HOST:
        remote_map = load_remote_quadlets()
        if image_base in remote_map:
            remote_service = remote_map[image_base]
        elif short_name in remote_map:
            remote_service = remote_map[short_name]

    return {"local": local_stack, "remote": remote_service}


async def run_remote_ssh(remote_command: str) -> Tuple[int, str, str]:
    """Runs a command over SSH on the remote host."""
    if not REMOTE_SSH_HOST:
        return 1, "", "No REMOTE_SSH_HOST configured."

    cmd = [
        "ssh",
        "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "BatchMode=yes",
        REMOTE_SSH_HOST,
        remote_command
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        return proc.returncode, stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace")
    except Exception as e:
        logger.error(f"SSH exception to {REMOTE_SSH_HOST}: {e}")
        return 1, "", str(e)


async def get_github_release_notes(repo: str, tag: str) -> Optional[dict]:
    """Queries GitHub API for changelog/release notes for a specific tag or latest release."""
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "DiunTap/1.0.0",
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

    async with httpx.AsyncClient(timeout=10.0, headers=headers) as client:
        candidate_tags = [tag]
        if tag.startswith("v"):
            candidate_tags.append(tag[1:])
        else:
            candidate_tags.append(f"v{tag}")

        for t_candidate in candidate_tags:
            try:
                url = f"https://api.github.com/repos/{repo}/releases/tags/{t_candidate}"
                resp = await client.get(url)
                if resp.status_code == 200:
                    return resp.json()
            except Exception:
                pass

        # Fallback: Latest release
        try:
            url = f"https://api.github.com/repos/{repo}/releases/latest"
            resp = await client.get(url)
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass

    return None


async def send_telegram_message(html_text: str, reply_markup: Optional[dict] = None) -> Optional[dict]:
    """Sends HTML formatted Telegram message with optional inline buttons."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing.")
        return None

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload: Dict[str, Any] = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": html_text[:4000],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 400 and "can't parse entities" in resp.text:
                logger.warning("HTML parsing failed in Telegram, retrying as plain text fallback...")
                payload.pop("parse_mode", None)
                payload["text"] = re.sub(r"<[^>]+>", "", html_text)[:4000]
                fallback_resp = await client.post(url, json=payload)
                if fallback_resp.status_code == 200:
                    return fallback_resp.json()
                logger.error(f"Telegram send fallback error: {fallback_resp.status_code} {fallback_resp.text}")
            else:
                logger.error(f"Telegram send error: {resp.status_code} {resp.text}")
    except Exception as e:
        logger.error(f"Telegram send exception: {e}")
    return None


async def edit_telegram_message(chat_id: str | int, message_id: int, new_html: str, reply_markup: Optional[dict] = None):
    """Edits an existing Telegram message."""
    if not TELEGRAM_BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText"
    payload: Dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": new_html[:4000],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code == 200:
                return
            if resp.status_code == 400 and "can't parse entities" in resp.text:
                logger.warning(f"HTML parsing failed in Telegram edit {message_id}, retrying as plain text fallback...")
                payload.pop("parse_mode", None)
                payload["text"] = re.sub(r"<[^>]+>", "", new_html)[:4000]
                await client.post(url, json=payload)
            elif "message is not modified" not in resp.text.lower():
                logger.warning(f"Telegram edit error {message_id}: {resp.status_code} {resp.text}")
    except Exception as e:
        logger.error(f"Telegram edit error {message_id}: {e}")


async def answer_callback_query(callback_query_id: str, text: Optional[str] = None):
    """Answers a Telegram inline button click."""
    if not TELEGRAM_BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery"
    payload: Dict[str, Any] = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(url, json=payload)
    except Exception as e:
        logger.error(f"answerCallbackQuery error: {e}")


# ==============================================================================
# LOCAL NODE (DOCKER COMPOSE) - UPDATE, HEALTHCHECK & ROLLBACK
# ==============================================================================

def find_local_compose_file(stack_name: str) -> Tuple[Optional[Path], Optional[Path], str]:
    """Returns (stack_dir, compose_file, effective_stack_name) for local Docker Compose."""
    stack_dir = DOCKER_STACKS_DIR / stack_name
    compose_file = None
    for cname in ("compose.yaml", "docker-compose.yml", "compose.yml"):
        if (stack_dir / cname).exists():
            compose_file = stack_dir / cname
            break

    if not compose_file and DOCKER_STACKS_DIR.exists():
        for candidate_dir in DOCKER_STACKS_DIR.iterdir():
            if candidate_dir.is_dir() and (candidate_dir.name == stack_name or candidate_dir.name.startswith(stack_name)):
                for cname in ("compose.yaml", "docker-compose.yml", "compose.yml"):
                    if (candidate_dir / cname).exists():
                        stack_dir = candidate_dir
                        stack_name = candidate_dir.name
                        compose_file = candidate_dir / cname
                        break
                if compose_file:
                    break

    return stack_dir, compose_file, stack_name


async def inspect_local_containers(stack_dir: Path, compose_file: Path) -> List[Dict[str, Any]]:
    """Returns list of container states in a docker compose stack."""
    cmd = ["docker", "compose", "--project-directory", str(stack_dir), "-f", str(compose_file), "ps", "--format", "json"]
    try:
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, _ = await proc.communicate()
        lines = stdout.decode("utf-8", errors="replace").strip().splitlines()
        containers = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                containers.append(data)
            except Exception:
                pass
        return containers
    except Exception as e:
        logger.error(f"Error inspecting local containers for {stack_dir}: {e}")
        return []


async def execute_local_update(chat_id: str | int, message_id: int, stack_name: str, tag: str, original_header: str):
    """Executes stack update with backup restore point and healthcheck verification."""
    stack_dir, compose_file, effective_stack = find_local_compose_file(stack_name)
    node_name = LOCAL_NODE_NAME
    if not compose_file:
        await edit_telegram_message(
            chat_id,
            message_id,
            f"{original_header}\n\n❌ <b>Error:</b> Compose file not found for <code>{stack_name}</code> in {DOCKER_STACKS_DIR}."
        )
        return

    safe_stack = effective_stack[:25]
    safe_tag = tag[:14]

    # Initial status
    await edit_telegram_message(
        chat_id,
        message_id,
        f"{original_header}\n\n" + t("msg_updating", node=node_name, tag=tag)
    )

    try:
        # Step 1: Backup Tagging before pull
        pre_containers = await inspect_local_containers(stack_dir, compose_file)
        for c in pre_containers:
            img_ref = c.get("Image")
            if img_ref and not img_ref.endswith(f":diun-backup-{safe_stack}"):
                backup_tag = f"diun-backup-{safe_stack}"
                repo = img_ref.rsplit(":", 1)[0] if ":" in img_ref else img_ref
                full_backup_name = f"{repo}:{backup_tag}"
                tag_proc = await asyncio.create_subprocess_exec(
                    "docker", "tag", img_ref, full_backup_name,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
                )
                await tag_proc.wait()

        # Step 2: docker compose pull
        cmd_pull = ["docker", "compose", "--project-directory", str(stack_dir), "-f", str(compose_file), "pull"]
        proc_pull = await asyncio.create_subprocess_exec(*cmd_pull, cwd=str(stack_dir), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout_pull, stderr_pull = await proc_pull.communicate()

        if proc_pull.returncode != 0:
            err = (stderr_pull or stdout_pull).decode("utf-8", errors="replace")
            await edit_telegram_message(
                chat_id,
                message_id,
                f"{original_header}\n\n" + t("msg_pull_error", error=html.escape(err[:1000]))
            )
            return

        # Step 3: docker compose up -d
        cmd_up = ["docker", "compose", "--project-directory", str(stack_dir), "-f", str(compose_file), "up", "-d"]
        proc_up = await asyncio.create_subprocess_exec(*cmd_up, cwd=str(stack_dir), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout_up, stderr_up = await proc_up.communicate()

        if proc_up.returncode != 0:
            err = (stderr_up or stdout_up).decode("utf-8", errors="replace")
            await edit_telegram_message(
                chat_id,
                message_id,
                f"{original_header}\n\n" + t("msg_up_error", error=html.escape(err[:1000]))
            )
            return

        # Step 4: Healthcheck & Stabilization
        await edit_telegram_message(
            chat_id,
            message_id,
            f"{original_header}\n\n" + t("msg_verifying", node=node_name, wait_sec=HEALTHCHECK_WAIT_SECONDS)
        )
        await asyncio.sleep(HEALTHCHECK_WAIT_SECONDS)

        post_containers = await inspect_local_containers(stack_dir, compose_file)
        has_failure = False
        status_lines = []

        if not post_containers:
            status_lines.append(t("no_containers_running"))
            has_failure = True
        else:
            for c in post_containers:
                cname = c.get("Name", "container")
                state = (c.get("State") or "unknown").lower()
                health = (c.get("Health") or "").lower()
                exit_code = c.get("ExitCode", 0)

                desc = f"<b>{html.escape(cname)}</b>: {html.escape(state)}"
                if health:
                    desc += f" ({html.escape(health)})"

                if state != "running" or health == "unhealthy" or exit_code != 0:
                    has_failure = True
                    status_lines.append(f"❌ {desc}")
                else:
                    status_lines.append(f"🟢 {desc}")

        status_summary = "\n".join(status_lines)

        if not has_failure:
            buttons = [
                [
                    {"text": t("btn_rollback"), "callback_data": f"rb:loc:{safe_stack}:{safe_tag}"},
                    {"text": t("btn_dismiss"), "callback_data": f"ccl:loc:{safe_stack}"}
                ]
            ]
            await edit_telegram_message(
                chat_id,
                message_id,
                f"{original_header}\n\n" + t("msg_success", node=node_name, stack=effective_stack, tag=tag, status=status_summary),
                reply_markup={"inline_keyboard": buttons}
            )
        else:
            # Failure - fetch last logs
            cmd_logs = ["docker", "compose", "--project-directory", str(stack_dir), "-f", str(compose_file), "logs", "--tail", "20"]
            proc_logs = await asyncio.create_subprocess_exec(*cmd_logs, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout_logs, stderr_logs = await proc_logs.communicate()
            logs_txt = (stdout_logs or stderr_logs).decode("utf-8", errors="replace")

            buttons = [
                [
                    {"text": t("btn_rollback"), "callback_data": f"rb:loc:{safe_stack}:{safe_tag}"},
                    {"text": t("btn_dismiss"), "callback_data": f"ccl:loc:{safe_stack}"}
                ]
            ]
            await edit_telegram_message(
                chat_id,
                message_id,
                f"{original_header}\n\n" + t("msg_warning_header", node=node_name, stack=effective_stack, status=status_summary, logs=html.escape(logs_txt[-1200:])),
                reply_markup={"inline_keyboard": buttons}
            )

    except Exception as e:
        logger.error(f"Exception during local update {effective_stack}: {e}")
        await edit_telegram_message(
            chat_id,
            message_id,
            f"{original_header}\n\n❌ <b>Update error:</b> {html.escape(str(e))}"
        )


async def execute_local_rollback(chat_id: str | int, message_id: int, stack_name: str, tag: str, original_header: str):
    """Rolls back local stack update to previous diun-backup image."""
    stack_dir, compose_file, effective_stack = find_local_compose_file(stack_name)
    if not compose_file:
        await edit_telegram_message(chat_id, message_id, f"{original_header}\n\n❌ Compose file not found for {stack_name}.")
        return

    safe_stack = effective_stack[:25]
    backup_tag = f"diun-backup-{safe_stack}"

    await edit_telegram_message(
        chat_id,
        message_id,
        f"{original_header}\n\n" + t("msg_rollback_in_progress")
    )

    try:
        proc_images = await asyncio.create_subprocess_exec(
            "docker", "images", "--format", "{{.Repository}}:{{.Tag}}",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout_imgs, _ = await proc_images.communicate()
        all_imgs = stdout_imgs.decode("utf-8", errors="replace").splitlines()

        matching_backup = None
        for img in all_imgs:
            if img.endswith(f":{backup_tag}"):
                matching_backup = img
                break

        if not matching_backup:
            await edit_telegram_message(
                chat_id,
                message_id,
                f"{original_header}\n\n" + t("msg_rollback_not_found", tag=backup_tag)
            )
            return

        repo = matching_backup.split(f":{backup_tag}")[0]
        target_tag = f"{repo}:{tag}"

        # Retag back
        tag_proc = await asyncio.create_subprocess_exec("docker", "tag", matching_backup, target_tag)
        await tag_proc.wait()

        # Recreate container
        cmd_recreate = ["docker", "compose", "--project-directory", str(stack_dir), "-f", str(compose_file), "up", "-d", "--force-recreate"]
        proc_rec = await asyncio.create_subprocess_exec(*cmd_recreate, cwd=str(stack_dir), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        await proc_rec.communicate()

        await asyncio.sleep(4)
        post_c = await inspect_local_containers(stack_dir, compose_file)
        all_running = all(c.get("State") == "running" for c in post_c) if post_c else False

        if all_running:
            await edit_telegram_message(
                chat_id,
                message_id,
                f"{original_header}\n\n" + t("msg_rollback_success", stack=effective_stack),
                reply_markup={"inline_keyboard": []}
            )
        else:
            await edit_telegram_message(
                chat_id,
                message_id,
                f"{original_header}\n\n" + t("msg_rollback_warning", stack=effective_stack),
                reply_markup={"inline_keyboard": []}
            )

    except Exception as e:
        logger.error(f"Exception during rollback {effective_stack}: {e}")
        await edit_telegram_message(chat_id, message_id, f"{original_header}\n\n❌ Rollback error: {html.escape(str(e))}")


# ==============================================================================
# REMOTE NODE (SSH / PODMAN QUADLETS / SERVICES) - UPDATE, HEALTHCHECK & ROLLBACK
# ==============================================================================

async def execute_remote_update(chat_id: str | int, message_id: int, service_name: str, tag: str, original_header: str):
    """Executes update on remote node via SSH with backup and verification."""
    node_name = REMOTE_NODE_NAME
    safe_svc = service_name[:25]
    safe_tag = tag[:14]

    await edit_telegram_message(
        chat_id,
        message_id,
        f"{original_header}\n\n" + t("msg_updating", node=node_name, tag=tag)
    )

    try:
        # Step 1: Detect image name
        rc, img_out, _ = await run_remote_ssh(f"podman inspect {service_name} --format '{{{{.ImageName}}}}' 2>/dev/null || podman inspect {service_name} --format '{{{{.Image}}}}'")
        full_image = img_out.strip() if rc == 0 and img_out.strip() else ""

        if full_image:
            repo = full_image.rsplit(":", 1)[0] if ":" in full_image else full_image
            backup_name = f"{repo}:diun-backup-{safe_svc}"
            await run_remote_ssh(f"podman tag {full_image} {backup_name}")

        # Step 2: Pull and restart systemd service
        pull_cmd = f"podman pull {full_image}" if full_image else "true"
        restart_cmd = f"{pull_cmd} && systemctl --user restart {service_name}.service"
        rc_up, out_up, err_up = await run_remote_ssh(restart_cmd)

        if rc_up != 0:
            err_msg = err_up or out_up or "Service restart failed"
            await edit_telegram_message(
                chat_id,
                message_id,
                f"{original_header}\n\n❌ <b>Update error on {node_name}:</b>\n<pre>{html.escape(err_msg[:1000])}</pre>"
            )
            return

        # Step 3: Healthcheck
        await edit_telegram_message(
            chat_id,
            message_id,
            f"{original_header}\n\n" + t("msg_verifying", node=node_name, wait_sec=HEALTHCHECK_WAIT_SECONDS)
        )
        await asyncio.sleep(HEALTHCHECK_WAIT_SECONDS)

        rc_active, active_out, _ = await run_remote_ssh(f"systemctl --user is-active {service_name}.service")
        is_active = active_out.strip() == "active"

        rc_p, podman_status, _ = await run_remote_ssh(f"podman inspect {service_name} --format '{{{{.State.Status}}}}|{{{{if .State.Health}}}}{{{{.State.Health.Status}}}}{{{{else}}}}none{{{{end}}}}' 2>/dev/null || echo 'not_found'")
        status_parts = podman_status.strip().split("|")
        p_state = status_parts[0] if status_parts else "unknown"
        p_health = status_parts[1] if len(status_parts) > 1 else "none"

        is_healthy = is_active and (p_state == "running" or p_state == "none") and p_health != "unhealthy"

        if is_healthy:
            buttons = [
                [
                    {"text": t("btn_rollback"), "callback_data": f"rb:rem:{safe_svc}:{safe_tag}"},
                    {"text": t("btn_dismiss"), "callback_data": f"ccl:rem:{safe_svc}"}
                ]
            ]
            status_desc = f"Active (State: {p_state}, Health: {p_health})"
            await edit_telegram_message(
                chat_id,
                message_id,
                f"{original_header}\n\n" + t("msg_success", node=node_name, stack=service_name, tag=tag, status=status_desc),
                reply_markup={"inline_keyboard": buttons}
            )
        else:
            _, logs_txt, _ = await run_remote_ssh(f"podman logs --tail 20 {service_name} 2>&1 || journalctl --user -u {service_name}.service -n 20 --no-pager")
            buttons = [
                [
                    {"text": t("btn_rollback"), "callback_data": f"rb:rem:{safe_svc}:{safe_tag}"},
                    {"text": t("btn_dismiss"), "callback_data": f"ccl:rem:{safe_svc}"}
                ]
            ]
            status_desc = f"Service: {active_out.strip()} (Podman: {p_state})"
            await edit_telegram_message(
                chat_id,
                message_id,
                f"{original_header}\n\n" + t("msg_warning_header", node=node_name, stack=service_name, status=status_desc, logs=html.escape(logs_txt[-1200:])),
                reply_markup={"inline_keyboard": buttons}
            )

    except Exception as e:
        logger.error(f"Exception during remote update {service_name}: {e}")
        await edit_telegram_message(
            chat_id,
            message_id,
            f"{original_header}\n\n❌ <b>Remote update error:</b> {html.escape(str(e))}"
        )


async def execute_remote_rollback(chat_id: str | int, message_id: int, service_name: str, tag: str, original_header: str):
    """Restores previous image for remote service."""
    safe_svc = service_name[:25]
    backup_tag = f"diun-backup-{safe_svc}"

    await edit_telegram_message(
        chat_id,
        message_id,
        f"{original_header}\n\n" + t("msg_rollback_in_progress")
    )

    try:
        rc, img_out, _ = await run_remote_ssh(f"podman inspect {service_name} --format '{{{{.ImageName}}}}' 2>/dev/null || podman inspect {service_name} --format '{{{{.Image}}}}'")
        full_image = img_out.strip() if rc == 0 and img_out.strip() else ""

        if not full_image:
            await edit_telegram_message(chat_id, message_id, f"{original_header}\n\n❌ Image not found for {service_name}.")
            return

        repo = full_image.rsplit(":", 1)[0] if ":" in full_image else full_image
        backup_name = f"{repo}:{backup_tag}"

        cmd_rb = f"podman tag {backup_name} {full_image} && systemctl --user restart {service_name}.service"
        rc_rb, _, err_rb = await run_remote_ssh(cmd_rb)

        if rc_rb != 0:
            await edit_telegram_message(
                chat_id,
                message_id,
                f"{original_header}\n\n❌ <b>Rollback error on {REMOTE_NODE_NAME}:</b>\n<pre>{html.escape(err_rb[:1000])}</pre>"
            )
            return

        await asyncio.sleep(4)
        _, active_out, _ = await run_remote_ssh(f"systemctl --user is-active {service_name}.service")
        if active_out.strip() == "active":
            await edit_telegram_message(
                chat_id,
                message_id,
                f"{original_header}\n\n" + t("msg_rollback_success", stack=service_name),
                reply_markup={"inline_keyboard": []}
            )
        else:
            await edit_telegram_message(
                chat_id,
                message_id,
                f"{original_header}\n\n" + t("msg_rollback_warning", stack=service_name),
                reply_markup={"inline_keyboard": []}
            )

    except Exception as e:
        logger.error(f"Exception during remote rollback {service_name}: {e}")
        await edit_telegram_message(chat_id, message_id, f"{original_header}\n\n❌ Rollback error: {html.escape(str(e))}")


# ==============================================================================
# DIUN WEBHOOK PROCESSING & TELEGRAM POLLING
# ==============================================================================

async def process_diun_notification(payload: DiunWebhookPayload):
    """Processes incoming DIUN webhook and sends interactive Telegram message."""
    img_name, tag = parse_image_and_tag(payload.image)
    logger.info(f"Received DIUN webhook: image={img_name}, tag={tag}, status={payload.status}")

    # Deduplication check (ignore identical notification within DEDUP_WINDOW_SECONDS)
    dedup_key = f"{payload.image}:{tag}:{payload.status}"
    now = time.time()
    if dedup_key in _recent_notifications and (now - _recent_notifications[dedup_key]) < DEDUP_WINDOW_SECONDS:
        logger.info(f"Duplicate notification ignored for {dedup_key} (within {DEDUP_WINDOW_SECONDS}s)")
        return
    _recent_notifications[dedup_key] = now

    # Resolve GitHub repo
    github_repo = resolve_github_repo(img_name, IMAGE_MAPPINGS)
    release_info = None
    if github_repo:
        release_info = await get_github_release_notes(github_repo, tag)

    # Find matched targets (runtime + filesystem)
    targets = await find_targets_for_image(img_name)
    local_stack = targets.get("local")
    remote_service = targets.get("remote")

    title_key = "title_update" if payload.status == "update" else "title_new"
    msg_lines = [
        f"<b>{t(title_key)}</b>",
        "",
        f"{t('lbl_image')} <code>{html.escape(payload.image)}</code>",
        f"{t('lbl_tag')} <code>{html.escape(tag)}</code>",
    ]

    if local_stack:
        msg_lines.append(f"{t('lbl_stack', node=LOCAL_NODE_NAME)} <code>{html.escape(local_stack)}</code>")
    if remote_service and REMOTE_SSH_HOST:
        msg_lines.append(f"{t('lbl_service', node=REMOTE_NODE_NAME)} <code>{html.escape(remote_service)}</code>")

    if release_info:
        rel_name = release_info.get("name") or release_info.get("tag_name") or "Release Notes"
        rel_url = release_info.get("html_url")
        rel_body = release_info.get("body", "")

        msg_lines.append("")
        msg_lines.append(f"{t('lbl_changelog')} <a href='{rel_url}'>{html.escape(rel_name)}</a>")

        if rel_body:
            formatted_notes = format_markdown_for_telegram(rel_body, max_length=1500)
            if formatted_notes:
                msg_lines.append(formatted_notes)
    elif payload.hub_link:
        msg_lines.append(f"{t('lbl_dockerhub')} <a href='{payload.hub_link}'>{html.escape(img_name)}</a>")

    full_message = "\n".join(msg_lines)

    # Build interactive buttons
    buttons = []
    safe_tag = tag[:14]

    if local_stack and remote_service and REMOTE_SSH_HOST:
        safe_loc = local_stack[:25]
        safe_rem = remote_service[:25]
        buttons.append([
            {"text": t("btn_update_node", node=LOCAL_NODE_NAME), "callback_data": f"upd:loc:{safe_loc}:{safe_tag}"},
            {"text": t("btn_update_remote", node=REMOTE_NODE_NAME), "callback_data": f"upd:rem:{safe_rem}:{safe_tag}"}
        ])
        buttons.append([{"text": t("btn_skip"), "callback_data": f"ccl:all:{safe_loc}"}])
    elif local_stack:
        safe_loc = local_stack[:25]
        buttons.append([
            {"text": t("btn_update_now"), "callback_data": f"upd:loc:{safe_loc}:{safe_tag}"},
            {"text": t("btn_skip"), "callback_data": f"ccl:loc:{safe_loc}"}
        ])
    elif remote_service and REMOTE_SSH_HOST:
        safe_rem = remote_service[:25]
        buttons.append([
            {"text": t("btn_update_remote", node=REMOTE_NODE_NAME), "callback_data": f"upd:rem:{safe_rem}:{safe_tag}"},
            {"text": t("btn_skip"), "callback_data": f"ccl:rem:{safe_rem}"}
        ])

    if release_info and release_info.get("html_url"):
        buttons.append([{"text": t("btn_open_github"), "url": release_info["html_url"]}])

    reply_markup = {"inline_keyboard": buttons} if buttons else None
    await send_telegram_message(full_message, reply_markup=reply_markup)


async def telegram_polling_loop():
    """Long Polling loop listening for inline button callbacks."""
    if not TELEGRAM_BOT_TOKEN:
        logger.warning("No TELEGRAM_BOT_TOKEN provided. Telegram polling disabled.")
        return

    logger.info("Starting Telegram button polling loop (Long Polling)...")
    offset = 0

    async with httpx.AsyncClient(timeout=35.0) as client:
        while is_polling_active:
            try:
                url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
                resp = await client.get(url, params={"offset": offset, "timeout": 25})
                if resp.status_code != 200:
                    await asyncio.sleep(5)
                    continue

                data = resp.json()
                for update in data.get("result", []):
                    offset = max(offset, update["update_id"] + 1)

                    if "callback_query" in update:
                        cb = update["callback_query"]
                        cb_id = cb["id"]
                        cb_data = cb.get("data", "")
                        from_user = cb.get("from", {})
                        msg = cb.get("message", {})
                        chat_id = msg.get("chat", {}).get("id")
                        msg_id = msg.get("message_id")
                        orig_text = msg.get("text") or msg.get("caption") or ""

                        # Security verification
                        if str(from_user.get("id")) != str(TELEGRAM_CHAT_ID) and str(chat_id) != str(TELEGRAM_CHAT_ID):
                            await answer_callback_query(cb_id, text=t("toast_no_permission"))
                            continue

                        header = orig_text.split("\n\n")[0] if orig_text else "Container Update"

                        # 1. UPDATE ACTION
                        if cb_data.startswith("upd:"):
                            parts = cb_data.split(":")
                            # Remote node: upd:rem:...
                            if len(parts) >= 4 and parts[1] == "rem":
                                service_name = parts[2]
                                tag = parts[3]
                                await answer_callback_query(cb_id, text=t("toast_updating", node=REMOTE_NODE_NAME, name=service_name))
                                asyncio.create_task(execute_remote_update(chat_id, msg_id, service_name, tag, html.escape(header)))
                            # Local node: upd:loc:...
                            elif len(parts) >= 4 and parts[1] == "loc":
                                stack_name = parts[2]
                                tag = parts[3]
                                await answer_callback_query(cb_id, text=t("toast_updating", node=LOCAL_NODE_NAME, name=stack_name))
                                asyncio.create_task(execute_local_update(chat_id, msg_id, stack_name, tag, html.escape(header)))
                            else:
                                # Fallback: upd:{stack}:{tag}
                                stack_name = parts[1]
                                tag = parts[2] if len(parts) > 2 else "latest"
                                await answer_callback_query(cb_id, text=t("toast_updating", node=LOCAL_NODE_NAME, name=stack_name))
                                asyncio.create_task(execute_local_update(chat_id, msg_id, stack_name, tag, html.escape(header)))

                        # 2. ROLLBACK ACTION
                        elif cb_data.startswith("rb:"):
                            parts = cb_data.split(":")
                            if len(parts) >= 4 and parts[1] == "rem":
                                service_name = parts[2]
                                tag = parts[3]
                                await answer_callback_query(cb_id, text=t("toast_rollback", node=REMOTE_NODE_NAME, name=service_name))
                                asyncio.create_task(execute_remote_rollback(chat_id, msg_id, service_name, tag, html.escape(header)))
                            else:
                                stack_name = parts[2] if len(parts) >= 3 else parts[1]
                                tag = parts[3] if len(parts) >= 4 else "latest"
                                await answer_callback_query(cb_id, text=t("toast_rollback", node=LOCAL_NODE_NAME, name=stack_name))
                                asyncio.create_task(execute_local_rollback(chat_id, msg_id, stack_name, tag, html.escape(header)))

                        # 3. DISMISS / CANCEL
                        elif cb_data.startswith("ccl:"):
                            parts = cb_data.split(":")
                            name = parts[-1]
                            await answer_callback_query(cb_id, text=t("toast_dismissed"))
                            new_text = f"<s>{html.escape(orig_text[:500])}</s>\n\n" + t("msg_skipped", name=html.escape(name))
                            await edit_telegram_message(chat_id, msg_id, new_text, reply_markup={"inline_keyboard": []})

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Exception in Telegram polling loop: {e}")
                await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global is_polling_active
    is_polling_active = True
    poll_task = asyncio.create_task(telegram_polling_loop())
    yield
    is_polling_active = False
    poll_task.cancel()
    try:
        await poll_task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="DiunTap - Interactive Docker Updates for DIUN", version="1.0.0", lifespan=lifespan)


@app.post("/webhook/diun")
async def webhook_diun(request: Request, background_tasks: BackgroundTasks):
    try:
        data = await request.json()
        payload = DiunWebhookPayload(**data)
    except Exception as e:
        logger.error(f"Invalid webhook payload: {e}")
        raise HTTPException(status_code=400, detail=f"Invalid payload: {e}")

    background_tasks.add_task(process_diun_notification, payload)
    return {"status": "accepted", "image": payload.image}


@app.get("/")
async def root():
    return await health()


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "app": "DiunTap",
        "version": "1.0.0",
        "telegram_configured": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID),
        "github_token_configured": bool(GITHUB_TOKEN),
        "local_node": {
            "name": LOCAL_NODE_NAME,
            "stacks_dir": str(DOCKER_STACKS_DIR),
            "stacks_dir_exists": DOCKER_STACKS_DIR.exists(),
        },
        "remote_node": {
            "enabled": bool(REMOTE_SSH_HOST),
            "name": REMOTE_NODE_NAME,
            "ssh_host": REMOTE_SSH_HOST or None,
            "configs_dir": str(REMOTE_CONFIGS_DIR) if REMOTE_SSH_HOST else None,
            "configs_dir_exists": REMOTE_CONFIGS_DIR.exists() if REMOTE_SSH_HOST else False,
        },
        "healthcheck_wait_seconds": HEALTHCHECK_WAIT_SECONDS,
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("WEBHOOK_PORT", 5000))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)

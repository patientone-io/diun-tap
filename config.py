"""Central configuration for DiunTap.

All settings come from environment variables (optionally via a .env file).
Security-relevant behaviour:
  - DIUN_WEBHOOK_SECRET is OPTIONAL for smooth migration. When unset the
    webhook works in legacy unauthenticated mode but logs a WARNING.
  - TELEGRAM_ALLOWED_USER_IDS is the authoritative allow-list. When unset it
    falls back to the legacy single TELEGRAM_CHAT_ID.
  - DOCKER_GID documents the host docker socket group id. It is consumed by
    compose.yaml (group_add) and validated at startup so a non-root container
    gets a actionable error instead of a cryptic PermissionError.
  - EXCLUDE_IMAGES / PINNED_STACKS silently skip notifications for frozen
    images or stacks (pin/freeze). See filters.py.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("diuntap")


def _parse_id_list(raw: str) -> list[str]:
    out: list[str] = []
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part:
            out.append(part)
    return out


@dataclass
class Settings:
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_allowed_user_ids: list[str] = field(default_factory=list)
    github_token: str = ""
    lang: str = "en"
    local_node_name: str = "Local"
    docker_stacks_dir: Path = field(default_factory=lambda: Path("/srv/docker"))
    docker_gid: str = ""
    remote_ssh_host: str = ""
    remote_node_name: str = "Remote"
    remote_configs_dir: Path = field(default_factory=lambda: Path("/remote-configs"))
    healthcheck_wait_seconds: int = 6
    webhook_port: int = 5000
    dedup_window_seconds: int = 30
    diun_webhook_secret: str = ""
    mappings_file: str = ""
    exclude_images: list[str] = field(default_factory=list)
    pinned_stacks: list[str] = field(default_factory=list)
    ssh_timeout_seconds: int = 30
    docker_timeout_seconds: int = 180

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def remote_enabled(self) -> bool:
        return bool(self.remote_ssh_host)

    @property
    def webhook_auth_enabled(self) -> bool:
        return bool(self.diun_webhook_secret)


def load_settings() -> Settings:
    lang = os.getenv("LANGUAGE", "en").lower().strip() or "en"
    if lang not in ("en", "pl"):
        lang = "en"

    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    allowed_raw = os.getenv("TELEGRAM_ALLOWED_USER_IDS", "").strip()
    allowed = _parse_id_list(allowed_raw)
    if not allowed and chat_id:
        allowed = [chat_id]

    try:
        health_wait = int(os.getenv("HEALTHCHECK_WAIT_SECONDS", "6"))
    except ValueError:
        health_wait = 6
    health_wait = max(2, min(health_wait, 300))

    try:
        webhook_port = int(os.getenv("WEBHOOK_PORT", "5000"))
    except ValueError:
        webhook_port = 5000

    try:
        ssh_timeout = int(os.getenv("SSH_TIMEOUT_SECONDS", "30"))
    except ValueError:
        ssh_timeout = 30
    ssh_timeout = max(5, min(ssh_timeout, 300))

    try:
        docker_timeout = int(os.getenv("DOCKER_TIMEOUT_SECONDS", "180"))
    except ValueError:
        docker_timeout = 180
    docker_timeout = max(10, min(docker_timeout, 900))

    settings = Settings(
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_id=chat_id,
        telegram_allowed_user_ids=allowed,
        github_token=os.getenv("GITHUB_TOKEN", "").strip(),
        lang=lang,
        local_node_name=os.getenv("LOCAL_NODE_NAME", "Local").strip() or "Local",
        docker_stacks_dir=Path(os.getenv("DOCKER_STACKS_DIR", "/srv/docker")),
        docker_gid=os.getenv("DOCKER_GID", "").strip(),
        remote_ssh_host=os.getenv("REMOTE_SSH_HOST", "").strip(),
        remote_node_name=os.getenv("REMOTE_NODE_NAME", "Remote").strip() or "Remote",
        remote_configs_dir=Path(os.getenv("REMOTE_CONFIGS_DIR", "/remote-configs")),
        healthcheck_wait_seconds=health_wait,
        webhook_port=webhook_port,
        dedup_window_seconds=30,
        diun_webhook_secret=os.getenv("DIUN_WEBHOOK_SECRET", "").strip(),
        mappings_file=os.getenv("MAPPINGS_FILE", "").strip(),
        exclude_images=_parse_id_list(os.getenv("EXCLUDE_IMAGES", "")),
        pinned_stacks=_parse_id_list(os.getenv("PINNED_STACKS", "")),
        ssh_timeout_seconds=ssh_timeout,
        docker_timeout_seconds=docker_timeout,
    )

    if not settings.diun_webhook_secret:
        logger.warning(
            "DIUN_WEBHOOK_SECRET is not set — /webhook/diun runs in legacy "
            "unauthenticated mode. Set DIUN_WEBHOOK_SECRET and configure DIUN "
            "with header X-Diun-Secret (or ?token=) to enable authentication."
        )

    if not settings.telegram_allowed_user_ids:
        logger.warning(
            "No TELEGRAM_ALLOWED_USER_IDS / TELEGRAM_CHAT_ID configured — "
            "Telegram actions will be rejected until configured."
        )

    socket_path = Path("/var/run/docker.sock")
    if socket_path.exists():
        try:
            import grp

            gid = socket_path.stat().st_gid
            try:
                name = grp.getgrgid(gid).gr_name
            except KeyError:
                name = "unknown"
            logger.info("docker.sock gid=%s name=%s (set DOCKER_GID=%s if Permission denied)", gid, name, gid)
        except Exception as exc:  # pragma: no cover - best effort log
            logger.debug("Could not stat docker.sock: %s", exc)

    return settings


settings = load_settings()

# Eagerly load image mappings so webhook processing never blocks on first request.
from mappings import load_custom_mappings  # noqa: E402

IMAGE_MAPPINGS = load_custom_mappings(settings.mappings_file or None)

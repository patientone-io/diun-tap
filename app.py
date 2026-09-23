"""Backward-compatible shim: `uvicorn app:app` keeps working.

The real application lives in main.py. This module re-exports the FastAPI app
plus the most commonly imported helpers so existing tooling, tests and docs
that do `import app` do not break.
"""

from config import IMAGE_MAPPINGS, settings
from main import (
    app,
    dedup,
    execute_local_rollback,
    execute_local_update,
    execute_remote_rollback,
    execute_remote_update,
    health,
    process_diun_notification,
    ready,
    root,
    webhook_diun,
)
from models import DiunWebhookPayload, parse_image_and_tag

DOCKER_STACKS_DIR = settings.docker_stacks_dir
TELEGRAM_BOT_TOKEN = settings.telegram_bot_token
TELEGRAM_CHAT_ID = settings.telegram_chat_id
GITHUB_TOKEN = settings.github_token
LOCAL_NODE_NAME = settings.local_node_name
REMOTE_SSH_HOST = settings.remote_ssh_host
REMOTE_NODE_NAME = settings.remote_node_name
REMOTE_CONFIGS_DIR = settings.remote_configs_dir
HEALTHCHECK_WAIT_SECONDS = settings.healthcheck_wait_seconds
DEDUP_WINDOW_SECONDS = settings.dedup_window_seconds
_recent_notifications = {}

from docker_local import find_local_compose_file, find_targets_for_image  # noqa: E402

__all__ = [
    "app",
    "DiunWebhookPayload",
    "parse_image_and_tag",
    "find_targets_for_image",
    "find_local_compose_file",
    "process_diun_notification",
    "execute_local_update",
    "execute_local_rollback",
    "execute_remote_update",
    "execute_remote_rollback",
    "webhook_diun",
    "health",
    "ready",
    "root",
    "settings",
    "IMAGE_MAPPINGS",
    "DOCKER_STACKS_DIR",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "GITHUB_TOKEN",
    "LOCAL_NODE_NAME",
    "REMOTE_SSH_HOST",
    "REMOTE_NODE_NAME",
    "REMOTE_CONFIGS_DIR",
    "HEALTHCHECK_WAIT_SECONDS",
    "DEDUP_WINDOW_SECONDS",
    "dedup",
]

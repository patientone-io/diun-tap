"""DiunTap FastAPI entrypoint: webhook, Telegram actions, Docker orchestration."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, Optional

from fastapi import FastAPI, HTTPException, Request
from pydantic import ValidationError as PydanticValidationError

from config import IMAGE_MAPPINGS, settings
from dedup import DedupCache
from errors import ImageTagMismatch
from docker_local import (
    _run,
    compose_logs,
    create_backups,
    find_local_compose_file,
    find_targets_for_image,
    inspect_local_containers,
    wait_for_healthy,
    update_services,
    same_repository,
)
from formatters import format_markdown_for_telegram
from github_client import get_github_release_notes
from i18n import t
from mappings import load_remote_quadlets_fallback, resolve_github_repo
from models import DiunWebhookPayload, action_store, parse_image_and_tag
from polling import telegram_polling_loop
from remote_ssh import (
    remote_inspect_image,
    remote_inspect_image_id,
    remote_wait_for_healthy,
    remote_service_uses_image,
    remote_logs,
    remote_pull_image,
    remote_prune_backups,
    remote_restart_service,
    remote_tag_image,
)
from security import (
    sanitize_service_name,
    sanitize_stack_name,
    sanitize_tag,
    verify_webhook_secret,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
# HTTP clients log request URLs containing the Telegram bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("diuntap")

dedup = DedupCache(maxsize=512, ttl=settings.dedup_window_seconds)
is_polling_active = True


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _display_short(value: str, limit: int) -> str:
    value = value or ""
    return value if len(value) <= limit else value[:limit] + "…"


def _rollback_message(result: bool | None) -> str:
    if result is None:
        return t("msg_auto_rollback_pending")
    return t("msg_auto_rollback_ok" if result else "msg_auto_rollback_failed")


def _manual_rollback_message(result: bool | None, stack: str) -> str:
    if result is None:
        return t("msg_auto_rollback_pending")
    return t("msg_rollback_success" if result else "msg_rollback_warning", stack=stack)


async def _load_remote_map() -> Dict[str, str]:
    if not settings.remote_enabled:
        return {}
    try:
        return await asyncio.to_thread(load_remote_quadlets_fallback, settings.remote_configs_dir)
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# LOCAL update / rollback
# ---------------------------------------------------------------------------


async def execute_local_update(
    chat_id: str | int,
    message_id: int,
    stack_name: str,
    tag: str,
    original_header: str,
    image: str = "",
) -> None:
    from telegram_client import edit_telegram_message

    try:
        safe_stack = sanitize_stack_name(stack_name)
        safe_tag = sanitize_tag(tag)
    except Exception:
        await edit_telegram_message(
            settings.telegram_bot_token,
            chat_id,
            message_id,
            f"{original_header}\n\n❌ <b>Error:</b> invalid stack/tag.",
        )
        return

    try:
        stack_dir, compose_file, effective_stack = find_local_compose_file(
            settings.docker_stacks_dir, safe_stack
        )
    except Exception:
        await edit_telegram_message(
            settings.telegram_bot_token,
            chat_id,
            message_id,
            f"{original_header}\n\n❌ <b>Error:</b> invalid stack name.",
        )
        return

    node_name = settings.local_node_name
    if not compose_file or stack_dir is None:
        await edit_telegram_message(
            settings.telegram_bot_token,
            chat_id,
            message_id,
            f"{original_header}\n\n❌ <b>Error:</b> Compose file not found for "
            f"<code>{html.escape(safe_stack)}</code> in {html.escape(str(settings.docker_stacks_dir))}.",
        )
        return

    await edit_telegram_message(
        settings.telegram_bot_token,
        chat_id,
        message_id,
        f"{original_header}\n\n" + t("msg_updating", node=node_name, tag=safe_tag),
    )

    try:
        try:
            service_images = await update_services(stack_dir, compose_file, safe_tag, image)
            services = list(service_images)
        except ImageTagMismatch as exc:
            await edit_telegram_message(
                settings.telegram_bot_token,
                chat_id,
                message_id,
                f"{original_header}\n\n"
                + t(
                    "msg_pinned_tag",
                    configured=html.escape(", ".join(exc.configured)),
                    requested=html.escape(safe_tag),
                ),
            )
            return
        except ValueError:
            await edit_telegram_message(
                settings.telegram_bot_token,
                chat_id,
                message_id,
                f"{original_header}\n\n" + t("msg_config_mismatch"),
            )
            return
        pre_containers = [
            c
            for c in await inspect_local_containers(stack_dir, compose_file)
            if c.get("Service") in services
        ]
        if not pre_containers or {c.get("Service") for c in pre_containers} != set(services):
            raise RuntimeError("Cannot create a complete restore point")
        # Back up the running IDs onto the refs that the resolved configuration uses.
        # This also handles a Compose tag edited before clicking the notification.
        pre_containers = [dict(c, Image=service_images[c["Service"]]) for c in pre_containers]
        backups = await create_backups(effective_stack, pre_containers)
        if not backups:
            raise RuntimeError("Cannot create restore point")
        restore_id = action_store.save_restore_point("loc", effective_stack, backups, services)

        rc_pull, out_pull, err_pull = await _run(
            [
                "docker",
                "compose",
                "--project-directory",
                str(stack_dir),
                "-f",
                str(compose_file),
                "pull",
                *services,
            ],
            cwd=str(stack_dir),
            timeout=settings.docker_timeout_seconds,
        )
        if rc_pull != 0:
            err = err_pull or out_pull or "pull failed"
            await edit_telegram_message(
                settings.telegram_bot_token,
                chat_id,
                message_id,
                f"{original_header}\n\n" + t("msg_pull_error", error=html.escape(err[:1000])),
            )
            return

        rc_up, out_up, err_up = await _run(
            [
                "docker",
                "compose",
                "--project-directory",
                str(stack_dir),
                "-f",
                str(compose_file),
                "up",
                "-d",
                "--pull",
                "never",
                "--no-deps",
                *services,
            ],
            cwd=str(stack_dir),
            timeout=settings.docker_timeout_seconds,
        )
        if rc_up != 0:
            err = err_up or out_up or "up failed"
            logger.warning("Update %s: up failed, attempting auto-rollback", effective_stack)
            rolled_back = await _auto_rollback(stack_dir, compose_file, backups, services)
            await edit_telegram_message(
                settings.telegram_bot_token,
                chat_id,
                message_id,
                f"{original_header}\n\n"
                + t("msg_up_error", error=html.escape(err[:1000]))
                + "\n\n"
                + _rollback_message(rolled_back),
            )
            return

        await edit_telegram_message(
            settings.telegram_bot_token,
            chat_id,
            message_id,
            f"{original_header}\n\n"
            + t("msg_verifying", node=node_name, wait_sec=settings.healthcheck_wait_seconds),
        )
        has_failure, status_summary = await wait_for_healthy(
            stack_dir, compose_file, settings.healthcheck_wait_seconds, services=services
        )

        rb_id = action_store.register(
            "rb", "loc", effective_stack, safe_tag, original_header, restore_id=restore_id
        )
        dis_id = action_store.register("ccl", "loc", effective_stack, safe_tag, original_header)
        buttons = [
            [
                {"text": t("btn_rollback"), "callback_data": f"rb:{rb_id}"},
                {"text": t("btn_dismiss"), "callback_data": f"ccl:{dis_id}"},
            ]
        ]
        if has_failure is None:
            await edit_telegram_message(
                settings.telegram_bot_token,
                chat_id,
                message_id,
                f"{original_header}\n\n" + t("msg_verification_pending", status=status_summary),
                reply_markup={"inline_keyboard": buttons},
            )
        elif has_failure is False:
            await edit_telegram_message(
                settings.telegram_bot_token,
                chat_id,
                message_id,
                f"{original_header}\n\n"
                + t(
                    "msg_success",
                    node=node_name,
                    stack=effective_stack,
                    tag=safe_tag,
                    status=status_summary,
                ),
                reply_markup={"inline_keyboard": buttons},
            )
        else:
            logs_txt = await compose_logs(stack_dir, compose_file, 20)
            rolled_back = await _auto_rollback(stack_dir, compose_file, backups, services)
            await edit_telegram_message(
                settings.telegram_bot_token,
                chat_id,
                message_id,
                f"{original_header}\n\n"
                + t(
                    "msg_warning_header",
                    node=node_name,
                    stack=effective_stack,
                    status=status_summary,
                    logs=html.escape(logs_txt[-1200:]),
                )
                + "\n\n"
                + _rollback_message(rolled_back),
                reply_markup={"inline_keyboard": buttons},
            )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error("Exception during local update %s: %s", safe_stack, type(exc).__name__)
        await edit_telegram_message(
            settings.telegram_bot_token,
            chat_id,
            message_id,
            f"{original_header}\n\n❌ <b>Update error:</b> {html.escape(type(exc).__name__)}",
        )


async def _auto_rollback(
    stack_dir: Path,
    compose_file: Path,
    backups: Dict[str, str],
    services: Optional[list[str]] = None,
) -> bool | None:
    """Restore exact image references and fail if any command or health check fails."""
    if not backups:
        return False
    try:
        for original, backup in backups.items():
            rc, _, _ = await _run(["docker", "tag", backup, original], timeout=60)
            if rc:
                return False
        rc, _, _ = await _run(
            [
                "docker",
                "compose",
                "--project-directory",
                str(stack_dir),
                "-f",
                str(compose_file),
                "up",
                "-d",
                "--pull",
                "never",
                "--force-recreate",
                "--no-deps",
                *(services or []),
            ],
            cwd=str(stack_dir),
            timeout=settings.docker_timeout_seconds,
        )
        if rc:
            return False
        failure, _ = await wait_for_healthy(
            stack_dir, compose_file, settings.healthcheck_wait_seconds, services=services
        )
        return None if failure is None else not failure
    except Exception:
        return False


async def execute_local_rollback(
    chat_id: str | int, message_id: int, stack_name: str, tag: str, original_header: str,
    restore_id: str = "",
) -> None:
    from telegram_client import edit_telegram_message

    try:
        safe_stack = sanitize_stack_name(stack_name)
        stack_dir, compose_file, effective_stack = find_local_compose_file(
            settings.docker_stacks_dir, safe_stack
        )
        point = action_store.restore_point("loc", effective_stack)
        if not compose_file or not point:
            await edit_telegram_message(
                settings.telegram_bot_token,
                chat_id,
                message_id,
                f"{original_header}\n\n" + t("msg_rollback_not_found", tag=safe_stack),
            )
            return
        if not restore_id or restore_id != point.get("restore_id"):
            await edit_telegram_message(
                settings.telegram_bot_token, chat_id, message_id,
                f"{original_header}\n\n" + t("msg_rollback_stale"),
                reply_markup={"inline_keyboard": []},
            )
            return
        # Do not restore a snapshot onto a configuration edited since the update.
        services = set()
        for ref in point["backups"]:
            services.update(
                await update_services(stack_dir, compose_file, parse_image_and_tag(ref)[1], ref)
            )
        if services != set(point["services"]):
            raise ValueError("Compose services have changed since the restore point")
        await edit_telegram_message(
            settings.telegram_bot_token,
            chat_id,
            message_id,
            f"{original_header}\n\n" + t("msg_rollback_in_progress"),
        )
        ok = await _auto_rollback(stack_dir, compose_file, point["backups"], point["services"])
        await edit_telegram_message(
            settings.telegram_bot_token,
            chat_id,
            message_id,
            f"{original_header}\n\n" + _manual_rollback_message(ok, safe_stack),
            reply_markup={"inline_keyboard": []},
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error("Local rollback failed: %s", type(exc).__name__)
        await edit_telegram_message(
            settings.telegram_bot_token,
            chat_id,
            message_id,
            f"{original_header}\n\n" + t("msg_auto_rollback_failed"),
        )


# ---------------------------------------------------------------------------
# REMOTE update / rollback
# ---------------------------------------------------------------------------


async def _restore_remote(host: str, svc: str, backups: Dict[str, str]) -> bool | None:
    if not backups:
        return False
    for original in backups:
        if not await remote_service_uses_image(host, svc, original, settings.ssh_timeout_seconds):
            return False
    for original, backup in backups.items():
        if not await remote_tag_image(host, backup, original, settings.ssh_timeout_seconds):
            return False
    rc, _, _ = await remote_restart_service(host, svc, 60)
    return rc == 0 and await remote_wait_for_healthy(
        host, svc, settings.healthcheck_wait_seconds, settings.ssh_timeout_seconds
    )


async def execute_remote_update(
    chat_id: str | int,
    message_id: int,
    service_name: str,
    tag: str,
    original_header: str,
    image: str = "",
) -> None:
    from telegram_client import edit_telegram_message

    try:
        svc = sanitize_service_name(service_name)
        safe_tag = sanitize_tag(tag)
        host = settings.remote_ssh_host
        current = await remote_inspect_image(host, svc, settings.ssh_timeout_seconds)
        if not current or (image and not same_repository(current, image)):
            message = t("msg_config_mismatch")
        elif "@" in current or parse_image_and_tag(current)[1] != safe_tag:
            message = t(
                "msg_pinned_tag", configured=html.escape(current), requested=html.escape(safe_tag)
            )
        elif not await remote_service_uses_image(host, svc, current, settings.ssh_timeout_seconds):
            message = t("msg_remote_unit_mismatch")
        else:
            message = ""
        if message:
            await edit_telegram_message(
                settings.telegram_bot_token, chat_id, message_id, f"{original_header}\n\n{message}"
            )
            return
        await edit_telegram_message(
            settings.telegram_bot_token,
            chat_id,
            message_id,
            f"{original_header}\n\n"
            + t("msg_updating", node=settings.remote_node_name, tag=safe_tag),
        )
        image_id = await remote_inspect_image_id(host, svc, settings.ssh_timeout_seconds)
        last_colon = current.rfind(":")
        repo = current[:last_colon] if last_colon > current.rfind("/") else current
        backup = f"{repo}:diun-backup-{svc}-{time.strftime('%Y%m%d%H%M%S')}"
        if not image_id or not await remote_tag_image(
            host, image_id, backup, settings.ssh_timeout_seconds
        ):
            raise RuntimeError("Cannot create restore point")
        backups = {current: backup}
        restore_id = action_store.save_restore_point("rem", svc, backups)
        await remote_prune_backups(host, repo, svc, settings.ssh_timeout_seconds)
        rc, out, err = await remote_pull_image(host, current, settings.docker_timeout_seconds)
        if rc:
            await edit_telegram_message(
                settings.telegram_bot_token,
                chat_id,
                message_id,
                f"{original_header}\n\n"
                + t("msg_pull_error", error=html.escape((err or out)[:1000])),
            )
            return
        rc, out, err = await remote_restart_service(host, svc, 60)
        healthy = rc == 0 and await remote_wait_for_healthy(
            host, svc, settings.healthcheck_wait_seconds, settings.ssh_timeout_seconds
        )
        rb_id = action_store.register(
            "rb", "rem", svc, safe_tag, original_header, restore_id=restore_id
        )
        dis_id = action_store.register("ccl", "rem", svc, safe_tag, original_header)
        buttons = {
            "inline_keyboard": [
                [
                    {"text": t("btn_rollback"), "callback_data": f"rb:{rb_id}"},
                    {"text": t("btn_dismiss"), "callback_data": f"ccl:{dis_id}"},
                ]
            ]
        }
        if healthy is None:
            text = t("msg_verification_pending", status=html.escape(svc))
        elif healthy is True:
            text = t(
                "msg_success",
                node=settings.remote_node_name,
                stack=svc,
                tag=safe_tag,
                status="running / healthy",
            )
        else:
            logs = await remote_logs(host, svc, settings.ssh_timeout_seconds)
            ok = await _restore_remote(host, svc, backups)
            text = t(
                "msg_warning_header",
                node=settings.remote_node_name,
                stack=svc,
                status=html.escape((err or out or "health check failed")[:500]),
                logs=html.escape(logs[-1200:]),
            )
            text += "\n\n" + _rollback_message(ok)
        await edit_telegram_message(
            settings.telegram_bot_token,
            chat_id,
            message_id,
            f"{original_header}\n\n{text}",
            reply_markup=buttons,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error("Remote update failed: %s", type(exc).__name__)
        await edit_telegram_message(
            settings.telegram_bot_token,
            chat_id,
            message_id,
            f"{original_header}\n\n❌ Remote update error: {html.escape(type(exc).__name__)}",
        )


async def execute_remote_rollback(
    chat_id: str | int, message_id: int, service_name: str, tag: str, original_header: str,
    restore_id: str = "",
) -> None:
    from telegram_client import edit_telegram_message

    try:
        svc = sanitize_service_name(service_name)
        point = action_store.restore_point("rem", svc)
        if not point:
            await edit_telegram_message(
                settings.telegram_bot_token,
                chat_id,
                message_id,
                f"{original_header}\n\n" + t("msg_rollback_not_found", tag=svc),
            )
            return
        if not restore_id or restore_id != point.get("restore_id"):
            await edit_telegram_message(
                settings.telegram_bot_token, chat_id, message_id,
                f"{original_header}\n\n" + t("msg_rollback_stale"),
                reply_markup={"inline_keyboard": []},
            )
            return
        current = await remote_inspect_image(
            settings.remote_ssh_host, svc, settings.ssh_timeout_seconds
        )
        if current not in point["backups"]:
            raise ValueError("Remote image changed since the restore point")
        await edit_telegram_message(
            settings.telegram_bot_token,
            chat_id,
            message_id,
            f"{original_header}\n\n" + t("msg_rollback_in_progress"),
        )
        ok = await _restore_remote(settings.remote_ssh_host, svc, point["backups"])
        await edit_telegram_message(
            settings.telegram_bot_token,
            chat_id,
            message_id,
            f"{original_header}\n\n" + _manual_rollback_message(ok, svc),
            reply_markup={"inline_keyboard": []},
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error("Remote rollback failed: %s", type(exc).__name__)
        await edit_telegram_message(
            settings.telegram_bot_token,
            chat_id,
            message_id,
            f"{original_header}\n\n" + t("msg_auto_rollback_failed"),
        )


# ---------------------------------------------------------------------------
# DIUN notification
# ---------------------------------------------------------------------------

async def process_diun_notification(payload: DiunWebhookPayload) -> None:
    img_name, tag = parse_image_and_tag(payload.image)
    logger.info("Received DIUN webhook: image=%s, tag=%s, status=%s", img_name, tag, payload.status)

    # Pin/freeze filters: frozen images never produce notifications (checked
    # before dedup so they don't pollute the cache either).
    from filters import match_excluded_image, normalize_patterns

    excluded_hit = match_excluded_image(
        payload.image, img_name, normalize_patterns(settings.exclude_images)
    )
    if excluded_hit:
        logger.info("Notification skipped (EXCLUDE_IMAGES=%s) for %s", excluded_hit, payload.image)
        return

    dedup_key = f"{payload.image}:{tag}:{payload.status}"
    if not await dedup.reserve(dedup_key):
        logger.info("Duplicate notification ignored for %s", dedup_key)
        return

    delivered = False
    try:
        delivered = await _send_diun_notification(payload, img_name, tag)
    finally:
        await dedup.finish(dedup_key, delivered)


async def _send_diun_notification(payload: DiunWebhookPayload, img_name: str, tag: str) -> bool:
    from telegram_client import send_telegram_message
    from filters import match_pinned_target, normalize_patterns

    github_repo = resolve_github_repo(img_name, IMAGE_MAPPINGS)
    release_info = None
    if github_repo:
        release_info = await get_github_release_notes(
            github_repo, tag, settings.github_token
        )

    remote_map = await _load_remote_map()
    targets = await find_targets_for_image(
        img_name, settings.docker_stacks_dir,
        remote_enabled=settings.remote_enabled, remote_map=remote_map,
    )
    local_stack = targets.get("local")
    remote_service = targets.get("remote")

    pinned_hit = match_pinned_target(
        local_stack, remote_service, normalize_patterns(settings.pinned_stacks)
    )
    if pinned_hit:
        logger.info("Notification skipped (PINNED_STACKS, target=%s) for %s", pinned_hit, payload.image)
        return False

    title_key = "title_update" if payload.status == "update" else "title_new"
    msg_lines = [
        f"<b>{t(title_key)}</b>",
        "",
        f"{t('lbl_image')} <code>{html.escape(payload.image)}</code>",
        f"{t('lbl_tag')} <code>{html.escape(tag)}</code>",
    ]
    if local_stack:
        msg_lines.append(
            f"{t('lbl_stack', node=settings.local_node_name)} "
            f"<code>{html.escape(local_stack)}</code>"
        )
    if remote_service and settings.remote_enabled:
        msg_lines.append(
            f"{t('lbl_service', node=settings.remote_node_name)} "
            f"<code>{html.escape(remote_service)}</code>"
        )

    if release_info:
        rel_name = release_info.get("name") or release_info.get("tag_name") or "Release Notes"
        rel_url = release_info.get("html_url") or ""
        rel_body = release_info.get("body", "") or ""
        msg_lines.append("")
        if rel_url:
            msg_lines.append(
                f"{t('lbl_changelog')} <a href='{html.escape(rel_url)}'>{html.escape(rel_name)}</a>"
            )
        else:
            msg_lines.append(f"{t('lbl_changelog')} {html.escape(rel_name)}")
        if rel_body:
            formatted_notes = format_markdown_for_telegram(rel_body, max_length=1500)
            if formatted_notes:
                msg_lines.append(formatted_notes)
    elif payload.hub_link:
        msg_lines.append(
            f"{t('lbl_dockerhub')} <a href='{html.escape(payload.hub_link)}'>{html.escape(img_name)}</a>"
        )

    full_message = "\n".join(msg_lines)
    header_plain = f"{t(title_key)} {img_name}:{tag}"

    buttons = []
    if local_stack and remote_service and settings.remote_enabled:
        up_loc = action_store.register("upd", "loc", local_stack, tag, header_plain, image=payload.image)
        up_rem = action_store.register("upd", "rem", remote_service, tag, header_plain, image=payload.image)
        skip = action_store.register("ccl", "all", local_stack, tag, header_plain, image=payload.image)
        buttons.append([
            {"text": t("btn_update_node", node=settings.local_node_name),
             "callback_data": f"upd:{up_loc}"},
            {"text": t("btn_update_remote", node=settings.remote_node_name),
             "callback_data": f"upd:{up_rem}"},
        ])
        buttons.append([{"text": t("btn_skip"), "callback_data": f"ccl:{skip}"}])
    elif local_stack:
        up_loc = action_store.register("upd", "loc", local_stack, tag, header_plain, image=payload.image)
        skip = action_store.register("ccl", "loc", local_stack, tag, header_plain, image=payload.image)
        buttons.append([
            {"text": t("btn_update_now"), "callback_data": f"upd:{up_loc}"},
            {"text": t("btn_skip"), "callback_data": f"ccl:{skip}"},
        ])
    elif remote_service and settings.remote_enabled:
        up_rem = action_store.register("upd", "rem", remote_service, tag, header_plain, image=payload.image)
        skip = action_store.register("ccl", "rem", remote_service, tag, header_plain, image=payload.image)
        buttons.append([
            {"text": t("btn_update_remote", node=settings.remote_node_name),
             "callback_data": f"upd:{up_rem}"},
            {"text": t("btn_skip"), "callback_data": f"ccl:{skip}"},
        ])

    if release_info and release_info.get("html_url"):
        buttons.append([{"text": t("btn_open_github"), "url": release_info["html_url"]}])

    reply_markup = {"inline_keyboard": buttons} if buttons else None
    result = await send_telegram_message(
        settings.telegram_bot_token, settings.telegram_chat_id,
        full_message, reply_markup=reply_markup,
    )
    return bool(result and result.get("ok"))


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global is_polling_active
    is_polling_active = True

    def _active() -> bool:
        return is_polling_active

    def _dismiss(name: str) -> str:
        return t("msg_skipped", name=name)

    poll_task = asyncio.create_task(
        telegram_polling_loop(
            settings.telegram_bot_token,
            settings.telegram_allowed_user_ids,
            settings.local_node_name,
            settings.remote_node_name,
            _active,
            execute_local_update,
            execute_local_rollback,
            execute_remote_update,
            execute_remote_rollback,
            _dismiss,
            t,
        )
    )
    yield
    is_polling_active = False
    poll_task.cancel()
    try:
        await poll_task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="DiunTap - Interactive Docker Updates for DIUN", version="1.1.0", lifespan=lifespan)


@app.post("/webhook/diun")
async def webhook_diun(request: Request):
    # Optional shared-secret auth with graceful legacy fallback.
    if settings.webhook_auth_enabled:
        provided = request.headers.get("X-Diun-Secret", "") or request.query_params.get("token", "")
        try:
            verify_webhook_secret(provided, settings.diun_webhook_secret)
        except Exception:
            logger.warning("Rejected DIUN webhook: invalid secret")
            raise HTTPException(status_code=401, detail="Invalid webhook secret")

    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 8192:
            raise HTTPException(status_code=413, detail="Payload too large")
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Invalid JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Payload must be a JSON object")

    try:
        payload = DiunWebhookPayload(**body)
    except PydanticValidationError as exc:
        logger.warning("Invalid webhook payload rejected")
        raise HTTPException(status_code=400, detail=f"Invalid payload: {exc}")

    asyncio.create_task(process_diun_notification(payload))
    return {"status": "accepted", "image": payload.image}


@app.get("/")
async def root():
    return await health()


@app.get("/health")
async def health():
    # Minimal public surface: no tokens, no hostnames, no filesystem paths.
    return {
        "status": "ok",
        "app": "DiunTap",
        "version": "1.1.0",
        "telegram_configured": settings.telegram_configured,
        "github_token_configured": bool(settings.github_token),
        "webhook_auth_enabled": settings.webhook_auth_enabled,
        "local_node": {"name": settings.local_node_name},
        "remote_node": {"enabled": settings.remote_enabled, "name": settings.remote_node_name},
    }


@app.get("/ready")
async def ready(request: Request):
    # Detailed readiness: localhost only, or webhook secret bearer.
    client_host = (request.client.host if request.client else "")
    authorized = client_host in ("127.0.0.1", "::1")
    if not authorized and settings.webhook_auth_enabled:
        provided = request.headers.get("X-Diun-Secret", "") or request.query_params.get("token", "")
        try:
            verify_webhook_secret(provided, settings.diun_webhook_secret)
            authorized = True
        except Exception:
            authorized = False
    if not authorized:
        raise HTTPException(status_code=403, detail="Forbidden")

    return {
        "status": "ok",
        "app": "DiunTap",
        "version": "1.1.0",
        "telegram_configured": settings.telegram_configured,
        "github_token_configured": bool(settings.github_token),
        "webhook_auth_enabled": settings.webhook_auth_enabled,
        "local_node": {
            "name": settings.local_node_name,
            "stacks_dir": str(settings.docker_stacks_dir),
            "stacks_dir_exists": settings.docker_stacks_dir.exists(),
        },
        "remote_node": {
            "enabled": settings.remote_enabled,
            "name": settings.remote_node_name,
            "configs_dir_exists": settings.remote_configs_dir.exists() if settings.remote_enabled else False,
        },
        "healthcheck_wait_seconds": settings.healthcheck_wait_seconds,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=settings.webhook_port, reload=False)

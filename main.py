"""DiunTap FastAPI entrypoint: webhook, Telegram actions, Docker orchestration."""

from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from pydantic import ValidationError as PydanticValidationError

from config import IMAGE_MAPPINGS, settings
from dedup import DedupCache
from docker_local import (
    _run,
    compose_logs,
    create_backups,
    describe_containers,
    find_local_compose_file,
    find_targets_for_image,
    inspect_local_containers,
    wait_for_healthy,
)
from formatters import format_markdown_for_telegram
from github_client import get_github_release_notes
from i18n import t
from mappings import load_remote_quadlets_fallback, resolve_github_repo
from models import DiunWebhookPayload, action_store, parse_image_and_tag
from polling import telegram_polling_loop
from remote_ssh import (
    remote_container_state,
    remote_inspect_image,
    remote_logs,
    remote_pull_image,
    remote_restart_service,
    remote_service_active,
    remote_tag_image,
    run_remote_ssh,
)
from security import (
    is_telegram_user_allowed,
    sanitize_service_name,
    sanitize_stack_name,
    sanitize_tag,
    verify_webhook_secret,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("diuntap")

dedup = DedupCache(maxsize=512, ttl=settings.dedup_window_seconds)
is_polling_active = True


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _display_short(value: str, limit: int) -> str:
    value = value or ""
    return value if len(value) <= limit else value[:limit] + "…"


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
    chat_id: str | int, message_id: int, stack_name: str, tag: str, original_header: str
) -> None:
    from telegram_client import edit_telegram_message

    try:
        safe_stack = sanitize_stack_name(stack_name)
        safe_tag = sanitize_tag(tag)
    except Exception:
        await edit_telegram_message(
            settings.telegram_bot_token, chat_id, message_id,
            f"{original_header}\n\n❌ <b>Error:</b> invalid stack/tag.",
        )
        return

    try:
        stack_dir, compose_file, effective_stack = find_local_compose_file(
            settings.docker_stacks_dir, safe_stack
        )
    except Exception:
        await edit_telegram_message(
            settings.telegram_bot_token, chat_id, message_id,
            f"{original_header}\n\n❌ <b>Error:</b> invalid stack name.",
        )
        return

    node_name = settings.local_node_name
    if not compose_file or stack_dir is None:
        await edit_telegram_message(
            settings.telegram_bot_token, chat_id, message_id,
            f"{original_header}\n\n❌ <b>Error:</b> Compose file not found for "
            f"<code>{html.escape(safe_stack)}</code> in {html.escape(str(settings.docker_stacks_dir))}.",
        )
        return

    await edit_telegram_message(
        settings.telegram_bot_token, chat_id, message_id,
        f"{original_header}\n\n" + t("msg_updating", node=node_name, tag=safe_tag),
    )

    try:
        pre_containers = await inspect_local_containers(stack_dir, compose_file)
        backups = await create_backups(effective_stack, pre_containers)

        rc_pull, out_pull, err_pull = await _run(
            ["docker", "compose", "--project-directory", str(stack_dir),
             "-f", str(compose_file), "pull"],
            cwd=str(stack_dir), timeout=settings.docker_timeout_seconds,
        )
        if rc_pull != 0:
            err = (err_pull or out_pull or "pull failed")
            await edit_telegram_message(
                settings.telegram_bot_token, chat_id, message_id,
                f"{original_header}\n\n" + t("msg_pull_error", error=html.escape(err[:1000])),
            )
            return

        rc_up, out_up, err_up = await _run(
            ["docker", "compose", "--project-directory", str(stack_dir),
             "-f", str(compose_file), "up", "-d", "--remove-orphans"],
            cwd=str(stack_dir), timeout=settings.docker_timeout_seconds,
        )
        if rc_up != 0:
            err = (err_up or out_up or "up failed")
            logger.warning("Update %s: up failed, attempting auto-rollback", effective_stack)
            await _auto_rollback(stack_dir, compose_file, backups)
            await edit_telegram_message(
                settings.telegram_bot_token, chat_id, message_id,
                f"{original_header}\n\n" + t("msg_up_error", error=html.escape(err[:1000])),
            )
            return

        await edit_telegram_message(
            settings.telegram_bot_token, chat_id, message_id,
            f"{original_header}\n\n"
            + t("msg_verifying", node=node_name, wait_sec=settings.healthcheck_wait_seconds),
        )
        has_failure, status_summary = await wait_for_healthy(
            stack_dir, compose_file, settings.healthcheck_wait_seconds
        )

        rb_id = action_store.register("rb", "loc", effective_stack, safe_tag, original_header)
        dis_id = action_store.register("ccl", "loc", effective_stack, safe_tag, original_header)
        buttons = [[
            {"text": t("btn_rollback"), "callback_data": f"rb:{rb_id}"},
            {"text": t("btn_dismiss"), "callback_data": f"ccl:{dis_id}"},
        ]]
        if not has_failure:
            await edit_telegram_message(
                settings.telegram_bot_token, chat_id, message_id,
                f"{original_header}\n\n"
                + t("msg_success", node=node_name, stack=effective_stack, tag=safe_tag, status=status_summary),
                reply_markup={"inline_keyboard": buttons},
            )
        else:
            logs_txt = await compose_logs(stack_dir, compose_file, 20)
            await edit_telegram_message(
                settings.telegram_bot_token, chat_id, message_id,
                f"{original_header}\n\n"
                + t("msg_warning_header", node=node_name, stack=effective_stack,
                    status=status_summary, logs=html.escape(logs_txt[-1200:])),
                reply_markup={"inline_keyboard": buttons},
            )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error("Exception during local update %s: %s", safe_stack, type(exc).__name__)
        await edit_telegram_message(
            settings.telegram_bot_token, chat_id, message_id,
            f"{original_header}\n\n❌ <b>Update error:</b> {html.escape(type(exc).__name__)}",
        )


async def _auto_rollback(
    stack_dir: Path, compose_file: Path, backups: Dict[str, str]
) -> bool:
    """Best-effort restore of backup tags + `up -d --no-pull`. Returns health."""
    try:
        for original, backup in backups.items():
            await _run(["docker", "tag", backup, original], timeout=60)
        await _run(
            ["docker", "compose", "--project-directory", str(stack_dir),
             "-f", str(compose_file), "up", "-d", "--no-pull", "--remove-orphans"],
            cwd=str(stack_dir), timeout=settings.docker_timeout_seconds,
        )
        await asyncio.sleep(4)
        containers = await inspect_local_containers(stack_dir, compose_file)
        failure, _ = describe_containers(containers)
        return bool(containers) and not failure
    except Exception:
        return False


async def execute_local_rollback(
    chat_id: str | int, message_id: int, stack_name: str, tag: str, original_header: str
) -> None:
    from telegram_client import edit_telegram_message

    try:
        safe_stack = sanitize_stack_name(stack_name)
    except Exception:
        await edit_telegram_message(
            settings.telegram_bot_token, chat_id, message_id,
            f"{original_header}\n\n❌ Invalid stack name.",
        )
        return

    try:
        stack_dir, compose_file, effective_stack = find_local_compose_file(
            settings.docker_stacks_dir, safe_stack
        )
    except Exception:
        await edit_telegram_message(
            settings.telegram_bot_token, chat_id, message_id,
            f"{original_header}\n\n❌ Invalid stack name.",
        )
        return

    if not compose_file or stack_dir is None:
        await edit_telegram_message(
            settings.telegram_bot_token, chat_id, message_id,
            f"{original_header}\n\n❌ Compose file not found for {html.escape(safe_stack)}.",
        )
        return

    await edit_telegram_message(
        settings.telegram_bot_token, chat_id, message_id,
        f"{original_header}\n\n" + t("msg_rollback_in_progress"),
    )

    try:
        rc, out, _ = await _run(
            ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"], timeout=60
        )
        all_imgs = out.splitlines() if rc == 0 else []
        prefix = f":diun-backup-{effective_stack}-"
        candidates = sorted([i.strip() for i in all_imgs if prefix in i], reverse=True)

        by_repo: Dict[str, str] = {}
        for ref in candidates:
            repo = ref.rsplit(":", 1)[0]
            if repo not in by_repo:
                by_repo[repo] = ref

        if not by_repo:
            # legacy single-tag fallback
            legacy = [i.strip() for i in all_imgs if i.strip().endswith(f":diun-backup-{effective_stack}")]
            for ref in legacy:
                repo = ref.rsplit(":", 1)[0]
                if repo not in by_repo:
                    by_repo[repo] = ref

        if not by_repo:
            await edit_telegram_message(
                settings.telegram_bot_token, chat_id, message_id,
                f"{original_header}\n\n"
                + t("msg_rollback_not_found", tag=f"diun-backup-{effective_stack}-*"),
            )
            return

        # Current images in stack define which repos to restore (multi-service safe).
        current = await inspect_local_containers(stack_dir, compose_file)
        current_repos = set()
        for c in current:
            img = c.get("Image") or ""
            if img and ":diun-backup-" not in img:
                current_repos.add(img.rsplit(":", 1)[0] if ":" in img else img)

        restored = 0
        for repo, backup_ref in by_repo.items():
            if current_repos and repo not in current_repos:
                continue
            # Restore the tag the compose file actually references: find current
            # image ref for this repo, else fall back to repo:latest naming.
            target = None
            for c in current:
                img = c.get("Image") or ""
                r = img.rsplit(":", 1)[0] if ":" in img else img
                if r == repo:
                    target = img
                    break
            if target is None:
                target = f"{repo}:latest"
            rc_tag, _, _ = await _run(["docker", "tag", backup_ref, target], timeout=60)
            if rc_tag == 0:
                restored += 1

        if restored == 0:
            await edit_telegram_message(
                settings.telegram_bot_token, chat_id, message_id,
                f"{original_header}\n\n"
                + t("msg_rollback_not_found", tag=f"diun-backup-{effective_stack}-*"),
            )
            return

        await _run(
            ["docker", "compose", "--project-directory", str(stack_dir),
             "-f", str(compose_file), "up", "-d", "--no-pull", "--force-recreate", "--remove-orphans"],
            cwd=str(stack_dir), timeout=settings.docker_timeout_seconds,
        )
        await asyncio.sleep(4)
        post = await inspect_local_containers(stack_dir, compose_file)
        failure, _ = describe_containers(post)
        if post and not failure:
            await edit_telegram_message(
                settings.telegram_bot_token, chat_id, message_id,
                f"{original_header}\n\n" + t("msg_rollback_success", stack=effective_stack),
                reply_markup={"inline_keyboard": []},
            )
        else:
            await edit_telegram_message(
                settings.telegram_bot_token, chat_id, message_id,
                f"{original_header}\n\n" + t("msg_rollback_warning", stack=effective_stack),
                reply_markup={"inline_keyboard": []},
            )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error("Exception during rollback %s: %s", safe_stack, type(exc).__name__)
        await edit_telegram_message(
            settings.telegram_bot_token, chat_id, message_id,
            f"{original_header}\n\n❌ Rollback error: {html.escape(type(exc).__name__)}",
        )


# ---------------------------------------------------------------------------
# REMOTE update / rollback
# ---------------------------------------------------------------------------

async def execute_remote_update(
    chat_id: str | int, message_id: int, service_name: str, tag: str, original_header: str
) -> None:
    from telegram_client import edit_telegram_message

    try:
        svc = sanitize_service_name(service_name)
        safe_tag = sanitize_tag(tag)
    except Exception:
        await edit_telegram_message(
            settings.telegram_bot_token, chat_id, message_id,
            f"{original_header}\n\n❌ Invalid service/tag.",
        )
        return

    node_name = settings.remote_node_name
    host = settings.remote_ssh_host
    await edit_telegram_message(
        settings.telegram_bot_token, chat_id, message_id,
        f"{original_header}\n\n" + t("msg_updating", node=node_name, tag=safe_tag),
    )

    try:
        current_image = await remote_inspect_image(host, svc, settings.ssh_timeout_seconds)
        # FIX: pull the NEW tag, not the stale inspected ref. The tag from DIUN
        # names the new version; current_image only tells us the repository.
        if current_image:
            repo = current_image.rsplit(":", 1)[0] if ":" in current_image else current_image
            new_image = f"{repo}:{safe_tag}" if safe_tag != "latest" else current_image
        else:
            new_image = ""

        backup_name = ""
        if current_image:
            repo = current_image.rsplit(":", 1)[0] if ":" in current_image else current_image
            ts = time.strftime("%Y%m%d%H%M%S")
            backup_name = f"{repo}:diun-backup-{svc}-{ts}"
            await remote_tag_image(host, current_image, backup_name, settings.ssh_timeout_seconds)

        if new_image:
            rc_pull, out_pull, err_pull = await remote_pull_image(
                host, new_image, settings.docker_timeout_seconds
            )
            if rc_pull != 0:
                err_msg = err_pull or out_pull or "pull failed"
                await edit_telegram_message(
                    settings.telegram_bot_token, chat_id, message_id,
                    f"{original_header}\n\n❌ <b>Pull error on {html.escape(node_name)}:</b>\n"
                    f"<pre>{html.escape(err_msg[:1000])}</pre>",
                )
                return
        else:
            logger.warning("Remote update %s: could not determine image, restarting only", svc)

        rc_up, out_up, err_up = await remote_restart_service(host, svc, 60)
        if rc_up != 0:
            err_msg = err_up or out_up or "Service restart failed"
            await edit_telegram_message(
                settings.telegram_bot_token, chat_id, message_id,
                f"{original_header}\n\n❌ <b>Update error on {html.escape(node_name)}:</b>\n"
                f"<pre>{html.escape(err_msg[:1000])}</pre>",
            )
            return

        await edit_telegram_message(
            settings.telegram_bot_token, chat_id, message_id,
            f"{original_header}\n\n"
            + t("msg_verifying", node=node_name, wait_sec=settings.healthcheck_wait_seconds),
        )
        await asyncio.sleep(settings.healthcheck_wait_seconds)

        active = await remote_service_active(host, svc, settings.ssh_timeout_seconds)
        p_state, p_health = await remote_container_state(host, svc, settings.ssh_timeout_seconds)
        is_healthy = (
            active == "active"
            and p_state in ("running",)
            and p_health != "unhealthy"
        )

        rb_id = action_store.register("rb", "rem", svc, safe_tag, original_header)
        dis_id = action_store.register("ccl", "rem", svc, safe_tag, original_header)
        buttons = [[
            {"text": t("btn_rollback"), "callback_data": f"rb:{rb_id}"},
            {"text": t("btn_dismiss"), "callback_data": f"ccl:{dis_id}"},
        ]]
        if is_healthy:
            status_desc = f"Active (State: {p_state}, Health: {p_health})"
            await edit_telegram_message(
                settings.telegram_bot_token, chat_id, message_id,
                f"{original_header}\n\n"
                + t("msg_success", node=node_name, stack=svc, tag=safe_tag, status=html.escape(status_desc)),
                reply_markup={"inline_keyboard": buttons},
            )
        else:
            logs_txt = await remote_logs(host, svc, settings.ssh_timeout_seconds)
            status_desc = f"Service: {active} (Podman: {p_state})"
            await edit_telegram_message(
                settings.telegram_bot_token, chat_id, message_id,
                f"{original_header}\n\n"
                + t("msg_warning_header", node=node_name, stack=svc,
                    status=html.escape(status_desc), logs=html.escape(logs_txt[-1200:])),
                reply_markup={"inline_keyboard": buttons},
            )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error("Exception during remote update %s: %s", service_name, type(exc).__name__)
        await edit_telegram_message(
            settings.telegram_bot_token, chat_id, message_id,
            f"{original_header}\n\n❌ <b>Remote update error:</b> {html.escape(type(exc).__name__)}",
        )


async def execute_remote_rollback(
    chat_id: str | int, message_id: int, service_name: str, tag: str, original_header: str
) -> None:
    from telegram_client import edit_telegram_message

    try:
        svc = sanitize_service_name(service_name)
    except Exception:
        await edit_telegram_message(
            settings.telegram_bot_token, chat_id, message_id,
            f"{original_header}\n\n❌ Invalid service name.",
        )
        return

    host = settings.remote_ssh_host
    await edit_telegram_message(
        settings.telegram_bot_token, chat_id, message_id,
        f"{original_header}\n\n" + t("msg_rollback_in_progress"),
    )

    try:
        current_image = await remote_inspect_image(host, svc, settings.ssh_timeout_seconds)
        if not current_image:
            await edit_telegram_message(
                settings.telegram_bot_token, chat_id, message_id,
                f"{original_header}\n\n❌ Image not found for {html.escape(svc)}.",
            )
            return

        repo = current_image.rsplit(":", 1)[0] if ":" in current_image else current_image
        rc, out, _ = await run_remote_ssh(
            host, "podman images --format '{{.Repository}}:{{.Tag}}'",
            settings.ssh_timeout_seconds,
        )
        prefix = f":diun-backup-{svc}-"
        candidates = sorted(
            [l.strip() for l in (out.splitlines() if rc == 0 else []) if prefix in l],
            reverse=True,
        )
        if not candidates:
            rc2, out2, _ = await run_remote_ssh(
                host, "podman images --format '{{.Repository}}:{{.Tag}}'",
                settings.ssh_timeout_seconds,
            )
            legacy = [l.strip() for l in (out2.splitlines() if rc2 == 0 else [])
                      if l.strip().endswith(f":diun-backup-{svc}")]
            candidates = legacy
        if not candidates:
            await edit_telegram_message(
                settings.telegram_bot_token, chat_id, message_id,
                f"{original_header}\n\n❌ <b>Rollback failed:</b> no backup for {html.escape(svc)}.",
            )
            return

        # Retag newest backup onto the CURRENT image ref (what the Quadlet runs),
        # then restart. No pull involved.
        newest = candidates[0]
        if newest.rsplit(":", 1)[0] != repo:
            repo = newest.rsplit(":", 1)[0]
        ok = await remote_tag_image(host, newest, current_image, settings.ssh_timeout_seconds)
        if not ok:
            await edit_telegram_message(
                settings.telegram_bot_token, chat_id, message_id,
                f"{original_header}\n\n❌ <b>Rollback error on {html.escape(settings.remote_node_name)}:</b>\n"
                f"<pre>retag failed</pre>",
            )
            return
        rc_rb, _, err_rb = await remote_restart_service(host, svc, 60)
        if rc_rb != 0:
            await edit_telegram_message(
                settings.telegram_bot_token, chat_id, message_id,
                f"{original_header}\n\n❌ <b>Rollback error on {html.escape(settings.remote_node_name)}:</b>\n"
                f"<pre>{html.escape((err_rb or 'restart failed')[:1000])}</pre>",
            )
            return

        await asyncio.sleep(4)
        active = await remote_service_active(host, svc, settings.ssh_timeout_seconds)
        if active == "active":
            await edit_telegram_message(
                settings.telegram_bot_token, chat_id, message_id,
                f"{original_header}\n\n" + t("msg_rollback_success", stack=svc),
                reply_markup={"inline_keyboard": []},
            )
        else:
            await edit_telegram_message(
                settings.telegram_bot_token, chat_id, message_id,
                f"{original_header}\n\n" + t("msg_rollback_warning", stack=svc),
                reply_markup={"inline_keyboard": []},
            )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error("Exception during remote rollback %s: %s", service_name, type(exc).__name__)
        await edit_telegram_message(
            settings.telegram_bot_token, chat_id, message_id,
            f"{original_header}\n\n❌ Rollback error: {html.escape(type(exc).__name__)}",
        )


# ---------------------------------------------------------------------------
# DIUN notification
# ---------------------------------------------------------------------------

async def process_diun_notification(payload: DiunWebhookPayload) -> None:
    from telegram_client import send_telegram_message

    img_name, tag = parse_image_and_tag(payload.image)
    logger.info("Received DIUN webhook: image=%s, tag=%s, status=%s", img_name, tag, payload.status)

    dedup_key = f"{payload.image}:{tag}:{payload.status}"
    if await dedup.check_and_mark(dedup_key):
        logger.info("Duplicate notification ignored for %s", dedup_key)
        return

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
        up_loc = action_store.register("upd", "loc", local_stack, tag, header_plain)
        up_rem = action_store.register("upd", "rem", remote_service, tag, header_plain)
        skip = action_store.register("ccl", "all", local_stack, tag, header_plain)
        buttons.append([
            {"text": t("btn_update_node", node=settings.local_node_name),
             "callback_data": f"upd:{up_loc}"},
            {"text": t("btn_update_remote", node=settings.remote_node_name),
             "callback_data": f"upd:{up_rem}"},
        ])
        buttons.append([{"text": t("btn_skip"), "callback_data": f"ccl:{skip}"}])
    elif local_stack:
        up_loc = action_store.register("upd", "loc", local_stack, tag, header_plain)
        skip = action_store.register("ccl", "loc", local_stack, tag, header_plain)
        buttons.append([
            {"text": t("btn_update_now"), "callback_data": f"upd:{up_loc}"},
            {"text": t("btn_skip"), "callback_data": f"ccl:{skip}"},
        ])
    elif remote_service and settings.remote_enabled:
        up_rem = action_store.register("upd", "rem", remote_service, tag, header_plain)
        skip = action_store.register("ccl", "rem", remote_service, tag, header_plain)
        buttons.append([
            {"text": t("btn_update_remote", node=settings.remote_node_name),
             "callback_data": f"upd:{up_rem}"},
            {"text": t("btn_skip"), "callback_data": f"ccl:{skip}"},
        ])

    if release_info and release_info.get("html_url"):
        buttons.append([{"text": t("btn_open_github"), "url": release_info["html_url"]}])

    reply_markup = {"inline_keyboard": buttons} if buttons else None
    await send_telegram_message(
        settings.telegram_bot_token, settings.telegram_chat_id,
        full_message, reply_markup=reply_markup,
    )


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


app = FastAPI(title="DiunTap - Interactive Docker Updates for DIUN", version="1.0.0", lifespan=lifespan)


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

    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}")

    if isinstance(body, dict) and len(str(body)) > 8192:
        raise HTTPException(status_code=413, detail="Payload too large")

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
        "version": "1.0.0",
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
        "version": "1.0.0",
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

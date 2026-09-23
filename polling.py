"""Telegram long-polling loop with strict auth and serialized stack updates.

Security:
  - ONLY from_user.id is checked against TELEGRAM_ALLOWED_USER_IDS
    (De Morgan fix for Critical-4),
  - buttons resolve via opaque ActionStore IDs; legacy callbacks fall back to
    strict sanitisation (never raw interpolation),
  - concurrent updates for the same (node, target) are serialised: a second
    tap gets an "already running" toast instead of racing docker/ssh.
"""

from __future__ import annotations

import asyncio
import html
import logging
from typing import Any, Callable, Coroutine, Dict, Optional, Tuple

import httpx

from models import action_store, parse_legacy_callback
from security import (
    is_telegram_user_allowed,
    sanitize_service_name,
    sanitize_stack_name,
    sanitize_tag,
)
from telegram_client import answer_callback_query, edit_telegram_message

logger = logging.getLogger("diuntap")

UpdateFn = Callable[[str | int, int, str, str, str], Coroutine[Any, Any, None]]

_running: Dict[Tuple[str, str], asyncio.Task] = {}
_running_lock = asyncio.Lock()


async def _run_serialized(key: Tuple[str, str], coro: Coroutine[Any, Any, None]) -> bool:
    async with _running_lock:
        existing = _running.get(key)
        if existing and not existing.done():
            return False
        task = asyncio.create_task(coro)
        _running[key] = task

        def _done(t: asyncio.Task) -> None:
            if t.cancelled():
                return
            exc = t.exception()
            if exc:
                logger.error("Background task %s failed: %s", key, exc)

        task.add_done_callback(_done)
        return True


async def _resolve_callback(
    cb_data: str,
) -> Optional[Tuple[str, str, str, str, str]]:
    """Return (kind, node, target, tag, header). Header is empty for legacy."""
    if ":" in cb_data:
        prefix, _, rest = cb_data.partition(":")
        if prefix in ("upd", "rb", "ccl") and ":" not in rest:
            action = action_store.resolve(rest)
            if action is None:
                return None
            return (action.kind, action.node, action.target, action.tag, action.header)
    legacy = parse_legacy_callback(cb_data)
    if legacy is None:
        return None
    kind, node, target, tag = legacy
    try:
        if node == "rem":
            target = sanitize_service_name(target)
        elif node == "all":
            target = sanitize_stack_name(target)
        else:
            node = "loc"
            target = sanitize_stack_name(target)
        tag = sanitize_tag(tag)
    except Exception:
        return None
    return (kind, node, target, tag, "")


async def telegram_polling_loop(
    bot_token: str,
    allowed_ids: list[str],
    local_node: str,
    remote_node: str,
    is_active: Callable[[], bool],
    execute_local_update: UpdateFn,
    execute_local_rollback: UpdateFn,
    execute_remote_update: UpdateFn,
    execute_remote_rollback: UpdateFn,
    dismiss_text_fn: Callable[[str], str],
    t_fn: Callable[..., str],
) -> None:
    if not bot_token:
        logger.warning("No TELEGRAM_BOT_TOKEN provided. Telegram polling disabled.")
        return

    logger.info("Starting Telegram button polling loop (Long Polling)...")
    offset = 0
    backoff = 5

    async with httpx.AsyncClient(timeout=35.0) as client:
        while is_active():
            try:
                url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
                resp = await client.get(url, params={"offset": offset, "timeout": 25})
                if resp.status_code == 429:
                    retry = resp.headers.get("Retry-After")
                    wait = int(retry) if retry and retry.isdigit() else backoff
                    logger.warning("Telegram polling rate limited, waiting %ss", wait)
                    await asyncio.sleep(min(wait, 60))
                    backoff = min(backoff * 2, 60)
                    continue
                if resp.status_code != 200:
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)
                    continue
                backoff = 5

                data = resp.json()
                for update in data.get("result", []):
                    offset = max(offset, update.get("update_id", 0) + 1)
                    if "callback_query" not in update:
                        continue
                    cb = update["callback_query"]
                    cb_id = cb.get("id", "")
                    cb_data = cb.get("data", "")
                    from_user = cb.get("from", {})
                    msg = cb.get("message", {})
                    chat_id = msg.get("chat", {}).get("id")
                    msg_id = msg.get("message_id")
                    orig_text = msg.get("text") or msg.get("caption") or ""

                    if not is_telegram_user_allowed(from_user.get("id"), allowed_ids):
                        await answer_callback_query(
                            bot_token, cb_id, text=t_fn("toast_no_permission")
                        )
                        logger.warning("Rejected Telegram action from unauthorized user %s", from_user.get("id"))
                        continue

                    resolved = await _resolve_callback(cb_data)
                    if resolved is None:
                        await answer_callback_query(bot_token, cb_id, text=t_fn("toast_dismissed"))
                        continue
                    kind, node, target, tag, stored_header = resolved
                    header_src = stored_header or (orig_text.split("\n\n")[0] if orig_text else "Container Update")
                    header = html.escape(header_src[:500])

                    if kind == "upd":
                        if node == "rem":
                            await answer_callback_query(
                                bot_token, cb_id,
                                text=t_fn("toast_updating", node=remote_node, name=target),
                            )
                            ok = await _run_serialized(
                                ("rem", target),
                                execute_remote_update(chat_id, msg_id, target, tag, header),
                            )
                            if not ok:
                                await answer_callback_query(bot_token, cb_id, text=t_fn("toast_already_running"))
                        else:
                            await answer_callback_query(
                                bot_token, cb_id,
                                text=t_fn("toast_updating", node=local_node, name=target),
                            )
                            ok = await _run_serialized(
                                ("loc", target),
                                execute_local_update(chat_id, msg_id, target, tag, header),
                            )
                            if not ok:
                                await answer_callback_query(bot_token, cb_id, text=t_fn("toast_already_running"))
                    elif kind == "rb":
                        if node == "rem":
                            await answer_callback_query(
                                bot_token, cb_id,
                                text=t_fn("toast_rollback", node=remote_node, name=target),
                            )
                            ok = await _run_serialized(
                                ("rem", target),
                                execute_remote_rollback(chat_id, msg_id, target, tag, header),
                            )
                            if not ok:
                                await answer_callback_query(bot_token, cb_id, text=t_fn("toast_already_running"))
                        else:
                            await answer_callback_query(
                                bot_token, cb_id,
                                text=t_fn("toast_rollback", node=local_node, name=target),
                            )
                            ok = await _run_serialized(
                                ("loc", target),
                                execute_local_rollback(chat_id, msg_id, target, tag, header),
                            )
                            if not ok:
                                await answer_callback_query(bot_token, cb_id, text=t_fn("toast_already_running"))
                    elif kind == "ccl":
                        await answer_callback_query(bot_token, cb_id, text=t_fn("toast_dismissed"))
                        new_text = (
                            f"<s>{html.escape(orig_text[:500])}</s>\n\n"
                            + dismiss_text_fn(html.escape(target))
                        )
                        await edit_telegram_message(bot_token, chat_id, msg_id, new_text, {"inline_keyboard": []})

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Exception in Telegram polling loop: %s", type(exc).__name__)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

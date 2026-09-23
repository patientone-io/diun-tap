"""Telegram Bot API client with secret redaction in logs."""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger("diuntap")


def _redact(text: str, token: str) -> str:
    if token and token in text:
        return text.replace(token, '[REDACTED]')
    return text


async def send_telegram_message(
    bot_token: str,
    chat_id: str,
    html_text: str,
    reply_markup: Optional[dict] = None,
) -> Optional[dict]:
    if not bot_token or not chat_id:
        logger.warning("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing.")
        return None

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload: Dict[str, Any] = {
        "chat_id": chat_id,
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
            if resp.status_code == 400 and "can't parse entities" in resp.text:
                logger.warning("HTML parsing failed in Telegram, retrying as plain text fallback...")
                payload.pop("parse_mode", None)
                payload["text"] = re.sub(r"<[^>]+>", "", html_text)[:4000]
                fallback_resp = await client.post(url, json=payload)
                if fallback_resp.status_code == 200:
                    return fallback_resp.json()
                logger.error("Telegram send fallback error: %s", fallback_resp.status_code)
            elif resp.status_code in (401, 403):
                logger.error("Telegram send auth error: %s (token redacted)", resp.status_code)
            else:
                logger.error("Telegram send error: %s", resp.status_code)
    except Exception as exc:
        logger.error("Telegram send exception: %s", type(exc).__name__)
    return None


async def edit_telegram_message(
    bot_token: str,
    chat_id: str | int,
    message_id: int,
    new_html: str,
    reply_markup: Optional[dict] = None,
) -> None:
    if not bot_token:
        return
    url = f"https://api.telegram.org/bot{bot_token}/editMessageText"
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
                logger.warning("HTML parsing failed in Telegram edit %s, retrying as plain text...", message_id)
                payload.pop("parse_mode", None)
                payload["text"] = re.sub(r"<[^>]+>", "", new_html)[:4000]
                await client.post(url, json=payload)
            elif "message is not modified" not in resp.text.lower():
                logger.warning("Telegram edit error %s: %s", message_id, resp.status_code)
    except Exception as exc:
        logger.error("Telegram edit error %s: %s", message_id, type(exc).__name__)


async def answer_callback_query(bot_token: str, callback_query_id: str, text: Optional[str] = None) -> None:
    if not bot_token:
        return
    url = f"https://api.telegram.org/bot{bot_token}/answerCallbackQuery"
    payload: Dict[str, Any] = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(url, json=payload)
    except Exception as exc:
        logger.error("answerCallbackQuery error: %s", type(exc).__name__)

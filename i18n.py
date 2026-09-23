import os
from typing import Dict, Any

LANGUAGE = os.getenv("LANGUAGE", "en").lower().strip()

STRINGS: Dict[str, Dict[str, str]] = {
    "en": {
        "title_update": "🚀 Container Update",
        "title_new": "🆕 New Container",
        "lbl_image": "📦 <b>Image:</b>",
        "lbl_tag": "🏷 <b>Tag:</b>",
        "lbl_stack": "📁 <b>Docker Stack ({node}):</b>",
        "lbl_service": "☁️ <b>Service ({node}):</b>",
        "lbl_changelog": "📋 <b>Changelog:</b>",
        "lbl_dockerhub": "🔗 <b>Docker Hub:</b>",
        "btn_update_now": "🚀 Update Now",
        "btn_update_node": "🚀 Update {node}",
        "btn_update_remote": "☁️ Update {node}",
        "btn_skip": "❌ Skip",
        "btn_open_github": "🌐 Open Release on GitHub",
        "btn_rollback": "⏪ Rollback",
        "btn_dismiss": "❌ Dismiss",
        "msg_updating": "⏳ <b>Update in progress ({node}):</b>\nCreating restore point and pulling new image <code>{tag}</code>...",
        "msg_verifying": "🔍 <b>Verifying services ({node}):</b>\nWaiting for containers to stabilize ({wait_sec}s)...",
        "msg_success": "🎉 <b>Successfully updated on {node}!</b>\n✅ Service <code>{stack}</code> is healthy with version <code>{tag}</code>.\n\n📊 <b>Container status:</b>\n{status}",
        "msg_warning_header": "⚠️ <b>WARNING: Issues detected after update on {node}!</b>\n❌ Container in <code>{stack}</code> reported an error or is unhealthy!\n\n📊 <b>Status:</b>\n{status}\n\n📋 <b>Last container logs:</b>\n<pre>{logs}</pre>",
        "msg_pull_error": "❌ <b>Error during image pull:</b>\n<pre>{error}</pre>",
        "msg_up_error": "❌ <b>Error during container start (up -d):</b>\n<pre>{error}</pre>",
        "msg_rollback_in_progress": "⏳ <b>Restoring previous version (Rollback)...</b>",
        "msg_rollback_success": "⏪ <b>Successfully rolled back!</b>\n✅ Service <code>{stack}</code> restored to previous stable image.\n🟢 All containers healthy.",
        "msg_rollback_warning": "⚠️ <b>Rollback executed, but container state requires attention:</b>\nCheck logs for <code>{stack}</code>.",
        "msg_rollback_not_found": "❌ <b>Rollback failed:</b> Restore point image <code>:{tag}</code> not found.",
        "msg_skipped": "❌ <i>Update for <code>{name}</code> was skipped.</i>",
        "toast_updating": "Starting update on {node}: {name}...",
        "toast_rollback": "Starting rollback on {node}: {name}...",
        "toast_dismissed": "Dismissed.",
        "toast_no_permission": "Unauthorized.",
        "toast_already_running": "Already running…",
        "no_containers_running": "⚠️ <i>No running containers found in stack.</i>",
        "truncated_changelog": "\n\n<i>…(truncated, click link for full notes)</i>",
    },
    "pl": {
        "title_update": "🚀 Aktualizacja Kontenera",
        "title_new": "🆕 Nowy Kontener",
        "lbl_image": "📦 <b>Obraz:</b>",
        "lbl_tag": "🏷 <b>Tag:</b>",
        "lbl_stack": "📁 <b>Stos Docker ({node}):</b>",
        "lbl_service": "☁️ <b>Usługa ({node}):</b>",
        "lbl_changelog": "📋 <b>Changelog:</b>",
        "lbl_dockerhub": "🔗 <b>Docker Hub:</b>",
        "btn_update_now": "🚀 Uaktualnij teraz",
        "btn_update_node": "🚀 Uaktualnij {node}",
        "btn_update_remote": "☁️ Uaktualnij {node}",
        "btn_skip": "❌ Pomiń",
        "btn_open_github": "🌐 Otwórz Wydanie na GitHubie",
        "btn_rollback": "⏪ Cofnij (Rollback)",
        "btn_dismiss": "❌ Zamknij",
        "msg_updating": "⏳ <b>Aktualizacja w toku ({node}):</b>\nTworzenie punktu przywracania i pobieranie nowego obrazu <code>{tag}</code>...",
        "msg_verifying": "🔍 <b>Weryfikacja usług ({node}):</b>\nOczekiwanie na stabilizację kontenerów ({wait_sec}s)...",
        "msg_success": "🎉 <b>Zaktualizowano pomyślnie na {node}!</b>\n✅ Usługa <code>{stack}</code> działa stabilnie z wersją <code>{tag}</code>.\n\n📊 <b>Status kontenerów:</b>\n{status}",
        "msg_warning_header": "⚠️ <b>OSTRZEŻENIE: Problem po aktualizacji na {node}!</b>\n❌ Kontener w usłudze <code>{stack}</code> zgłosił błąd lub nie działa!\n\n📊 <b>Stan:</b>\n{status}\n\n📋 <b>Ostatnie logi kontenera:</b>\n<pre>{logs}</pre>",
        "msg_pull_error": "❌ <b>Błąd podczas pobierania (pull):</b>\n<pre>{error}</pre>",
        "msg_up_error": "❌ <b>Błąd podczas uruchamiania (up -d):</b>\n<pre>{error}</pre>",
        "msg_rollback_in_progress": "⏳ <b>Trwa przywracanie poprzedniej wersji (Rollback)...</b>",
        "msg_rollback_success": "⏪ <b>Pomyślnie cofnięto aktualizację!</b>\n✅ Usługa <code>{stack}</code> została przywrócona do poprzedniego stabilnego obrazu.\n🟢 Status: Wszystkie kontenery działają poprawnie.",
        "msg_rollback_warning": "⚠️ <b>Rollback wykonany, lecz stan kontenera wymaga uwagi:</b>\nSprawdź logi kontenera <code>{stack}</code>.",
        "msg_rollback_not_found": "❌ <b>Błąd rollbacku:</b> Nie znaleziono obrazu punktu przywracania <code>:{tag}</code>.",
        "msg_skipped": "❌ <i>Aktualizacja usługi <code>{name}</code> została pominięta/zamknięta.</i>",
        "toast_updating": "Rozpoczynam aktualizację na {node}: {name}...",
        "toast_rollback": "Rozpoczynam rollback na {node}: {name}...",
        "toast_dismissed": "Zamknięto.",
        "toast_no_permission": "Brak uprawnień.",
        "toast_already_running": "Już w trakcie…",
        "no_containers_running": "⚠️ <i>Brak działających kontenerów w stosie.</i>",
        "truncated_changelog": "\n\n<i>…(skrócono, kliknij link po całość)</i>",
    }
}


def t(key: str, **kwargs: Any) -> str:
    """Gets translated string for given key with keyword interpolation."""
    lang_dict = STRINGS.get(LANGUAGE, STRINGS["en"])
    template = lang_dict.get(key, STRINGS["en"].get(key, key))
    if kwargs:
        return template.format(**kwargs)
    return template

"""Regression tests for snapshot identity and retryable notification delivery."""

import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from cachetools import TTLCache
from unittest.mock import AsyncMock, patch

import main
import polling
from dedup import DedupCache
from models import ActionStore, DiunWebhookPayload


class RestoreIdentity(unittest.IsolatedAsyncioTestCase):
    async def test_old_and_legacy_buttons_cannot_restore_new_snapshot(self):
        store = ActionStore()
        self.addCleanup(store.close)
        for node, execute in (("loc", main.execute_local_rollback),
                              ("rem", main.execute_remote_rollback)):
            old = store.save_restore_point(node, "app", {"vendor/a:latest": "backup-a"}, ["a"])
            button = store.register("rb", node, "app", "latest", "A", restore_id=old)
            legacy_button = store.register("rb", node, "app", "latest", "A")
            store.save_restore_point(node, "app", {"vendor/b:latest": "backup-b"}, ["b"])
            with (
                patch.object(main, "action_store", store),
                patch.object(polling, "action_store", store),
                patch.object(main, "find_local_compose_file", return_value=(Path("/tmp"), Path("/tmp/compose.yaml"), "app")),
                patch.object(main, "update_services", AsyncMock()) as config,
                patch.object(main, "remote_inspect_image", AsyncMock()) as inspect,
                patch.object(main, "_auto_rollback", AsyncMock()) as local,
                patch.object(main, "_restore_remote", AsyncMock()) as remote,
                patch("telegram_client.edit_telegram_message", AsyncMock()) as edit,
            ):
                for callback in (f"rb:{button}", f"rb:{legacy_button}", f"rb:{node}:app:latest"):
                    resolved = await polling._resolve_callback(callback)
                    await execute("chat", 1, resolved[2], resolved[3], resolved[4], resolved[6])
                    self.assertEqual(edit.await_args.args[3], resolved[4] + "\n\n" + main.t("msg_rollback_stale"))
                config.assert_not_awaited()
                inspect.assert_not_awaited()
                local.assert_not_awaited()
                remote.assert_not_awaited()

    async def test_current_remote_button_restores_exact_snapshot(self):
        store = ActionStore()
        self.addCleanup(store.close)
        backups = {"vendor/app:latest": "vendor/app:backup"}
        restore_id = store.save_restore_point("rem", "app", backups)
        with (
            patch.object(main, "action_store", store),
            patch.object(main, "remote_inspect_image", AsyncMock(return_value="vendor/app:latest")),
            patch.object(main, "_restore_remote", AsyncMock(return_value=True)) as restore,
            patch("telegram_client.edit_telegram_message", AsyncMock()),
        ):
            await main.execute_remote_rollback("chat", 1, "app", "latest", "header", restore_id)
        restore.assert_awaited_once_with(main.settings.remote_ssh_host, "app", backups)

    async def test_identity_survives_reopen_and_legacy_json_remains_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "actions.sqlite3"
            store = ActionStore(db_path=path)
            restore_id = store.save_restore_point("loc", "app", {"image": "backup"})
            button = store.register("rb", "loc", "app", "latest", "header", restore_id=restore_id)
            legacy = store.register("rb", "loc", "old", "latest", "header")
            payload = json.loads(store._db.execute("SELECT payload FROM actions WHERE id=?", (legacy,)).fetchone()[0])
            payload.pop("restore_id")
            with store._db:
                store._db.execute("UPDATE actions SET payload=? WHERE id=?", (json.dumps(payload), legacy))
                store._db.execute("INSERT INTO restores VALUES (?, ?, ?)", ("loc", "old", json.dumps({"backups": {"image": "old-backup"}, "services": []})))
            store.close()
            reopened = ActionStore(db_path=path)
            try:
                self.assertEqual(reopened.resolve(button).restore_id, restore_id)
                self.assertEqual(reopened.restore_point("loc", "app")["restore_id"], restore_id)
                self.assertEqual(reopened.resolve(legacy).restore_id, "")
                self.assertNotIn("restore_id", reopened.restore_point("loc", "old"))
            finally:
                reopened.close()


class DeliveryRetry(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.payload = DiunWebhookPayload(image="vendor/app:latest")
        for target, value in (
            ("dedup", DedupCache()),
            ("resolve_github_repo", lambda *args: None),
            ("_load_remote_map", AsyncMock(return_value={})),
            ("find_targets_for_image", AsyncMock(return_value={})),
        ):
            patcher = patch.object(main, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for field in ("exclude_images", "pinned_stacks"):
            patcher = patch.object(main.settings, field, [])
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_failed_send_retries_but_success_is_deduplicated(self):
        with patch("telegram_client.send_telegram_message", AsyncMock(side_effect=[None, {"ok": True}])) as send:
            for _ in range(3):
                await main.process_diun_notification(self.payload)
        self.assertEqual(send.await_count, 2)

    async def test_exception_and_cancellation_release_reservation(self):
        for error in (RuntimeError("network"), asyncio.CancelledError()):
            main.dedup = DedupCache()
            with patch("telegram_client.send_telegram_message", AsyncMock(side_effect=[error, {"ok": True}])) as send:
                with self.assertRaises(type(error)):
                    await main.process_diun_notification(self.payload)
                await main.process_diun_notification(self.payload)
            self.assertEqual(send.await_count, 2)
            self.assertFalse(main.dedup._pending)

    async def test_parallel_notifications_have_one_sender(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def send(*args, **kwargs):
            entered.set()
            await release.wait()
            return {"ok": True}

        with patch("telegram_client.send_telegram_message", AsyncMock(side_effect=send)) as sender:
            first = asyncio.create_task(main.process_diun_notification(self.payload))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                await main.process_diun_notification(self.payload)
            finally:
                release.set()
                await first
        sender.assert_awaited_once()

    async def test_ttl_starts_after_delivery_and_never_expires_active_send(self):
        cache = DedupCache(ttl=30)
        now = 0
        cache._cache = TTLCache(maxsize=8, ttl=30, timer=lambda: now)
        self.assertTrue(await cache.reserve("image"))
        now = 60
        self.assertFalse(await cache.reserve("image"))
        await cache.finish("image", True)
        now = 89
        self.assertFalse(await cache.reserve("image"))
        now = 91
        self.assertTrue(await cache.reserve("image"))

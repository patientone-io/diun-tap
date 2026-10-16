"""Regression coverage for update, restore, packaging and notification failures."""

import asyncio
import contextlib
import io
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx

import docker_local as local
import main
import polling
import remote_ssh as remote
from models import ActionStore


class FakeClock:
    def __init__(self):
        self.now = 0

    def monotonic(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds


class PersistentActions(unittest.TestCase):
    def test_actions_and_exact_restore_refs_survive_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "actions.sqlite3"
            store = ActionStore(db_path=path)
            aid = store.register("upd", "loc", "app", "v2", "header", "vendor/app:v2")
            backups = {
                "registry:5000/app:v1": "registry:5000/app:backup1",
                "registry:5000/app:v2": "registry:5000/app:backup2",
            }
            store.save_restore_point("loc", "app", backups, ["first", "second"])
            store.close()
            reopened = ActionStore(db_path=path)
            self.addCleanup(reopened.close)
            self.assertEqual(reopened.resolve(aid).image, "vendor/app:v2")
            self.assertEqual(reopened.restore_point("loc", "app")["backups"], backups)

    def test_expiration_and_capacity(self):
        store = ActionStore(ttl_seconds=5, max_items=2)
        self.addCleanup(store.close)
        with patch("models.time.time", return_value=10):
            old = store.register("upd", "loc", "a", "latest", "")
        with patch("models.time.time", return_value=11):
            second = store.register("upd", "loc", "b", "latest", "")
        with patch("models.time.time", return_value=12):
            third = store.register("upd", "loc", "c", "latest", "")
            self.assertIsNone(store.resolve(old))
            self.assertIsNotNone(store.resolve(second))
            self.assertIsNotNone(store.resolve(third))
        with patch("models.time.time", return_value=20):
            self.assertIsNone(store.resolve(third))


class Discovery(unittest.IsolatedAsyncioTestCase):
    async def test_same_short_name_cannot_select_another_repository(self):
        with tempfile.TemporaryDirectory() as tmp:
            stack = Path(tmp) / "app"
            stack.mkdir()
            (stack / "compose.yaml").write_text(
                "services:\n  app:\n    image: other/app:latest\n# vendor/app\n"
            )
            self.assertIsNone(local._scan_stacks_sync(Path(tmp), "vendor/app", "app"))
            with patch.object(
                local, "_run", AsyncMock(return_value=(0, "other/app:latest|app", ""))
            ):
                targets = await local.find_targets_for_image(
                    "vendor/app", Path(tmp), True, {"app": "other"}
                )
            self.assertEqual(targets, {"local": None, "remote": None})

    async def test_remote_quadlet_map_has_no_short_aliases_or_ambiguous_targets(self):
        from mappings import load_remote_quadlets_fallback

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "first.container").write_text("[Container]\nImage=docker.io/vendor/app:v1\n")
            self.assertEqual(load_remote_quadlets_fallback(root), {"vendor/app": "first"})
            (root / "second.container").write_text("[Container]\nImage=vendor/app:v2\n")
            self.assertEqual(load_remote_quadlets_fallback(root), {})

    async def test_docker_hub_aliases_and_ambiguous_stacks(self):
        self.assertTrue(local.same_repository("docker.io/library/alpine:3", "alpine"))
        with patch.object(
            local,
            "_run",
            AsyncMock(return_value=(0, "vendor/app:v1|first\nvendor/app:v2|second", "")),
        ):
            self.assertIsNone(await local.find_local_stack_via_docker("vendor/app"))

    async def test_resolved_compose_selects_exact_repo_and_tag(self):
        config = {
            "services": {
                "app": {"image": "docker.io/vendor/app:v2"},
                "db": {"image": "postgres:v2"},
                "pinned": {"image": "vendor/app:v1"},
                "digest": {"image": "vendor/app:v2@sha256:abc"},
            }
        }
        with patch.object(local, "_run", AsyncMock(return_value=(0, json.dumps(config), ""))):
            self.assertEqual(
                await local.update_services(
                    Path("/stacks/a"), Path("/stacks/a/compose.yaml"), "v2", "vendor/app:v2"
                ),
                {"app": "docker.io/vendor/app:v2"},
            )
            with self.assertRaises(ValueError):
                await local.update_services(
                    Path("/a"), Path("/a/compose.yaml"), "v3", "vendor/app:v3"
                )

    async def test_compose_ps_accepts_array_and_json_lines(self):
        rows = [{"Name": "a", "Service": "app"}, {"Name": "b", "Service": "db"}]
        for output in (json.dumps(rows), "\n".join(map(json.dumps, rows))):
            with (
                self.subTest(output=output),
                patch.object(local, "_run", AsyncMock(return_value=(0, output, ""))),
            ):
                self.assertEqual(
                    await local.inspect_local_containers(Path("/a"), Path("/a/compose.yaml")), rows
                )


class HealthAndBackups(unittest.IsolatedAsyncioTestCase):
    async def test_starting_is_not_healthy_and_requires_consecutive_samples(self):
        starting = [{"Name": "app", "Service": "app", "State": "running", "Health": "starting"}]
        healthy = [dict(starting[0], Health="healthy")]
        self.assertFalse(local.describe_containers(starting)[0])
        self.assertIn("⏳", local.describe_containers(starting)[1])
        with (
            patch.object(
                local,
                "inspect_local_containers",
                AsyncMock(side_effect=[starting, healthy, healthy]),
            ) as inspect,
            patch.object(local.asyncio, "sleep", AsyncMock()),
        ):
            self.assertFalse(
                (
                    await local.wait_for_healthy(
                        Path("/a"), Path("/a/compose.yaml"), 6, services=["app"]
                    )
                )[0]
            )
            self.assertEqual(inspect.await_count, 3)

    async def test_missing_selected_service_cannot_be_healthy(self):
        from types import SimpleNamespace

        one = [{"Service": "app", "State": "running", "Health": "healthy"}]
        clock = FakeClock()
        with (
            patch.object(local, "inspect_local_containers", AsyncMock(return_value=one)),
            patch.object(local, "time", SimpleNamespace(monotonic=clock.monotonic)),
            patch.object(local.asyncio, "sleep", clock.sleep),
        ):
            result, _ = await local.wait_for_healthy(
                Path("/a"), Path("/a/compose.yaml"), 2, services=["app", "db"]
            )
        self.assertIsNone(result)

    async def test_backups_use_running_id_and_do_not_overwrite_other_tags(self):
        containers = [
            {"ID": "container1", "Image": "registry:5000/app:v1"},
            {"ID": "container2", "Image": "registry:5000/app:v2"},
        ]

        async def run(cmd, **kwargs):
            return (0, "sha256:" + cmd[2], "") if cmd[1] == "inspect" else (0, "", "")

        with (
            patch.object(local, "_run", AsyncMock(side_effect=run)),
            patch.object(local, "_docker_tag", AsyncMock(return_value=True)) as tag,
        ):
            backups = await local.create_backups("stack", containers)
            self.assertEqual(len(set(backups.values())), 2)
            self.assertEqual(tag.await_args_list[0].args[0], "sha256:container1")
            self.assertTrue(
                all(
                    ref.startswith("registry:5000/app:diun-backup-stack-")
                    for ref in backups.values()
                )
            )

    async def test_retention_prunes_only_this_stack_keeping_latest_three(self):
        refs = [f"repo/app:diun-backup-stack-2026101{i}120000" for i in range(1, 6)]
        unrelated = "repo/app:diun-backup-stack-other-20261011120000"

        async def run(cmd, **kwargs):
            if cmd[1] == "images":
                return 0, "\n".join(refs + [unrelated]), ""
            return 0, "", ""

        with patch.object(local, "_run", AsyncMock(side_effect=run)) as run_mock:
            await local.create_backups("stack", [])
        deleted = [call.args[0][2] for call in run_mock.await_args_list if call.args[0][1] == "rmi"]
        self.assertEqual(set(deleted), set(refs[:2]))

    async def test_failed_tag_or_compose_command_never_reports_rollback_success(self):
        for outputs in ([(1, "", "tag failed")], [(0, "", ""), (1, "", "up failed")]):
            with (
                self.subTest(outputs=outputs),
                patch.object(main, "_run", AsyncMock(side_effect=outputs)),
                patch.object(main, "wait_for_healthy", AsyncMock()) as health,
            ):
                self.assertFalse(
                    await main._auto_rollback(
                        Path("/a"), Path("/a/compose.yaml"), {"repo:v1": "repo:backup"}, ["app"]
                    )
                )
                health.assert_not_awaited()

    async def test_rollback_uses_supported_no_pull_option(self):
        with (
            patch.object(main, "_run", AsyncMock(return_value=(0, "", ""))) as run,
            patch.object(main, "wait_for_healthy", AsyncMock(return_value=(False, "healthy"))),
        ):
            self.assertTrue(
                await main._auto_rollback(
                    Path("/a"), Path("/a/compose.yaml"), {"repo:v1": "repo:backup"}, ["app"]
                )
            )
        command = run.await_args_list[-1].args[0]
        self.assertNotIn("--no-pull", command)
        self.assertEqual(command[command.index("--pull") + 1], "never")


class Orchestration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.stack = Path(self.temp.name) / "app"
        self.stack.mkdir()
        (self.stack / "compose.yaml").write_text("services: {}")
        self.store = ActionStore()
        self.addCleanup(self.store.close)
        self.context = contextlib.ExitStack()
        self.addCleanup(self.context.close)
        self.context.enter_context(patch.object(main, "action_store", self.store))
        self.context.enter_context(patch.object(main, "remote_prune_backups", AsyncMock()))
        self.context.enter_context(
            patch.object(main.settings, "docker_stacks_dir", Path(self.temp.name))
        )
        self.edit = self.context.enter_context(
            patch("telegram_client.edit_telegram_message", AsyncMock())
        )

    async def test_unhealthy_local_update_restores_and_only_updates_selected_service(self):
        with (
            patch.object(main, "update_services", AsyncMock(return_value={"app": "vendor/app:v2"})),
            patch.object(
                main,
                "inspect_local_containers",
                AsyncMock(return_value=[{"ID": "c1", "Service": "app", "Image": "vendor/app:v1"}]),
            ),
            patch.object(
                main,
                "create_backups",
                AsyncMock(return_value={"vendor/app:v2": "vendor/app:backup"}),
            ) as backup,
            patch.object(main, "_run", AsyncMock(return_value=(0, "", ""))) as run,
            patch.object(main, "wait_for_healthy", AsyncMock(return_value=(True, "unhealthy"))),
            patch.object(main, "compose_logs", AsyncMock(return_value="failed")),
            patch.object(main, "_auto_rollback", AsyncMock(return_value=True)) as restore,
        ):
            await main.execute_local_update("chat", 1, "app", "v2", "header", "vendor/app:v2")
        restore.assert_awaited_once()
        self.assertEqual(backup.await_args.args[1][0]["Image"], "vendor/app:v2")
        self.assertEqual(run.await_args_list[0].args[0][-2:], ["pull", "app"])
        self.assertEqual(run.await_args_list[1].args[0][-2:], ["--no-deps", "app"])
        self.assertIn("rollback", self.edit.await_args.args[3].lower())
        self.assertEqual(self.store.restore_point("loc", "app")["services"], ["app"])

    async def test_pending_local_verification_never_rolls_back(self):
        with (
            patch.object(main, "update_services", AsyncMock(return_value={"app": "vendor/app:v2"})),
            patch.object(
                main,
                "inspect_local_containers",
                AsyncMock(return_value=[{"ID": "c1", "Service": "app", "Image": "vendor/app:v2"}]),
            ),
            patch.object(
                main,
                "create_backups",
                AsyncMock(return_value={"vendor/app:v2": "vendor/app:backup"}),
            ),
            patch.object(main, "_run", AsyncMock(return_value=(0, "", ""))),
            patch.object(main, "wait_for_healthy", AsyncMock(return_value=(None, "starting"))),
            patch.object(main, "_auto_rollback", AsyncMock()) as restore,
        ):
            await main.execute_local_update("chat", 1, "app", "v2", "header", "vendor/app:v2")
        restore.assert_not_awaited()
        self.assertIn("pending", self.edit.await_args.args[3])
        self.assertNotIn("Successfully", self.edit.await_args.args[3])

    async def test_pending_remote_verification_never_rolls_back(self):
        with (
            patch.object(main, "remote_inspect_image", AsyncMock(return_value="vendor/app:v2")),
            patch.object(main, "remote_service_uses_image", AsyncMock(return_value=True)),
            patch.object(main, "remote_inspect_image_id", AsyncMock(return_value="sha256:old")),
            patch.object(main, "remote_tag_image", AsyncMock(return_value=True)),
            patch.object(main, "remote_pull_image", AsyncMock(return_value=(0, "", ""))),
            patch.object(main, "remote_restart_service", AsyncMock(return_value=(0, "", ""))),
            patch.object(main, "remote_wait_for_healthy", AsyncMock(return_value=None)),
            patch.object(main, "_restore_remote", AsyncMock()) as restore,
        ):
            await main.execute_remote_update("chat", 1, "app", "v2", "header", "vendor/app:v2")
        restore.assert_not_awaited()
        self.assertIn("pending", self.edit.await_args.args[3])
        self.assertNotIn("Successfully", self.edit.await_args.args[3])

    async def test_pinned_tag_message_names_actual_and_requested_image(self):
        from errors import ImageTagMismatch

        with (
            patch.object(
                main,
                "update_services",
                AsyncMock(side_effect=ImageTagMismatch(["vendor/app:10.8.10"])),
            ),
            patch.object(main, "_run", AsyncMock()) as run,
        ):
            await main.execute_local_update(
                "chat", 1, "app", "10.8.11", "header", "vendor/app:10.8.11"
            )
        run.assert_not_awaited()
        message = self.edit.await_args.args[3]
        self.assertIn("10.8.10", message)
        self.assertIn("10.8.11", message)
        self.assertNotIn("Pull=never", message)

    async def test_compose_start_failure_runs_automatic_rollback(self):
        with (
            patch.object(main, "update_services", AsyncMock(return_value={"app": "vendor/app:v2"})),
            patch.object(
                main,
                "inspect_local_containers",
                AsyncMock(return_value=[{"ID": "c1", "Service": "app", "Image": "vendor/app:v2"}]),
            ),
            patch.object(
                main,
                "create_backups",
                AsyncMock(return_value={"vendor/app:v2": "vendor/app:backup"}),
            ),
            patch.object(
                main, "_run", AsyncMock(side_effect=[(0, "", ""), (1, "", "start failed")])
            ),
            patch.object(main, "_auto_rollback", AsyncMock(return_value=False)) as restore,
        ):
            await main.execute_local_update("chat", 1, "app", "v2", "header", "vendor/app:v2")
        restore.assert_awaited_once()
        self.assertIn("could not be verified", self.edit.await_args.args[3])

    async def test_manual_rollback_uses_persisted_exact_refs_and_reports_failure(self):
        backups = {"registry:5000/app:v2": "registry:5000/app:backup"}
        self.store.save_restore_point("loc", "app", backups, ["app"])
        with (
            patch.object(
                main, "update_services", AsyncMock(return_value={"app": "registry:5000/app:v2"})
            ),
            patch.object(main, "_auto_rollback", AsyncMock(return_value=False)) as restore,
        ):
            await main.execute_local_rollback("chat", 1, "app", "v2", "header")
        restore.assert_awaited_once_with(
            self.stack.resolve(), self.stack.resolve() / "compose.yaml", backups, ["app"]
        )
        self.assertNotIn("Successfully rolled back", self.edit.await_args.args[3])

    async def test_manual_rollback_refuses_changed_configuration(self):
        self.store.save_restore_point("loc", "app", {"vendor/app:v2": "vendor/app:backup"}, ["app"])
        with (
            patch.object(main, "update_services", AsyncMock(side_effect=ValueError("changed tag"))),
            patch.object(main, "_auto_rollback", AsyncMock()) as restore,
        ):
            await main.execute_local_rollback("chat", 1, "app", "v2", "header")
        restore.assert_not_awaited()

    async def test_mismatched_tag_and_failed_backup_do_not_pull(self):
        for service_result, backup_error in (
            (ValueError("tag mismatch"), None),
            ({"app": "vendor/app:v2"}, RuntimeError("backup failed")),
        ):
            service_mock = (
                AsyncMock(side_effect=service_result)
                if isinstance(service_result, Exception)
                else AsyncMock(return_value=service_result)
            )
            with (
                self.subTest(service_result=service_result),
                patch.object(main, "update_services", service_mock),
                patch.object(
                    main,
                    "inspect_local_containers",
                    AsyncMock(
                        return_value=[{"ID": "c1", "Service": "app", "Image": "vendor/app:v2"}]
                    ),
                ),
                patch.object(main, "create_backups", AsyncMock(side_effect=backup_error)),
                patch.object(main, "_run", AsyncMock()) as run,
            ):
                await main.execute_local_update("chat", 1, "app", "v2", "header", "vendor/app:v2")
                run.assert_not_awaited()

    async def test_remote_mismatched_tag_does_not_pull_or_restart(self):
        with (
            patch.object(main, "remote_inspect_image", AsyncMock(return_value="vendor/app:v1")),
            patch.object(main, "remote_pull_image", AsyncMock()) as pull,
            patch.object(main, "remote_restart_service", AsyncMock()) as restart,
        ):
            await main.execute_remote_update("chat", 1, "app", "v2", "header", "vendor/app:v2")
        pull.assert_not_awaited()
        restart.assert_not_awaited()

    async def test_failed_remote_healthcheck_restores_running_image_id(self):
        with (
            patch.object(
                main, "remote_inspect_image", AsyncMock(return_value="registry:5000/app:v2")
            ),
            patch.object(main, "remote_service_uses_image", AsyncMock(return_value=True)),
            patch.object(main, "remote_inspect_image_id", AsyncMock(return_value="sha256:old")),
            patch.object(main, "remote_tag_image", AsyncMock(return_value=True)) as tag,
            patch.object(main, "remote_pull_image", AsyncMock(return_value=(0, "", ""))),
            patch.object(main, "remote_restart_service", AsyncMock(return_value=(0, "", ""))),
            patch.object(main, "remote_wait_for_healthy", AsyncMock(return_value=False)),
            patch.object(main, "remote_logs", AsyncMock(return_value="failed")),
            patch.object(main, "_restore_remote", AsyncMock(return_value=True)) as restore,
        ):
            await main.execute_remote_update(
                "chat", 1, "app", "v2", "header", "registry:5000/app:v2"
            )
        self.assertEqual(tag.await_args.args[1], "sha256:old")
        restore.assert_awaited_once()
        self.assertIn("registry:5000/app:v2", self.store.restore_point("rem", "app")["backups"])


class RemoteCommands(unittest.IsolatedAsyncioTestCase):
    async def test_health_template_has_double_braces(self):
        with patch.object(
            remote, "run_remote_ssh", AsyncMock(return_value=(0, "running|healthy", ""))
        ) as ssh:
            self.assertEqual(
                await remote.remote_container_state("host", "app"), ("running", "healthy")
            )
        cmd = ssh.await_args.args[1]
        self.assertNotIn("{{{{", cmd)
        self.assertIn("{{.State.Status}}", cmd)

    async def test_loaded_quadlet_requires_exact_image_and_never_pull(self):
        for args, expected in (
            ("podman run vendor/app:v2", True),
            ("podman run --pull=missing vendor/app:v2", True),
            ("podman run --pull missing vendor/app:v2", True),
            ("podman run --pull=missing docker.io/vendor/app:v2", True),
            ("podman run --pull=missing vendor/app:v1", False),
            ("podman run --pull=newer vendor/app:v2", False),
            ("podman run --pull=never vendor/app:v2", True),
            ("podman run --pull never vendor/app:v2", True),
            ("podman run --pull=always vendor/app:v2", False),
            ("podman run --pull=never --pull=always vendor/app:v2", False),
            ("podman run --pull=never vendor/app:v1", False),
        ):
            with (
                self.subTest(args=args),
                patch.object(
                    remote,
                    "run_remote_ssh",
                    AsyncMock(return_value=(0, "{ argv[]=" + args + " ; }", "")),
                ),
            ):
                self.assertEqual(
                    await remote.remote_service_uses_image("host", "app", "vendor/app:v2"), expected
                )

    async def test_remote_retention_keeps_three_and_ignores_other_targets(self):
        refs = [f"registry:5000/app:diun-backup-app-2026101{i}120000" for i in range(1, 6)]
        unrelated = "registry:5000/app:diun-backup-app-other-20261011120000"
        with patch.object(
            remote,
            "run_remote_ssh",
            AsyncMock(
                side_effect=[(0, "\n".join(refs + [unrelated]), ""), (0, "", ""), (0, "", "")]
            ),
        ) as ssh:
            await remote.remote_prune_backups("host", "registry:5000/app", "app")
        deleted = [call.args[1] for call in ssh.await_args_list[1:]]
        self.assertEqual(set(deleted), {f"podman rmi {ref}" for ref in refs[:2]})

    async def test_remote_starting_waits_for_two_healthy_samples(self):
        with (
            patch.object(remote, "remote_service_active", AsyncMock(return_value="active")),
            patch.object(
                remote,
                "remote_container_state",
                AsyncMock(
                    side_effect=[
                        ("running", "starting"),
                        ("running", "healthy"),
                        ("running", "healthy"),
                    ]
                ),
            ) as state,
            patch.object(remote.asyncio, "sleep", AsyncMock()),
        ):
            self.assertTrue(await remote.remote_wait_for_healthy("host", "app", 6))
            self.assertEqual(state.await_count, 3)


class WebhookAndLogging(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_shapes_oversized_payload_and_secret(self):
        transport = httpx.ASGITransport(app=main.app, raise_app_exceptions=False)
        with (
            patch.object(main.settings, "diun_webhook_secret", ""),
            patch.object(main, "process_diun_notification", AsyncMock()) as process,
        ):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                for value in ([], None, 42, "text"):
                    self.assertEqual(
                        (await client.post("/webhook/diun", json=value)).status_code, 400
                    )
                self.assertEqual(
                    (await client.post("/webhook/diun", content=b" " * 8193)).status_code, 413
                )
                self.assertEqual(
                    (await client.post("/webhook/diun", json={"image": "alpine:3"})).status_code,
                    200,
                )
                await asyncio.sleep(0)
                process.assert_awaited_once()
                with patch.object(main.settings, "diun_webhook_secret", "secret"):
                    self.assertEqual(
                        (
                            await client.post("/webhook/diun", json={"image": "alpine:3"})
                        ).status_code,
                        401,
                    )

    async def test_default_http_logs_do_not_expose_bot_token(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logging.getLogger().addHandler(handler)
        self.addCleanup(logging.getLogger().removeHandler, handler)
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200))
        ) as client:
            await client.get("https://api.telegram.org/botFAKE_TEST_TOKEN/getUpdates")
        self.assertNotIn("FAKE_TEST_TOKEN", stream.getvalue())

    async def test_serialization_closes_rejected_coroutine_and_cleans_completed_tasks(self):
        event = asyncio.Event()

        async def job():
            await event.wait()

        self.assertTrue(await polling._run_serialized(("loc", "test"), job()))
        rejected = job()
        self.assertFalse(await polling._run_serialized(("loc", "test"), rejected))
        self.assertIsNone(rejected.cr_frame)
        task = polling._running[("loc", "test")]
        event.set()
        await task
        await asyncio.sleep(0)
        self.assertNotIn(("loc", "test"), polling._running)


class StartupRegressions(unittest.IsolatedAsyncioTestCase):
    async def local_result(self, state, health, wait=6):
        from types import SimpleNamespace

        clock = FakeClock()

        async def inspect(*args, **kwargs):
            return [{"Service": "app", "State": state(clock.now), "Health": health(clock.now)}]

        with (
            patch.object(local, "inspect_local_containers", inspect),
            patch.object(local, "time", SimpleNamespace(monotonic=clock.monotonic)),
            patch.object(local.asyncio, "sleep", clock.sleep),
        ):
            result, _ = await local.wait_for_healthy(
                Path("/a"), Path("/a/compose.yaml"), wait, services=["app"]
            )
        return result, clock.now

    async def test_local_startup_longer_than_legacy_six_seconds_succeeds(self):
        result, elapsed = await self.local_result(
            lambda _: "running", lambda now: "starting" if now < 40 else "healthy"
        )
        self.assertIs(result, False)
        self.assertGreaterEqual(elapsed, 41)

    async def test_local_starting_timeout_is_pending_not_failed(self):
        result, elapsed = await self.local_result(lambda _: "running", lambda _: "starting")
        self.assertIsNone(result)
        self.assertEqual(elapsed, 60)

    async def test_local_confirmed_failures_are_not_pending(self):
        for state, health in (
            ("running", "unhealthy"),
            ("exited", ""),
            ("dead", ""),
            ("restarting", ""),
        ):
            with self.subTest(state=state, health=health):
                result, elapsed = await self.local_result(lambda _: state, lambda _: health)
                self.assertIs(result, True)
                self.assertEqual(elapsed, 0)

    async def test_remote_long_startup_and_pending_timeout(self):
        from types import SimpleNamespace

        for settles in (True, False):
            with self.subTest(settles=settles):
                clock = FakeClock()

                async def sample(*args):
                    return "running", "healthy" if settles and clock.now >= 40 else "starting"

                with (
                    patch.object(remote, "time", SimpleNamespace(monotonic=clock.monotonic)),
                    patch.object(remote.asyncio, "sleep", clock.sleep),
                    patch.object(remote, "remote_service_active", AsyncMock(return_value="active")),
                    patch.object(remote, "remote_container_state", sample),
                ):
                    result = await remote.remote_wait_for_healthy("host", "app", 6)
                self.assertIs(result, True if settles else None)
                self.assertEqual(clock.now, 41 if settles else 60)

    async def test_remote_confirmed_failures(self):
        for state, health in (
            ("running", "unhealthy"),
            ("exited", ""),
            ("dead", ""),
            ("restarting", ""),
        ):
            with (
                self.subTest(state=state),
                patch.object(remote, "remote_service_active", AsyncMock(return_value="active")),
                patch.object(
                    remote, "remote_container_state", AsyncMock(return_value=(state, health))
                ),
            ):
                self.assertIs(await remote.remote_wait_for_healthy("host", "app", 6), False)

    async def test_pending_rollback_is_neither_success_nor_failure(self):
        with (
            patch.object(main, "_run", AsyncMock(return_value=(0, "", ""))),
            patch.object(main, "wait_for_healthy", AsyncMock(return_value=(None, "starting"))),
        ):
            self.assertIsNone(
                await main._auto_rollback(
                    Path("/a"), Path("/a/compose.yaml"), {"repo:v1": "repo:backup"}, ["app"]
                )
            )
        self.assertIn("pending", main._rollback_message(None))
        self.assertNotIn("could not", main._rollback_message(None))


class SQLiteConcurrency(unittest.TestCase):
    def test_wal_read_does_not_write_or_block_behind_a_writer(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.db"
            store = ActionStore(db_path=path)
            self.addCleanup(store.close)
            aid = store.register("upd", "loc", "app", "latest", "")
            self.assertEqual(store._db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(store._db.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
            statements = []
            store._db.set_trace_callback(statements.append)
            writer = sqlite3.connect(path)
            try:
                writer.execute("BEGIN IMMEDIATE")
                writer.execute("DELETE FROM actions WHERE id = ?", (aid,))
                self.assertIsNotNone(store.resolve(aid))
                with patch("models.time.time", return_value=10**12):
                    self.assertIsNone(store.resolve(aid))
            finally:
                writer.rollback()
                writer.close()
            self.assertTrue(all(sql.startswith("SELECT") for sql in statements))

    def test_concurrent_writes_through_separate_connections(self):
        from concurrent.futures import ThreadPoolExecutor

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.db"
            stores = [ActionStore(db_path=path), ActionStore(db_path=path)]
            try:

                def write(index):
                    store = stores[index % 2]
                    return store.register("upd", "loc", f"app{index}", "latest", "")

                with ThreadPoolExecutor(max_workers=4) as pool:
                    ids = list(pool.map(write, range(40)))
                self.assertTrue(all(stores[0].resolve(aid) for aid in ids))
            finally:
                for store in stores:
                    store.close()


class DockerPackaging(unittest.TestCase):
    def test_packaged_app_processes_notification_without_missing_modules(self):
        root = Path(__file__).resolve().parent
        text = (root / "Dockerfile").read_text()
        copy_block = text.split("COPY --chown=1000:1000 main.py", 1)[1].split("./", 1)[0]
        files = ["main.py"] + [
            word for word in copy_block.replace("\\", "").split() if word.endswith(".py")
        ]
        with tempfile.TemporaryDirectory() as tmp:
            for name in files:
                shutil.copy(root / name, Path(tmp) / name)
            script = """import asyncio
from unittest.mock import AsyncMock, patch
import main
from models import DiunWebhookPayload
async def check():
    with patch.object(main, 'get_github_release_notes', AsyncMock(return_value=None)), patch.object(main, 'find_targets_for_image', AsyncMock(return_value={})), patch('telegram_client.send_telegram_message', AsyncMock()) as send:
        await main.process_diun_notification(DiunWebhookPayload(image='vendor/app:v1'))
        send.assert_awaited_once()
asyncio.run(check())
"""
            env = dict(os.environ, PYTHONPATH=tmp, DIUNTAP_STATE_DIR=str(Path(tmp) / "state"))
            result = subprocess.run(
                [sys.executable, "-c", script],
                cwd=tmp,
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()

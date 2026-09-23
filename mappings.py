import os
import json
import logging
import re
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger("diuntap")

# Built-in popular image mappings -> GitHub repo (owner/repo)
DEFAULT_IMAGE_TO_GITHUB: Dict[str, str] = {
    # Media & Streaming
    "deluan/navidrome": "navidrome/navidrome",
    "navidrome/navidrome": "navidrome/navidrome",
    "jellyfin/jellyfin": "jellyfin/jellyfin",
    "linuxserver/jellyfin": "jellyfin/jellyfin",
    "advplyr/audiobookshelf": "advplyr/audiobookshelf",
    "ghcr.io/advplyr/audiobookshelf": "advplyr/audiobookshelf",
    "rommapp/romm": "rommapp/romm",
    "snapotter/snapotter": "snapotter/snapotter",
    "slskd/slskd": "slskd/slskd",
    # Books & Documents
    "linuxserver/calibre-web": "janeczku/calibre-web",
    "crocodilestick/calibre-web-automated": "crocodilestick/Calibre-Web-Automated",
    "kavita/kavita": "Kareadita/Kavita",
    "alam00000/bentopdf": "alam00000/bentopdf",
    "paperlessngx/paperless-ngx": "paperless-ngx/paperless-ngx",
    # Downloads & Utilities
    "linuxserver/qbittorrent": "qbittorrent/qBittorrent",
    "jlesage/jdownloader-2": "jlesage/docker-jdownloader-2",
    "c4illin/convertx": "C4illin/ConvertX",
    "corentinth/it-tools": "CorentinTh/it-tools",
    "everywall/ladder": "everywall/ladder",
    "unclecode/crawl4ai": "unclecode/crawl4ai",
    "stgmt/crawl4ai-mcp": "stgmt/crawl4ai-mcp",
    # Dashboards & Monitoring
    "gethomepage/homepage": "gethomepage/homepage",
    "ghcr.io/gethomepage/homepage": "gethomepage/homepage",
    "glanceapp/glance": "glanceapp/glance",
    "henrygd/beszel": "henrygd/beszel",
    "henrygd/beszel-agent": "henrygd/beszel",
    "louislam/uptime-kuma": "louislam/uptime-kuma",
    # Security & Networking
    "vaultwarden/server": "dani-garcia/vaultwarden",
    "caddyserver/caddy": "caddyserver/caddy",
    "cloudflare/cloudflared": "cloudflare/cloudflared",
    "pihole/pihole": "pi-hole/docker-pi-hole",
    "adguard/adguardhome": "AdguardTeam/AdGuardHome",
    "wg-easy/wg-easy": "wg-easy/wg-easy",
    # AI & Automation
    "berriai/litellm": "BerriAI/litellm",
    "crazymax/diun": "crazymax/diun",
    "portainer/portainer-ce": "portainer/portainer",
    "mealie-recipes/mealie": "mealie-recipes/mealie",
    "browserless/chromium": "browserless/browserless",
    "immich-app/immich-server": "immich-app/immich",
}


def load_custom_mappings(config_path: Optional[str] = None) -> Dict[str, str]:
    """
    Loads custom image -> GitHub mappings from a JSON or YAML file.
    Merges custom mappings on top of DEFAULT_IMAGE_TO_GITHUB.
    """
    mappings = dict(DEFAULT_IMAGE_TO_GITHUB)

    if not config_path:
        env_path = os.getenv("MAPPINGS_FILE", "")
        if env_path:
            config_path = env_path
        else:
            # Check default candidate locations
            candidates = [
                Path("mappings.yaml"),
                Path("mappings.yml"),
                Path("mappings.json"),
                Path("/config/mappings.yaml"),
                Path("/config/mappings.yml"),
                Path("/config/mappings.json"),
            ]
            for c in candidates:
                if c.exists():
                    config_path = str(c)
                    break

    if not config_path:
        return mappings

    target = Path(config_path)
    if not target.exists():
        logger.warning(f"Configured mappings file does not exist: {config_path}")
        return mappings

    try:
        content = target.read_text(encoding="utf-8")
        custom_data = {}
        if target.suffix.lower() in (".yaml", ".yml"):
            try:
                import yaml
                custom_data = yaml.safe_load(content) or {}
            except ImportError:
                logger.error("PyYAML is not installed. Install pyyaml to support YAML mappings.")
        else:
            custom_data = json.loads(content)

        if isinstance(custom_data, dict):
            # Check if mappings are nested under an 'images' key or at root
            img_map = custom_data.get("images", custom_data)
            if isinstance(img_map, dict):
                for k, v in img_map.items():
                    if isinstance(k, str) and isinstance(v, str):
                        mappings[k.strip().lower()] = v.strip()
                logger.info(f"Loaded {len(img_map)} custom mappings from {target}")
    except Exception as e:
        logger.error(f"Failed to read custom mappings from {target}: {e}")

    return mappings


def resolve_github_repo(image_name: str, mappings: Dict[str, str]) -> Optional[str]:
    """
    Resolves the GitHub repository (owner/repo) for a given docker image name.
    Attempts:
      1. Exact match in mappings
      2. Lowercase match
      3. Strip registry prefix (docker.io, ghcr.io, etc.)
      4. Fallback heuristic: if format is 'owner/repo' without dots in owner.
    """
    clean = image_name.lower().strip()
    for prefix in ("docker.io/", "library/", "ghcr.io/", "quay.io/"):
        if clean.startswith(prefix):
            clean = clean[len(prefix):]

    # 1. Exact or cleaned in mappings
    if clean in mappings:
        return mappings[clean]
    if image_name in mappings:
        return mappings[image_name]

    # 2. Check short name in mappings (e.g., 'navidrome')
    short = clean.split("/")[-1]
    if short in mappings:
        return mappings[short]

    # 3. Fallback heuristic: if clean image has format owner/repo and owner does not contain dot
    parts = clean.split("/")
    if len(parts) == 2 and "." not in parts[0]:
        return clean

    return None


def load_remote_quadlets_fallback(configs_dir: Path) -> Dict[str, str]:
    """Read image -> service mappings from remote *.container quadlet files.

    Synchronous by design: callers run it via asyncio.to_thread().
    Symlinks are skipped to avoid directory escapes.
    """
    services: Dict[str, str] = {}
    try:
        if not configs_dir.exists():
            return services
        try:
            from models import parse_image_and_tag
        except Exception:  # pragma: no cover - fallback parser
            parse_image_and_tag = None  # type: ignore

        for cfile in configs_dir.glob("*.container"):
            try:
                if cfile.is_symlink():
                    continue
                service_name = cfile.stem
                text = cfile.read_text(encoding="utf-8", errors="ignore")
                img_match = re.search(r"^Image=(.+)$", text, re.MULTILINE)
                if img_match:
                    raw_img = img_match.group(1).strip()
                    if parse_image_and_tag is not None:
                        clean_img, _ = parse_image_and_tag(raw_img)
                    else:
                        clean_img = raw_img.split("@")[0].split(":")[0]
                    services[clean_img] = service_name
                    short = clean_img.split("/")[-1]
                    if short not in services:
                        services[short] = service_name
            except Exception:
                continue
    except Exception as e:
        logger.warning(f"Error reading remote configs dir ({configs_dir}): {e}")
    return services
